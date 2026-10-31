#!/usr/bin/env python3
"""Package, schema-order, footnote and text audit of a finished DOCX.

Checks what makes Word refuse or "repair" a file (broken ZIP, malformed XML,
missing content types or relationship targets, undeclared mc:Ignorable
prefixes, out-of-order paragraph/section properties) and what makes the
book wrong (unmatched footnotes, internal links without bookmarks, missing
TOC field, styles not applied, text that differs from the source HTML).

Exit status: 0 when no ERROR was found, 1 otherwise.
"""
from __future__ import annotations

import argparse
import collections
import posixpath
import re
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path
import xml.etree.ElementTree as ET

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
PR = 'http://schemas.openxmlformats.org/package/2006/relationships'
WORD = re.compile(r'\w+', re.UNICODE)

PPR_ORDER = [
    'pStyle', 'keepNext', 'keepLines', 'pageBreakBefore', 'framePr',
    'widowControl', 'numPr', 'suppressLineNumbers', 'pBdr', 'shd', 'tabs',
    'suppressAutoHyphens', 'kinsoku', 'wordWrap', 'overflowPunct',
    'topLinePunct', 'autoSpaceDE', 'autoSpaceDN', 'bidi', 'adjustRightInd',
    'snapToGrid', 'spacing', 'ind', 'contextualSpacing', 'mirrorIndents',
    'suppressOverlap', 'jc', 'textDirection', 'textAlignment',
    'textboxTightWrap', 'outlineLvl', 'divId', 'cnfStyle', 'rPr', 'sectPr',
    'pPrChange',
]
STYLE_ORDER = [
    'name', 'aliases', 'basedOn', 'next', 'link', 'autoRedefine', 'hidden',
    'uiPriority', 'semiHidden', 'unhideWhenUsed', 'qFormat', 'locked',
    'personal', 'personalCompose', 'personalReply', 'rsid', 'pPr', 'rPr',
    'tblPr', 'trPr', 'tcPr', 'tblStylePr',
]
SECTPR_ORDER = [
    'headerReference', 'footerReference', 'footnotePr', 'endnotePr', 'type',
    'pgSz', 'pgMar', 'paperSrc', 'pgBorders', 'lnNumType', 'pgNumType', 'cols',
    'formProt', 'vAlign', 'noEndnote', 'titlePg', 'textDirection', 'bidi',
    'rtlGutter', 'docGrid', 'printerSettings', 'sectPrChange',
]
ORDERS = {'pPr': PPR_ORDER, 'style': STYLE_ORDER, 'sectPr': SECTPR_ORDER}


def w(name: str) -> str:
    return f'{{{W}}}{name}'


def local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


class SourceText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.body: list[str] = []
        self.notes: list[str] = []
        self.refs = 0
        self._aside = 0
        self._ref = False
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'script', 'style', 'title'}:
            self._skip += 1
        elif tag == 'aside':
            self._aside += 1
        elif tag == 'a' and attrs.get('epub:type') == 'noteref':
            self._ref = True
            self.refs += 1
        elif tag == 'br':
            self.handle_data(' ')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'title'} and self._skip:
            self._skip -= 1
        elif tag == 'aside' and self._aside:
            self._aside -= 1
        elif tag == 'a':
            self._ref = False
        if tag in {'p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'figcaption', 'aside',
                   'td', 'th', 'tr', 'caption'}:
            self.handle_data(' ')   # table cells are separate paragraphs in Word

    def handle_data(self, data):
        if self._skip or self._ref:
            return
        (self.notes if self._aside else self.body).append(data)


def paragraph_texts(root: ET.Element) -> str:
    chunks = []
    for paragraph in root.iter(w('p')):
        for node in paragraph.iter():
            if node.tag == w('t'):
                chunks.append(node.text or '')
            elif node.tag in (w('br'), w('tab'), w('cr')):
                chunks.append(' ')
        chunks.append(' ')
    return ''.join(chunks)


def words(text: str) -> collections.Counter:
    return collections.Counter(x.lower() for x in WORD.findall(text))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--docx', required=True, type=Path)
    parser.add_argument('--source', type=Path, help='reconstructed book HTML to compare against')
    parser.add_argument('--limit', type=int, default=8)
    args = parser.parse_args()

    errors: list[str] = []
    warnings: list[str] = []
    facts: list[str] = []

    zf = zipfile.ZipFile(args.docx)
    bad = zf.testzip()
    if bad:
        errors.append(f'zip: corrupt member {bad}')
    names = set(zf.namelist())
    parts: dict[str, ET.Element] = {}
    for name in sorted(names):
        if name.endswith(('.xml', '.rels')):
            data = zf.read(name)
            try:
                root = ET.fromstring(data)
            except ET.ParseError as error:
                errors.append(f'xml: {name} is not well-formed ({error})')
                continue
            parts[name] = root
            root_tag = re.search(rb'<(?![?!])[^>]*>', data).group(0).decode('utf-8', 'replace')
            ignorable = root.get(f'{{{MC}}}Ignorable')
            if ignorable:
                declared = set(re.findall(r'xmlns:([\w.-]+)=', root_tag))
                missing = [p for p in ignorable.split() if p not in declared]
                if missing:
                    errors.append(f'mc-ignorable: {name} lists undeclared prefixes {missing} '
                                  '(Word reports the file as corrupt)')

    types = parts.get('[Content_Types].xml')
    if types is None:
        errors.append('package: [Content_Types].xml is missing')
    else:
        defaults = {d.get('Extension', '').lower() for d in types.iter(f'{{{CT}}}Default')}
        overrides = {o.get('PartName', '').lstrip('/') for o in types.iter(f'{{{CT}}}Override')}
        for name in names:
            if name.endswith('/') or name == '[Content_Types].xml':
                continue
            extension = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
            if name not in overrides and extension not in defaults:
                errors.append(f'content-types: {name} has no content type')
        for override in overrides:
            if override not in names:
                errors.append(f'content-types: override for missing part {override}')

    for name, root in parts.items():
        if not name.endswith('.rels'):
            continue
        source_dir = posixpath.dirname(posixpath.dirname(name))
        for rel in root.iter(f'{{{PR}}}Relationship'):
            if rel.get('TargetMode') == 'External':
                continue
            target = rel.get('Target', '')
            resolved = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(source_dir, target))
            if resolved not in names:
                errors.append(f'relationship: {name} {rel.get("Id")} -> {target} does not exist')

    for name in ('word/document.xml', 'word/styles.xml', 'word/footnotes.xml'):
        root = parts.get(name)
        if root is None:
            continue
        for element in root.iter():
            order = ORDERS.get(local(element.tag))
            if order is None or element.tag.split('}')[0][1:] != W:
                continue
            ranks = [order.index(local(c.tag)) for c in element if local(c.tag) in order and c.tag.startswith('{' + W)]
            if ranks != sorted(ranks):
                children = [local(c.tag) for c in element]
                errors.append(f'schema-order: {name}: <w:{local(element.tag)}> children out of order: {children}')

    document = parts.get('word/document.xml')
    styles = parts.get('word/styles.xml')
    footnotes = parts.get('word/footnotes.xml')
    if document is None or styles is None:
        errors.append('package: word/document.xml or word/styles.xml is missing')
        report(errors, warnings, facts, args.limit)

    style_ids = {s.get(w('styleId')): s for s in styles.iter(w('style'))}

    def first_line(style_id: str) -> str | None:
        style = style_ids.get(style_id)
        ind = style.find(f'{w("pPr")}/{w("ind")}') if style is not None else None
        return ind.get(w('firstLine')) if ind is not None else None

    if first_line('BodyText') in (None, '0'):
        warnings.append('styles: Body Text has no first-line indent (was format_docx.py run?)')
    if first_line('FirstParagraph') != '0':
        warnings.append('styles: First Paragraph does not override the indent with firstLine="0"')
    used = collections.Counter(p.get(w('val')) for p in document.iter(w('pStyle')))
    for style_id in used:
        if style_id not in style_ids:
            errors.append(f'styles: paragraph style {style_id} is used but not defined')
    facts.append('paragraph styles: ' + ', '.join(f'{k}×{v}' for k, v in used.most_common(10)))

    refs = [r.get(w('id')) for r in document.iter(w('footnoteReference'))]
    notes = {}
    if footnotes is not None:
        notes = {n.get(w('id')): n for n in footnotes.iter(w('footnote'))
                 if n.get(w('type')) in (None, 'normal')}
    for ref in refs:
        if ref not in notes:
            errors.append(f'footnotes: reference to missing footnote id {ref}')
    duplicated = [r for r, n in collections.Counter(refs).items() if n > 1]
    if duplicated:
        errors.append(f'footnotes: footnotes referenced more than once: {duplicated[:5]}')
    unreferenced = set(notes) - set(refs)
    if unreferenced:
        errors.append(f'footnotes: {len(unreferenced)} footnote(s) are never referenced')
    custom = sum(1 for r in document.iter(w('footnoteReference')) if r.get(w('customMarkFollows')) in ('1', 'true'))
    sections = sum(1 for _ in document.iter(w('sectPr')))
    facts.append(f'{len(refs)} footnote reference(s), {len(notes)} footnote(s), {custom} custom mark(s), '
                 f'{sections} section(s)')

    bookmarks = {b.get(w('name')) for b in document.iter(w('bookmarkStart'))}
    for link in document.iter(w('hyperlink')):
        anchor = link.get(w('anchor'))
        if anchor and anchor not in bookmarks:
            errors.append(f'links: internal hyperlink to #{anchor} has no bookmark')

    instructions = ' '.join(t.text or '' for t in document.iter(w('instrText')))
    if 'TOC' not in instructions:
        warnings.append('toc: no TOC field (run Pandoc with --toc)')

    if args.source:
        source = SourceText()
        source.feed(args.source.read_text(encoding='utf-8'))
        if source.refs != len(refs):
            errors.append(f'footnotes: the source has {source.refs} note reference(s), the DOCX {len(refs)}')
        title_text = ' '.join(''.join(t.text or '' for t in p.iter(w('t')))
                              for p in document.iter(w('p'))
                              if (p.find(f'{w("pPr")}/{w("pStyle")}') is not None
                                  and p.find(f'{w("pPr")}/{w("pStyle")}').get(w('val')) in
                                  {'Title', 'Subtitle', 'Author', 'Date', 'Abstract', 'TOCHeading'}))
        for label, expected, actual in (
                ('body text', words(''.join(source.body)), words(paragraph_texts(document)) - words(title_text)),
                ('note text', words(''.join(source.notes)),
                 words(paragraph_texts(footnotes)) if footnotes is not None else collections.Counter())):
            missing, extra = expected - actual, actual - expected
            total = sum(expected.values()) or 1
            facts.append(f'{label}: {total} source words, {sum(missing.values())} missing, {sum(extra.values())} extra')
            if missing or extra:
                facts.append('    ' + '; '.join(part for part in (
                    'missing ' + ', '.join(f'{k}×{v}' for k, v in missing.most_common(8)) if missing else '',
                    'extra ' + ', '.join(f'{k}×{v}' for k, v in extra.most_common(8)) if extra else '') if part))
            if sum(missing.values()) / total > 0.002:
                errors.append(f'text-missing: {label}: ' + ', '.join(f'{k}×{v}' for k, v in missing.most_common(12)))
            if sum(extra.values()) / total > 0.002:
                errors.append(f'text-extra: {label}: ' + ', '.join(f'{k}×{v}' for k, v in extra.most_common(12)))

    report(errors, warnings, facts, args.limit, args.docx)


def report(errors, warnings, facts, limit, path=None):
    if path:
        print(f'DOCX: {path}')
    for fact in facts:
        print(f'  {fact}')
    for kind, items in (('ERROR', errors), ('WARNING', warnings)):
        shown = collections.Counter()
        for item in items:
            key = item.split(':', 1)[0]
            shown[key] += 1
            if shown[key] <= limit:
                print(f'{kind}: {item}')
    print(f'{len(errors)} error(s), {len(warnings)} warning(s)')
    sys.exit(1 if errors else 0)


if __name__ == '__main__':
    main()
