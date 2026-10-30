#!/usr/bin/env python3
"""Finish a Pandoc-generated DOCX for book use.

What it changes:

* paragraph styles: Body Text gets a first-line indent and no paragraph
  spacing; First Paragraph and every other style built on Body Text are
  explicitly unindented; the book's custom styles (Chapter Author,
  Bibliography, Index Entry, ...) get their indents;
* chapters: a section break is inserted before every Heading 1 so that each
  chapter starts on a new page and Word restarts footnote numbering per
  chapter when the source does;
* footnote marks: when the source HTML is given, the printed labels are
  compared with Word's automatic numbering; symbols such as "*" become
  custom marks so the visible marks match the PDF.

Safety rules: every XML part is parsed and re-serialised with its original
root element (namespace declarations and mc:Ignorable stay intact) and new
elements are inserted in schema order. The input DOCX is never modified.
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path
import xml.etree.ElementTree as ET

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def w(name: str) -> str:
    return f'{{{W}}}{name}'


# Child order of the elements this script edits (ECMA-376, Part 1).
STYLE_ORDER = [
    'name', 'aliases', 'basedOn', 'next', 'link', 'autoRedefine', 'hidden',
    'uiPriority', 'semiHidden', 'unhideWhenUsed', 'qFormat', 'locked',
    'personal', 'personalCompose', 'personalReply', 'rsid', 'pPr', 'rPr',
    'tblPr', 'trPr', 'tcPr', 'tblStylePr',
]
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
RPR_ORDER = [
    'rStyle', 'rFonts', 'b', 'bCs', 'i', 'iCs', 'caps', 'smallCaps', 'strike',
    'dstrike', 'outline', 'shadow', 'emboss', 'imprint', 'noProof',
    'snapToGrid', 'vanish', 'webHidden', 'color', 'spacing', 'w', 'kern',
    'position', 'sz', 'szCs', 'highlight', 'u', 'effect', 'bdr', 'shd',
    'fitText', 'vertAlign', 'rtl', 'cs', 'em', 'lang', 'eastAsianLayout',
    'specVanish', 'oMath',
]
SECTPR_ORDER = [
    'headerReference', 'footerReference', 'footnotePr', 'endnotePr', 'type',
    'pgSz', 'pgMar', 'paperSrc', 'pgBorders', 'lnNumType', 'pgNumType', 'cols',
    'formProt', 'vAlign', 'noEndnote', 'titlePg', 'textDirection', 'bidi',
    'rtlGutter', 'docGrid', 'printerSettings', 'sectPrChange',
]
FOOTNOTE_PR_ORDER = ['pos', 'numFmt', 'numStart', 'numRestart', 'footnote']
SETTINGS_ORDER = [
    'writeProtection', 'view', 'zoom', 'removePersonalInformation',
    'removeDateAndTime', 'doNotDisplayPageBoundaries', 'displayBackgroundShape',
    'printPostScriptOverText', 'printFractionalCharacterWidth', 'printFormsData',
    'embedTrueTypeFonts', 'embedSystemFonts', 'saveSubsetFonts', 'saveFormsData',
    'mirrorMargins', 'alignBordersAndEdges', 'bordersDoNotSurroundHeader',
    'bordersDoNotSurroundFooter', 'gutterAtTop', 'hideSpellingErrors',
    'hideGrammaticalErrors', 'activeWritingStyle', 'proofState', 'formsDesign',
    'attachedTemplate', 'linkStyles', 'stylePaneFormatFilter',
    'stylePaneSortMethod', 'documentType', 'mailMerge', 'revisionView',
    'trackRevisions', 'doNotTrackMoves', 'doNotTrackFormatting',
    'documentProtection', 'autoFormatOverride', 'styleLockTheme',
    'styleLockQFSet', 'defaultTabStop', 'autoHyphenation',
    'consecutiveHyphenLimit', 'hyphenationZone', 'doNotHyphenateCaps',
    'showEnvelope', 'summaryLength', 'clickAndTypeStyle', 'defaultTableStyle',
    'evenAndOddHeaders', 'bookFoldRevPrinting', 'bookFoldPrinting',
    'bookFoldPrintingSheets', 'drawingGridHorizontalSpacing',
    'drawingGridVerticalSpacing', 'displayHorizontalDrawingGridEvery',
    'displayVerticalDrawingGridEvery', 'doNotUseMarginsForDrawingGridOrigin',
    'drawingGridHorizontalOrigin', 'drawingGridVerticalOrigin',
    'doNotShadeFormData', 'noPunctuationKerning', 'characterSpacingControl',
    'printTwoOnOne', 'strictFirstAndLastChars', 'noLineBreaksAfter',
    'noLineBreaksBefore', 'savePreviewPicture', 'doNotValidateAgainstSchema',
    'saveInvalidXml', 'ignoreMixedContent', 'alwaysShowPlaceholderText',
    'doNotDemarcateInvalidXml', 'saveXmlDataOnly', 'useXSLTWhenSaving',
    'saveThroughXslt', 'showXMLTags', 'alwaysMergeEmptyNamespace',
    'updateFields', 'hdrShapeDefaults', 'footnotePr', 'endnotePr', 'compat',
    'docVars', 'rsids', 'mathPr', 'attachedSchema', 'themeFontLang',
    'clrSchemeMapping', 'doNotIncludeSubdocsInStats',
    'doNotAutoCompressPictures', 'forceUpgrade', 'captions',
    'readModeInkLockDown', 'smartTagType', 'schemaLibrary', 'shapeDefaults',
    'doNotEmbedSmartTags', 'decimalSymbol', 'listSeparator',
]

# A Body Text paragraph directly after one of these styles is the first
# paragraph of its block and is set without indent.
OPENING_STYLES = {'ChapterAuthor', 'ChapterLabel', 'Epigraph', 'EpigraphSource', 'Dedication'}

BODY_INDENT = '360'  # twips: 0.25 inch, a conventional book indent

# Book styles that the reconstructed HTML may request with data-custom-style.
# Values: paragraph properties (spacing, indentation, alignment) and run
# properties.  Pandoc creates a style it does not know as a bare custom style
# based on Body Text, so each one needs explicit, unindented properties.
CUSTOM_STYLES = {
    'Chapter Author': {'ind': {'firstLine': '0'}, 'spacing': {'before': '0', 'after': '480'}, 'keepNext': True},
    'Chapter Label': {'ind': {'firstLine': '0'}, 'spacing': {'before': '0', 'after': '120'}, 'keepNext': True},
    'Epigraph': {'ind': {'left': '1440', 'firstLine': '0'}, 'spacing': {'before': '0', 'after': '120'}, 'italic': True},
    'Epigraph Source': {'ind': {'left': '1440', 'firstLine': '0'}, 'jc': 'right', 'spacing': {'before': '0', 'after': '360'}},
    'Bibliography': {'ind': {'left': '720', 'hanging': '720'}, 'spacing': {'before': '0', 'after': '60'}},
    # Chicago style: runover lines are indented deeper than subentries.
    'Index Entry': {'ind': {'left': '720', 'hanging': '720'}, 'spacing': {'before': '0', 'after': '0'}},
    'Index Subentry': {'ind': {'left': '720', 'hanging': '360'}, 'spacing': {'before': '0', 'after': '0'}},
    'Index Letter': {'ind': {'firstLine': '0'}, 'spacing': {'before': '240', 'after': '60'}, 'keepNext': True},
    'Centered': {'ind': {'firstLine': '0'}, 'jc': 'center'},
    'Right': {'ind': {'firstLine': '0'}, 'jc': 'right'},
    'Dedication': {'ind': {'firstLine': '0'}, 'jc': 'center', 'italic': True, 'spacing': {'before': '2400', 'after': '0'}},
    'Copyright': {'ind': {'firstLine': '0'}, 'spacing': {'before': '0', 'after': '60'}},
    'Front Matter': {'ind': {'firstLine': '0'}, 'spacing': {'before': '0', 'after': '120'}},
    # Pandoc's notes are body-size; books (and Word's own default) use 10 pt.
    'Footnote Text': {'spacing': {'before': '0', 'after': '60'}, 'size': '20'},
}

ROOT_TAG = re.compile(r'<(?![?!])[^>]*>')
XMLNS = re.compile(r'\sxmlns(?::([\w.-]+))?="([^"]*)"')


# --------------------------------------------------------------------------
# Namespace-preserving XML round trip

def load_part(data: bytes) -> ET.Element:
    for _event, (prefix, uri) in ET.iterparse(io.BytesIO(data), events=('start-ns',)):
        if prefix and not re.fullmatch(r'ns\d+', prefix):
            ET.register_namespace(prefix, uri)
    return ET.fromstring(data)


def dump_part(original: bytes, root: ET.Element) -> bytes:
    """Serialise root, keeping the original prolog and root start tag.

    ElementTree drops namespace declarations it does not see used, which
    breaks mc:Ignorable in Word-made parts.  Re-using the original start tag
    keeps them; declarations ElementTree needed but the original lacked are
    added back.
    """
    source = original.decode('utf-8')
    old = ROOT_TAG.search(source)
    out = ET.tostring(root, encoding='unicode')
    new = ROOT_TAG.search(out)
    tag = old.group(0)
    declared = {m.group(1) or '' for m in XMLNS.finditer(tag)}
    extra = ''.join(m.group(0) for m in XMLNS.finditer(new.group(0))
                    if (m.group(1) or '') not in declared)
    if extra:
        tag = tag[:-2] + extra + '/>' if tag.endswith('/>') else tag[:-1] + extra + '>'
    return (source[:old.start()] + tag + out[new.end():]).encode('utf-8')


def child(parent: ET.Element, name: str, order: list[str]) -> ET.Element:
    """Return parent/w:name, creating it at its schema position."""
    found = parent.find(w(name))
    if found is not None:
        return found
    element = ET.Element(w(name))
    rank = order.index(name)
    position = len(parent)
    for index, existing in enumerate(parent):
        local = existing.tag.rsplit('}', 1)[-1]
        if local in order and order.index(local) > rank:
            position = index
            break
    parent.insert(position, element)
    return element


# --------------------------------------------------------------------------
# Source HTML: note labels in document order and chapter boundaries

class SourceNotes(HTMLParser):
    """Collect (chapter_index, label) for every noteref link in the source."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chapter = 0
        self.labels: list[tuple[int, str]] = []
        self._in_ref = False
        self._text: list[str] = []
        self._aside_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'h1' and not self._aside_depth:
            self.chapter += 1
        elif tag == 'aside':
            self._aside_depth += 1
        elif tag == 'a' and attrs.get('epub:type') == 'noteref':
            self._in_ref = True
            self._text = []

    def handle_endtag(self, tag):
        if tag == 'aside' and self._aside_depth:
            self._aside_depth -= 1
        elif tag == 'a' and self._in_ref:
            self.labels.append((self.chapter, ''.join(self._text).strip()))
            self._in_ref = False

    def handle_data(self, data):
        if self._in_ref:
            self._text.append(data)


def source_labels(path: Path) -> list[tuple[int, str]]:
    parser = SourceNotes()
    parser.feed(path.read_text(encoding='utf-8'))
    return parser.labels


# --------------------------------------------------------------------------
# styles.xml

def style_map(root: ET.Element) -> dict[str, ET.Element]:
    return {s.get(w('styleId')): s for s in root.findall(w('style'))}


def based_on_chain(style: ET.Element, styles: dict[str, ET.Element]) -> list[str]:
    chain, seen = [], set()
    node = style
    while node is not None:
        parent = node.find(w('basedOn'))
        if parent is None:
            break
        sid = parent.get(w('val'))
        if sid in seen:
            break
        seen.add(sid)
        chain.append(sid)
        node = styles.get(sid)
    return chain


def set_indent(ppr: ET.Element, **values: str) -> None:
    ind = child(ppr, 'ind', PPR_ORDER)
    if 'firstLine' in values:
        ind.attrib.pop(w('hanging'), None)
    if 'hanging' in values:
        ind.attrib.pop(w('firstLine'), None)
    for key, value in values.items():
        ind.set(w(key), value)


def apply_properties(style: ET.Element, props: dict) -> None:
    ppr = child(style, 'pPr', STYLE_ORDER)
    if props.get('keepNext'):
        child(ppr, 'keepNext', PPR_ORDER)
    if 'spacing' in props:
        spacing = child(ppr, 'spacing', PPR_ORDER)
        for key, value in props['spacing'].items():
            spacing.set(w(key), value)
    if 'ind' in props:
        set_indent(ppr, **props['ind'])
    if 'jc' in props:
        child(ppr, 'jc', PPR_ORDER).set(w('val'), props['jc'])
    if props.get('italic'):
        rpr = child(style, 'rPr', STYLE_ORDER)
        child(rpr, 'i', RPR_ORDER)
        child(rpr, 'iCs', RPR_ORDER)
    if 'size' in props:  # half-points
        rpr = child(style, 'rPr', STYLE_ORDER)
        child(rpr, 'sz', RPR_ORDER).set(w('val'), props['size'])
        child(rpr, 'szCs', RPR_ORDER).set(w('val'), props['size'])


def schema_sort(element: ET.Element, order: list[str]) -> None:
    """Reorder children into schema order when every child is a known element.

    Pandoc 3.1's own reference styles have pPr before qFormat and jc before
    spacing; Word tolerates that, validators do not.
    """
    children = list(element)
    names = [c.tag.rsplit('}', 1)[-1] for c in children]
    if not children or any(n not in order or not c.tag.startswith(f'{{{W}}}') for n, c in zip(names, children)):
        return
    ordered = [c for _, c in sorted(zip(names, children), key=lambda pair: order.index(pair[0]))]
    if ordered != children:
        for c in children:
            element.remove(c)
        element.extend(ordered)


def format_styles(data: bytes, indent: str = BODY_INDENT) -> bytes:
    root = load_part(data)
    for style in root.findall(w('style')):
        schema_sort(style, STYLE_ORDER)
        for ppr in style.findall(w('pPr')):
            schema_sort(ppr, PPR_ORDER)
        for rpr in style.findall(w('rPr')):
            schema_sort(rpr, RPR_ORDER)
    styles = style_map(root)
    body = styles.get('BodyText')
    first = styles.get('FirstParagraph')
    if body is None or first is None:
        raise RuntimeError('styles.xml lacks the Pandoc Body Text / First Paragraph styles')

    apply_properties(body, {'spacing': {'before': '0', 'after': '0'}, 'ind': {'firstLine': indent}})
    apply_properties(first, {'ind': {'firstLine': '0'}})

    names = {s.find(w('name')).get(w('val')): s for s in styles.values() if s.find(w('name')) is not None}
    for name, props in CUSTOM_STYLES.items():
        if name in names:
            apply_properties(names[name], props)

    # Any other style inheriting from Body Text (Compact list items, captions,
    # custom styles) must not inherit the first-line indent.
    for sid, style in styles.items():
        if sid in {'BodyText', 'FirstParagraph'} or style.get(w('type')) != 'paragraph':
            continue
        if 'BodyText' in based_on_chain(style, styles):
            ppr = style.find(w('pPr'))
            if ppr is None or ppr.find(w('ind')) is None:
                apply_properties(style, {'ind': {'firstLine': '0'}})
    return dump_part(data, root)


# --------------------------------------------------------------------------
# document.xml / footnotes.xml

def paragraph_style(paragraph: ET.Element) -> str | None:
    node = paragraph.find(f'{w("pPr")}/{w("pStyle")}')
    return node.get(w('val')) if node is not None else None


def final_sectpr(body: ET.Element) -> ET.Element:
    sectpr = body.find(w('sectPr'))
    if sectpr is None:
        sectpr = ET.SubElement(body, w('sectPr'))
    return sectpr


def set_note_restart(footnote_pr: ET.Element, restart: bool) -> None:
    num_restart = child(footnote_pr, 'numRestart', FOOTNOTE_PR_ORDER)
    num_restart.set(w('val'), 'eachSect' if restart else 'continuous')


def insert_chapter_sections(body: ET.Element, restart: bool) -> int:
    """End a section before every Heading 1 that follows other content.

    The new section breaks copy the page setup of the final section but no
    footnote properties: they inherit the document-wide footnotePr written to
    settings.xml.  Word restarts per section either way; LibreOffice shows
    "0" for every note when each section carries its own restart setting.
    """
    template = final_sectpr(body)
    set_note_restart(child(template, 'footnotePr', SECTPR_ORDER), restart)
    blocks = list(body)
    count = 0
    for index, block in enumerate(blocks):
        if block.tag != w('p') or paragraph_style(block) != 'Heading1' or index == 0:
            continue
        previous = blocks[index - 1]
        if previous.tag == w('p') and previous.find(f'{w("pPr")}/{w("sectPr")}') is not None:
            continue
        sectpr = ET.fromstring(ET.tostring(template))
        for inherited in sectpr.findall(w('footnotePr')):
            sectpr.remove(inherited)
        child(sectpr, 'type', SECTPR_ORDER).set(w('val'), 'nextPage')
        if previous.tag == w('p'):
            holder = previous
        else:  # tables or other blocks cannot carry a section break
            holder = ET.Element(w('p'))
            body.insert(list(body).index(block), holder)
        ppr = holder.find(w('pPr'))
        if ppr is None:
            ppr = ET.Element(w('pPr'))
            holder.insert(0, ppr)
        ppr.append(sectpr)  # sectPr follows every other paragraph property except pPrChange
        change = ppr.find(w('pPrChange'))
        if change is not None:
            ppr.remove(change)
            ppr.append(change)
        count += 1
    return count


def note_plan(doc_root: ET.Element, labels: list[tuple[int, str]] | None, restart_mode: str):
    """Decide numbering restart and which notes need custom marks.

    Returns (restart, {footnote_id: custom_label}, references).
    """
    body = doc_root.find(w('body'))
    refs: list[tuple[int, ET.Element]] = []
    chapter = 0
    for block in body.iter():
        if block.tag == w('p') and paragraph_style(block) == 'Heading1':
            chapter += 1
        elif block.tag == w('footnoteReference'):
            refs.append((chapter, block))
    if labels is None:
        return restart_mode != 'never', {}, refs
    if len(labels) != len(refs):
        raise RuntimeError(f'source has {len(labels)} note references but the DOCX has {len(refs)}')

    def custom_marks(restart: bool) -> dict[str, str]:
        marks: dict[str, str] = {}
        counter, current = 1, None
        for (chapter_index, element), (_source_chapter, label) in zip(refs, labels):
            if restart and chapter_index != current:
                counter, current = 1, chapter_index
            if label == str(counter):
                counter += 1
            else:
                marks[element.get(w('id'))] = label
        return marks

    options = {True: custom_marks(True), False: custom_marks(False)}
    if restart_mode == 'chapter':
        restart = True
    elif restart_mode == 'never':
        restart = False
    else:
        restart = len(options[True]) <= len(options[False])
    return restart, options[restart], refs


def mark_references(refs, marks: dict[str, str], doc_root: ET.Element) -> None:
    parents = {c: p for p in doc_root.iter() for c in p}
    for _chapter, ref in refs:
        label = marks.get(ref.get(w('id')))
        if label is None:
            continue
        ref.set(w('customMarkFollows'), '1')
        run = parents[ref]
        text = ET.Element(w('t'))
        text.text = label
        run.insert(list(run).index(ref) + 1, text)


def mark_footnotes(data: bytes, marks: dict[str, str]) -> bytes:
    root = load_part(data)
    for note in root.findall(w('footnote')):
        label = marks.get(note.get(w('id')))
        if label is None:
            continue
        for run in note.iter(w('r')):
            ref = run.find(w('footnoteRef'))
            if ref is not None:
                text = ET.Element(w('t'))
                text.text = label
                run.insert(list(run).index(ref), text)
                run.remove(ref)
                break
    return dump_part(data, root)


def promote_first_paragraphs(body: ET.Element) -> None:
    """Body Text right after an author line, label or epigraph is unindented."""
    previous = None
    for block in body:
        if block.tag != w('p'):
            previous = None
            continue
        style = paragraph_style(block)
        if style == 'BodyText' and previous in OPENING_STYLES:
            block.find(f'{w("pPr")}/{w("pStyle")}').set(w('val'), 'FirstParagraph')
            style = 'FirstParagraph'
        if style is not None or block.find(f'.//{w("t")}') is not None:
            previous = style


def format_settings(data: bytes, restart: bool, special_ids: list[str]) -> bytes:
    """Write the document-wide footnote numbering rule in schema order."""
    root = load_part(data)
    footnote_pr = child(root, 'footnotePr', SETTINGS_ORDER)
    set_note_restart(footnote_pr, restart)
    present = {f.get(w('id')) for f in footnote_pr.findall(w('footnote'))}
    for note_id in special_ids:
        if note_id not in present:
            ET.SubElement(footnote_pr, w('footnote')).set(w('id'), note_id)
    return dump_part(data, root)


def special_footnote_ids(data: bytes | None) -> list[str]:
    if data is None:
        return []
    root = ET.fromstring(data)
    return [n.get(w('id')) for n in root.findall(w('footnote'))
            if n.get(w('type')) in {'separator', 'continuationSeparator', 'continuationNotice'}]


def format_document(data: bytes, labels, restart_mode: str):
    root = load_part(data)
    body = root.find(w('body'))
    restart, marks, refs = note_plan(root, labels, restart_mode)
    promote_first_paragraphs(body)
    sections = insert_chapter_sections(body, restart)
    mark_references(refs, marks, root)
    return dump_part(data, root), marks, restart, sections, len(refs)


# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--docx', required=True, type=Path, help='DOCX written by Pandoc')
    parser.add_argument('--out', required=True, type=Path, help='finished DOCX (must differ from --docx)')
    parser.add_argument('--source', '--html', dest='source', type=Path,
                        help='reconstructed book HTML; used to reproduce the printed note marks')
    parser.add_argument('--note-restart', choices=['auto', 'chapter', 'never'], default='auto',
                        help='restart footnote numbers at each chapter (auto: follow the source)')
    parser.add_argument('--indent', default=BODY_INDENT, help='Body Text first-line indent in twips')
    args = parser.parse_args()

    source = args.docx.resolve()
    output = args.out.resolve()
    if source == output:
        sys.exit('--out must not overwrite --docx; keep the Pandoc DOCX so the pass can be rerun')
    labels = source_labels(args.source) if args.source else None

    with zipfile.ZipFile(source) as zin:
        names = zin.namelist()
        if 'word/document.xml' not in names or 'word/styles.xml' not in names:
            sys.exit(f'{source} is not a Pandoc DOCX')
        document, marks, restart, sections, notes = format_document(
            zin.read('word/document.xml'), labels, args.note_restart)
        footnotes = zin.read('word/footnotes.xml') if 'word/footnotes.xml' in names else None
        temporary = output.with_name(output.name + '.tmp')
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == 'word/styles.xml':
                    data = format_styles(data, args.indent)
                elif item.filename == 'word/document.xml':
                    data = document
                elif item.filename == 'word/settings.xml':
                    data = format_settings(data, restart, special_footnote_ids(footnotes))
                elif item.filename == 'word/footnotes.xml' and marks:
                    data = mark_footnotes(data, marks)
                zout.writestr(item, data)
    temporary.replace(output)
    print(f'{output}: {notes} footnotes, numbering '
          f'{"restarts each chapter" if restart else "continuous"}, '
          f'{len(marks)} custom mark(s), {sections} chapter section break(s)')


if __name__ == '__main__':
    main()
