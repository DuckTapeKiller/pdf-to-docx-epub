#!/usr/bin/env python3
"""Reconstruct semantic book HTML from a text-based PDF.

Reads Poppler's positioned XML (pdftohtml -xml) and writes the HTML that the
rest of the skill expects:

* chapters as <h1> (with the "Chapter 1" label kept inside the heading),
  sections as <h2>/<h3>;
* paragraphs joined across lines and pages, line-end hyphenation repaired
  using the book's own vocabulary;
* running heads, folios and printer slugs removed (and listed in the report);
* footnotes read from the foot of each page, including notes that continue on
  the next page, linked as <a epub:type="noteref"> / <aside epub:type="footnote">
  so Pandoc makes real Word footnotes and EPUB pop-up notes;
* block quotations, italics, bold, small caps, superscripts, figures;
* per-font character maps for fonts whose text extracts as garbage.

The result is a first draft tuned by a JSON config, not an oracle: read the
report it prints, compare the HTML with the PDF, adjust the config, re-run.
"""
from __future__ import annotations

import argparse
import collections
import copy
import difflib
import html
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
import xml.etree.ElementTree as ET

LIGATURES = {'ﬀ': 'ff', 'ﬁ': 'fi', 'ﬂ': 'fl', 'ﬃ': 'ffi', 'ﬄ': 'ffl',
             'ﬅ': 'st', 'ﬆ': 'st'}
INVISIBLE = dict.fromkeys(map(ord, '​‌‍⁠﻿'), None)
NOTE_SYMBOLS = '*†‡§¶‖#'
MARKER = re.compile(rf'^\s*(\d{{1,4}}|[{re.escape(NOTE_SYMBOLS)}]{{1,3}})\s*$')
NOTE_START = re.compile(rf'^\s*(\d{{1,4}}|[{re.escape(NOTE_SYMBOLS)}]{{1,3}})(?:[.)]\s*|\s+)(?=\S)')
GARBLE = re.compile(r'[#$%&()*+,/;<=>?@\[\\\]^_{|}~]|\d[a-zà-ÿ]|[a-zà-ÿ]\d')
CAPTION = re.compile(r'^\s*(fig(ure|ura)?|ill(ustration|ustración)?|il\.|imagen|image|mapa|map|lámina|plate|tabla|table|cuadro|gráfico)\b', re.I)
LEADERS = re.compile(r'(\.\s*){6,}|…{3,}')
BIBLIOGRAPHY = re.compile(r'^\W*(?:[\dIVXLC]+[.\s]+)?(?:select(?:ed)?\s+|general\s+)?(?:bibliogra(?:f|ph)\w*(?:\s+\w+)?'
                          r'|works\s+cited|obras\s+citadas|references|referencias(?:\s+bibliogr\w+)?'
                          r'|fuentes(?:\s+y\s+bibliograf\w+)?|sources(?:\s+and\s+bibliography)?'
                          r'|literatur(?:verzeichnis)?|further\s+reading)\W*$', re.I)
# A standalone short line naming a division is a chapter-level heading even
# when it is set in body-size type (letter-spaced small caps, for instance).
DIVISION = re.compile(r'^\s*(libro|book|parte?|part|cap[ií]tulo|chapter|chapitre|kapitel|capitolo|'
                      r'ep[ií]tome|ep[ií]logo|epilogue|pr[oó]logo|prologue|introducci[oó]n|introduction|'
                      r'conclusi[oó]n|conclusion|ap[eé]ndice|appendix)\b[\s\dIVXLCivxlc.:–-]*$', re.I)
MARGINAL = re.compile(r'^\s*(\d{1,4}[a-z]?|[IVXLC]{1,6})(\s?/\s?(\d{1,4}[a-z]?|[IVXLC]{1,6}))?\s*$')
LETTERSPACED = re.compile(r'^\s*(?:[A-ZÀ-ÖØ-Þ] ){2,}[A-ZÀ-ÖØ-Þ]\s*$')
SENTENCE_START = re.compile(r'[.;:!?»”’)\]]\s+(?=[«“‘(¿¡]?[A-ZÀ-ÖØ-Þ])')
HANGING = re.compile(r'abbreviations|abbrevations|abreviaturas|siglas|glossary|glosario', re.I)
# Dashes are not bullets: they open dialogue and repeated-author bibliography entries.
LIST_START = re.compile(r'^\s*(?P<marker>(?:\d{1,3}|[a-z]|[ivxlc]{1,6})[.)]|[•◦▪■●○‣⁃∙*])\s+(?=\S)')
NAME_LINE = re.compile(r"^(?:[A-ZÀ-Ý][\w'’.-]*|[A-Z]\.|and|&|y|e|de|del|da|di|du|van|von|der|den|la|le|of|Jr\.?|Sr\.?)"
                       r"(?:,?\s+(?:[A-ZÀ-Ý][\w'’.-]*|[A-Z]\.|and|&|y|e|de|del|da|di|du|van|von|der|den|la|le|of|Jr\.?|Sr\.?)){1,7}$")
INDEX = re.compile(r'^\s*(índice|indice|index)\s+(onomástico|analítico|temático|conceptual|de nombres|general\b(?! de))|^\s*index\b|^\s*(register|indice dei nomi)\b', re.I)

FIGURE_WORD = {'es': 'Figura', 'en': 'Figure', 'fr': 'Figure', 'de': 'Abbildung', 'it': 'Figura',
               'pt': 'Figura', 'la': 'Figura', 'ca': 'Figura', 'nl': 'Afbeelding'}

DEFAULT_CONFIG = {
    'lang': None,                # BCP 47 tag; inferred from the text when null
    'title': None,
    'skip_pages': [],            # PDF page numbers to ignore completely
    'toc_pages': None,           # printed contents pages (auto-detected when null)
    'front_matter_until': None,  # last PDF page of front matter (default: before first h1)
    'drop_fonts': [],            # font family substrings whose text is never content
    'font_maps': {},             # {"FAMILY": {"garbled char": "real char", ...}}
    'smallcaps_fonts': [],       # font family substrings set in small caps
    'heading_fonts': {},         # {"FAMILY@SIZE" or "FAMILY": level}
    'heading_patterns': {},      # {"regex matched against the line": level}, e.g. {"^Libro [IVX]+$": 1}
    'chapter_author_after_h1': False,
    'body_size': None, 'note_size': None, 'quote_sizes': None,
    'header_band': None, 'footer_band': None,  # y limits in pdftohtml units
    'superscript_ratio': 0.75,   # max size ratio of a note marker to its line
    'keep_hyphen_words': [],     # compounds whose line-end hyphen is real
    'hanging_sections': [],      # extra heading texts whose entries use hanging indents
    'min_figure_area': 0.015,    # smallest raster image kept as a figure (share of the page)
    'dictionary': None,          # word list for hyphenation evidence (default for English: /usr/share/dict/words)
}


# --------------------------------------------------------------------------
# Data model

@dataclass
class Run:
    text: str
    font: str
    size: int
    family: str
    italic: bool
    bold: bool
    left: float
    top: float
    width: float
    height: float
    sup: bool = False
    smallcaps: bool = False
    dropcap: bool = False
    marginal: bool = False   # section number printed in the outer margin
    rtl: bool = False        # Hebrew, Arabic: wrapped in dir="rtl"

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height


@dataclass
class Line:
    page: int
    runs: list[Run]
    kind: str = 'body'
    column: int = 0
    beside_dropcap: bool = False  # indented only to make room for a drop cap
    outline: tuple | None = None  # (level, entry number) when a PDF bookmark points to this heading

    @property
    def top(self) -> float:
        return min(r.top for r in self.main_runs())

    @property
    def bottom(self) -> float:
        return max(r.bottom for r in self.main_runs())

    @property
    def left(self) -> float:
        texts = [r for r in self.runs if r.text.strip() and not r.marginal]
        return min(r.left for r in texts) if texts else self.runs[0].left

    @property
    def right(self) -> float:
        texts = [r for r in self.runs if not r.marginal]
        return max(r.right for r in texts or self.runs)

    @property
    def size(self) -> int:
        # Small capitals set in a smaller size count as the line's type
        # ("Polvos de" + "PINI PINICHE" in 11 pt beside 16 pt).
        main = [r for r in self.runs if not r.sup and not r.marginal and not r.dropcap and r.text.strip()]
        top = max((r.size for r in main), default=0)
        weights = collections.Counter()
        for r in main:
            weights[top if r.smallcaps and r.size < top else r.size] += len(r.text.strip())
        return weights.most_common(1)[0][0] if weights else self.runs[0].size

    def main_runs(self) -> list[Run]:
        main = [r for r in self.runs if not r.sup and not r.dropcap and not r.marginal and r.text.strip()]
        return main or self.runs

    @property
    def text(self) -> str:
        return ''.join(r.text for r in self.runs if not r.marginal)


@dataclass
class Block:
    kind: str                     # p, quote, heading, centered, front, figure, note-orphan
    lines: list[Line] = field(default_factory=list)
    level: int = 0
    css: str = ''                 # class for <div>
    style: str = ''               # Word style for data-custom-style
    page: int = 0
    label: str = ''               # chapter label for headings
    image: str = ''
    caption: list[Line] = field(default_factory=list)
    outline: int | None = None    # bookmark entry a heading was anchored to
    heuristic_level: int = 0      # level found from the type, before bookmarks


@dataclass
class Note:
    label: str
    page: int
    lines: list[list[Line]] = field(default_factory=list)  # paragraphs
    chapter: int = 0
    used: bool = False
    note_id: str = ''


class Report:
    def __init__(self) -> None:
        self.data: dict = collections.defaultdict(list)

    def add(self, key: str, value) -> None:
        self.data[key].append(value)

    def set(self, key: str, value) -> None:
        self.data[key] = value


# --------------------------------------------------------------------------
# Parsing pdftohtml -xml

def repair_text_layer(pdf: Path, workdir: Path, report: Report) -> Path:
    """Fix fonts whose ToUnicode map contradicts their glyph names (repair_text_layer.py)."""
    script = Path(__file__).resolve().parent / 'repair_text_layer.py'
    repaired, details = workdir / 'repaired.pdf', workdir / 'repairs.json'
    result = subprocess.run([sys.executable, str(script), '--pdf', str(pdf), '--out', str(repaired),
                             '--report', str(details)], capture_output=True, text=True)
    if result.returncode != 0 or not repaired.exists():
        report.set('text_layer_repair', {'status': 'not possible', 'reason': (result.stderr or result.stdout).strip()})
        return pdf
    data = json.loads(details.read_text(encoding='utf-8'))
    report.set('text_layer_repair', data)
    return repaired


RTL = re.compile(r'[\u0590-\u05FF\u0600-\u06FF\u0700-\u074F\uFB1D-\uFDFF\uFE70-\uFEFF]')
LTR_LETTER = re.compile(r'[A-Za-zÀ-ɏͰ-Ͽἀ-῿]')
GREEK = re.compile(r'[\u0370-\u03FF\u1F00-\u1FFF]')


def logical_order(text: str) -> str:
    """Reverse a right-to-left word that pdftohtml wrote in visual order,
    keeping combining marks (niqqud, harakat) after their letters."""
    clusters: list[str] = []
    for char in text:
        if clusters and unicodedata.combining(char):
            clusters[-1] += char
        else:
            clusters.append(char)
    return ''.join(reversed(clusters))


def split_smallcaps(text: str) -> list[tuple[str, bool]]:
    """Split text at small-capital glyphs marked U+F7xx by the repair step."""
    if not any(0xF720 <= ord(c) <= 0xF7FF for c in text):
        return [(text, False)] if text else []
    pieces: list[tuple[list[str], bool]] = []   # characters gathered in lists: linear time
    for char in text:
        smallcap = 0xF720 <= ord(char) <= 0xF7FF
        letter = chr(ord(char) - 0xF700) if smallcap else char
        state = smallcap if not char.isspace() or not pieces else pieces[-1][1]
        if pieces and pieces[-1][1] == state:
            pieces[-1][0].append(letter)
        else:
            pieces.append(([letter], state))
    return [(''.join(chars), state) for chars, state in pieces]


def run_pdftohtml(pdf: Path, workdir: Path, media: Path | None, first: int | None, last: int | None) -> Path:
    if shutil.which('pdftohtml') is None:
        sys.exit('pdftohtml (Poppler) is required: brew install poppler')
    out = workdir / 'layout'
    command = ['pdftohtml', '-xml', '-q', '-nodrm', '-hidden', '-fontfullname', '-zoom', '1.5']
    if media is None:
        command.append('-i')
    if first:
        command += ['-f', str(first)]
    if last:
        command += ['-l', str(last)]
    subprocess.run(command + [str(pdf), str(out)], check=True)
    xml_path = workdir / 'layout.xml'
    if media is not None:
        media.mkdir(parents=True, exist_ok=True)
        for image in workdir.glob('layout-*'):
            shutil.move(str(image), media / image.name)
    return xml_path


def clean_text(text: str) -> str:
    for ligature, letters in LIGATURES.items():
        text = text.replace(ligature, letters)
    text = text.translate(INVISIBLE).replace(' ', ' ')
    return unicodedata.normalize('NFC', text)


def parse_runs(element: ET.Element, base: dict, italic=False, bold=False) -> list[tuple[str, bool, bool]]:
    pieces = []
    tag = element.tag
    italic = italic or tag == 'i'
    bold = bold or tag == 'b'
    if element.text:
        pieces.append((element.text, italic, bold))
    for child in element:
        pieces.extend(parse_runs(child, base, italic, bold))
        if child.tail:
            pieces.append((child.tail, italic, bold))
    return pieces


def load_layout(xml_path: Path, config: dict, report: Report):
    raw = xml_path.read_bytes().decode('utf-8', 'replace')
    raw = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', raw)
    raw = re.sub(r'<!DOCTYPE[^>]*>', '', raw)
    root = ET.fromstring(raw)
    fonts: dict[str, tuple[int, str]] = {}
    pages: dict[int, dict] = {}
    font_maps = config['font_maps']
    for page in root.iter('page'):
        number = int(page.get('number'))
        info = {'width': float(page.get('width')), 'height': float(page.get('height')), 'runs': [], 'images': []}
        for node in page:
            if node.tag == 'fontspec':
                fonts[node.get('id')] = (int(round(float(node.get('size')))), node.get('family', ''))
            elif node.tag == 'image':
                # flipped placements give negative sizes: normalise the box
                top, left = float(node.get('top')), float(node.get('left'))
                width, height = float(node.get('width')), float(node.get('height'))
                if width < 0:
                    left, width = left + width, -width
                if height < 0:
                    top, height = top + height, -height
                info['images'].append({'top': top, 'left': left, 'width': width, 'height': height,
                                       'src': node.get('src')})
            elif node.tag == 'text':
                size, family = fonts.get(node.get('font'), (0, ''))
                if any(d in family for d in config['drop_fonts']):
                    continue
                pieces = parse_runs(node, {})
                text = ''.join(p[0] for p in pieces)
                if not text:
                    continue
                mapping = next((m for key, m in font_maps.items() if key in family), None)
                left, top = float(node.get('left')), float(node.get('top'))
                width, height = float(node.get('width')), float(node.get('height'))
                total = max(len(text), 1)
                offset = 0
                font_smallcaps = any(s in family for s in config['smallcaps_fonts']) or bool(
                    re.search(r'(SC\d*|SmallCaps|Caps)(\W|$)', family.split('+')[-1]))
                if RTL.search(text) and not LTR_LETTER.search(text):
                    pieces = [(logical_order(p), i, b) for p, i, b in reversed(pieces)]
                for piece, italic, bold in pieces:
                    for part, marked in split_smallcaps(piece):
                        part_text = ''.join(mapping.get(c, c) for c in part) if mapping else part
                        part_text = clean_text(part_text)
                        run = Run(part_text, node.get('font'), size, family, italic, bold,
                                  left + width * offset / total, top, width * len(part) / total, height)
                        run.smallcaps = font_smallcaps or marked
                        offset += len(part)
                        info['runs'].append(run)
        info['runs'] = drop_overprints(info['runs'], report, number)
        for run in info['runs']:
            # letter-spaced capitals ("P I N I C H E") are one word
            if LETTERSPACED.match(run.text):
                joined = re.sub(r'(?<=[^\W\d_]) (?=[^\W\d_])', '', run.text)
                report.add('letterspaced_words', f'p{number}:{joined.strip()}')
                run.text = joined
        # Rotated text (a spine, a vertical table head) has no width in the
        # layout; left among the lines it would chain them together.
        rotated = [r for r in info['runs'] if r.width <= 1 and r.text.strip()]
        if rotated:
            report.add('rotated_text', {'page': number, 'text': ' '.join(r.text.strip() for r in rotated)[:200]})
            info['runs'] = [r for r in info['runs'] if not (r.width <= 1 and r.text.strip())]
        pages[number] = info

    # A face that never produces a capital is a small-caps font (its glyphs sit
    # on lowercase code points); capitals beside it come from the text face.
    cases = collections.defaultdict(lambda: [0, 0])
    for info in pages.values():
        for run in info['runs']:
            cases[run.family][0] += sum(c.islower() for c in run.text)
            cases[run.family][1] += sum(c.isupper() for c in run.text)
    detected = [f for f, (lower, upper) in cases.items() if lower >= 40 and upper <= lower * 0.01]
    if config.get('detect_smallcaps', True) and detected:
        report.set('smallcaps_fonts_detected', detected)
        for info in pages.values():
            for run in info['runs']:
                if run.family in detected:
                    run.smallcaps = True
    return fonts, pages, read_outline(root)


def drop_overprints(runs: list[Run], report: Report, number: int) -> list[Run]:
    """Text printed several times a hair apart (a fake bold or a shadow) once."""
    kept, seen = [], collections.defaultdict(list)
    for run in runs:
        key = (run.text.strip(), run.size)
        tolerance = max(2.0, run.size * 0.15)
        if key[0] and any(abs(run.top - t) <= tolerance and abs(run.left - l) <= tolerance for t, l in seen[key]):
            report.add('overprinted_text', f'p{number}:{key[0][:30]}')
            continue
        seen[key].append((run.top, run.left))
        kept.append(run)
    return kept


def read_outline(root) -> list[dict]:
    """The PDF's bookmarks, from pdftohtml -xml: [{'depth', 'page', 'title'}]."""
    entries: list[dict] = []

    def walk(node, depth: int) -> None:
        for child in node:
            if child.tag == 'item':
                title = clean_text(' '.join(''.join(child.itertext()).split()))
                page = child.get('page', '')
                if title and page.isdigit():
                    entries.append({'depth': depth, 'page': int(page), 'title': title})
            elif child.tag == 'outline':
                walk(child, depth + 1)

    top = root.find('outline')
    if top is not None:
        walk(top, 0)
    return entries


def pdf_title(pdf: Path) -> str:
    if shutil.which('pdfinfo') is None:
        return ''
    result = subprocess.run(['pdfinfo', str(pdf)], capture_output=True, text=True, errors='replace')
    match = re.search(r'^Title:\s*(.+)$', result.stdout, re.M)
    return match.group(1).strip() if match else ''


def fold(text: str) -> str:
    """Comparison key: no case, accents or punctuation."""
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return ' '.join(re.sub(r'[\W_]+', ' ', text.casefold()).split())


def match_title(title: str, content: list[Line]) -> list[Line] | None:
    """The line or lines (up to four) that print a bookmark's title."""
    key = fold(title)
    if not key:
        return None
    best, best_score = None, 0.0
    for start in range(len(content)):
        joined = ''
        for end in range(start, min(start + 4, len(content))):
            joined = (joined + ' ' + fold(content[end].text)).strip()
            if len(joined) > len(key) * 1.5 + 10:
                break
            if joined == key:
                return content[start:end + 1]
            score = difflib.SequenceMatcher(None, joined, key).ratio()
            if score > best_score:
                best, best_score = content[start:end + 1], score
    return best if best_score >= 0.86 else None


def anchor_outline(outline: list[dict], all_lines, config: dict, report: Report) -> bool:
    """Mark the printed headings that the PDF's bookmarks point to.

    The bookmarks give the publisher's own structure and levels; a single
    root bookmark holding all the others is the book itself.  Returns True
    when the bookmarks describe the book well enough to lead the headings.
    """
    entries = [e for e in outline if e['page'] in all_lines]
    roots = [e for e in entries if e['depth'] == 0]
    if len(roots) == 1 and len(entries) > 1:
        entries = [e for e in entries if e is not roots[0]]
    if not entries:
        return False
    skip = set(config['skip_pages'])
    shallowest = min(e['depth'] for e in entries)
    matched, missing = 0, []
    for number, entry in enumerate(entries):
        found = None
        for page in (entry['page'], entry['page'] + 1, entry['page'] - 1):
            if page in skip or page not in all_lines:
                continue
            content = [l for l in all_lines[page] if l.kind not in ('margin', 'note') and l.text.strip()
                       and l.outline is None]
            found = match_title(entry['title'], content)
            if found:
                break
        if not found:
            missing.append({'page': entry['page'], 'title': entry['title']})
            continue
        matched += 1
        level = min(entry['depth'] - shallowest + 1, 4)
        for line in found:
            line.outline = (level, number)
    active = matched >= 3 and matched >= len(entries) * 0.5
    report.set('outline', {'entries': len(entries), 'matched': matched, 'leads_headings': active,
                           'not_found': missing})
    if not active:
        for lines in all_lines.values():
            for line in lines:
                line.outline = None
    return active


# --------------------------------------------------------------------------
# Geometry

def group_lines(page_no: int, runs: list[Run]) -> list[Line]:
    """Group runs into lines; drop caps join the first line they stand beside."""
    heights = sorted(r.height for r in runs if r.text.strip())
    usual = heights[len(heights) // 2] if heights else 0
    # one letter, perhaps after an opening quotation mark ("‘I asked ...")
    dropcaps = [r for r in runs if usual and r.height > usual * 2.2
                and re.fullmatch(r'[‘“"\'«¿¡(]?[^\W\d_]', r.text.strip())]
    lines = _group(page_no, [r for r in runs if r not in dropcaps])
    for cap in dropcaps:
        cap.dropcap = True
        beside = [l for l in lines if cap.top - 4 <= l.top < cap.bottom - 4 and l.left > cap.left]
        if not beside:
            lines.append(Line(page_no, [cap]))
            continue
        beside[0].runs.insert(0, cap)
        for line in beside[1:]:
            line.beside_dropcap = True
        beside[0].beside_dropcap = False
    lines.sort(key=lambda l: (l.top, l.left))
    return lines


def _group(page_no: int, runs: list[Run]) -> list[Line]:
    lines: list[Line] = []
    for run in sorted(runs, key=lambda r: (r.top, r.left)):
        best = None
        for line in lines[-6:]:
            main = line.main_runs()
            # the height of most of the line's letters (not of a lone tall run)
            weights = collections.Counter()
            for r in main:
                weights[r.height] += max(1, len(r.text.strip()))
            typical = weights.most_common(1)[0][0]
            # the line's extent is that of its ordinary runs: one tall run must
            # not stretch it over the next line and chain them together
            band = [r for r in main if r.height <= typical * 1.5] or main
            top = min(r.top for r in band)
            bottom = max(r.bottom for r in band)
            # display type beside running text (a wraparound cover) is never one
            # line; a note marker beside a large title is
            if run.height > 2.2 * max(1.0, typical) or (
                    typical > 2.2 * max(1.0, run.height) and len(run.text.strip()) > 4):
                continue
            overlap = min(bottom, run.bottom) - max(top, run.top)
            if overlap >= 0.5 * min(run.height, bottom - top):
                best = line
        if best is None:
            lines.append(Line(page_no, [run]))
        else:
            best.runs.append(run)
    for line in lines:
        line.runs.sort(key=lambda r: r.left)
        # a stretch of right-to-left words is read from right to left
        index = 0
        while index < len(line.runs):
            if RTL.search(line.runs[index].text):
                end = index
                while end + 1 < len(line.runs) and (RTL.search(line.runs[end + 1].text)
                                                    or not line.runs[end + 1].text.strip()):
                    end += 1
                stretch = line.runs[index:end + 1]
                for run in stretch:
                    run.rtl = True
                line.runs[index:end + 1] = list(reversed(stretch))
                index = end + 1
            else:
                index += 1
    lines.sort(key=lambda l: (l.top, l.left))
    return lines


def page_lines(page_no: int, runs: list[Run], width: float) -> list[Line]:
    """Group a page into lines, reading two-column pages (indexes) column by column.

    A gutter is a vertical strip near the middle that at most two runs cross
    (a heading set across both columns) with real text on both sides.  Columns
    must be split before lines are formed, or a heading in one column fuses
    with the line beside it.
    """
    words = [r for r in runs if r.text.strip()]
    gutter = None
    for x in range(int(width * 0.35), int(width * 0.65), 2):
        crossing = [r for r in words if r.left < x < r.right]
        if len(crossing) > 2:
            continue
        left = [r for r in words if r.right <= x]
        right = [r for r in words if r.left >= x]
        # real text on both sides: index pages have many short runs
        # (a capital, a small-caps headword, page numbers)
        if len(left) >= 6 and len(right) >= 6 \
                and sum(len(r.text.strip()) for r in right) >= 60 \
                and sum(len(r.text.strip()) for r in left) >= 60 \
                and statistics.mean(len(r.text.strip()) for r in right) >= 3 \
                and statistics.mean(len(r.text.strip()) for r in left) >= 3:
            score = (len(crossing), abs(x - width / 2))
            if gutter is None or score < gutter[0]:
                gutter = (score, x)
    if gutter is None:
        return group_lines(page_no, runs)
    x = gutter[1]
    across = [r for r in runs if r.left < x < r.right]

    def beside(run: Run) -> bool:  # on the same line as a run that crosses the gutter
        return any(min(run.bottom, s.bottom) - max(run.top, s.top) >= 0.5 * min(run.height, s.height)
                   for s in across)

    across += [r for r in runs if r not in across and beside(r)]
    spanning = group_lines(page_no, across)
    left = group_lines(page_no, [r for r in runs if r.right <= x and r not in across])
    right = group_lines(page_no, [r for r in runs if r.left >= x and r not in across])
    for line in right:
        line.column = 1
    columns_top = min((l.top for l in left + right), default=0)
    above = [l for l in spanning if l.top < columns_top]
    below = [l for l in spanning if l.top >= columns_top]
    return above + left + right + below


def mode(values, default=0):
    values = [v for v in values if v is not None]
    if not values:
        return default
    return collections.Counter(values).most_common(1)[0][0]


def face_name(family: str) -> str:
    """A font's name without its subset tag: merged PDFs embed the same face
    once per chapter ("EPIKLH+MinionPro-Regular", "QWERTY+MinionPro-Regular")."""
    return family.split('+', 1)[-1]


def right_edge(geometry: dict, page: int) -> float:
    """Right edge of the text block on this page (recto and verso may differ)."""
    return geometry.get('rights', {}).get(page % 2, geometry['right'])


def left_edge(geometry: dict, page: int) -> float:
    return geometry['margin'].get(page % 2, geometry['margin'].get(0, 0))


def mark_marginal(line: Line, geometry: dict) -> list[str]:
    """Section or line numbers printed in the margin beside the text block.

    The runs lie wholly outside the text block and are set apart from the
    text by a visible gap (a note marker touches its word).  Pieces in
    different fonts are read together: bold "3" + "/4" is "3/4".
    """
    runs = [r for r in line.runs if r.text.strip()]
    if not runs:
        return []
    margin, right = left_edge(geometry, line.page), right_edge(geometry, line.page)
    found = []
    tail = []
    for r in reversed(runs):
        if r.left > right + 1.5:
            tail.insert(0, r)
        else:
            break
    head = []
    for r in runs:
        if r.right < margin - 1.5:
            head.append(r)
        else:
            break
    note_line = geometry['note_size'] is not None and line.size <= geometry['note_size']
    for group, neighbour in ((tail, runs[-len(tail) - 1] if len(tail) < len(runs) else None),
                             (head, runs[len(head)] if len(head) < len(runs) else None)):
        if not group or group is head and tail and len(tail) + len(head) >= len(runs):
            continue
        text = ''.join(r.text for r in group).strip()
        if not MARGINAL.match(text):
            continue
        if neighbour is not None:
            gap = group[0].left - neighbour.right if group is tail else neighbour.left - group[-1].right
            raised = any(r.sup for r in group)
            if gap < (max(3.0, neighbour.size * 0.3) if raised else max(1.5, neighbour.size * 0.12)):
                continue    # touching: a note marker or a hanging note number
            if group is head and note_line and abs(neighbour.left - margin) <= 3:
                continue    # "12 Text of the note" set with the number outdented
        group[0].text = text
        for r in group:
            r.marginal, r.sup = True, False
        for r in group[1:]:
            r.text = ''
        found.append(text)
    return found


def sized_small_caps(all_lines: dict[int, list[Line]], report: Report) -> None:
    """Caps and small caps set with two sizes of capitals: a capital starting
    a word and smaller capitals touching it ("A" + "BANTE") read "Abante" in
    small caps, as a small-caps face would have encoded it."""
    for lines in all_lines.values():
        for line in lines:
            runs = line.runs
            for index, head in enumerate(runs[:-1]):
                if not re.search(r'(^|[^\w])[^\W\d_]$', head.text) or not head.text[-1].isupper():
                    continue
                small = runs[index + 1]
                if small.size > head.size * 0.88 or small.left - head.right > max(1.0, head.size * 0.12):
                    continue
                follow = index + 1
                changed = []
                while follow < len(runs) and (runs[follow].size == small.size or not runs[follow].text.strip()):
                    piece = runs[follow]
                    if not piece.text.strip():
                        follow += 1
                        continue
                    letters = [c for c in piece.text if c.isalpha()]
                    if not letters or not all(c.isupper() for c in letters):
                        break
                    if changed and piece.left - runs[follow - 1].right > piece.size * 3:
                        break
                    changed.append(piece)
                    follow += 1
                if not changed:
                    continue
                head.smallcaps = True
                for piece in changed:
                    piece.text, piece.smallcaps = piece.text.lower(), True
                report.add('sized_small_caps', head.text[-1] + ''.join(p.text for p in changed)[:30])
            # capitals set smaller than the line, continuing small caps ("Polvos de PINI PINICHE")
            for index, run in enumerate(runs):
                letters = [c for c in run.text if c.isalpha()]
                if len(letters) < 2 or not all(c.isupper() for c in letters):
                    continue
                before = next((r for r in reversed(runs[:index]) if r.text.strip()), None)
                top = max((r.size for r in runs if r.text.strip() and not r.sup and not r.dropcap), default=0)
                if before is not None and (before.smallcaps or run.smallcaps) and run.size <= top * 0.8:
                    run.text, run.smallcaps = run.text.lower(), True
                    report.add('sized_small_caps', run.text.strip()[:30])


def analyse(pages: dict[int, dict], config: dict, report: Report) -> dict:
    sizes = collections.Counter()
    families = collections.Counter()
    for info in pages.values():
        for run in info['runs']:
            sizes[run.size] += len(run.text.strip())
    body_size = config['body_size'] or sizes.most_common(1)[0][0]
    for info in pages.values():
        for run in info['runs']:
            if run.size == body_size and not run.italic and not run.bold:
                families[face_name(run.family)] += len(run.text.strip())
    body_family = families.most_common(1)[0][0] if families else ''

    all_lines = {}
    for number, info in pages.items():
        all_lines[number] = page_lines(number, info['runs'], info['width'])
    sized_small_caps(all_lines, report)

    body_lines = [l for lines in all_lines.values() for l in lines if l.size == body_size and len(l.text.strip()) > 20]
    lefts = collections.defaultdict(list)
    for l in body_lines:
        lefts[l.page % 2].append(round(l.left))
    margin = {parity: mode(v) for parity, v in lefts.items()}

    def full_measure(values: list[int]) -> float:
        values = sorted(values)
        return statistics.median(values[len(values) // 2:]) if values else 0

    right = full_measure([round(l.right) for l in body_lines])
    # recto and verso pages can sit at different offsets
    rights_by_parity = collections.defaultdict(list)
    for l in body_lines:
        rights_by_parity[l.page % 2].append(round(l.right))
    rights = {parity: full_measure(v) for parity, v in rights_by_parity.items() if len(v) >= 20}
    pitches = []
    for lines in all_lines.values():
        tops = [l.top for l in lines if l.size == body_size]
        pitches += [round(b - a) for a, b in zip(tops, tops[1:]) if 0 < b - a < body_size * 2.5]
    pitch = mode(pitches, body_size * 1.2)
    indents = [round(l.left) - margin.get(l.page % 2, 0) for l in body_lines]
    indent = mode([i for i in indents if pitch * 0.4 < i < pitch * 4], pitch)

    firsts, lasts = [], []
    for lines in all_lines.values():
        body = [l for l in lines if l.size == body_size]
        if body:
            firsts.append(round(body[0].top))
    text_top = config['header_band'] or (mode(firsts, 0) - pitch * 0.25)

    note_candidates = collections.Counter()
    for lines in all_lines.values():
        body = [l for l in lines if l.size == body_size]
        if not body:
            continue
        last_body = body[-1].bottom
        for l in lines:
            if l.top > last_body and l.size < body_size:
                note_candidates[l.size] += len(l.text)
    note_size = config['note_size'] or (note_candidates.most_common(1)[0][0] if note_candidates else None)
    for lines in all_lines.values():
        content = [l for l in lines if l.size in (body_size, note_size)]
        if content:
            lasts.append(round(max(l.bottom for l in content)))
    text_bottom = config['footer_band'] or (sorted(lasts)[int(len(lasts) * 0.95) - 1] + pitch * 0.25 if lasts else 1e9)

    quote_sizes = config['quote_sizes']
    if quote_sizes is None:
        # smaller type above the last body line of a page: block quotations
        # (which may share the size of the notes)
        quote_sizes = []
        between = collections.Counter()
        for lines in all_lines.values():
            body = [l for l in lines if l.size == body_size]
            if not body:
                continue
            last_body = body[-1].bottom
            for l in lines:
                if l.bottom <= last_body and l.size < body_size and text_top < l.top < text_bottom:
                    between[l.size] += len(l.text)
        quote_sizes = [s for s, n in between.items() if n > 200]
    note_tops = []
    for lines in all_lines.values():
        tops = [l.top for l in lines if l.size == note_size]
        note_tops += [round(b - a) for a, b in zip(tops, tops[1:]) if 0 < b - a < (note_size or 1) * 2]
    note_pitch = mode(note_tops, pitch)
    geometry = {
        'body_size': body_size, 'body_family': body_family, 'note_size': note_size, 'note_pitch': note_pitch,
        'quote_sizes': quote_sizes, 'margin': margin, 'right': right, 'rights': rights, 'pitch': pitch,
        'indent': indent, 'text_top': text_top, 'text_bottom': text_bottom,
        'size_histogram': dict(sizes.most_common(12)),
    }
    report.set('geometry', geometry)
    return geometry, all_lines


def garbled_fonts(pages: dict[int, dict], report: Report) -> None:
    """Fonts whose extracted text looks like a substitution cipher.

    Subset fonts with a custom encoding and a wrong (or no) ToUnicode map
    extract as "C(p;+&l%" instead of "Capítulo"; re-saving a PDF with macOS
    Preview is a common cause.  The report lists every distinct run of such a
    font so its real text can be read from the page image and turned into a
    font_maps entry (see --font-map-pairs).
    """
    stats = collections.defaultdict(lambda: {'tokens': 0, 'garbled': 0, 'chars': set(), 'runs': {}})
    for number, info in pages.items():
        for run in info['runs']:
            entry = stats[run.family]
            for token in run.text.split():
                if not re.search(r'[A-Za-zÀ-ÿ]', token):
                    continue
                entry['tokens'] += 1
                if GARBLE.search(token.strip('.,;:()[]«»“”"\'¿?¡!')):
                    entry['garbled'] += 1
            entry['chars'].update(run.text)
            text = run.text.strip()
            if text and len(entry['runs']) < 120:
                entry['runs'].setdefault(text, number)
    for family, entry in sorted(stats.items(), key=lambda kv: -kv[1]['garbled']):
        if entry['tokens'] >= 3 and entry['garbled'] / entry['tokens'] > 0.25:
            report.add('suspect_fonts', {
                'font': family, 'tokens': entry['tokens'], 'garbled': entry['garbled'],
                'chars': ''.join(sorted(entry['chars'])),
                'samples': [{'page': p, 'text': t} for t, p in list(entry['runs'].items())[:6]],
                'runs': [{'page': p, 'text': t} for t, p in entry['runs'].items()],
            })


STOPWORDS = {
    'es': 'de la que el en y los del se las por un para con no una su al lo como más pero sus le ya o este sí porque esta entre cuando muy sin sobre',
    'en': 'the of and to in a is that for it as with was on be by this which are or from at his but not have had were they',
    'fr': 'le la les de des et en du un une est que qui dans pour pas sur au par ne se ce il elle sont avec plus',
    'de': 'der die und in den von zu das mit sich des auf für ist im dem nicht ein eine als auch es an werden aus er',
    'it': 'di e il la che per un in del della non è una da le si dei con sono come alla nel gli più',
    'pt': 'de a o que e do da em um para com não uma os no se na por mais as dos como mas ao ele das',
    'la': 'et in est non ad cum quod qui sed ut quae enim si ab nec esse sunt per hoc ex autem',
}


def infer_language(words: collections.Counter) -> str:
    scores = {lang: sum(words[w] for w in vocabulary.split()) for lang, vocabulary in STOPWORDS.items()}
    return max(scores, key=scores.get)


def reference_words(pdf: Path, first: int | None, last: int | None) -> collections.Counter:
    """Words as pdftotext reads them: an independent word segmentation."""
    command = ['pdftotext', '-enc', 'UTF-8']
    if first:
        command += ['-f', str(first)]
    if last:
        command += ['-l', str(last)]
    result = subprocess.run(command + [str(pdf), '-'], capture_output=True, text=True)
    text = ''.join(t for t, _ in split_smallcaps(result.stdout))
    text = unicodedata.normalize('NFC', clean_text(text))
    text = re.sub(r'(\w)-\n([a-zà-ÿ])', r'\1\2', text)
    return collections.Counter(re.findall(r'\w+', text))


def repair_spaces(pages: dict[int, dict], reference: collections.Counter, report: Report) -> None:
    """Join words that pdftohtml split with a stray space ("Pa tton").

    Only when pdftotext has the joined word and at least one fragment is not
    a word of the book; a fragment about to be hyphen-joined is left alone.
    """
    def fix(match: re.Match) -> str:
        a, b = match.group(1), match.group(2)
        joined = a + b
        if len(joined) >= 4 and reference[joined] and (not reference[a] or not reference[b]):
            report.add('spaces_repaired', f'{a} {b} -> {joined}')
            return joined
        return match.group(0)

    for info in pages.values():
        for run in info['runs']:
            if ' ' in run.text.strip():
                run.text = re.sub(r'(?<!\w)(\w+) (\w+)(?![\w-])', fix, run.text)


def mixed_script_words(all_lines, report: Report) -> None:
    """Latin and Greek letters in one word ("eunμchus") betray a wrong font map."""
    for number, lines in all_lines.items():
        for line in lines:
            if line.kind == 'margin':
                continue
            # runs apart on the page are apart in the text ("εὐνή" + "bed")
            text = line.runs[0].text if line.runs else ''
            for a, b in zip(line.runs, line.runs[1:]):
                text += (' ' if b.left - a.right >= 1.0 else '') + b.text
            for word in re.findall(r'\w+', text):
                if re.search(r'[A-Za-z]', word) and re.search(r'[Ͱ-Ͽἀ-῿]', word):
                    fonts = sorted({r.family for r in line.runs if any(c in r.text for c in word)})
                    report.add('mixed_script_words', {'page': number, 'word': word, 'fonts': fonts})


def derive_font_maps(pairs_path: Path) -> dict:
    """Build font_maps from [{"font": ..., "garbled": ..., "text": ...}] pairs."""
    maps: dict[str, dict[str, str]] = collections.defaultdict(dict)
    problems = []
    for pair in json.loads(pairs_path.read_text(encoding='utf-8')):
        garbled, text, font = pair['garbled'], pair['text'], pair['font']
        if len(garbled) != len(text):
            problems.append(f'{font}: "{garbled}" and "{text}" differ in length; split the pair')
            continue
        for g, t in zip(garbled, text):
            known = maps[font].get(g)
            if known is not None and known != t:
                problems.append(f'{font}: "{g}" maps to both "{known}" and "{t}"')
            maps[font][g] = t
    for problem in problems:
        print(f'  CONFLICT {problem}', file=sys.stderr)
    return {font: {g: t for g, t in sorted(m.items()) if g != t} for font, m in maps.items()}


# --------------------------------------------------------------------------
# Classification

def classify(all_lines: dict[int, list[Line]], pages: dict, geometry: dict, config: dict, report: Report) -> None:
    body_size, note_size = geometry['body_size'], geometry['note_size']
    ratio = config['superscript_ratio']
    removed = collections.Counter()
    previous_had_notes = False
    # Running heads repeat (with other numbers); a title set high on its page
    # in display type is printed once.
    band_text = collections.Counter()
    for lines in all_lines.values():
        for key in {band_key(l) for l in lines
                    if l.bottom < geometry['text_top'] or l.top > geometry['text_bottom']}:
            band_text[key] += 1
    for number in sorted(all_lines):
        lines = all_lines[number]
        for line in lines:
            weights = collections.Counter()
            for r in line.runs:
                weights[r.size] += len(r.text.strip())
            # by characters, not runs: "3" + "Johan Huizinga's image ..." is a size-14 line
            dominant = weights.most_common(1)[0][0] if weights else line.runs[0].size
            base = max((r.bottom for r in line.runs if r.size == dominant), default=line.runs[0].bottom)
            height = max((r.height for r in line.runs if r.size == dominant), default=1)
            for run in line.runs:
                if run.size <= dominant * ratio and run.bottom <= base - height * 0.18 and run.text.strip():
                    run.sup = True
            if (line.bottom < geometry['text_top'] or line.top > geometry['text_bottom']) \
                    and not (band_text[band_key(line)] == 1 and line.size >= body_size * 1.15
                             and re.search(r'[^\W\d_]{3,}', line.text)):
                line.kind = 'margin'
                removed[re.sub(r'\d+', '#', line.text.strip().lower())] += 1
                continue
            for text in mark_marginal(line, geometry):
                report.add('marginal_numbers', f'p{number}:{text}')
        # a margin number between two text lines joins the nearest one
        for line in [l for l in lines if l.kind != 'margin' and l.runs
                     and all(r.marginal or not r.text.strip() for r in l.runs)]:
            others = [l for l in lines if l is not line and l.kind != 'margin'
                      and any(not r.marginal and r.text.strip() for r in l.runs)]
            nearest = min(others, key=lambda l: abs(l.top - line.top), default=None)
            if nearest is not None and abs(nearest.top - line.top) <= geometry['pitch'] * 1.2:
                nearest.runs = sorted(nearest.runs + line.runs, key=lambda r: r.left)
                lines.remove(line)
        # Foot of the page: note-size lines below the last body-size line.  A
        # block quotation in the same size can end the page, so the notes begin
        # after the separator space (the largest gap) or at a numbered line.
        content = [l for l in lines if l.kind != 'margin']
        body_idx = [i for i, l in enumerate(content) if l.size == body_size]
        if note_size is not None:
            if not body_idx:
                # A page of notes only: a long note running on from the previous
                # page.  Never a two-column page (index), a page with larger type
                # (a heading: bibliography, index, part title) or a page after
                # one without notes.
                continuation = (previous_had_notes and content
                                and not any(l.column for l in content)
                                and all(l.size <= note_size + 1 for l in content)
                                and not is_table(content, geometry) and prose_like(content))
                for line in content:
                    if continuation and line.size <= note_size:
                        line.kind = 'note'
                previous_had_notes = bool(continuation)
                continue
            last = content[body_idx[-1]]
            candidates = [l for l in content[body_idx[-1] + 1:] if l.size <= note_size]
            continued_notes, previous_had_notes = previous_had_notes, False
            if not candidates:
                continue
            gaps, previous = [], last
            for line in candidates:
                gaps.append(line.top - previous.bottom)
                previous = line
            widest = max(range(len(gaps)), key=lambda i: gaps[i])
            numbered = next((i for i, l in enumerate(candidates) if note_start(l)), None)
            note_pitch = geometry['note_pitch']
            body_text = sum(len(l.text.strip()) for l in content[:body_idx[-1] + 1] if l.size == body_size)
            columns = any(l.column for l in candidates)
            if numbered is not None and not (columns and body_text < 150):
                start = widest if widest <= numbered and gaps[widest] >= note_pitch * 1.3 else numbered
            elif numbered is None and gaps[widest] >= note_pitch * 2 and continued_notes \
                    and body_text >= 150 and not columns:
                # a note continued from the previous page, after the separator;
                # never small type under a lone heading (an index, a bibliography)
                start = widest
            else:
                previous_had_notes = False
                continue        # e.g. a block quotation ending the page
            for line in candidates[start:]:
                line.kind = 'note'
            previous_had_notes = True
    report.set('removed_margin_text', [{'text': t, 'pages': n} for t, n in removed.most_common(40)])


def band_key(line: Line) -> str:
    return re.sub(r'\d+', '#', ' '.join(line.text.lower().split()))


def is_centered(line: Line, geometry: dict) -> bool:
    margin = geometry['margin'].get(line.page % 2, 0)
    left_gap = line.left - margin
    right_gap = right_edge(geometry, line.page) - line.right
    return left_gap > geometry['indent'] * 1.5 and abs(left_gap - right_gap) < geometry['indent'] * 1.2


# --------------------------------------------------------------------------
# Notes

def note_start(line: Line) -> str | None:
    runs = [r for r in line.runs if r.text.strip()]
    if not runs:
        return None
    if runs[0].sup and MARKER.match(runs[0].text):
        return runs[0].text.strip()
    match = NOTE_START.match(line.text)
    return match.group(1) if match else None


def strip_note_label(line: Line, label: str) -> None:
    """Remove the printed "12." from the first line of a note."""
    for run in line.runs:
        if not run.text.strip():
            continue
        if run.sup and run.text.strip() == label:
            run.text = ''
        else:
            run.text = re.sub(rf'^\s*{re.escape(label)}(?:[.)]\s*|\s+)', '', run.text, count=1)
        return


def collect_notes(all_lines, geometry, report: Report) -> list[Note]:
    notes: list[Note] = []
    starts = [round(l.left) for lines in all_lines.values() for l in lines if l.kind == 'note' and note_start(l)]
    start_left = mode(starts, None)
    open_note: Note | None = None
    resume: list[int] = []   # where an interrupted series goes on (main notes after a table's own notes)
    for number in sorted(all_lines):
        zone = [l for l in all_lines[number] if l.kind == 'note']
        for index, line in enumerate(zone):
            label = note_start(line)
            plausible = label is not None and (start_left is None or abs(line.left - start_left) <= geometry['indent'] * 0.6
                                              or line.runs[0].sup)
            if label and label.isdigit() and open_note and open_note.label.isdigit():
                number_ = int(label)
                # the next number (or one a little further: a marker misread), a new
                # series, or the series a table's own notes interrupted;
                following = int(open_note.label) + 1
                # never backwards or the same number again ("4 (1995)" inside note 4)
                if not (following <= number_ <= following + 3) and number_ != 1 and number_ not in resume[-3:]:
                    plausible = False
            if plausible:
                if label.isdigit() and open_note and open_note.label.isdigit():
                    if int(label) == 1 and int(open_note.label) > 1:
                        resume.append(int(open_note.label) + 1)   # a chapter's or a table's new series
                    elif int(label) in resume:
                        resume.remove(int(label))
                strip_note_label(line, label)
                open_note = Note(label, number, [[line]])
                notes.append(open_note)
            elif open_note is None:
                report.add('note_problems', {'page': number, 'problem': 'note text with no note number', 'text': line.text.strip()[:80]})
                open_note = Note('?', number, [[line]])
                notes.append(open_note)
            elif index > 0 and start_left is not None and abs(line.left - start_left) <= 2 and not line.runs[0].sup:
                open_note.lines.append([line])  # indented line without number: new paragraph in the note
            else:
                if index == 0 and number != open_note.page:
                    report.add('notes_continued', {'note': open_note.label, 'from_page': open_note.page, 'to_page': number})
                open_note.lines[-1].append(line)
    return notes


# --------------------------------------------------------------------------
# Text assembly

class Vocabulary:
    def __init__(self, all_lines, reference: collections.Counter | None = None) -> None:
        self.reference = reference or collections.Counter()  # pdftotext's words
        self.lower = collections.Counter()                   # the same, without case
        for word, count in self.reference.items():
            self.lower[word.lower()] += count
        self.words = collections.Counter()
        self.hyphenated = collections.Counter()
        for lines in all_lines.values():
            for line in lines:
                text = line.text
                for word in re.findall(r'\w+(?:-\w+)+', text):
                    self.hyphenated[word.lower()] += 1
                for word in re.findall(r'\w+', text):
                    self.words[word.lower()] += 1


_WORDS: dict = {}


def system_words(config: dict) -> set[str]:
    """Word list for the book's language: config "dictionary" (a plain or Hunspell
    .dic list), else /usr/share/dict/words for English (present on macOS)."""
    path = config.get('dictionary') or ('/usr/share/dict/words' if (config['lang'] or '').startswith('en') else None)
    if not path:
        return set()
    if path not in _WORDS:
        try:
            lines = Path(path).read_text(encoding='utf-8', errors='replace').splitlines()
            _WORDS[path] = {l.split('/')[0].strip().lower() for l in lines if l.strip() and not l[0].isdigit()}
        except OSError:
            _WORDS[path] = set()
    return _WORDS[path]


# English prefixes and second halves that are hyphenated, not syllables of
# a longer word ("thresh-old" and "stub-born" are why "old" and "born" are not here)
ENGLISH_PREFIXES = {'self', 'well', 'ill', 'non', 'half', 'quasi'}
ENGLISH_SUFFIXES = {'like', 'based', 'related', 'century', 'centuries', 'year', 'years', 'free', 'wide',
                    'minded', 'headed', 'headedness', 'humoured', 'humored', 'centred', 'centered', 'oriented',
                    'specific', 'scale', 'speaking', 'owned', 'sided', 'shaped', 'coloured', 'colored', 'eyed',
                    'omened'}


def join_decision(left_word: str, right_word: str, vocab: Vocabulary, config: dict, report: Report,
                  before: str = '') -> str:
    """How to join "left-" + "right" at a line break: '' (join) or '-' (keep).

    Evidence, in order: capitals or digits after the break (a real compound or
    name), the config, the book itself (the word elsewhere joined or hyphenated),
    then the language: Spanish and Portuguese repeat a real hyphen at the start
    of the next line, so a single line-end hyphen is a syllable break; in other
    languages two frequent words around the break are a compound.  Only what no
    rule covers is reported as a guess.
    """
    joined = (left_word + right_word).lower()
    compound = f'{left_word}-{right_word}'.lower()
    if right_word[:1].isupper() or right_word[:1].isdigit() or left_word[-1:].isdigit():
        return '-'
    if re.search(r'[/@:]|www\.|\w\.\w', before):
        return '-'      # a web or mail address keeps its hyphens
    if compound in {w.lower() for w in config['keep_hyphen_words']}:
        return '-'
    if vocab.hyphenated[compound] > vocab.words[joined]:
        report.add('hyphen_kept', compound)
        return '-'
    if vocab.words[joined]:
        return ''
    lang = (config['lang'] or '').lower()[:2]
    if lang in ('es', 'pt'):
        report.add('hyphen_by_rule', f'{left_word}-{right_word}')
        return ''
    dictionary = system_words(config)
    if dictionary:
        if joined in dictionary:
            return ''
        if left_word.lower() in dictionary and right_word.lower() in dictionary:
            report.add('hyphen_kept', compound)
            return '-'
    if vocab.words[left_word.lower()] >= 3 and vocab.words[right_word.lower()] >= 3 and len(left_word) > 2:
        report.add('hyphen_kept', compound)
        return '-'
    if lang == 'en':
        left, right = left_word.lower(), right_word.lower()
        # a compound joins whole words ("flute-player", "seventy-eight");
        # a syllable break leaves fragments ("acci-dentally")
        if left in ENGLISH_PREFIXES or right in ENGLISH_SUFFIXES \
                or (len(right) >= 3 and vocab.lower[left] and vocab.lower[right]):
            report.add('hyphen_kept', compound)
            return '-'
    report.add('hyphen_guessed', f'{left_word}-{right_word}')
    return ''


STRICT_ROMAN = re.compile(r'^(?=[mdclxvi])m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$')
NUMBERED_BY = {'siglo', 'siglos', 'century', 'centuries', 'tomo', 'libro', 'capítulo', 'vol', 'volumen',
               'parte', 'part', 'book', 'chapter', 'acto', 'act', 'canto'}


def upper_roman(text: str, before: str) -> str:
    """Small-caps roman numerals ("siglo xvii") read as capitals outside small caps."""
    def fix(match: re.Match) -> str:
        word = match.group(0)
        previous = re.findall(r'\w+', before + text[:match.start()])
        if STRICT_ROMAN.match(word) and (len(word) >= 3 or (previous and previous[-1].lower() in NUMBERED_BY)):
            return word.upper()
        return word
    return re.sub(r'\b[ivxlcdm]+\b', fix, text)


def runs_to_html(runs: list[Run], in_heading: bool = False) -> str:
    out: list[str] = []
    stack: list[str] = []   # open inline tags, innermost last: closed in reverse order

    def close_to(depth: int) -> None:
        while len(stack) > depth:
            tag = stack.pop()
            out.append('</span>' if tag == 'sc' else f'</{tag}>')

    def close_all() -> None:
        close_to(0)

    for index, run in enumerate(runs):
        text = html.escape(run.text, quote=False)
        if in_heading and run.smallcaps:
            text = upper_roman(text, ''.join(r.text for r in runs[:index]))
        if run.rtl and text.strip():
            close_all()
            out.append(f'<span dir="rtl">{text}</span>')
            continue
        if run.marginal:
            close_all()
            out.append(f' <span class="marginal" data-custom-style="Marginal Number">{text.strip()}</span> ')
            continue
        if getattr(run, 'noteref', None):
            close_all()
            # the marker's own text ("32 ") is replaced: keep its trailing space
            out.append(run.noteref + (' ' if run.text[-1:].isspace() else ''))
            continue
        if run.sup:
            close_all()
            out.append(f'<sup>{text}</sup>')
            continue
        # Headings keep their small caps too: the TOC gets the plain text, the
        # page and Word get the printed typography.
        smallcaps = run.smallcaps
        if smallcaps and len(run.text.strip()) <= 2 and 0 < index < len(runs) - 1 \
                and runs[index - 1].text[-1:].isalpha() and runs[index + 1].text[:1].isalpha():
            smallcaps = False  # a glyph borrowed from the small-caps font inside a word (Sigurður)
        want = {'i': run.italic, 'b': run.bold, 'sc': smallcaps}
        if not run.text.strip():
            out.append(text)
            continue
        keep = 0
        while keep < len(stack) and want[stack[keep]]:
            keep += 1
        close_to(keep)
        for tag in ('i', 'b', 'sc'):
            if want[tag] and tag not in stack:
                out.append('<span class="smallcaps">' if tag == 'sc' else f'<{tag}>')
                stack.append(tag)
        out.append(text)
    close_all()
    result = ''.join(out)
    result = re.sub(r'</i>(\s*)<i>', r'\1', result)
    result = re.sub(r'</b>(\s*)<b>', r'\1', result)
    result = re.sub(r'</span>(\s*)<span class="smallcaps">', r'\1', result)
    result = re.sub(r'</span>(\s*)<span dir="rtl">', r'\1', result)
    result = re.sub(r'\s+', ' ', result)
    result = re.sub(r'\s+(<a [^>]*epub:type="noteref")', r'\1', result)
    return result.strip()


def rejoin_split_words(fragment: str, reference: collections.Counter, report: Report) -> str:
    """Join "Mexi co" when pdftotext knows "Mexico" and a half is not a word.

    Works on assembled text (after hyphen repair), outside tags; a loosely
    justified line can put a space inside a word in the PDF itself.
    """
    if not reference:
        return fragment

    def fix(match: re.Match) -> str:
        a, b = match.group(1), match.group(2)
        joined = a + b
        if (len(joined) >= 4 and not (a.isdigit() or b.isdigit()) and reference[joined] >= 2
                and (reference[a] < 2 or reference[b] < 2)):
            report.add('spaces_repaired', f'{a} {b} -> {joined}')
            return joined
        return match.group(0)

    parts = re.split(r'(<[^>]+>)', fragment)
    return ''.join(p if p.startswith('<') else re.sub(r'(?<!\w)(\w+) (\w+)(?!\w)', fix, p) for p in parts)


def place_marginal(line_runs: list[Run], marginal: list[Run], paragraph_start: bool) -> None:
    """Put a margin number where its section most likely begins on the line:
    at the start of a paragraph, else before the first sentence that starts
    on the line, else after the line's first word (never inside a word
    continued from the line above)."""
    if paragraph_start or not line_runs:
        if line_runs:
            line_runs[0].text = line_runs[0].text.lstrip()
        line_runs[0:0] = marginal
        return
    spans, text = [], ''
    for position, run in enumerate(line_runs):
        if not run.sup and not getattr(run, 'noteref', None):
            spans.append((len(text), len(text) + len(run.text), position))
        text += run.text
    match = SENTENCE_START.search(text) or re.search(r'\S(?=\s)', text)
    if match:
        cut = match.end()
        for begin, finish, position in spans:
            if begin <= cut <= finish:
                run = line_runs[position]
                tail = copy.copy(run)
                run.text, tail.text = run.text[:cut - begin].rstrip(), run.text[cut - begin:].lstrip()
                line_runs[position + 1:position + 1] = marginal + ([tail] if tail.text else [])
                return
    line_runs.extend(marginal)


def assemble(lines: list[Line], vocab: Vocabulary, config: dict, report: Report) -> list[Run]:
    """Join lines into one run list with spaces and hyphenation handled."""
    runs: list[Run] = []
    for index, line in enumerate(lines):
        line_runs = [copy.copy(r) for r in line.runs if r.text]
        marginal = [r for r in line_runs if r.marginal]
        line_runs = [r for r in line_runs if not r.marginal]
        # Spaces pdftohtml dropped at a font change ("hijiras" + "of"): a word
        # space measures 2-5 units, touching runs 0-1.  pdftotext breaks ties.
        for a, b in zip(line_runs, line_runs[1:]):
            if a.dropcap and not b.text.startswith(' '):
                # "T" + "he" is "The", but "I" + "asked" is "I asked": ask the book
                letter, word = re.search(r'([^\W\d_]+)$', a.text), re.match(r'([^\W\d_]+)', b.text)
                if letter and word and vocab.reference:
                    joined = letter.group(1) + word.group(1)
                    if not any(vocab.reference[w] for w in (joined, joined.lower(), joined.capitalize())) \
                            and (vocab.reference[word.group(1)] or vocab.reference[word.group(1).lower()]):
                        b.text = ' ' + b.text
                continue
            if b.sup or getattr(b, 'noteref', None) or a.dropcap or a.text.endswith(' ') or b.text.startswith(' '):
                continue
            if a.rtl and b.rtl:
                a.text += ' '   # visual gaps between reversed words are word spaces
                continue
            if a.text[-1:].isalpha() and b.text[:1].isalpha() \
                    and bool(GREEK.match(a.text[-1])) != bool(GREEK.match(b.text[0])) and b.left - a.right >= 0.5:
                b.text = ' ' + b.text   # Greek next to Latin: a script change is a word break
                continue
            if b.left - a.right < max(1.5, a.size * 0.1):
                continue
            if re.match(r'[,.;:!?)\]’”»…]', b.text) or re.search(r'[(\[‘“«¿¡/–-]$', a.text):
                continue
            left_word, right_word = re.search(r'(\w+)$', a.text), re.match(r'(\w+)', b.text)
            if left_word and right_word and vocab.reference and not a.sup:
                joined = left_word.group(1) + right_word.group(1)
                if vocab.reference[joined] and not (vocab.reference[left_word.group(1)] and vocab.reference[right_word.group(1)]):
                    continue
            b.text = ' ' + b.text  # on b: a note marker's own text is replaced by its link
        if runs and line_runs:
            previous = next((r for r in reversed(runs) if r.text.strip() and not getattr(r, 'noteref', None)), None)
            nxt = next((r for r in line_runs if r.text.strip()), None)
            if previous is not None and nxt is not None:
                tail = previous.text.rstrip()
                if tail.endswith('-') and nxt.text.lstrip().startswith('-'):
                    nxt.text = nxt.text.lstrip()[1:]   # "franco-" / "-alemán": one real hyphen
                    previous.text = tail
                elif tail.endswith(('-', '­')) and not tail.endswith('--') and not getattr(runs[-1], 'noteref', None):
                    left_word = re.search(r'(\w+)[-­]$', tail)
                    right_word = re.match(r'\s*(\w+)', nxt.text)
                    handled = False
                    if left_word is None and tail in ('-', '­'):
                        # the hyphen is a piece of its own (after a link): the word is before it
                        position = max(i for i, r in enumerate(runs) if r is previous)
                        prior = next((r for r in reversed(runs[:position]) if r.text.strip()), None)
                        word = re.search(r'(\w+)$', prior.text) if prior is not None else None
                        if word and right_word:
                            context = prior.text.split()[-1] if prior.text.split() else ''
                            keep = join_decision(word.group(1), right_word.group(1), vocab, config, report, context)
                            previous.text = keep
                            nxt.text = nxt.text.lstrip()
                            handled = True
                    if handled:
                        pass
                    elif left_word and right_word:
                        token = tail.split()[-1] if tail.split() else ''
                        keep = '-' if tail.endswith('-') and join_decision(left_word.group(1), right_word.group(1), vocab,
                                                                       config, report, token[:-1]) else ''
                        previous.text = tail[:-1] + keep
                        nxt.text = nxt.text.lstrip()
                    else:
                        previous.text = tail
                elif tail.endswith(('—', '–', '/')) and re.search(r'(\s|^)[—–]$', tail):
                    previous.text = tail
                    nxt.text = nxt.text.lstrip()
                else:
                    # the space goes on the next line: a closing note marker's text is replaced
                    first = line_runs[0]
                    if not runs[-1].text.endswith(' ') and not first.text.startswith(' '):
                        first.text = ' ' + first.text
        if marginal:
            place_marginal(line_runs, marginal, paragraph_start=not runs)
        runs.extend(line_runs)
    return runs


# --------------------------------------------------------------------------
# Structure

def section_face(line: Line, geometry: dict) -> int:
    """2 for a body-size line set in small caps, a display face or bold; 3 for italic."""
    if line.size != geometry['body_size']:
        return 0
    runs = [r for r in line.runs if r.text.strip() and not r.sup]
    if not runs:
        return 0
    letters = collections.Counter()
    for r in runs:
        letters[face_name(r.family)] += sum(c.isalpha() for c in r.text)
    body_share = letters[geometry['body_family']] / max(sum(letters.values()), 1)
    if body_share < 0.4 and not all(r.italic for r in runs):
        return 2  # capitals may come from the text face, the rest from small caps
    smallcaps = sum(sum(c.isalpha() for c in r.text) for r in runs if r.smallcaps)
    if smallcaps >= sum(letters.values()) * 0.6:
        return 2  # small capitals encoded in the text face
    if all(r.bold for r in runs):
        return 2
    if all(r.italic for r in runs):
        return 3
    return 0


def section_headings(content: list[Line], geometry: dict) -> dict[int, int]:
    """Body-size headings, which may wrap: index -> level for every heading line."""
    pitch = geometry['pitch']
    faces = [section_face(l, geometry) for l in content]
    found: dict[int, int] = {}
    index = 0
    while index < len(content):
        level = faces[index]
        if not level:
            index += 1
            continue
        end = index
        while end + 1 < len(content) and faces[end + 1] == level \
                and content[end + 1].top - content[end].top < max(pitch, content[end].size) * 1.35:
            end += 1
        # extra space measured top to top: independent of the fonts' heights
        before = content[index].top - content[index - 1].top if index else None
        after = content[end + 1].top - content[end].top if end + 1 < len(content) else None
        text = ' '.join(l.text.strip() for l in content[index:end + 1])
        needed_before = pitch * (2.4 if level == 3 else 1.8)
        if ((before is None or before > needed_before) and (after is None or after > pitch * 1.5)
                and end - index < 3 and len(text) < 200 and not text.endswith(('.', ',', ';'))):
            for k in range(index, end + 1):
                found[k] = level
        index = end + 1
    for k, line in enumerate(content):
        before = line.top - content[k - 1].bottom if k else None
        after = content[k + 1].top - line.bottom if k + 1 < len(content) else None
        if DIVISION.match(line.text) and (before is None or before > pitch * 1.2) \
                and (after is None or after > pitch * 1.2):
            found[k] = 1
    return found


def first_chapter_page(all_lines, geometry) -> int | None:
    """First page with large type followed by real body text."""
    body_chars = collections.Counter()
    for number, lines in all_lines.items():
        for line in lines:
            if line.kind not in ('margin', 'note') and line.size == geometry['body_size']:
                body_chars[number] += len(line.text.strip())
    for number in sorted(all_lines):
        big = any(l.kind not in ('margin', 'note') and (l.size >= geometry['body_size'] * 1.3
                                                         or DIVISION.match(l.text)) for l in all_lines[number])
        if big and body_chars[number] + body_chars[number + 1] >= 1200:
            return number
    return None


def alignment(line: Line, geometry: dict) -> str:
    margin = geometry['margin'].get(line.page % 2, geometry['margin'].get(0, 0))
    right = right_edge(geometry, line.page)
    left_gap, right_gap = line.left - margin, right - line.right
    indent = geometry['indent']
    if left_gap > indent * 1.5 and abs(left_gap - right_gap) < indent * 1.2:
        return 'centered'
    if right_gap < indent * 0.5 and left_gap > (right - margin) * 0.33:
        return 'right'
    return 'left'


def front_blocks(all_lines, numbers, geometry, images: dict | None = None) -> list[Block]:
    """Front matter keeps its layout: one block per gap-separated group."""
    blocks = []
    for number in numbers:
        current = None
        previous = None
        content = [l for l in all_lines[number] if l.kind not in ('margin', 'note') and l.text.strip()]
        figures = page_figures(number, (images or {}).get(number, []), content, geometry)
        captions = {id(l) for f in figures for l in f.caption}
        for line in content:
            while figures and figures[0].top <= line.top:
                blocks.append(figures.pop(0))
                current = None
            if id(line) in captions:
                continue
            align = alignment(line, geometry)
            gap = line.top - previous.bottom if previous is not None else None
            if current is None or gap > (previous.bottom - previous.top) * 0.9 or align != current.css:
                current = Block('front', [line], css=align, page=number)
                blocks.append(current)
            else:
                current.lines.append(line)
            previous = line
        blocks.extend(figures)
    return blocks


def display_ladder(all_lines, geometry, skip) -> dict[int, int]:
    """Display sizes that are sections, not chapters.

    A size only a little larger than the text (under 1.6 times) whose
    headings mostly come after a clearly larger heading, with no other
    display size in between, is a level 2 heading under it.  Chapter and
    part titles (both large) keep level 1.
    """
    body = geometry['body_size']
    sequence = []
    for number in sorted(all_lines):
        if number in skip:
            continue
        for line in all_lines[number]:
            if line.kind not in ('margin', 'note') and line.size >= body * 1.3 and 0 < len(line.text.strip()) < 120:
                sequence.append(line.size)
    counts, under = collections.Counter(sequence), collections.Counter()
    for index, size in enumerate(sequence):
        previous = next((s for s in reversed(sequence[:index]) if s != size), None)
        if previous is not None and previous >= size * 1.25:
            under[size] += 1
    return {size: 2 for size, n in counts.items() if size < body * 1.6 and under[size] > n * 0.6}


def build_blocks(all_lines, geometry, config, report: Report, images: dict | None = None,
                 outline_active: bool = False):
    images = images or {}
    body_size = geometry['body_size']
    pitch, indent = geometry['pitch'], geometry['indent']
    quote_sizes = set(geometry['quote_sizes'])
    heading_fonts = config['heading_fonts']
    skip = set(config['skip_pages'])
    first = first_chapter_page(all_lines, geometry)
    front_until = config['front_matter_until'] if config['front_matter_until'] is not None else ((first - 1) if first else 0)
    geometry['front_until'] = front_until
    front_pages = [n for n in sorted(all_lines) if n <= front_until and n not in skip]
    skip |= set(front_pages)
    blocks: list[Block] = []
    current: Block | None = None
    previous_line: Line | None = None

    def flush():
        nonlocal current
        if current is not None and current.lines:
            blocks.append(current)
        current = None

    def heading_level(line: Line) -> int:
        text = line.text.strip()
        for pattern, level in config['heading_patterns'].items():
            if re.search(pattern, text):
                return int(level)
        families = collections.Counter(r.family for r in line.runs if r.text.strip() and not r.sup)
        for key, level in heading_fonts.items():
            family, _, size = key.partition('@')
            if any(family in f for f in families) and (not size or int(size) == line.size):
                return int(level)
        if len(text) > 40 and text.endswith('.') and not DIVISION.match(text):
            return 0    # a sentence set large (a call-out), not a title
        if line.size >= body_size * 1.3:
            return ladder.get(line.size, 1)
        if line.size >= body_size * 1.08:
            return 2
        return 0

    headings: list[tuple[int, str]] = []  # open headings: (level, text)
    outline_level = 0                     # level of the last bookmarked heading
    ladder = display_ladder(all_lines, geometry, skip)
    if ladder:
        report.set('display_sizes_as_sections', sorted(ladder))

    def mode() -> str:
        for _level, text in headings:
            if BIBLIOGRAPHY.search(text) or HANGING.search(text) \
                    or any(h.lower() in text.lower() for h in config['hanging_sections']):
                return 'bibliography'
            if INDEX.search(text):
                return 'index'
        return 'normal'

    def entry_open(block: Block) -> bool:
        """An index entry whose parenthesis is still open runs on."""
        text = ' '.join(l.text for l in block.lines)
        return text.count('(') > text.count(')')

    pages = sorted(all_lines)
    for number in pages:
        if number in skip:
            continue
        content = [l for l in all_lines[number] if l.kind not in ('margin', 'note') and l.text.strip()]
        sections = section_headings(content, geometry)
        figures = page_figures(number, images.get(number, []), content, geometry)
        captions = {id(l) for f in figures for l in f.caption}
        column_margin = {}
        for l in content:
            column_margin[l.column] = min(column_margin.get(l.column, l.left), l.left)
        for index, line in enumerate(content):
            while figures and figures[0].top <= line.top:
                flush()
                blocks.append(figures.pop(0))
            if id(line) in captions:
                continue
            margin = geometry['margin'].get(number % 2, geometry['margin'].get(0, 0))
            gap_before = (line.top - content[index - 1].bottom) if index else None
            if line.outline is not None:
                # a heading the PDF's bookmarks point to: their level, their extent
                level, entry = line.outline
                if current is not None and current.kind == 'heading' and current.outline == entry:
                    if line.size > current.lines[-1].size and not current.label:
                        current.label = ' '.join(l.text.strip() for l in current.lines)
                        current.lines = [line]
                    else:
                        current.lines.append(line)
                    headings[-1] = (level, ' '.join(l.text for l in current.lines))
                else:
                    flush()
                    current = Block('heading', [line], level=level, page=number)
                    current.outline = entry
                    headings[:] = [h for h in headings if h[0] < level] + [(level, line.text)]
                outline_level = level
                previous_line = line
                continue
            if mode() == 'index' and re.fullmatch(r'\s*[A-ZÁÉÍÓÚÑ]\s*', line.text):
                flush()
                current = Block('p', [line], page=number, css='index-letter', style='Index Letter')
                previous_line = line
                continue
            level = heading_level(line) or sections.get(index, 0)
            heuristic_level = level
            if level and outline_active and outline_level:
                level = max(level, min(outline_level + 1, 4))   # beneath the bookmarked heading
            if level and current is not None and current.kind == 'heading' and current.level == 1 \
                    and current.page == number and level > 1 and NAME_LINE.match(line.text.strip()) \
                    and gap_before is not None and gap_before < pitch * 3.5:
                # the contributor's name under a chapter title (edited volumes)
                flush()
                current = Block('p', [line], page=number, css='chapter-author', style='Chapter Author')
                report.add('chapter_authors', {'page': number, 'text': line.text.strip()})
                previous_line = line
                continue
            if level:
                label_then_title = line.size > current.lines[-1].size if current is not None and current.lines else False
                short_label = (current is not None and current.kind == 'heading' and len(current.lines) == 1
                               and len(current.lines[0].text.strip()) <= 40)
                step = line.top - current.lines[-1].top if current is not None and current.lines else None
                # a wrapped title keeps its size (its face may change: a work's title in italics)
                wrapped = step is not None and step < max(line.size, current.lines[-1].size) * 1.6 \
                    and abs(line.size - current.lines[-1].size) <= 1
                if current is not None and current.kind == 'heading' and current.page == number \
                        and gap_before is not None and getattr(current, 'outline', None) is None \
                        and ((level == current.level and wrapped)
                             or (label_then_title and not current.label and short_label and gap_before < pitch * 6)):
                    if line.size > current.lines[-1].size and not current.label:
                        current.label = ' '.join(l.text.strip() for l in current.lines)
                        current.lines = [line]
                        current.level = level
                    else:
                        current.lines.append(line)
                    # the label may have promoted the heading: close every section it ends
                    headings[:] = [h for h in headings[:-1] if h[0] < current.level] + \
                        [(current.level, ' '.join(l.text for l in current.lines))]
                    previous_line = line
                    continue
                flush()
                current = Block('heading', [line], level=level, page=number)
                current.heuristic_level = heuristic_level
                headings[:] = [h for h in headings if h[0] < level] + [(level, line.text)]
                previous_line = line
                continue

            section = mode()
            if section in ('bibliography', 'index'):  # whatever its type size
                # hanging indents: a flush line starts an entry, a deeper one continues it
                offset = line.left - (column_margin[line.column] if section == 'index' else margin)
                in_entry = current is not None and current.css in ('bibliography', 'index', 'index-sub')
                continues = in_entry and offset > indent * (1.5 if section == 'index' else 0.4)
                last_text = previous_line.text.rstrip() if previous_line is not None else ''
                if section == 'index' and in_entry and (
                        last_text.endswith('-')
                        or (last_text.endswith((',', '–')) and re.match(r'\s*\d', line.text))
                        # a wrapped description: the entry has not reached its page numbers
                        or (offset > indent * 0.3
                            and (re.search(r'[^\W\d_][;(]?$|[;(]$', last_text)
                                 or entry_open(current)
                                 or re.match(r'\s*[;,)(]', line.text)
                                 or (re.match(r'\s*\d', line.text) and not re.search(r'\d\W?$', last_text))))):
                    continues = True  # the page list runs over to the next line
                if continues:
                    current.lines.append(line)
                else:
                    flush()
                    if section == 'bibliography':
                        current = Block('p', [line], page=number, css='bibliography', style='Bibliography')
                    elif offset < indent * 0.5:
                        current = Block('p', [line], page=number, css='index', style='Index Entry')
                    else:
                        current = Block('p', [line], page=number, css='index-sub', style='Index Subentry')
                previous_line = line
                continue

            # Lists: an item starts at a marker ("13.", "a)", "•") and its
            # wrapped lines hang to the right of the marker.
            marker = LIST_START.match(line.text)
            in_list = current is not None and current.kind == 'li'
            same_page_gap = None if previous_line is None or previous_line.page != number else line.top - previous_line.bottom
            if in_list and not marker and line.left > current.list_left + indent * 0.4 \
                    and line.left < current.list_left + indent * 4 and (same_page_gap is None or same_page_gap < pitch * 0.9):
                current.lines.append(line)
                previous_line = line
                continue
            if marker:
                following = content[index + 1] if index + 1 < len(content) else None
                # a wrapped item fills its first line; a short line is a heading or label
                hangs = following is not None and not LIST_START.match(following.text) \
                    and line.left + indent * 0.4 < following.left < line.left + indent * 4 \
                    and line.right > right_edge(geometry, line.page) - indent * 3
                sibling = following is not None and LIST_START.match(following.text) and abs(following.left - line.left) <= 3
                levels = current.list_levels if in_list else set()
                if (in_list and any(abs(line.left - level) <= 3 for level in levels)) or hangs or sibling:
                    flush()
                    current = Block('li', [line], page=number)
                    current.list_levels = levels | {round(line.left)}
                    current.list_left = line.left
                    current.marker = marker.group('marker')
                    previous_line = line
                    continue

            kind = 'quote' if line.size in quote_sizes else 'p'
            if kind == 'p' and is_centered(line, geometry) and (gap_before is None or gap_before > pitch * 0.9):
                kind = 'centered'
            new_page = previous_line is None or previous_line.page != line.page
            indented = indent * 0.5 < line.left - margin < indent * 3 and not line.beside_dropcap
            if current is not None and current.kind == kind == 'p':
                gap = None if new_page else line.top - previous_line.bottom
                if indented or (gap is not None and gap > pitch * 0.9):
                    flush()
                    current = Block('p', [line], page=number)
                    if not indented:
                        current.css, current.style = 'noindent', 'First Paragraph'
                else:
                    current.lines.append(line)
            elif current is not None and current.kind == kind == 'quote':
                quote_margin = min(l.left for l in current.lines)
                gap = None if new_page else line.top - previous_line.bottom
                if line.left > quote_margin + indent * 0.5 or (gap is not None and gap > pitch * 0.9):
                    flush()
                    current = Block('quote', [line], page=number)
                else:
                    current.lines.append(line)
            elif current is not None and current.kind == kind == 'centered' and not new_page \
                    and line.top - previous_line.bottom < pitch * 0.6:
                current.lines.append(line)
            else:
                after = current.kind if current is not None else None
                flush()
                current = Block(kind, [line], page=number)
                if kind == 'p' and after == 'quote':
                    if indented:
                        current.css, current.style = 'indent', 'Body Text'
                    else:
                        current.css, current.style = 'noindent', 'First Paragraph'
                elif kind == 'p' and after not in (None, 'heading') and not indented:
                    current.css, current.style = 'noindent', 'First Paragraph'
            previous_line = line
        if figures:
            flush()
            blocks.extend(figures)
    flush()
    return front_blocks(all_lines, front_pages, geometry, images) + blocks


def page_figures(number: int, page_images: list[dict], content: list[Line], geometry: dict) -> list[Block]:
    """Raster images in the text block, each with the caption lines below it."""
    figures = []
    for image in page_images:
        top, height = float(image['top']), float(image['height'])
        block = Block('figure', page=number, image=image['src'])
        block.top = top
        below = [l for l in content if l.top >= top + height - 2]

        def signature(line: Line) -> tuple:
            runs = [r for r in line.runs if r.text.strip() and not r.sup]
            return line.size, sum(r.italic for r in runs) * 2 > len(runs)

        for line in below:
            first = not block.caption
            near = line.top - (block.caption[-1].bottom if block.caption else top + height) < geometry['pitch'] * (2 if first else 0.9)
            looks = CAPTION.match(line.text) or line.size < geometry['body_size']
            # a caption continues only in its own face: same size and the same italics
            if near and (looks if first else signature(line) == signature(block.caption[0])
                         and not (line.size == geometry['body_size'] and not signature(line)[1])):
                block.caption.append(line)
            else:
                break
        figures.append(block)
    return sorted(figures, key=lambda f: f.top)


def demote_display_headings(blocks: list[Block], all_lines, geometry, report: Report,
                            title: str = '', outline_active: bool = False) -> None:
    """Large type on title, half-title and series pages is not a chapter.

    Before the first chapter only: a bibliography or an index is set in small
    type, so its heading is not followed by body-size text, yet it is a chapter.
    Later, a page that only repeats the book's title (a half-title) is not a
    chapter either, and nor is display type the PDF's bookmarks leave out.
    """
    body_chars = collections.Counter()
    for number, lines in all_lines.items():
        for line in lines:
            if line.kind not in ('margin', 'note') and line.size == geometry['body_size']:
                body_chars[number] += len(line.text.strip())
    first = first_chapter_page(all_lines, geometry) or 0
    key = fold(title)
    for block in blocks:
        if block.kind != 'heading' or block.outline is not None:
            continue
        text = ' '.join(l.text.strip() for l in block.lines)
        if block.level >= 2 and (len(block.lines) > 3 or (text.endswith('.') and len(text.split()) > 8)) \
                and not DIVISION.match(text):
            # a sentence or a block of text set in display type (a call-out, an address)
            block.kind, block.level = ('centered' if is_centered(block.lines[0], geometry) else 'p'), 0
            report.add('display_text', {'page': block.page, 'text': text[:80]})
            continue
        alone = body_chars[block.page] < 400
        if block.level == 1 and block.page < first:
            alone = body_chars[block.page] + body_chars[block.page + 1] < 400
        elif not ((key and fold(text) == key) or (outline_active and block.heuristic_level == 1)):
            continue
        if alone:
            block.kind, block.level = 'centered', 0
            report.add('display_text', {'page': block.page, 'text': text[:80]})


def toc_line(line: Line, indent: float) -> bool:
    """A printed contents entry: dot leaders, or a page number standing apart.

    Index entries also end in page numbers, but those follow a comma.
    """
    # a title: words, not the figures of a table ("1156     1158")
    if not re.search(r'[^\W\d_]{3,}', re.sub(r'(\d{1,4}|[ivxlcdm]{1,7})\s*$', '', line.text, flags=re.I)):
        return False
    if LEADERS.search(line.text):
        return True
    if re.search(r'\S\s{3,}(\d{1,4}|[ivxlcdm]{1,7})\s*$', line.text, re.I):
        return True  # page number after a wide gap inside the same text run
    runs = [r for r in line.runs if r.text.strip()]
    if len(runs) < 2 or not re.fullmatch(r'\s*(\d{1,4}|[ivxlcdm]{1,7})\s*', runs[-1].text, re.I):
        return False
    return runs[-1].left - runs[-2].right > indent * 2 and not runs[-2].text.rstrip().endswith(',')


def detect_toc_pages(all_lines, config, geometry) -> set[int]:
    if config['toc_pages'] is not None:
        return set(config['toc_pages'])
    found = set()
    numbers = sorted(all_lines)
    # contents pages open or close a book (Spanish books print the índice last)
    ends = set(numbers[:max(12, len(numbers) // 5)]) | set(numbers[-max(8, len(numbers) // 10):])
    for number, lines in all_lines.items():
        content = [l for l in lines if l.kind != 'margin' and l.text.strip()]
        entries = sum(1 for l in content if toc_line(l, geometry['indent']))
        if entries >= 4 and entries >= len(content) * 0.3 and number in ends:
            found.add(number)
    return found


def is_table(lines: list[Line], geometry: dict) -> bool:
    """Many lines of cells set apart by wide gaps: three or more cells, or
    two cells of figures (a sparse table of years)."""
    rows = 0
    for line in lines:
        if line.kind == 'margin':
            continue
        runs = [r for r in line.runs if r.text.strip()]
        text = ''.join(r.text for r in runs)
        gaps = sum(1 for a, b in zip(runs, runs[1:]) if b.left - a.right > max(geometry['indent'], a.size * 1.5))
        gaps += len(re.findall(r'\S {4,}(?=\S)', text))
        figures = sum(c.isdigit() for c in text) >= sum(c.isalpha() for c in text)
        rows += gaps >= 2 or (gaps >= 1 and figures)
    return rows >= 6


def prose_like(lines: list[Line]) -> bool:
    """Running text, as notes are: most lines hold several words."""
    worded = sum(1 for l in lines if len(re.findall(r'[^\W\d_]{2,}', l.text)) >= 3)
    return worded >= len(lines) * 0.6


def table_pages(all_lines, geometry) -> list[int]:
    """Pages laid out as a table.  Tables are not rebuilt; they come out as
    lines of text and must be rebuilt by hand."""
    return [number for number, lines in all_lines.items() if is_table(lines, geometry)]


def make_id(prefix: str, n: int) -> str:
    return f'{prefix}{n:02d}'


def render(blocks, notes, all_lines, geometry, vocab, config, report: Report, media_dir: str | None, pages) -> str:
    lang = config['lang']
    out = [f'<!DOCTYPE html>\n<html lang="{lang}" xml:lang="{lang}">\n<head>\n<meta charset="utf-8" />\n'
           f'<title>{html.escape(config["title"] or "Book")}</title>\n</head>\n<body>']
    chapter = 0
    chapter_notes: list[Note] = []
    notes_by_page = collections.defaultdict(list)
    for note in notes:
        notes_by_page[note.page].append(note)
    pending_notes: list[Note] = []
    seen_pages: set[int] = set()
    front_until = geometry.get('front_until', 0)
    in_front = False
    ref_total = 0

    def emit_notes(before_page: float = float('inf')):
        """Write the chapter's notes; unreferenced notes become visible paragraphs."""
        nonlocal chapter_notes
        for note in chapter_notes:
            paragraphs = []
            for para in note.lines:
                runs = assemble(para, vocab, config, report)
                paragraphs.append(f'<p>{rejoin_split_words(runs_to_html(runs), vocab.reference, report)}</p>')
            out.append(f'<aside epub:type="footnote" id="{note.note_id}">{"".join(paragraphs)}</aside>')
        chapter_notes = []
        for note in [n for n in pending_notes if not n.used and n.page < before_page]:
            report.add('note_problems', {'page': note.page, 'problem': f'note {note.label} has no reference in the text; kept as a visible paragraph',
                                         'text': ' '.join(l.text.strip() for p in note.lines for l in p)[:80]})
            runs = assemble([l for p in note.lines for l in p], vocab, config, report)
            out.append(f'<div class="orphan-note" data-custom-style="Front Matter"><p>[{html.escape(note.label)}] {runs_to_html(runs)}</p></div>')
            note.used = True
        pending_notes[:] = [n for n in pending_notes if not n.used]

    def queue_page(page: int):
        if page not in seen_pages:
            seen_pages.add(page)
            pending_notes.extend(notes_by_page.get(page, []))

    def attach_refs(lines: list[Line]):
        nonlocal ref_total
        for line in lines:
            queue_page(line.page)
            for run in line.runs:
                if not run.sup or not MARKER.match(run.text):
                    continue
                label = run.text.strip()
                match = next((n for n in pending_notes if not n.used and n.label == label), None)
                if match is None:  # the note may start at the foot of the next page
                    queue_page(line.page + 1)
                    match = next((n for n in pending_notes if not n.used and n.label == label), None)
                if match is None:
                    match = next((n for n in pending_notes if not n.used and n.label == '?'), None)
                if match is None:
                    report.add('note_problems', {'page': line.page, 'problem': f'reference {label} has no note on this or an earlier page of the chapter'})
                    continue
                ref_total += 1
                match.used = True
                match.note_id = f'{chapter_id}-n{ref_total}'
                chapter_notes.append(match)
                run.noteref = f'<a epub:type="noteref" href="#{match.note_id}">{html.escape(label)}</a>'

    # Group consecutive list items: bullets -> <ul>; numbers in sequence ->
    # <ol start>; anything else keeps its printed markers as hanging paragraphs.
    index = 0
    while index < len(blocks):
        if blocks[index].kind != 'li':
            index += 1
            continue
        end = index
        while end + 1 < len(blocks) and blocks[end + 1].kind == 'li':
            end += 1
        group = blocks[index:end + 1]
        markers = [b.marker for b in group]
        numbers = [re.match(r'\d+', m) for m in markers]
        base = min(b.list_left for b in group)
        nested = any(b.list_left > base + geometry['indent'] * 0.5 for b in group)
        for b in group:
            b.list_sub = b.list_left > base + geometry['indent'] * 0.5
        if nested:
            kind, start = 'hanging', None   # mixed levels keep their printed markers
        elif all(not re.match(r'[\w]', m) for m in markers):
            kind, start = 'ul', None
        elif all(numbers) and [int(n.group(0)) for n in numbers] == list(
                range(int(numbers[0].group(0)), int(numbers[0].group(0)) + len(group))):
            kind, start = 'ol', int(numbers[0].group(0))
        else:
            kind, start = 'hanging', None
        for position, item in enumerate(group):
            item.list_kind, item.list_start = kind, start
            item.list_first, item.list_last = position == 0, position == len(group) - 1
            if kind != 'hanging':
                for run in item.lines[0].runs:
                    if run.text.strip():
                        run.text = re.sub(r'^\s*' + re.escape(item.marker) + r'\s*', '', run.text, count=1)
                        break
        report.add('lists', {'page': group[0].page, 'items': len(group), 'kind': kind})
        index = end + 1

    chapter_id = 'front'
    for block in blocks:
        if block.kind == 'heading' and block.level == 1:
            if in_front:
                out.append('</div>')
                in_front = False
            emit_notes(before_page=block.page)
            chapter += 1
            chapter_id = make_id('ch', chapter)
        elif block.page <= front_until and not in_front and chapter == 0:
            out.append('<div class="front-matter" data-custom-style="Front Matter">')
            in_front = True

        attach_refs(block.lines)
        if block.kind == 'figure':
            attach_refs(block.caption)
            caption = runs_to_html(assemble(block.caption, vocab, config, report)) if block.caption else ''
            src = html.escape(f'{media_dir.rstrip("/")}/{Path(block.image).name}' if media_dir else block.image)
            word = FIGURE_WORD.get(config['lang'].split('-')[0].lower(), 'Figure')
            alt = html.escape(re.sub(r'<[^>]+>', '', caption)[:150] or word, quote=True)
            figcaption = f'<figcaption>{caption}</figcaption>' if caption else ''
            out.append(f'<figure><img src="{src}" alt="{alt}" />{figcaption}</figure>')
            continue
        if block.kind == 'front':
            pieces, group = [], []
            for line in block.lines:
                group.append(line)
                short = right_edge(geometry, line.page) - line.right > geometry['indent'] * 2
                if block.css == 'right' or (block.css == 'left' and short):
                    pieces.append(runs_to_html(assemble(group, vocab, config, report)))
                    group = []
            if group:
                pieces.append(runs_to_html(assemble(group, vocab, config, report)))
            style = {'centered': ('centered', 'Centered'), 'right': ('right', 'Right')}.get(block.css)
            body = '<br />'.join(p for p in pieces if p)
            if style:
                out.append(f'<div class="{style[0]}" data-custom-style="{style[1]}"><p>{body}</p></div>')
            else:
                out.append(f'<p>{body}</p>')
            continue
        if block.kind == 'heading':
            runs = assemble(block.lines, vocab, config, report)
            title = runs_to_html(runs, in_heading=True)
            level = max(1, min(block.level, 4))
            ident = f' id="{chapter_id}"' if level == 1 else ''
            label = f'<span class="chapter-label">{html.escape(block.label)}</span><br />' if block.label else ''
            out.append(f'<h{level}{ident}>{label}{title}</h{level}>')
            report.add('headings', {'level': level, 'page': block.page, 'text': (block.label + ' ' if block.label else '') + re.sub('<[^>]+>', '', title)})
            continue
        runs = assemble(block.lines, vocab, config, report)
        body = rejoin_split_words(runs_to_html(runs), vocab.reference, report)
        if not body:
            continue
        if block.kind == 'li':
            if block.list_kind == 'hanging':
                css, style = ('hanging-sub', 'Hanging Subentry') if block.list_sub else ('hanging', 'Hanging Entry')
                out.append(f'<div class="{css}" data-custom-style="{style}"><p>{body}</p></div>')
                continue
            if block.list_first:
                out.append('<ul>' if block.list_kind == 'ul' else f'<ol start="{block.list_start}">')
            out.append(f'<li><p>{body}</p></li>')
            if block.list_last:
                out.append('</ul>' if block.list_kind == 'ul' else '</ol>')
            continue
        if block.kind == 'quote':
            out.append(f'<blockquote><p>{body}</p></blockquote>')
        elif block.kind == 'centered':
            out.append(f'<div class="centered" data-custom-style="Centered"><p>{body}</p></div>')
        elif block.css:
            out.append(f'<div class="{block.css}" data-custom-style="{block.style}"><p>{body}</p></div>')
        else:
            out.append(f'<p>{body}</p>')
    if in_front:
        out.append('</div>')
    emit_notes()
    out.append('</body>\n</html>\n')
    text = '\n'.join(out)
    text = re.sub(r'</blockquote>\n<blockquote>', '\n', text)
    return text


# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--pdf', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path, help='book HTML to write')
    parser.add_argument('--config', type=Path, help='JSON config (see DEFAULT_CONFIG in this script)')
    parser.add_argument('--report', type=Path, help='JSON report (default: OUT with .report.json)')
    parser.add_argument('--media', type=Path, help='directory for extracted raster images (default: no images)')
    parser.add_argument('--first-page', type=int)
    parser.add_argument('--last-page', type=int)
    parser.add_argument('--print-config', action='store_true', help='print the default config and exit')
    parser.add_argument('--no-repair', action='store_true',
                        help='extract from the PDF as it is, without repairing fonts whose text map '
                             'contradicts their glyph names (see repair_text_layer.py)')
    parser.add_argument('--font-map-pairs', type=Path,
                        help='JSON list of {"font", "garbled", "text"} pairs read from the page images; '
                             'prints the font_maps config entry they imply and exits')
    args = parser.parse_args()

    if args.print_config:
        print(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False))
        return
    if args.font_map_pairs:
        print(json.dumps({'font_maps': derive_font_maps(args.font_map_pairs)}, indent=2, ensure_ascii=False))
        return
    config = dict(DEFAULT_CONFIG)
    if args.config:
        config.update(json.loads(args.config.read_text(encoding='utf-8')))
    report = Report()

    with tempfile.TemporaryDirectory() as tmp:
        source = args.pdf if args.no_repair else repair_text_layer(args.pdf, Path(tmp), report)
        xml_path = run_pdftohtml(source, Path(tmp), args.media, args.first_page, args.last_page)
        fonts, pages, outline = load_layout(xml_path, config, report)
        reference = reference_words(source, args.first_page, args.last_page)
    title = config['title'] or pdf_title(args.pdf)
    if not config['title'] and title:
        config['title'] = title
    if not config['lang']:
        config['lang'] = infer_language(collections.Counter({w.lower(): n for w, n in reference.items()}))
        report.set('lang_inferred', config['lang'])
        example = {'es': 'es-ES', 'en': 'en-GB', 'fr': 'fr-FR', 'de': 'de-DE', 'it': 'it-IT', 'pt': 'pt-PT'}.get(
            config['lang'], config['lang'])
        print(f'  language: {config["lang"]} (inferred from the text; set "lang" in the config, '
              f'e.g. "{example}", to state it)')
    repair_spaces(pages, reference, report)
    garbled_fonts(pages, report)
    empty = [n for n, info in pages.items() if not info['runs']]
    scanned = [n for n in empty if pages[n]['images']]
    report.set('pages', {'total': len(pages), 'without_text': empty, 'image_only': scanned})
    if len(scanned) > len(pages) * 0.5:
        sys.exit('Most pages have no text layer: run OCR first (e.g. ocrmypdf -l spa+eng in.pdf ocr.pdf).')

    geometry, all_lines = analyse(pages, config, report)
    two_column = sorted(n for n, lines in all_lines.items() if any(l.column for l in lines))
    report.set('two_column_pages', two_column)
    landscape = [n for n in two_column if pages[n]['width'] > pages[n]['height'] * 1.2]
    if len(landscape) >= max(2, len(pages) * 0.5):
        report.set('two_up_pages', landscape)
        print(f'  {len(landscape)} page(s) hold two book pages side by side (a booklet or 2-up scan): '
              'each sheet is read left half, then right half; if the halves are in printing order, '
              'split and reorder the pages first (see SKILL.md)')
    classify(all_lines, pages, geometry, config, report)
    mixed_script_words(all_lines, report)
    toc_pages = detect_toc_pages(all_lines, config, geometry)
    report.set('toc_pages_skipped', sorted(toc_pages))
    report.set('table_pages', [n for n in table_pages(all_lines, geometry) if n not in toc_pages])
    config['skip_pages'] = sorted(set(config['skip_pages']) | toc_pages)
    report.set('skipped_pages', config['skip_pages'])
    report.set('margin_lines', sorted({' '.join(r.text.strip() for r in l.runs).strip()
                                       for lines in all_lines.values() for l in lines if l.kind == 'margin'}))
    for number in config['skip_pages']:
        for line in all_lines.get(number, []):
            line.kind = 'margin'
    # Front matter (title, copyright, dedication pages) has no footnotes: small
    # print at the foot of a copyright page is text, not a note.
    first = first_chapter_page(all_lines, geometry)
    front_until = config['front_matter_until'] if config['front_matter_until'] is not None else ((first - 1) if first else 0)
    for number, lines in all_lines.items():
        if number <= front_until:
            for line in lines:
                if line.kind == 'note':
                    line.kind = 'body'
    notes = collect_notes(all_lines, geometry, report)
    vocab = Vocabulary(all_lines, reference)
    images = {}
    first_page = min(pages) if pages else 1
    cover = pages.get(first_page)
    if cover and cover['images'] and sum(len(r.text.strip()) for r in cover['runs']) < 300 \
            and sum(i['width'] * i['height'] for i in cover['images']) >= cover['width'] * cover['height'] * 0.5:
        report.set('cover_page', first_page)
    for number, info in pages.items():
        area = info['width'] * info['height']
        text_chars = sum(len(l.text.strip()) for l in all_lines.get(number, [])
                         if l.kind not in ('margin', 'note') and l.size == geometry['body_size'])
        for image in info['images']:
            share = image['width'] * image['height'] / area
            name = Path(image['src']).name
            inside = geometry['text_top'] - geometry['pitch'] < image['top'] < geometry['text_bottom']
            if number in config['skip_pages']:
                reason = 'skipped page'
            elif number == report.data.get('cover_page'):
                reason = 'cover: render the page with pdftoppm for the EPUB cover image'
            elif share < config['min_figure_area']:
                reason = 'too small (ornament, logo)'
            elif share > 0.85 and text_chars > 800:
                reason = 'page background behind running text'
            elif not inside and share < 0.25:
                reason = 'outside the text block'
            else:
                images.setdefault(number, []).append(image)
                report.add('figures', {'page': number, 'src': name, 'share': round(share, 3)})
                continue
            report.add('images_skipped', {'page': number, 'src': name, 'share': round(share, 3), 'reason': reason})
    outline_active = anchor_outline(outline, all_lines, config, report)
    blocks = build_blocks(all_lines, geometry, config, report, images, outline_active)
    demote_display_headings(blocks, all_lines, geometry, report, title, outline_active)
    media_rel = Path(os.path.relpath(args.media.resolve(), args.out.resolve().parent)).as_posix() if args.media else None
    html_text = render(blocks, notes, all_lines, geometry, vocab, config, report, media_rel, pages)

    args.out.write_text(html_text, encoding='utf-8')
    report_path = args.report or args.out.with_suffix('.report.json')
    data = dict(report.data)
    data['hyphen_kept'] = collections.Counter(data.get('hyphen_kept', [])).most_common()
    data['hyphen_guessed'] = sorted(set(data.get('hyphen_guessed', [])))
    data['hyphen_by_rule'] = len(data.get('hyphen_by_rule', []))
    data['marginal_numbers'] = len(data.get('marginal_numbers', []))
    data['notes'] = {'found': len(notes), 'linked': sum(1 for n in notes if n.note_id)}
    report_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')

    print(f'{args.out}: {len([b for b in blocks if b.kind == "heading"])} headings, '
          f'{len([b for b in blocks if b.kind != "heading"])} blocks, {data["notes"]["linked"]}/{len(notes)} notes linked')
    print(f'report: {report_path}')
    mapped = set(config['font_maps'])
    for font in data.get('suspect_fonts', []):
        hint = 'already in font_maps: check the map' if any(k in font['font'] for k in mapped) else 'add a font_maps entry'
        print(f'  SUSPECT FONT {font["font"]}: {font["garbled"]}/{font["tokens"]} words look garbled, '
              f'e.g. {[s["text"] for s in font["samples"][:3]]} -> {hint}')
    mixed = data.get('mixed_script_words', [])
    if mixed:
        print(f'  {len(mixed)} word(s) mix Latin and Greek letters (a wrong font map?), '
              f'e.g. {[m["word"] for m in mixed[:4]]}; see mixed_script_words')
    if data.get('spaces_repaired'):
        print(f'  {len(data["spaces_repaired"])} stray space(s) inside words repaired; see spaces_repaired')
    if geometry['note_size'] is None:
        print('  NO NOTE AREA FOUND: no smaller type below the text; if the book has footnotes, '
              'set note_size in the config (see size_histogram in the report)')
    problems = data.get('note_problems', [])
    if problems:
        print(f'  {len(problems)} note problem(s); first: {problems[0]}')
    if data['hyphen_guessed']:
        print(f'  {len(data["hyphen_guessed"])} line-end hyphen(s) removed without evidence; review hyphen_guessed')


if __name__ == '__main__':
    main()
