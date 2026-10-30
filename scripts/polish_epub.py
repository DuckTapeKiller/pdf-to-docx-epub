#!/usr/bin/env python3
"""Polish a Pandoc EPUB 3 so that its notes work in every reading system.

Pandoc marks note references with epub:type="noteref" and notes with
<aside epub:type="footnote">, which Apple Books, Kobo, Kindle and Thorium
show as pop-ups.  This pass makes the result robust everywhere:

* each note reference is wrapped in <sup> and carries role="doc-noteref";
* each note starts with its label linked back to the reference
  (Pandoc 3.9 and later emit neither the number nor the back link, which
  leaves readers without pop-ups with unnumbered notes and no way back);
* with --source, labels are copied from the reconstructed HTML, so the
  EPUB shows exactly the marks printed in the PDF (per-chapter or
  continuous numbering, symbols such as "*");
* Pandoc's empty inline <style> elements are removed;
* data-custom-style attributes (from DOCX input) also become CSS classes;
* chapter files get their heading as <title> instead of "ch001.xhtml".

Only Pandoc's own, regular markup is edited, and every changed file is
re-parsed as XML before the EPUB is written.  mimetype stays the first,
uncompressed entry.
"""
from __future__ import annotations

import argparse
import html
import posixpath
import re
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
import xml.etree.ElementTree as ET

EMPTY_STYLE = re.compile(r'\s*<style\b[^>]*>\s*</style>\s*', re.IGNORECASE)
NOTEREF = re.compile(r'<a\b(?P<attrs>[^>]*\bepub:type="noteref"[^>]*)>(?P<body>.*?)</a>', re.S)
ASIDE = re.compile(r'<aside\b(?P<attrs>[^>]*\bepub:type="(?:footnote|endnote|rearnote)"[^>]*)>(?P<body>.*?)</aside>', re.S)
BACKLINK = re.compile(r'<a\b[^>]*\bclass="[^"]*\bfootnote-back\b[^"]*"[^>]*>.*?</a>\.?\s*', re.S)
# Pandoc's DOCX reader keeps Word's footnote mark as a span: empty for an
# automatic number, the symbol itself for a custom mark such as "*".
DOCX_NOTE_MARK = re.compile(r'<span\b[^>]*data-custom-style="Footnote Reference"[^>]*>(?P<mark>.*?)</span>\s*', re.S)
CUSTOM_STYLE_TAG = re.compile(r'<(?P<tag>[a-z]+)\b(?P<attrs>[^>]*\bdata-custom-style="(?P<style>[^"]+)"[^>]*)>')
TITLE = re.compile(r'<title>(?P<title>[^<]*)</title>')
HEADING = re.compile(r'<h[1-3]\b[^>]*>(?P<text>.*?)</h[1-3]>', re.S)
OPENING_P = re.compile(r'^(?P<lead>\s*(?:<div\b[^>]*>\s*)*)<p\b(?P<attrs>[^>]*)>', re.S)
ATTR = re.compile(r'\b(?P<name>[\w:-]+)="(?P<value>[^"]*)"')


def attrs_of(text: str) -> dict[str, str]:
    return {m.group('name'): html.unescape(m.group('value')) for m in ATTR.finditer(text)}


def add_attr(attrs: str, name: str, value: str) -> str:
    if re.search(rf'\b{re.escape(name)}="', attrs):
        return attrs
    return f'{attrs} {name}="{html.escape(value, quote=True)}"'


def add_class(attrs: str, cls: str) -> str:
    match = re.search(r'\bclass="([^"]*)"', attrs)
    if match is None:
        return f'{attrs} class="{cls}"'
    classes = match.group(1).split()
    if cls in classes:
        return attrs
    return attrs[:match.start(1)] + ' '.join(classes + [cls]) + attrs[match.end(1):]


def slug(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def plain(fragment: str) -> str:
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', fragment))).strip()


class SourceLabels(HTMLParser):
    """Labels of the noteref links of the reconstructed HTML, in order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.labels: list[str] = []
        self._text: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == 'a' and dict(attrs).get('epub:type') == 'noteref':
            self._text = []

    def handle_endtag(self, tag):
        if tag == 'a' and self._text is not None:
            self.labels.append(''.join(self._text).strip())
            self._text = None

    def handle_data(self, data):
        if self._text is not None:
            self._text.append(data)


def spine_documents(zf: zipfile.ZipFile) -> list[str]:
    container = ET.fromstring(zf.read('META-INF/container.xml'))
    opf_path = container.find('.//{*}rootfile').get('full-path')
    opf = ET.fromstring(zf.read(opf_path))
    base = posixpath.dirname(opf_path)
    items = {i.get('id'): posixpath.normpath(posixpath.join(base, unquote(i.get('href'))))
             for i in opf.iter('{http://www.idpf.org/2007/opf}item')}
    return [items[r.get('idref')] for r in opf.iter('{http://www.idpf.org/2007/opf}itemref')]


def polish_document(text: str, labels: list[str] | None, cursor: list[int],
                    counter: list[int]) -> tuple[str, dict]:
    """Normalise one XHTML file.

    labels/cursor: source labels and the position reached in them.
    counter: next automatic note number when there is no source; Word does
    not count custom marks, so symbols do not advance it.
    """
    stats = {'styles': 0, 'refs': 0, 'notes': 0}
    text, stats['styles'] = EMPTY_STYLE.subn('\n', text)

    custom_marks: dict[str, str] = {}
    for note in ASIDE.finditer(text):
        mark = DOCX_NOTE_MARK.search(note.group('body'))
        note_id = attrs_of(note.group('attrs')).get('id')
        if note_id and mark and plain(mark.group('mark')):
            custom_marks[note_id] = plain(mark.group('mark'))

    note_labels: dict[str, str] = {}
    note_refs: dict[str, str] = {}

    def fix_ref(match: re.Match) -> str:
        attrs = match.group('attrs')
        values = attrs_of(attrs)
        href = values.get('href', '')
        if labels is not None:
            if cursor[0] >= len(labels):
                raise RuntimeError('the EPUB has more note references than the source HTML')
            label = labels[cursor[0]]
            cursor[0] += 1
        elif href[1:] in custom_marks:
            label = custom_marks[href[1:]]
        else:
            label = str(counter[0])
            counter[0] += 1
        if href.startswith('#'):
            note_labels[href[1:]] = label
            if values.get('id'):
                note_refs.setdefault(href[1:], values['id'])
        attrs = add_attr(attrs, 'role', 'doc-noteref')
        stats['refs'] += 1
        return f'<a{attrs}><sup>{html.escape(label)}</sup></a>'

    text = NOTEREF.sub(fix_ref, text)

    def fix_note(match: re.Match) -> str:
        attrs = match.group('attrs')
        values = attrs_of(attrs)
        note_id = values.get('id', '')
        label = note_labels.get(note_id) or re.sub(r'^\D*', '', note_id) or '*'
        ref_id = note_refs.get(note_id, '')
        body = DOCX_NOTE_MARK.sub('', BACKLINK.sub('', match.group('body')))
        back = (f'<a href="#{ref_id}" class="footnote-back" role="doc-backlink">{html.escape(label)}</a>'
                if ref_id else f'<span class="footnote-label">{html.escape(label)}</span>')
        opening = OPENING_P.match(body)
        if opening:
            body = (body[:opening.end()] + back + '. ' + body[opening.end():].lstrip())
        else:
            body = f'\n<p class="footnote-label">{back}.</p>{body}'
        role = 'doc-endnote' if 'endnote' in values.get('epub:type', '') else 'doc-footnote'
        attrs = add_class(add_attr(attrs, 'role', role), 'footnote')
        stats['notes'] += 1
        return f'<aside{attrs}>{body}</aside>'

    text = ASIDE.sub(fix_note, text)

    def fix_custom(match: re.Match) -> str:
        return f'<{match.group("tag")}{add_class(match.group("attrs"), slug(match.group("style")))}>'

    text = CUSTOM_STYLE_TAG.sub(fix_custom, text)

    title = TITLE.search(text)
    heading = HEADING.search(text)
    if title and heading and re.fullmatch(r'(ch\d+|title_page|nav)\.x?html', title.group('title').strip()):
        name = plain(re.sub(r'<a\b[^>]*epub:type="noteref".*?</a>', '', heading.group('text'), flags=re.S))
        if name:
            text = text[:title.start('title')] + html.escape(name) + text[title.end('title'):]
    return text, stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--epub', required=True, type=Path, help='EPUB written by Pandoc')
    parser.add_argument('--out', type=Path, help='output EPUB (default: replace --epub)')
    parser.add_argument('--source', type=Path,
                        help='reconstructed book HTML; its noteref labels become the EPUB labels')
    parser.add_argument('--numbering', choices=['chapter', 'continuous'], default='chapter',
                        help='without --source: restart note numbers in every chapter file '
                             '(Pandoc\'s behaviour) or number them through the whole book')
    args = parser.parse_args()

    source = args.epub.resolve()
    output = (args.out or source).resolve()
    labels = None
    if args.source:
        collector = SourceLabels()
        collector.feed(args.source.read_text(encoding='utf-8'))
        labels = collector.labels

    totals = {'styles': 0, 'refs': 0, 'notes': 0}
    cursor, counter = [0], [1]
    with zipfile.ZipFile(source) as zin:
        order = spine_documents(zin)
        changed: dict[str, bytes] = {}
        for name in order + [n for n in zin.namelist() if n.endswith(('.xhtml', '.html')) and n not in order]:
            if name in changed or name not in zin.namelist():
                continue
            text = zin.read(name).decode('utf-8')
            if args.numbering == 'chapter':
                counter[0] = 1
            polished, stats = polish_document(text, labels if name in order else None, cursor, counter)
            for key in totals:
                totals[key] += stats[key]
            if polished != text:
                try:
                    ET.fromstring(polished.encode('utf-8'))
                except ET.ParseError as error:
                    sys.exit(f'{name}: polishing produced invalid XML ({error}); nothing was written')
                changed[name] = polished.encode('utf-8')
        if labels is not None and cursor[0] != len(labels):
            sys.exit(f'the source HTML has {len(labels)} note references but the EPUB has {cursor[0]}; '
                     'nothing was written')

        temporary = output.with_name(output.name + '.tmp')
        with zipfile.ZipFile(temporary, 'w') as zout:
            items = sorted(zin.infolist(), key=lambda i: i.filename != 'mimetype')
            for item in items:
                data = changed.get(item.filename, zin.read(item.filename))
                if item.filename == 'mimetype':
                    zout.writestr(zipfile.ZipInfo('mimetype', item.date_time), data, zipfile.ZIP_STORED)
                else:
                    zout.writestr(item, data, zipfile.ZIP_DEFLATED)
    temporary.replace(output)
    print(f'{output}: {totals["refs"]} note reference(s), {totals["notes"]} note(s) normalised, '
          f'{totals["styles"]} empty style element(s) removed')


if __name__ == '__main__':
    main()
