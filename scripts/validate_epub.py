#!/usr/bin/env python3
"""Structural, link and footnote audit of an EPUB 3 (complements epubcheck).

epubcheck accepts EPUBs whose notes do not work: unlinked superscripts,
notes without a way back, note numbers leaking into the table of contents,
duplicated numbering.  This script checks those things, plus the package
rules the skill relies on.  With --source it also compares the note labels
and the text with the reconstructed HTML.

Exit status: 0 when no ERROR was found, 1 otherwise.
"""
from __future__ import annotations

import argparse
import collections
import posixpath
import re
import shutil
import subprocess
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urldefrag
import xml.etree.ElementTree as ET

XHTML = 'http://www.w3.org/1999/xhtml'
OPS = 'http://www.idpf.org/2007/ops'
OPF = 'http://www.idpf.org/2007/opf'
DC = 'http://purl.org/dc/elements/1.1/'
EPUB_TYPE = f'{{{OPS}}}type'
NOTE_TYPES = {'footnote', 'endnote', 'rearnote'}
NOTE_ROLES = {'doc-footnote', 'doc-endnote'}
# A note marker: digits or note symbols, optionally bracketed ("[12]").
# Single letters are not markers: Spanish ordinals are written 1.<sup>a</sup>.
MARKER = re.compile(r'^\s*[\[(]?(\d{1,4}|[*†‡§¶#]+)[\])]?\s*$')
WORD = re.compile(r'\w+', re.UNICODE)
BLOCKS = {'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote', 'figcaption',
          'aside', 'section', 'td', 'th', 'br', 'dt', 'dd', 'figure', 'body'}


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.facts: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def fact(self, message: str) -> None:
        self.facts.append(message)

    def show(self, limit: int) -> None:
        for fact in self.facts:
            print(f'  {fact}')
        for kind, items in (('ERROR', self.errors), ('WARNING', self.warnings)):
            counts = collections.Counter(item.split(':', 1)[0] for item in items)
            shown = collections.Counter()
            for item in items:
                key = item.split(':', 1)[0]
                shown[key] += 1
                if shown[key] <= limit:
                    print(f'{kind}: {item}')
                elif shown[key] == limit + 1:
                    print(f'{kind}: {key}: ... {counts[key] - limit} more')
        print(f'{len(self.errors)} error(s), {len(self.warnings)} warning(s)')


def local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def text_of(element: ET.Element) -> str:
    return re.sub(r'\s+', ' ', ''.join(element.itertext())).strip()


def classes(element: ET.Element) -> set[str]:
    return set((element.get('class') or '').split())


def is_note(element: ET.Element) -> bool:
    return bool(set((element.get(EPUB_TYPE) or '').split()) & NOTE_TYPES
                or element.get('role') in NOTE_ROLES)


def is_noteref(element: ET.Element) -> bool:
    return (local(element.tag) == 'a'
            and ('noteref' in (element.get(EPUB_TYPE) or '').split()
                 or element.get('role') == 'doc-noteref'))


class SourceText(HTMLParser):
    """Body text, note text and noteref labels of the reconstructed HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.body: list[str] = []
        self.notes: list[str] = []
        self.labels: list[str] = []
        self._aside = 0
        self._ref: list[str] | None = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'script', 'style', 'title'}:
            self._skip += 1
        elif tag == 'aside':
            self._aside += 1
        elif tag == 'a' and attrs.get('epub:type') == 'noteref':
            self._ref = []
        elif tag == 'br':
            self.handle_data(' ')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'title'} and self._skip:
            self._skip -= 1
        elif tag == 'aside' and self._aside:
            self._aside -= 1
        elif tag == 'a' and self._ref is not None:
            self.labels.append(''.join(self._ref).strip())
            self._ref = None
        if tag in BLOCKS:
            self.handle_data(' ')

    def handle_data(self, data):
        if self._skip:
            return
        if self._ref is not None:
            self._ref.append(data)
            return
        (self.notes if self._aside else self.body).append(data)


def words(text: str) -> collections.Counter:
    return collections.Counter(w.lower() for w in WORD.findall(text))


def describe_diff(name: str, expected: collections.Counter, actual: collections.Counter, report: Report) -> None:
    missing = expected - actual
    extra = actual - expected
    total = sum(expected.values()) or 1
    lost = sum(missing.values())
    added = sum(extra.values())
    report.fact(f'{name}: {total} source words, {lost} missing, {added} extra '
                f'({100 * (1 - lost / total):.2f}% of source words present)')
    if lost / total > 0.002:
        report.error(f'text-missing: {name}: words missing from the EPUB, e.g. '
                     + ', '.join(f'{w}×{n}' for w, n in missing.most_common(12)))
    if added / total > 0.002:
        report.error(f'text-extra: {name}: words not in the source (running heads? page numbers?), e.g. '
                     + ', '.join(f'{w}×{n}' for w, n in extra.most_common(12)))


def check_package(zf: zipfile.ZipFile, report: Report):
    infos = zf.infolist()
    if not infos or infos[0].filename != 'mimetype':
        report.error('package: mimetype is not the first ZIP entry')
    else:
        first = infos[0]
        if first.compress_type != zipfile.ZIP_STORED:
            report.error('package: mimetype is compressed')
        if first.extra:
            report.warn('package: mimetype entry has an extra field')
        if zf.read('mimetype') != b'application/epub+zip':
            report.error('package: mimetype content is not exactly application/epub+zip')
    bad = zf.testzip()
    if bad:
        report.error(f'package: corrupt ZIP member {bad}')

    names = set(zf.namelist())
    try:
        container = ET.fromstring(zf.read('META-INF/container.xml'))
        opf_path = container.find('.//{*}rootfile').get('full-path')
        opf = ET.fromstring(zf.read(opf_path))
    except (KeyError, ET.ParseError, AttributeError) as error:
        report.error(f'package: cannot read container/OPF ({error})')
        return None
    base = posixpath.dirname(opf_path)
    metadata = opf.find(f'{{{OPF}}}metadata')
    for field in ('title', 'identifier', 'language'):
        if metadata is None or metadata.find(f'{{{DC}}}{field}') is None:
            report.error(f'metadata: dc:{field} is missing')
    if metadata is not None and not any(m.get('property') == 'dcterms:modified'
                                        for m in metadata.findall(f'{{{OPF}}}meta')):
        report.error('metadata: dcterms:modified is missing')
    language = metadata.find(f'{{{DC}}}language') if metadata is not None else None

    manifest = {}
    for item in opf.iter(f'{{{OPF}}}item'):
        href = posixpath.normpath(posixpath.join(base, unquote(item.get('href', ''))))
        manifest[item.get('id')] = (href, item.get('media-type', ''), set((item.get('properties') or '').split()))
        if href not in names:
            report.error(f'manifest: {href} is listed but missing from the ZIP')
    spine = []
    for ref in opf.iter(f'{{{OPF}}}itemref'):
        entry = manifest.get(ref.get('idref'))
        if entry is None:
            report.error(f'spine: itemref {ref.get("idref")} is not in the manifest')
        else:
            spine.append(entry[0])
    navs = [h for h, _, p in manifest.values() if 'nav' in p]
    if len(navs) != 1:
        report.error(f'package: expected exactly one nav document, found {len(navs)}')
    covers = [h for h, mt, p in manifest.values() if 'cover-image' in p]
    if not covers:
        report.warn('package: no manifest item has properties="cover-image"')
    report.fact(f'OPF {opf_path}: {len(manifest)} manifest items, {len(spine)} spine items, '
                f'language {language.text if language is not None else "?"}')
    return opf_path, manifest, spine, navs[0] if navs else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--epub', required=True, type=Path)
    parser.add_argument('--source', type=Path, help='reconstructed book HTML to compare against')
    parser.add_argument('--epubcheck', default='auto',
                        help='"auto" (run epubcheck if installed), "none", or the path to epubcheck.jar')
    parser.add_argument('--limit', type=int, default=8, help='messages shown per category')
    args = parser.parse_args()

    report = Report()
    zf = zipfile.ZipFile(args.epub)
    package = check_package(zf, report)
    if package is None:
        report.show(args.limit)
        sys.exit(1)
    _opf, manifest, spine, nav = package
    names = set(zf.namelist())

    documents: dict[str, ET.Element] = {}
    for href, media_type, _ in manifest.values():
        if href not in names:
            continue
        data = zf.read(href)
        if media_type in {'application/xhtml+xml', 'image/svg+xml', 'application/x-dtbncx+xml'} \
                or href.endswith(('.xhtml', '.xml', '.ncx', '.opf')):
            try:
                tree = ET.fromstring(data)
            except ET.ParseError as error:
                report.error(f'xml: {href} is not well-formed ({error})')
                continue
            if media_type == 'application/xhtml+xml':
                documents[href] = tree
        if media_type == 'text/css' and re.search(rb'(?<![\w.-])hanging-indent\s*:', data):
            report.error(f'css: {href} uses the non-standard hanging-indent property')

    ids = {href: {el.get('id'): el for el in tree.iter() if el.get('id')} for href, tree in documents.items()}
    parents = {href: {c: p for p in tree.iter() for c in p} for href, tree in documents.items()}

    def resolve(doc: str, href: str):
        target, fragment = urldefrag(href)
        target_doc = posixpath.normpath(posixpath.join(posixpath.dirname(doc), unquote(target))) if target else doc
        return target_doc, fragment

    link_targets = set()
    for doc, tree in documents.items():
        for a in tree.iter(f'{{{XHTML}}}a'):
            if a.get('href') and not re.match(r'^[a-z][a-z0-9+.-]*:', a.get('href')):
                link_targets.add(resolve(doc, a.get('href')))

    incoming = collections.Counter()
    note_count = ref_count = 0
    epub_labels: list[str] = []
    for doc in spine:
        tree = documents.get(doc)
        if tree is None:
            continue
        html_el = tree
        if not (html_el.get('lang') or html_el.get('{http://www.w3.org/XML/1998/namespace}lang')):
            report.warn(f'lang: {doc} has no lang/xml:lang on <html>')
        for style in tree.iter(f'{{{XHTML}}}style'):
            if not (style.text or '').strip():
                report.error(f'style: {doc} contains an empty <style> element')
        if doc != nav and not any(l.get('rel') == 'stylesheet' for l in tree.iter(f'{{{XHTML}}}link')):
            report.warn(f'css: {doc} links no stylesheet')

        for a in tree.iter(f'{{{XHTML}}}a'):
            href = a.get('href')
            if not href or re.match(r'^[a-z][a-z0-9+.-]*:', href):
                continue
            target_doc, fragment = resolve(doc, href)
            if target_doc not in names:
                report.error(f'link: {doc}: {href} points to a missing file')
                continue
            if fragment and target_doc in ids and fragment not in ids[target_doc]:
                report.error(f'link: {doc}: {href} points to a missing id')
                continue
            if not is_noteref(a):
                sup = a.find(f'.//{{{XHTML}}}sup')
                if sup is not None and MARKER.match(text_of(sup)) and doc != nav:
                    report.error(f'noteref-semantics: {doc}: link "{text_of(a)}" to {href} looks like a note '
                                 'reference but lacks epub:type="noteref" (no pop-up in Apple Books, Kobo, Thorium)')
                continue
            ref_count += 1
            label = text_of(a)
            epub_labels.append(label)
            target = ids.get(target_doc, {}).get(fragment)
            note = target
            while note is not None and not is_note(note):
                note = parents[target_doc].get(note)
            if note is None:
                report.error(f'noteref: {doc}: {href} does not point into an epub:type footnote/endnote')
                continue
            incoming[(target_doc, note.get('id'))] += 1
            backs = [b for b in note.iter(f'{{{XHTML}}}a') if b.get('href')]
            back_ok = False
            for back in backs:
                back_doc, back_fragment = resolve(target_doc, back.get('href'))
                if back_doc == doc and a.get('id') and back_fragment == a.get('id'):
                    back_ok = True
                    if text_of(back) != label:
                        report.warn(f'label: {doc}: reference "{label}" but its note is labelled "{text_of(back)}"')
                    break
            if not back_ok:
                report.error(f'backlink: {doc}: note {target_doc}#{fragment} has no link back to its reference')
            if a.find(f'{{{XHTML}}}sup') is None and 'footnote-ref' not in classes(a):
                report.warn(f'noteref: {doc}: reference "{label}" is not superscript')

        for element in tree.iter():
            if is_note(element) and element.get('id'):
                note_count += 1
                if not incoming[(doc, element.get('id'))]:
                    report.error(f'orphan-note: {doc}#{element.get("id")} has no incoming note reference')

        doc_parents = parents[doc]
        for sup in tree.iter(f'{{{XHTML}}}sup'):
            if not MARKER.match(text_of(sup)):
                continue
            node, accounted = sup, False
            while node is not None:
                # linked, inside a note (its label), or marked as a real superscript
                if (local(node.tag) == 'a' or is_note(node) or 'not-a-note' in classes(node)
                        or (node.get('id') and (doc, node.get('id')) in link_targets)):
                    accounted = True
                    break
                node = doc_parents.get(node)
            if not accounted:
                report.error(f'bare-sup: {doc}: superscript "{text_of(sup)}" is not a linked note reference '
                             '(if it is a siglum or exponent, wrap it in <span class="not-a-note">)')

    if nav and nav in documents:
        for a in documents[nav].iter(f'{{{XHTML}}}a'):
            if a.find(f'.//{{{XHTML}}}sup') is not None or is_noteref(a):
                report.error(f'toc: navigation entry "{text_of(a)}" contains a note marker')
    if 'toc.ncx' in ' '.join(names):
        for name in names:
            if name.endswith('.ncx'):
                ncx = ET.fromstring(zf.read(name))
                for label in ncx.iter('{http://www.daisy.org/z3986/2005/ncx/}text'):
                    if re.search(r'[a-záéíóúñ]\d+$', (label.text or '').strip()):
                        report.warn(f'toc: NCX label "{label.text}" ends in a digit glued to a word (note marker?)')

    for href, media_type, _ in manifest.values():
        if media_type.startswith('image/') and href not in names:
            report.error(f'image: {href} is missing')
    for doc, tree in documents.items():
        for img in tree.iter(f'{{{XHTML}}}img'):
            target, _ = resolve(doc, img.get('src', ''))
            if target not in names:
                report.error(f'image: {doc}: <img src="{img.get("src")}"> is missing')
            if img.get('alt') is None:
                report.warn(f'image: {doc}: <img src="{img.get("src")}"> has no alt attribute')

    report.fact(f'{ref_count} note reference(s), {note_count} note(s)')

    if args.source:
        source = SourceText()
        source.feed(args.source.read_text(encoding='utf-8'))
        if source.labels != epub_labels:
            report.error(f'source-notes: the source has {len(source.labels)} note reference(s), '
                         f'the EPUB {len(epub_labels)}; first difference at position '
                         f'{next((i for i, (x, y) in enumerate(zip(source.labels, epub_labels)) if x != y), min(len(source.labels), len(epub_labels)))}')
        body_words, note_words = collections.Counter(), collections.Counter()
        for doc in spine:
            tree = documents.get(doc)
            if tree is None or doc == nav or re.search(r'(title_?page|cover)\.x?html$', doc):
                continue
            body = tree.find(f'{{{XHTML}}}body')
            notes_text, main_text = [], []

            def walk(element: ET.Element, in_note: bool) -> None:
                here = in_note or is_note(element)
                bucket = notes_text if here else main_text
                if is_noteref(element) or 'footnote-back' in classes(element):
                    return  # labels are compared separately
                bucket.append(element.text or '')
                for child_element in element:
                    walk(child_element, here)
                    bucket.append(child_element.tail or '')
                if local(element.tag) in BLOCKS:
                    bucket.append(' ')

            walk(body, False)
            body_words += words(' '.join(main_text))
            note_words += words(' '.join(notes_text))
        describe_diff('body text', words(''.join(source.body)), body_words, report)
        describe_diff('note text', words(''.join(source.notes)), note_words, report)

    if args.epubcheck != 'none':
        command = None
        if args.epubcheck == 'auto':
            if shutil.which('epubcheck'):
                command = ['epubcheck']
        else:
            command = ['java', '-jar', args.epubcheck]
        if command:
            result = subprocess.run(command + [str(args.epub)], capture_output=True, text=True)
            summary = [l for l in (result.stdout + result.stderr).splitlines() if l.startswith(('ERROR', 'FATAL', 'Messages'))]
            for line in summary[:args.limit]:
                (report.error if line.startswith(('ERROR', 'FATAL')) else report.fact)(f'epubcheck: {line}')
        else:
            report.fact('epubcheck: not run (not installed)')

    print(f'EPUB: {args.epub}')
    report.show(args.limit)
    sys.exit(1 if report.errors else 0)


if __name__ == '__main__':
    main()
