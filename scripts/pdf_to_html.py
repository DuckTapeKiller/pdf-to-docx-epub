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
BIBLIOGRAPHY = re.compile(r'bibliogra|works cited|obras citadas|references|referencias|fuentes|sources|literatur', re.I)
HANGING = re.compile(r'abbreviations|abbrevations|abreviaturas|siglas|glossary|glosario', re.I)
NAME_LINE = re.compile(r"^(?:[A-ZÀ-Ý][\w'’.-]*|[A-Z]\.|and|&|y|e|de|del|da|di|du|van|von|der|den|la|le|of|Jr\.?|Sr\.?)"
                       r"(?:,?\s+(?:[A-ZÀ-Ý][\w'’.-]*|[A-Z]\.|and|&|y|e|de|del|da|di|du|van|von|der|den|la|le|of|Jr\.?|Sr\.?)){1,7}$")
INDEX = re.compile(r'^\s*(índice|indice|index)\s+(onomástico|analítico|temático|conceptual|de nombres|general\b(?! de))|^\s*index\b|^\s*(register|indice dei nomi)\b', re.I)

DEFAULT_CONFIG = {
    'lang': 'en',
    'title': None,
    'skip_pages': [],            # PDF page numbers to ignore completely
    'toc_pages': None,           # printed contents pages (auto-detected when null)
    'front_matter_until': None,  # last PDF page of front matter (default: before first h1)
    'drop_fonts': [],            # font family substrings whose text is never content
    'font_maps': {},             # {"FAMILY": {"garbled char": "real char", ...}}
    'smallcaps_fonts': [],       # font family substrings set in small caps
    'heading_fonts': {},         # {"FAMILY@SIZE" or "FAMILY": level}
    'chapter_author_after_h1': False,
    'body_size': None, 'note_size': None, 'quote_sizes': None,
    'header_band': None, 'footer_band': None,  # y limits in pdftohtml units
    'superscript_ratio': 0.75,   # max size ratio of a note marker to its line
    'keep_hyphen_words': [],     # compounds whose line-end hyphen is real
    'hanging_sections': [],      # extra heading texts whose entries use hanging indents
    'min_figure_area': 0.015,    # smallest raster image kept as a figure (share of the page)
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

    @property
    def top(self) -> float:
        return min(r.top for r in self.main_runs())

    @property
    def bottom(self) -> float:
        return max(r.bottom for r in self.main_runs())

    @property
    def left(self) -> float:
        return min(r.left for r in self.runs if r.text.strip()) if any(r.text.strip() for r in self.runs) else self.runs[0].left

    @property
    def right(self) -> float:
        return max(r.right for r in self.runs)

    @property
    def size(self) -> int:
        weights = collections.Counter()
        for r in self.runs:
            if not r.sup:
                weights[r.size] += len(r.text.strip())
        return weights.most_common(1)[0][0] if weights else self.runs[0].size

    def main_runs(self) -> list[Run]:
        main = [r for r in self.runs if not r.sup and not r.dropcap and r.text.strip()]
        return main or self.runs

    @property
    def text(self) -> str:
        return ''.join(r.text for r in self.runs)


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
                info['images'].append({k: node.get(k) for k in ('top', 'left', 'width', 'height', 'src')})
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
                for piece, italic, bold in pieces:
                    piece_text = ''.join(mapping.get(c, c) for c in piece) if mapping else piece
                    piece_text = clean_text(piece_text)
                    share = len(piece) / total
                    run = Run(piece_text, node.get('font'), size, family, italic, bold,
                              left + width * offset / total, top, width * share, height)
                    run.smallcaps = any(s in family for s in config['smallcaps_fonts']) or bool(
                        re.search(r'(SC\d*|SmallCaps|Caps)(\W|$)', family.split('+')[-1]))
                    offset += len(piece)
                    info['runs'].append(run)
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
    return fonts, pages


# --------------------------------------------------------------------------
# Geometry

def group_lines(page_no: int, runs: list[Run]) -> list[Line]:
    """Group runs into lines; drop caps join the first line they stand beside."""
    heights = sorted(r.height for r in runs if r.text.strip())
    usual = heights[len(heights) // 2] if heights else 0
    dropcaps = [r for r in runs if usual and r.height > usual * 2.2 and len(r.text.strip()) == 1
                and r.text.strip().isalpha()]
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
            top = min(r.top for r in main)
            bottom = max(r.bottom for r in main)
            overlap = min(bottom, run.bottom) - max(top, run.top)
            if overlap >= 0.5 * min(run.height, bottom - top):
                best = line
        if best is None:
            lines.append(Line(page_no, [run]))
        else:
            best.runs.append(run)
    for line in lines:
        line.runs.sort(key=lambda r: r.left)
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
        if len(left) >= 8 and len(right) >= 8 \
                and statistics.mean(len(r.text.strip()) for r in right) >= 8 \
                and statistics.mean(len(r.text.strip()) for r in left) >= 8:
            score = (len(crossing), abs(x - width / 2))
            if gutter is None or score < gutter[0]:
                gutter = (score, x)
    if gutter is None:
        return group_lines(page_no, runs)
    x = gutter[1]
    spanning = group_lines(page_no, [r for r in runs if r.left < x < r.right])
    left = group_lines(page_no, [r for r in runs if r.right <= x])
    right = group_lines(page_no, [r for r in runs if r.left >= x and not (r.left < x < r.right)])
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
                families[run.family] += len(run.text.strip())
    body_family = families.most_common(1)[0][0] if families else ''

    all_lines = {}
    for number, info in pages.items():
        all_lines[number] = page_lines(number, info['runs'], info['width'])

    body_lines = [l for lines in all_lines.values() for l in lines if l.size == body_size and len(l.text.strip()) > 20]
    lefts = collections.defaultdict(list)
    for l in body_lines:
        lefts[l.page % 2].append(round(l.left))
    margin = {parity: mode(v) for parity, v in lefts.items()}
    right = statistics.median(sorted(round(l.right) for l in body_lines)[len(body_lines) // 2:]) if body_lines else 0
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
        'quote_sizes': quote_sizes, 'margin': margin, 'right': right, 'pitch': pitch,
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


def reference_words(pdf: Path, first: int | None, last: int | None) -> collections.Counter:
    """Words as pdftotext reads them: an independent word segmentation."""
    command = ['pdftotext', '-enc', 'UTF-8']
    if first:
        command += ['-f', str(first)]
    if last:
        command += ['-l', str(last)]
    result = subprocess.run(command + [str(pdf), '-'], capture_output=True, text=True)
    text = unicodedata.normalize('NFC', clean_text(result.stdout))
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
            text = ''.join(r.text for r in line.runs)
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
    for number, lines in all_lines.items():
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
            if line.bottom < geometry['text_top'] or line.top > geometry['text_bottom']:
                line.kind = 'margin'
                removed[re.sub(r'\d+', '#', line.text.strip().lower())] += 1
                continue
        # Foot of the page: note-size lines below the last body-size line.  A
        # block quotation in the same size can end the page, so the notes begin
        # after the separator space (the largest gap) or at a numbered line.
        content = [l for l in lines if l.kind != 'margin']
        body_idx = [i for i, l in enumerate(content) if l.size == body_size]
        if note_size is not None:
            if not body_idx:  # a page of notes only (continuations)
                for line in content:
                    if line.size <= note_size:
                        line.kind = 'note'
                continue
            last = content[body_idx[-1]]
            candidates = [l for l in content[body_idx[-1] + 1:] if l.size <= note_size]
            if not candidates:
                continue
            gaps, previous = [], last
            for line in candidates:
                gaps.append(line.top - previous.bottom)
                previous = line
            widest = max(range(len(gaps)), key=lambda i: gaps[i])
            numbered = next((i for i, l in enumerate(candidates) if note_start(l)), None)
            note_pitch = geometry['note_pitch']
            if numbered is not None:
                start = widest if widest <= numbered and gaps[widest] >= note_pitch * 1.3 else numbered
            elif gaps[widest] >= note_pitch * 2:
                start = widest  # a note continued from the previous page, after the separator
            else:
                continue        # e.g. a block quotation ending the page
            for line in candidates[start:]:
                line.kind = 'note'
    report.set('removed_margin_text', [{'text': t, 'pages': n} for t, n in removed.most_common(40)])


def is_centered(line: Line, geometry: dict) -> bool:
    margin = geometry['margin'].get(line.page % 2, 0)
    left_gap = line.left - margin
    right_gap = geometry['right'] - line.right
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
    for number in sorted(all_lines):
        zone = [l for l in all_lines[number] if l.kind == 'note']
        for index, line in enumerate(zone):
            label = note_start(line)
            plausible = label is not None and (start_left is None or abs(line.left - start_left) <= geometry['indent'] * 0.6
                                              or line.runs[0].sup)
            if label and label.isdigit() and open_note and open_note.label.isdigit():
                expected = int(open_note.label) + 1
                if int(label) not in (expected, 1) and abs(int(label) - expected) > 3:
                    plausible = False
            if plausible:
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
        self.words = collections.Counter()
        self.hyphenated = collections.Counter()
        for lines in all_lines.values():
            for line in lines:
                text = line.text
                for word in re.findall(r'\w+(?:-\w+)+', text):
                    self.hyphenated[word.lower()] += 1
                for word in re.findall(r'\w+', text):
                    self.words[word.lower()] += 1


def join_decision(left_word: str, right_word: str, vocab: Vocabulary, config: dict, report: Report) -> str:
    """How to join "left-" + "right" at a line break: '' (join) or '-' (keep)."""
    joined = (left_word + right_word).lower()
    compound = f'{left_word}-{right_word}'.lower()
    if right_word[:1].isupper() or right_word[:1].isdigit() or left_word[-1:].isdigit():
        return '-'
    if compound in {w.lower() for w in config['keep_hyphen_words']}:
        return '-'
    if vocab.hyphenated[compound] > vocab.words[joined]:
        report.add('hyphen_kept', compound)
        return '-'
    if vocab.words[joined] == 0 and vocab.hyphenated[compound] == 0:
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
    out, state = [], {'i': False, 'b': False, 'sc': False}

    def close_all():
        for tag in ('sc', 'b', 'i'):
            if state[tag]:
                out.append('</span>' if tag == 'sc' else f'</{tag}>')
                state[tag] = False

    for index, run in enumerate(runs):
        text = html.escape(run.text, quote=False)
        if in_heading and run.smallcaps:
            text = upper_roman(text, ''.join(r.text for r in runs[:index]))
        if getattr(run, 'noteref', None):
            close_all()
            out.append(run.noteref)
            continue
        if run.sup:
            close_all()
            out.append(f'<sup>{text}</sup>')
            continue
        smallcaps = run.smallcaps and not in_heading
        if smallcaps and len(run.text.strip()) <= 2 and 0 < index < len(runs) - 1 \
                and runs[index - 1].text[-1:].isalpha() and runs[index + 1].text[:1].isalpha():
            smallcaps = False  # a glyph borrowed from the small-caps font inside a word (Sigurður)
        want = {'i': run.italic, 'b': run.bold, 'sc': smallcaps}
        if not run.text.strip():
            out.append(text)
            continue
        for tag in ('sc', 'b', 'i'):
            if state[tag] and not want[tag]:
                close_all()
                break
        for tag in ('i', 'b', 'sc'):
            if want[tag] and not state[tag]:
                out.append('<span class="smallcaps">' if tag == 'sc' else f'<{tag}>')
                state[tag] = True
        out.append(text)
    close_all()
    result = ''.join(out)
    result = re.sub(r'</i>(\s*)<i>', r'\1', result)
    result = re.sub(r'</b>(\s*)<b>', r'\1', result)
    result = re.sub(r'</span>(\s*)<span class="smallcaps">', r'\1', result)
    result = re.sub(r'\s+', ' ', result)
    result = re.sub(r'\s+(<a [^>]*epub:type="noteref")', r'\1', result)
    return result.strip()


def assemble(lines: list[Line], vocab: Vocabulary, config: dict, report: Report) -> list[Run]:
    """Join lines into one run list with spaces and hyphenation handled."""
    runs: list[Run] = []
    for index, line in enumerate(lines):
        line_runs = [copy.copy(r) for r in line.runs if r.text]
        # Spaces pdftohtml dropped at a font change ("hijiras" + "of"): a word
        # space measures 2-5 units, touching runs 0-1.  pdftotext breaks ties.
        for a, b in zip(line_runs, line_runs[1:]):
            if b.sup or getattr(b, 'noteref', None) or a.dropcap or a.text.endswith(' ') or b.text.startswith(' '):
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
                if tail.endswith(('-', '­')) and not tail.endswith('--') and not getattr(runs[-1], 'noteref', None):
                    left_word = re.search(r'(\w+)[-­]$', tail)
                    right_word = re.match(r'\s*(\w+)', nxt.text)
                    if left_word and right_word:
                        keep = '-' if tail.endswith('-') and join_decision(left_word.group(1), right_word.group(1), vocab, config, report) else ''
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
        letters[r.family] += sum(c.isalpha() for c in r.text)
    body_share = letters[geometry['body_family']] / max(sum(letters.values()), 1)
    if body_share < 0.4 and not all(r.italic for r in runs):
        return 2  # capitals may come from the text face, the rest from small caps
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
                and content[end + 1].top - content[end].bottom < pitch * 0.9:
            end += 1
        before = content[index].top - content[index - 1].bottom if index else None
        after = content[end + 1].top - content[end].bottom if end + 1 < len(content) else None
        text = ' '.join(l.text.strip() for l in content[index:end + 1])
        needed_before = pitch * (1.6 if level == 3 else 1.2)
        if ((before is None or before > needed_before) and (after is None or after > pitch * 0.9)
                and end - index < 3 and len(text) < 200 and not text.endswith(('.', ',', ';'))):
            for k in range(index, end + 1):
                found[k] = level
        index = end + 1
    return found


def first_chapter_page(all_lines, geometry) -> int | None:
    """First page with large type followed by real body text."""
    body_chars = collections.Counter()
    for number, lines in all_lines.items():
        for line in lines:
            if line.kind not in ('margin', 'note') and line.size == geometry['body_size']:
                body_chars[number] += len(line.text.strip())
    for number in sorted(all_lines):
        big = any(l.kind not in ('margin', 'note') and l.size >= geometry['body_size'] * 1.3 for l in all_lines[number])
        if big and body_chars[number] + body_chars[number + 1] >= 400:
            return number
    return None


def alignment(line: Line, geometry: dict) -> str:
    margin = geometry['margin'].get(line.page % 2, geometry['margin'].get(0, 0))
    left_gap, right_gap = line.left - margin, geometry['right'] - line.right
    indent = geometry['indent']
    if left_gap > indent * 1.5 and abs(left_gap - right_gap) < indent * 1.2:
        return 'centered'
    if right_gap < indent * 0.5 and left_gap > (geometry['right'] - margin) * 0.33:
        return 'right'
    return 'left'


def front_blocks(all_lines, numbers, geometry) -> list[Block]:
    """Front matter keeps its layout: one block per gap-separated group."""
    blocks = []
    for number in numbers:
        current = None
        previous = None
        for line in [l for l in all_lines[number] if l.kind not in ('margin', 'note') and l.text.strip()]:
            align = alignment(line, geometry)
            gap = line.top - previous.bottom if previous is not None else None
            if current is None or gap > (previous.bottom - previous.top) * 0.9 or align != current.css:
                current = Block('front', [line], css=align, page=number)
                blocks.append(current)
            else:
                current.lines.append(line)
            previous = line
    return blocks


def build_blocks(all_lines, geometry, config, report: Report, images: dict | None = None):
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
        families = collections.Counter(r.family for r in line.runs if r.text.strip() and not r.sup)
        for key, level in heading_fonts.items():
            family, _, size = key.partition('@')
            if any(family in f for f in families) and (not size or int(size) == line.size):
                return int(level)
        if line.size >= body_size * 1.3:
            return 1
        if line.size >= body_size * 1.08:
            return 2
        return 0

    headings: list[tuple[int, str]] = []  # open headings: (level, text)

    def mode() -> str:
        for _level, text in headings:
            if BIBLIOGRAPHY.search(text) or HANGING.search(text) \
                    or any(h.lower() in text.lower() for h in config['hanging_sections']):
                return 'bibliography'
            if INDEX.search(text):
                return 'index'
        return 'normal'

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
            if mode() == 'index' and re.fullmatch(r'\s*[A-ZÁÉÍÓÚÑ]\s*', line.text):
                flush()
                current = Block('p', [line], page=number, css='index-letter', style='Index Letter')
                previous_line = line
                continue
            level = heading_level(line) or sections.get(index, 0)
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
                if current is not None and current.kind == 'heading' and current.page == number \
                        and gap_before is not None and gap_before < pitch * 2.2 \
                        and (level == current.level or (label_then_title and not current.label)):
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
                headings[:] = [h for h in headings if h[0] < level] + [(level, line.text)]
                previous_line = line
                continue

            section = mode()
            if section in ('bibliography', 'index') and line.size not in quote_sizes:
                # hanging indents: a flush line starts an entry, a deeper one continues it
                offset = line.left - (column_margin[line.column] if section == 'index' else margin)
                in_entry = current is not None and current.css in ('bibliography', 'index', 'index-sub')
                continues = in_entry and offset > indent * (1.5 if section == 'index' else 0.4)
                if section == 'index' and in_entry and (
                        previous_line.text.rstrip().endswith('-')
                        or (previous_line.text.rstrip().endswith((',', '–')) and re.match(r'\s*\d', line.text))):
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

            kind = 'quote' if line.size in quote_sizes else 'p'
            if kind == 'p' and is_centered(line, geometry) and (gap_before is None or gap_before > pitch * 0.9):
                kind = 'centered'
            new_page = previous_line is None or previous_line.page != line.page
            indented = indent * 0.5 < line.left - margin < indent * 3 and not line.beside_dropcap
            if current is not None and current.kind == kind == 'p':
                gap = None if new_page else line.top - previous_line.bottom
                if indented or (gap is not None and gap > pitch * 0.9):
                    previous_kind = current.kind
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
    return front_blocks(all_lines, front_pages, geometry) + blocks


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


def demote_display_headings(blocks: list[Block], all_lines, geometry, report: Report) -> None:
    """Large type on title, half-title and series pages is not a chapter."""
    body_chars = collections.Counter()
    for number, lines in all_lines.items():
        for line in lines:
            if line.kind not in ('margin', 'note') and line.size == geometry['body_size']:
                body_chars[number] += len(line.text.strip())
    for block in blocks:
        if block.kind == 'heading' and block.level == 1:
            following = body_chars[block.page] + body_chars[block.page + 1]
            if following < 400:
                block.kind, block.level = 'centered', 0
                report.add('display_text', {'page': block.page, 'text': ' '.join(l.text.strip() for l in block.lines)[:80]})


def toc_line(line: Line, indent: float) -> bool:
    """A printed contents entry: dot leaders, or a page number standing apart.

    Index entries also end in page numbers, but those follow a comma.
    """
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
    for number, lines in all_lines.items():
        content = [l for l in lines if l.kind != 'margin' and l.text.strip()]
        entries = sum(1 for l in content if toc_line(l, geometry['indent']))
        if entries >= 4 and entries >= len(content) * 0.3:
            found.add(number)
    return found


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
                paragraphs.append(f'<p>{runs_to_html(runs)}</p>')
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
            alt = html.escape(re.sub(r'<[^>]+>', '', caption)[:150] or 'Figure', quote=True)
            figcaption = f'<figcaption>{caption}</figcaption>' if caption else ''
            out.append(f'<figure><img src="{src}" alt="{alt}" />{figcaption}</figure>')
            continue
        if block.kind == 'front':
            pieces, group = [], []
            for line in block.lines:
                group.append(line)
                short = geometry['right'] - line.right > geometry['indent'] * 2
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
        body = runs_to_html(runs)
        if not body:
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
        xml_path = run_pdftohtml(args.pdf, Path(tmp), args.media, args.first_page, args.last_page)
        fonts, pages = load_layout(xml_path, config, report)
    reference = reference_words(args.pdf, args.first_page, args.last_page)
    repair_spaces(pages, reference, report)
    garbled_fonts(pages, report)
    empty = [n for n, info in pages.items() if not info['runs']]
    scanned = [n for n in empty if pages[n]['images']]
    report.set('pages', {'total': len(pages), 'without_text': empty, 'image_only': scanned})
    if len(scanned) > len(pages) * 0.5:
        sys.exit('Most pages have no text layer: run OCR first (e.g. ocrmypdf -l spa+eng in.pdf ocr.pdf).')

    geometry, all_lines = analyse(pages, config, report)
    report.set('two_column_pages', sorted(n for n, lines in all_lines.items() if any(l.column for l in lines)))
    classify(all_lines, pages, geometry, config, report)
    mixed_script_words(all_lines, report)
    toc_pages = detect_toc_pages(all_lines, config, geometry)
    report.set('toc_pages_skipped', sorted(toc_pages))
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
    for number, info in pages.items():
        area = info['width'] * info['height']
        for image in info['images']:
            size = float(image['width']) * float(image['height'])
            inside = geometry['text_top'] - geometry['pitch'] < float(image['top']) < geometry['text_bottom']
            name = Path(image['src']).name
            if number in config['skip_pages'] or size < area * config['min_figure_area'] or not inside:
                report.add('images_skipped', {'page': number, 'src': name, 'share': round(size / area, 3)})
                continue
            images.setdefault(number, []).append(image)
            report.add('figures', {'page': number, 'src': name})
    blocks = build_blocks(all_lines, geometry, config, report, images)
    demote_display_headings(blocks, all_lines, geometry, report)
    media_rel = Path(os.path.relpath(args.media.resolve(), args.out.resolve().parent)).as_posix() if args.media else None
    html_text = render(blocks, notes, all_lines, geometry, vocab, config, report, media_rel, pages)

    args.out.write_text(html_text, encoding='utf-8')
    report_path = args.report or args.out.with_suffix('.report.json')
    data = dict(report.data)
    data['hyphen_kept'] = collections.Counter(data.get('hyphen_kept', [])).most_common()
    data['hyphen_guessed'] = sorted(set(data.get('hyphen_guessed', [])))
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
