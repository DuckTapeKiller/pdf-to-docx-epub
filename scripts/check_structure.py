#!/usr/bin/env python3
"""Check the book's structure against the PDF itself, not against the HTML.

The validators compare files the pipeline wrote, so they cannot see a
section that was never recognised.  This check reads the PDF again (its
bookmarks, its printed contents, the titles at the top of its pages, its
images, its words page by page) and asks of the reconstructed HTML:

- does every printed contents entry and bookmark have a heading?
- does a bibliography or index the PDF prints exist as its own section, with
  its entries in it?
- did a note swallow other text (an index, the end of a chapter), and do the
  note numbers run without gaps?
- were large images skipped?
- does every page's text reach the HTML?
- is the declared language the language of the text?
- do headings look like headings?

Problems are printed as ERROR (wrong structure) or WARN (look at the page).
Exit status: 1 when there is an ERROR.
"""
from __future__ import annotations

import argparse
import collections
import difflib
import json
import re
import shutil
import statistics
import subprocess
import sys
import unicodedata
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

BIBLIOGRAPHY = re.compile(r'^(?:select(?:ed)? |general )?(?:bibliograf\w*(?: \w+)?|bibliography|works cited|'
                          r'obras citadas|references|referencias(?: bibliograficas)?|fuentes y bibliografia|'
                          r'sources and bibliography|literaturverzeichnis|further reading)$')
INDEX = re.compile(r'^(?:index(?: of \w+(?: \w+)?)?|general index|subject index|name index|'
                   r'indice (?:onomastico|analitico|tematico|conceptual|de nombres|de autores|de materias|'
                   r'alfabetico)(?: \w+){0,4}|register|namenregister|sachregister|indice dei nomi)$')
LOCATORS = re.compile(r'\b\d{1,4}(?:[-–]\d{1,4})?(?:\s*[,;]\s*\d{1,4}(?:[-–]\d{1,4})?){2,}')
STOPWORDS = {
    'es': 'de la que el en y los se del las un por con una su para es al lo como más pero sus le ya o fue este',
    'en': 'the of and to in a is that for it as was with be by on not he this are or his from at which but',
    'fr': 'de la le et les des en un du une que est pour qui dans par au sur pas plus ne se ce il sont',
    'de': 'der die und in den von zu das mit sich des auf für ist im dem nicht ein eine als auch es an',
    'it': 'di e il la che in a per un del non è da una sono le si con della al dei nel',
    'pt': 'de a o que e do da em um para com não uma os no se na por mais as dos como mas ao',
    'la': 'et in est non ad cum quod ut sed qui quae de per ex esse enim ab si nec',
}


REGION = {'es': 'es-ES', 'en': 'en-GB', 'fr': 'fr-FR', 'de': 'de-DE', 'it': 'it-IT', 'pt': 'pt-PT', 'la': 'la'}


def fold(text: str) -> str:
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return ' '.join(re.sub(r'[\W_]+', ' ', text.casefold()).split())


class Book(HTMLParser):
    """Headings, notes, note references, section classes and images of the HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang = ''
        self.headings: list[dict] = []     # {'level', 'text', 'position'}
        self.notes: list[dict] = []        # {'id', 'text', 'chapter'}
        self.refs: list[dict] = []         # {'href', 'label', 'chapter'}
        self.blocks: list[tuple[str, str]] = []  # (class, text) of div blocks, in order
        self.paragraphs: list[str] = []
        self.images = 0
        self.body: list[str] = []
        self._stack: list[tuple[str, dict]] = []
        self._text: list[str] = []
        self._capture = None
        self.chapter = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'html':
            self.lang = attrs.get('lang', '') or attrs.get('xml:lang', '')
        if tag == 'img':
            self.images += 1
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            if tag == 'h1':
                self.chapter += 1
            self._open(tag, attrs)
        elif tag == 'aside' and 'footnote' in attrs.get('epub:type', ''):
            self._open(tag, attrs)
        elif tag == 'a' and attrs.get('epub:type') == 'noteref':
            self._open(tag, attrs)
        elif tag == 'div' and attrs.get('class') and self._capture is None:
            self._open(tag, attrs)
        elif tag == 'p' and self._capture is None:
            self._open(tag, attrs)
        elif tag == 'br':
            self._text.append(' ')

    def _open(self, tag, attrs):
        if self._capture is not None and tag != 'a':
            return
        if tag == 'a' and self._capture is not None:
            self._stack.append((tag, attrs, len(self._text)))
            return
        self._capture = (tag, attrs, len(self._text))

    def handle_endtag(self, tag):
        if self._stack and self._stack[-1][0] == tag:
            _tag, attrs, start = self._stack.pop()
            self.refs.append({'href': attrs.get('href', ''), 'label': ''.join(self._text[start:]).strip(),
                              'chapter': self.chapter})
            return
        if self._capture is None or self._capture[0] != tag:
            return
        _tag, attrs, start = self._capture
        text = ' '.join(''.join(self._text[start:]).split())
        self._capture = None
        if tag.startswith('h'):
            self.headings.append({'level': int(tag[1]), 'text': text, 'position': len(self.blocks)})
        elif tag == 'aside':
            self.notes.append({'id': attrs.get('id', ''), 'text': text, 'chapter': self.chapter})
        elif tag == 'a':
            self.refs.append({'href': attrs.get('href', ''), 'label': text, 'chapter': self.chapter})
        elif tag == 'div':
            self.blocks.append((attrs.get('class', ''), text))
        elif tag == 'p':
            self.blocks.append(('', text))
            self.paragraphs.append(text)
        if tag != 'aside':
            self.body.append(text)

    def handle_data(self, data):
        if self._capture is not None:
            self._text.append(data)


def run(command: list[str]) -> str:
    return subprocess.run(command, capture_output=True, text=True, errors='replace').stdout


def pdf_outline(pdf: Path) -> list[dict]:
    raw = run(['pdftohtml', '-xml', '-i', '-q', '-stdout', '-f', '1', '-l', '1', str(pdf)])
    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', re.sub(r'<!DOCTYPE[^>]*>', '', raw))
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    entries = []

    def walk(node, depth):
        for child in node:
            if child.tag == 'item' and (child.get('page') or '').isdigit():
                entries.append({'depth': depth, 'page': int(child.get('page')),
                                'title': ' '.join(''.join(child.itertext()).split())})
            elif child.tag == 'outline':
                walk(child, depth + 1)

    top = root.find('outline')
    if top is not None:
        walk(top, 0)
    roots = [e for e in entries if e['depth'] == 0]
    if len(roots) == 1 and len(entries) > 1:
        entries.remove(roots[0])
    return entries


def pdf_page_texts(pdf: Path, layout: bool = False) -> dict[int, str]:
    command = ['pdftotext', '-enc', 'UTF-8'] + (['-layout'] if layout else []) + [str(pdf), '-']
    pages = run(command).split('\f')
    return {n + 1: unicodedata.normalize('NFC', text) for n, text in enumerate(pages) if text.strip() or n + 1 < len(pages)}


def contents_entries(pages: dict[int, str], toc_pages: list[int], repeated: set[str]) -> list[tuple[int, list[str]]]:
    """Titles from the printed contents: a title, leaders or a gap, a page number.

    A title may wrap: each entry carries its own line and, as an alternative,
    that line joined to the lines above it (which may instead be an author).
    """
    entries = []
    for number in toc_pages:
        pending = ''
        for line in pages.get(number, '').splitlines():
            line = line.strip()
            if not line or line_key(line) in repeated:
                pending = ''
                continue
            match = re.match(r'^(.*?\S)\s*(?:\.{2,}|…+|\s{2,}|\s)\s*(\d{1,4}|[ivxlc]{1,6})$', line, re.I)
            if match and re.search(r'[^\W\d_]{3,}', match.group(1)):
                own = re.sub(r'^\s*(\d{1,3}|[ivxlc]{1,6})[.\s]+(?=\S)', '', match.group(1), flags=re.I)
                own = re.sub(r'[\s.…]+$', '', own)
                candidates = [own] + ([re.sub(r'[\s.…]+$', '', pending + ' ' + own)] if pending else [])
                if len(fold(own)) >= 3:
                    entries.append((number, candidates))
                pending = ''
            elif len(line) < 90:
                pending = (pending + ' ' + line).strip() if pending else line
            else:
                pending = ''
    return entries


def line_key(line: str) -> str:
    """Running heads and printer's slugs differ only in their numbers."""
    return re.sub(r'\d+', '#', fold(line))


def best_heading(title: str, headings: list[dict]) -> tuple[float, dict | None]:
    key = fold(title)
    best, best_score = None, 0.0
    for heading in headings:
        text = fold(heading['text'])
        if not text:
            continue
        if key == text or key.replace(' ', '') == text.replace(' ', '') \
                or (len(key) >= 6 and (key in text or text in key)):
            return 1.0, heading
        score = difflib.SequenceMatcher(None, key, text).ratio()
        if score > best_score:
            best, best_score = heading, score
    return best_score, best


def language_of(text: str) -> tuple[str, float]:
    words = collections.Counter(w.lower() for w in re.findall(r'[^\W\d_]+', text))
    total = sum(words.values()) or 1
    scores = {lang: sum(words[w] for w in stop.split()) / total for lang, stop in STOPWORDS.items()}
    lang = max(scores, key=scores.get)
    return lang, scores[lang]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--pdf', required=True, type=Path)
    parser.add_argument('--html', required=True, type=Path)
    parser.add_argument('--report', type=Path, help='pdf_to_html report (default: HTML with .report.json)')
    parser.add_argument('--show', type=int, default=12, help='examples to print per problem')
    args = parser.parse_args()
    for tool in ('pdftotext', 'pdftohtml'):
        if shutil.which(tool) is None:
            sys.exit(f'{tool} (Poppler) is required')
    report_path = args.report or args.html.with_suffix('.report.json')
    report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {}
    skipped = set(report.get('skipped_pages', []))

    book = Book()
    book.feed(args.html.read_text(encoding='utf-8'))
    errors: list[str] = []
    warnings: list[str] = []
    info: list[str] = []

    def problem(kind: str, message: str) -> None:
        (errors if kind == 'ERROR' else warnings if kind == 'WARN' else info).append(message)

    pages = pdf_page_texts(args.pdf)
    headings = book.headings

    # 1. Headings: at least one chapter, and nothing that reads like body text
    if not any(h['level'] == 1 for h in headings):
        problem('ERROR', 'no chapter heading (h1) at all')
    for heading in headings:
        text = heading['text']
        if not text:
            problem('ERROR', f'empty h{heading["level"]}')
        elif len(text) > 160 or re.match(r'[a-zà-ÿ]', text) or re.search(r'[,;:]$', text):
            problem('WARN', f'h{heading["level"]} reads like body text: "{text[:90]}"')

    # 2. Printed contents and bookmarks
    line_pages = collections.Counter()
    for text in pages.values():
        for key in {line_key(l) for l in text.splitlines() if l.strip()}:
            line_pages[key] += 1
    repeated = {k for k, n in line_pages.items() if n >= 4}
    toc_pages = report.get('toc_pages_skipped', [])
    layout_pages = pdf_page_texts(args.pdf, layout=True) if toc_pages else {}
    for number in toc_pages:
        lines = [l for l in layout_pages.get(number, '').splitlines() if l.strip() and line_key(l) not in repeated]
        worded = [l for l in lines if re.search(r'[^\W\d_]{3,}', l)]
        if lines and len(worded) < len(lines) * 0.5:
            problem('ERROR', f'page {number} was skipped as a contents page but is mostly figures '
                             '(a table?): its text is missing from the book')
    entries = contents_entries(layout_pages, toc_pages, repeated)
    unmatched = []
    for page, candidates in entries:
        if max(best_heading(c, headings)[0] for c in candidates) < 0.8:
            unmatched.append(f'"{candidates[0][:70]}" (contents page {page})')
    if entries:
        info.append(f'printed contents: {len(entries)} entries, {len(entries) - len(unmatched)} with a heading')
    for item in unmatched[:args.show]:
        problem('ERROR' if len(unmatched) > len(entries) * 0.3 else 'WARN', f'contents entry without a heading: {item}')
    outline = pdf_outline(args.pdf)
    if outline:
        missing = [e for e in outline if e['page'] not in skipped and best_heading(e['title'], headings)[0] < 0.8]
        info.append(f'bookmarks: {len(outline)}, {len(outline) - len(missing)} with a heading')
        for entry in missing[:args.show]:
            problem('WARN', f'bookmark without a heading: "{entry["title"][:70]}" (page {entry["page"]}); '
                            'a navigation-only bookmark is fine, a printed title is not')

    # 3. Bibliography and index sections printed in the PDF
    sections = {}
    for number, text in sorted(pages.items()):
        if number in skipped:
            continue
        lines = [l.strip() for l in text.splitlines() if l.strip()][:5]
        for line in lines:
            key = fold(line)
            kind = 'bibliography' if BIBLIOGRAPHY.match(key) else 'index' if INDEX.match(key) else None
            if kind and key not in sections:
                sections[key] = (kind, number, line)
    checked: list[dict] = []
    for key, (kind, number, line) in sections.items():
        tight = key.replace(' ', '')     # "Bibliograf ía": letter-spaced or split by the extraction
        heading = next((h for h in headings if fold(h['text']).replace(' ', '') == tight), None)
        if heading is None:
            heading = next((h for h in headings if tight in fold(h['text']).replace(' ', '')), None)
        if heading is None:
            problem('ERROR', f'the PDF has a {kind} section ("{line}", page {number}) but the HTML has no heading for it')
            continue
        if any(heading is seen for seen in checked):
            continue
        checked.append(heading)
        following = book.blocks[heading['position']:]
        nxt = next((h for h in headings if h['position'] > heading['position'] and h['level'] <= heading['level']), None)
        if nxt is not None:
            following = book.blocks[heading['position']:nxt['position']]
        wanted = ('index', 'index-sub') if kind == 'index' else ('bibliography', 'hanging', 'hanging-sub')
        styled = sum(1 for css, _ in following if css in wanted)
        if following and styled < len(following) * 0.5:
            problem('WARN', f'{kind} "{heading["text"][:50]}": only {styled} of {len(following)} blocks are '
                            f'{kind} entries')
        info.append(f'{kind}: "{heading["text"][:50]}" (h{heading["level"]}), {styled} entries')

    # 4. Notes: nothing swallowed, numbers in sequence
    lengths = [len(n['text']) for n in book.notes]
    if lengths:
        median = statistics.median(lengths)
        for index, note in enumerate(book.notes):
            text = note['text']
            last = index == len(book.notes) - 1 or book.notes[index + 1]['chapter'] != note['chapter']
            if len(text) > max(3000, median * 10):
                problem('ERROR' if last else 'WARN',
                        f'note {note["id"]} is {len(text)} characters (median {median:.0f})'
                        f'{": the last note of its chapter may hold the text after it" if last else ""}: '
                        f'"{text[:60]}…{text[-40:]}"')
            if len(LOCATORS.findall(text)) >= 6:
                problem('ERROR', f'note {note["id"]} reads like an index (page lists): "{text[:80]}"')
            for line in re.split(r'(?<=[.)])\s+', text):
                key = fold(line)
                if (BIBLIOGRAPHY.match(key) or INDEX.match(key)) and len(text) > 1500:
                    problem('ERROR', f'note {note["id"]} contains a section title "{line}"')
                    break
    by_chapter = collections.defaultdict(list)
    for ref in book.refs:
        by_chapter[ref['chapter']].append(ref['label'])
    for chapter, labels in sorted(by_chapter.items()):
        numbers = [int(l) for l in labels if l.isdigit()]
        if len(numbers) != len(labels) or not numbers:
            continue
        breaks = [(a, b) for a, b in zip(numbers, numbers[1:]) if b != a + 1]
        if breaks:
            shown = ', '.join(f'{a}→{b}' for a, b in breaks[:6])
            problem('WARN', f'chapter {chapter}: note numbers jump {shown} (a missed or misread marker?)')
    ids = {n['id'] for n in book.notes}
    dangling = [r['href'] for r in book.refs if r['href'].lstrip('#') not in ids]
    if dangling:
        problem('ERROR', f'{len(dangling)} note reference(s) point to no note, e.g. {dangling[:5]}')
    referenced = {r['href'].lstrip('#') for r in book.refs}
    orphans = [n['id'] for n in book.notes if n['id'] not in referenced]
    if orphans:
        problem('WARN', f'{len(orphans)} note(s) without a reference, e.g. {orphans[:5]}')

    # 5. Figures
    figures = report.get('figures', [])
    if len(figures) != book.images:
        problem('WARN', f'{len(figures)} figure(s) kept in the report but {book.images} image(s) in the HTML')
    for image in report.get('images_skipped', []):
        if image.get('share', 0) >= 0.15 and image.get('reason') != 'skipped page' \
                and not image.get('reason', '').startswith('cover'):
            problem('WARN', f'large image skipped on page {image["page"]} ({image["share"]:.0%} of the page, '
                            f'{image["reason"]}): look at the page')

    for number in report.get('table_pages', []):
        problem('WARN', f'page {number} is laid out as a table: tables are not rebuilt, its cells come out '
                        'as lines of text; rebuild it as an HTML table or keep an image of it')

    # 6. Every page's text reaches the HTML
    html_words = collections.Counter(w.lower() for w in re.findall(r'\w+', ' '.join(book.body + [n['text'] for n in book.notes])))
    pdf_counts = collections.Counter()
    page_words = {}
    for number, text in pages.items():
        text = re.sub(r'(\w)-\n(\w)', r'\1\2', text)
        words = [w.lower() for w in re.findall(r'[^\W\d_]{5,}', text)]
        page_words[number] = words
        pdf_counts.update(words)
    thin = []
    for number, words in sorted(page_words.items()):
        if number in skipped or len(words) < 30:
            continue
        found = sum(1 for w in words if html_words[w])
        if found / len(words) < 0.8:
            thin.append(f'{number} ({found}/{len(words)})')
    if thin:
        problem('ERROR', f'pages whose words are mostly missing from the HTML: {", ".join(thin[:args.show])}')

    # 7. Language
    lang, share = language_of(' '.join(book.paragraphs[:400]))
    declared = book.lang.split('-')[0].lower()
    if not book.lang:
        problem('ERROR', 'the HTML declares no language')
    elif share > 0.08 and declared != lang:
        problem('ERROR', f'the HTML declares lang="{book.lang}" but the text reads as "{lang}"')
    if report.get('lang_inferred'):
        inferred = report['lang_inferred']
        info.append(f'language inferred as "{inferred}"; set "lang" in the config to state it '
                    f'(a regional tag such as "{REGION.get(inferred, inferred)}" is fine)')

    # 8. Notes kept as paragraphs, and paragraphs that may be several fused together
    for css, text in book.blocks:
        if 'orphan-note' in css.split():
            if len(text) > max(3000, (statistics.median(lengths) if lengths else 0) * 10):
                problem('ERROR', f'a note without a reference holds {len(text)} characters (it swallowed the '
                                 f'text after it: a table, other notes?): "{text[:70]}…"')
            else:
                problem('WARN', f'a note without a reference in the text, kept as a paragraph: "{text[:70]}"')
        elif len(text) > 9000:
            problem('WARN', f'a block of {len(text)} characters (several paragraphs fused?): "{text[:70]}"')

    for message in info:
        print(f'  {message}')
    for message in warnings:
        print(f'WARN {message}')
    for message in errors:
        print(f'ERROR {message}')
    print(f'{len(errors)} error(s), {len(warnings)} warning(s)')
    sys.exit(1 if errors else 0)


if __name__ == '__main__':
    main()
