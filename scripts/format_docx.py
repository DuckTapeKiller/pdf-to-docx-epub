#!/usr/bin/env python3
"""Apply book paragraph styles to a Pandoc-generated DOCX safely."""
from __future__ import annotations

import argparse
import html
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
import xml.etree.ElementTree as ET

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
M = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
O = 'urn:schemas-microsoft-com:office:office'
V = 'urn:schemas-microsoft-com:vml'
W10 = 'urn:schemas-microsoft-com:office:word'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
PIC = 'http://schemas.openxmlformats.org/drawingml/2006/picture'
WP = 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'

for prefix, uri in {
    'w': W, 'r': R, 'm': M, 'o': O, 'v': V, 'w10': W10,
    'a': A, 'pic': PIC, 'wp': WP,
}.items():
    ET.register_namespace(prefix, uri)

NO_INDENT_CLASSES = {
    'noindent', 'author', 'publisher-note', 'illustration-list',
    'contributor', 'abbreviation', 'bibliography-entry', 'index-entry',
    'fragment', 'book-title', 'book-subtitle',
}


def normalise(value: str) -> str:
    return re.sub(r'\s+', ' ', html.unescape(value).replace('\u00a0', ' ')).strip()


class ParagraphCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[tuple[set[str], str]] = []
        self.classes: set[str] | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == 'p':
            self.classes = set((dict(attrs).get('class') or '').split())
            self.text = []
        elif tag == 'br' and self.classes is not None:
            self.text.append(' ')

    def handle_data(self, data: str) -> None:
        if self.classes is not None:
            self.text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == 'p' and self.classes is not None:
            self.paragraphs.append((self.classes, normalise(''.join(self.text))))
            self.classes = None
            self.text = []


def source_sets(html_path: Path) -> tuple[set[str], set[str]]:
    parser = ParagraphCollector()
    parser.feed(html_path.read_text(encoding='utf-8'))
    noindent: set[str] = set()
    authors: set[str] = set()
    for classes, text in parser.paragraphs:
        if classes & NO_INDENT_CLASSES:
            noindent.add(text)
        if 'author' in classes:
            authors.add(text)
    return noindent, authors


def paragraph_text(paragraph: ET.Element) -> str:
    return normalise(''.join(node.text or '' for node in paragraph.iter(f'{{{W}}}t')))


def get_style(paragraph: ET.Element) -> str | None:
    node = paragraph.find(f'{{{W}}}pPr/{{{W}}}pStyle')
    return node.get(f'{{{W}}}val') if node is not None else None


def set_style(paragraph: ET.Element, style_id: str) -> None:
    ppr = paragraph.find(f'{{{W}}}pPr')
    if ppr is None:
        ppr = ET.Element(f'{{{W}}}pPr')
        paragraph.insert(0, ppr)
    style = ppr.find(f'{{{W}}}pStyle')
    if style is None:
        style = ET.Element(f'{{{W}}}pStyle')
        ppr.insert(0, style)
    style.set(f'{{{W}}}val', style_id)


def set_first_line(style: ET.Element, value: str) -> None:
    ppr = style.find(f'{{{W}}}pPr')
    if ppr is None:
        ppr = ET.Element(f'{{{W}}}pPr')
        style.append(ppr)
    ind = ppr.find(f'{{{W}}}ind')
    if ind is None:
        ind = ET.Element(f'{{{W}}}ind')
        ppr.append(ind)
    ind.set(f'{{{W}}}firstLine', value)
    ind.attrib.pop(f'{{{W}}}hanging', None)


def format_styles(data: bytes) -> bytes:
    root = ET.fromstring(data)
    body = root.find(f"{{{W}}}style[@{{{W}}}styleId='BodyText']")
    first = root.find(f"{{{W}}}style[@{{{W}}}styleId='FirstParagraph']")
    if body is None or first is None:
        raise RuntimeError('Pandoc styles.xml lacks BodyText or FirstParagraph')
    set_first_line(body, '360')
    set_first_line(first, '0')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def format_document(data: bytes, noindent: set[str], authors: set[str]) -> bytes:
    root = ET.fromstring(data)
    pending_first = False
    for paragraph in root.iter(f'{{{W}}}p'):
        text = paragraph_text(paragraph)
        style = get_style(paragraph)
        if style in {'Heading1', 'Heading2', 'Heading3'}:
            pending_first = True
            continue
        if text in noindent:
            set_style(paragraph, 'FirstParagraph')
            pending_first = text in authors
            continue
        if pending_first and style in {'BodyText', 'FirstParagraph'}:
            set_style(paragraph, 'FirstParagraph')
            pending_first = False
            continue
        if style == 'FirstParagraph' and text:
            set_style(paragraph, 'BodyText')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--docx', required=True, type=Path)
    parser.add_argument('--html', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()

    noindent, authors = source_sets(args.html)
    output = args.out.resolve()
    temporary = output.with_suffix(output.suffix + '.tmp')
    with zipfile.ZipFile(args.docx, 'r') as zin, zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == 'word/styles.xml':
                data = format_styles(data)
            elif item.filename == 'word/document.xml':
                data = format_document(data, noindent, authors)
            zout.writestr(item, data)
    temporary.replace(output)
    print(output)


if __name__ == '__main__':
    main()
