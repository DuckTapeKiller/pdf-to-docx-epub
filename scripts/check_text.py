#!/usr/bin/env python3
"""Compare the reconstructed HTML with the PDF's own text layer.

pdf_to_html.py and the validators only compare files the pipeline wrote.
This check goes back to the source: it extracts the PDF with pdftotext (an
independent reading order), repairs line-end hyphens, and compares word
counts with the HTML (body, notes and note labels).  Differences explained by
the reconstruction report (removed running heads and folios, skipped pages,
decoded garbled fonts) are listed separately; what remains is text that was
dropped, duplicated or mangled and must be checked against the page.

Exit status: 0 when the unexplained difference is below --tolerance.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

WORD = re.compile(r'\w+', re.UNICODE)
GARBLE = re.compile(r'[#$%&()*+,/;<=>?@\[\\\]^_{|}~]|\d[a-zà-ÿ]|[a-zà-ÿ]\d')


class HtmlWords(HTMLParser):
    """Text of the HTML without note labels (and without inventing spaces)."""
    BLOCKS = {'p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'figcaption', 'aside', 'br'}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'title', 'style', 'script'} or (tag == 'a' and dict(attrs).get('epub:type') == 'noteref'):
            self._skip += 1
        elif tag in self.BLOCKS:
            self.chunks.append(' ')

    def handle_endtag(self, tag):
        if tag in {'title', 'style', 'script', 'a'} and self._skip:
            self._skip -= 1
        elif tag in self.BLOCKS:
            self.chunks.append(' ')

    def handle_data(self, data):
        if not self._skip:
            self.chunks.append(data)


URL = re.compile(r'(?:https?://|www\.)\S+')


def counter(text: str) -> collections.Counter:
    text = URL.sub(' ', unicodedata.normalize('NFC', text))
    return collections.Counter(w.lower() for w in WORD.findall(text))


def pdf_pages(pdf: Path, first: int | None, last: int | None) -> list[tuple[int, str]]:
    command = ['pdftotext', '-enc', 'UTF-8']
    if first:
        command += ['-f', str(first)]
    if last:
        command += ['-l', str(last)]
    text = subprocess.run(command + [str(pdf), '-'], capture_output=True, text=True, check=True).stdout
    start = first or 1
    pages = []
    for offset, page in enumerate(text.split('\f')):
        page = unicodedata.normalize('NFC', page.replace('­\n', '').replace('­', '').replace('¬\n', ''))
        # "Sala-\nMolins" keeps its hyphen; a margin number may sit between the halves
        number = r'(?:\d{1,4}[a-z]?|[IVXLC]{1,6})(?:/\S+)?'
        page = re.sub(rf'(\w)-(?:[ \t]+{number})?[ \t]*\n(?:[ \t]*{number}[ \t]*\n)*([a-zà-ÿ])', r'\1\2', page)
        page = re.sub(r'\b([^\W\d]{3,})\d{1,3}\b', r'\1', page)  # note markers glued to words: "Nubian37"
        page = re.sub(r'[ﬀ-ﬆ]', lambda m: {'ﬀ': 'ff', 'ﬁ': 'fi', 'ﬂ': 'fl', 'ﬃ': 'ffi',
                                                     'ﬄ': 'ffl', 'ﬅ': 'st', 'ﬆ': 'st'}[m.group(0)], page)
        pages.append((start + offset, page))
    return pages


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--pdf', required=True, type=Path)
    parser.add_argument('--html', required=True, type=Path)
    parser.add_argument('--report', type=Path, help='pdf_to_html report (default: HTML with .report.json)')
    parser.add_argument('--first-page', type=int)
    parser.add_argument('--last-page', type=int)
    parser.add_argument('--tolerance', type=float, default=0.002,
                        help='largest acceptable unexplained share of PDF words (default 0.2%%)')
    parser.add_argument('--show', type=int, default=30)
    args = parser.parse_args()
    if shutil.which('pdftotext') is None:
        sys.exit('pdftotext (Poppler) is required')

    report_path = args.report or args.html.with_suffix('.report.json')
    report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {}
    skipped = set(report.get('skipped_pages', []))
    margin_words = counter(' '.join(report.get('margin_lines', []) +
                                    [r['text'] for r in report.get('rotated_text', [])] +
                                    [r['text'] for r in report.get('outside_crop_box', [])]))

    pages = pdf_pages(args.pdf, args.first_page, args.last_page)
    where = collections.defaultdict(list)
    pdf_words = collections.Counter()
    skipped_words = collections.Counter()
    for number, text in pages:
        words = counter(text)
        if number in skipped:
            skipped_words += words
            continue
        pdf_words += words
        for word in words:
            if len(where[word]) < 5:
                where[word].append(number)

    parser_html = HtmlWords()
    parser_html.feed(args.html.read_text(encoding='utf-8'))
    html_text = unicodedata.normalize('NFC', ''.join(parser_html.chunks))
    html_words = counter(html_text)
    # pdftotext drops the hyphen of "Anglo-\nNorman"; the HTML keeps it
    compounds = {re.sub(r'-', '', c).lower() for c in re.findall(r'\w+(?:-\w+)+', html_text)}

    missing = pdf_words - html_words
    extra = html_words - pdf_words
    explained = collections.Counter()
    unexplained = collections.Counter()
    longer = [w for w in extra if len(w) > 3]
    for word, count in missing.items():
        if word.isdigit():
            explained['numbers (folios, note markers, page ranges)'] += count
        elif margin_words[word] or (len(word) <= 3 and any(word in m for m in margin_words)):
            explained['running heads, slugs, rotated text and text outside the crop box'] += count
        elif GARBLE.search(word) or len(word) == 1:
            explained['garbled or split extraction (font maps, letter-spaced heads)'] += count
        elif word in compounds:
            explained['line-end hyphens pdftotext removed but the HTML rightly keeps'] += count
        elif len(word) >= 2 and any(e.startswith(word) or e.endswith(word) for e in longer):
            explained['halves of words split by a page break or a stray space'] += count
        else:
            unexplained[word] = count
    # Two common PDF words fused in the HTML ("nubianmethod") are a lost space.
    # Not numbers (a year before a note marker: pdftotext reads "19501"), and
    # not the halves of a compound the HTML rightly keeps ("good-humoured",
    # which pdftotext joined at the line end).
    in_compounds = collections.defaultdict(set)
    for compound in re.findall(r'\w+(?:-\w+)+', html_text.lower()):
        for part in compound.split('-'):
            in_compounds[part].add(compound.replace('-', ''))
    glued = collections.Counter()
    for word, count in extra.items():
        if word.isdigit() or any(pdf_words[joined] for joined in in_compounds.get(word, ())):
            continue
        for cut in range(2, len(word) - 1):
            head, tail = word[:cut], word[cut:]
            if pdf_words[head] >= 2 and pdf_words[tail] >= 2 and not pdf_words[word]:
                glued[f'{word} ({head} {tail})'] += count
                unexplained[head] = unexplained.get(head, 0) + count
                break
    decoded = sum(c for w, c in extra.items() if not w.isdigit())

    total = sum(pdf_words.values()) or 1
    lost = sum(unexplained.values())
    print(f'PDF: {total} words on {len(pages) - len(skipped & {n for n, _ in pages})} page(s) '
          f'({sum(skipped_words.values())} more on {len(skipped)} skipped page(s)); HTML: {sum(html_words.values())} words')
    for reason, count in explained.most_common():
        print(f'  explained: {count} {reason}')
    print(f'  words only in the HTML (decoded fonts, repaired hyphens, headings): {decoded}')
    if extra:
        print('    e.g. ' + ', '.join(f'{w}×{c}' for w, c in extra.most_common(args.show) if not w.isdigit()))
    if glued:
        print(f'GLUED words in the HTML (a space was lost): {sum(glued.values())}')
        for word, count in glued.most_common(args.show):
            print(f'  {word}×{count}')
    source = args.html.read_text(encoding='utf-8')
    stuck = re.findall(r'(\w{0,12}[^\w\s<>]?)<a [^>]*epub:type="noteref"[^>]*>([^<]*)</a>(\w{1,12})', source)
    if stuck:
        print(f'NOTE MARKERS followed directly by a letter (a space was lost): {len(stuck)}')
        for before, label, after in stuck[:args.show]:
            print(f'  {before}[{label}]{after}')
    print(f'UNEXPLAINED words in the PDF but not in the HTML: {lost} ({100 * lost / total:.3f}%)')
    for word, count in unexplained.most_common(args.show):
        print(f'  {word}×{count}  (PDF pages {where[word]})')
    sys.exit(0 if lost / total <= args.tolerance and not stuck and not glued else 1)


if __name__ == '__main__':
    main()
