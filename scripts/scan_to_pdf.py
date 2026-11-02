#!/usr/bin/env python3
"""Rebuild a scanned book as a PDF that pdf_to_html.py can read accurately.

A scan gives pdf_to_html.py nothing to work with: one font, no type sizes, no italics,
note markers misread as junk, and often a poor text layer. This script replaces all of
that, working from the page images:

  split    one image per book page, cutting two-page spreads at the blank gutter; straighten
  read     two independent engines: Apple Vision (macOS) and Tesseract
  vote     word by word; the PDF's old text layer and the spell checker settle disagreements
  measure  from the pixels: note rules, raised markers, type sizes, italics, figures,
           rules standing for a repeated author, and any ink no engine read
  write    text_layer.pdf (hidden text and figures, for pdf_to_html.py), a searchable PDF
           with the page images, a suggested config, and sheets of what is still in doubt

What the vote cannot settle goes on review sheets: small crops of the page for the agent
to read. Answers are kept in decisions.json, so the script can be run again at no cost.
Every stage caches its results in --work.

Standard library only, plus magick, pdfimages, pdftotext and tesseract; on macOS swiftc
builds the helpers for Vision and the spell checker. Without them it falls back to
Tesseract and the old text layer, and says so.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import csv
import difflib
import html
import json
import math
import os
import random
import re
import shutil
import statistics as st
import subprocess
import sys
import zlib
from functools import partial
from pathlib import Path

HERE = Path(__file__).resolve().parent

# language: Tesseract, Vision, macOS spell checker
LANGUAGES = {
    'es': ('spa', 'es-ES', 'es'),
    'en': ('eng', 'en-GB', 'en_GB'),
    'pt': ('por', 'pt-BR', 'pt_BR'),
    'fr': ('fra', 'fr-FR', 'fr'),
    'de': ('deu', 'de-DE', 'de'),
    'it': ('ita', 'it-IT', 'it'),
}
LABEL_FONTS = ('/System/Library/Fonts/Supplemental/Arial.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
POPCOUNT = [bin(i).count('1') for i in range(256)]

# Every distance below is written for a body x-height of 15 pixels (about 200 dpi) and
# scaled by the measured one, so a 400 dpi scan behaves like a 200 dpi scan.
U = 1.0


def u(n: float) -> float:
    return n * U


def set_unit(unit: float) -> None:
    global U
    U = unit


def pool_map(fn, items, unit: float) -> list:
    with cf.ProcessPoolExecutor(max_workers=os.cpu_count() or 4, initializer=set_unit, initargs=(unit,)) as ex:
        return list(ex.map(fn, items, chunksize=4))


# --------------------------------------------------------------------------
# Pixels

def page_file(work: Path, stem: str) -> Path:
    png = work / 'pages' / f'{stem}.png'
    return png if png.exists() else png.with_suffix('.jpg')


def binarize_args(bilevel: bool) -> list[str]:
    return ['-colorspace', 'Gray'] + (['-threshold', '50%'] if bilevel else ['-auto-threshold', 'OTSU'])


def parse_pbm(data: bytes) -> tuple[int, int, bytes]:
    i, tokens = 2, []
    while len(tokens) < 2:
        while data[i] in b' \t\r\n':
            i += 1
        if data[i] == 35:
            while data[i] != 10:
                i += 1
            continue
        j = i
        while data[j] not in b' \t\r\n':
            j += 1
        tokens.append(int(data[i:j]))
        i = j
    return tokens[0], tokens[1], data[i + 1:]


def load_bits(path: Path) -> tuple[int, int, bytes]:
    return parse_pbm(path.read_bytes())


def bit_rows(path: Path) -> tuple[int, int, list[str]]:
    w, h, d = load_bits(path)
    row_bytes = (w + 7) // 8
    rows = [format(int.from_bytes(d[y * row_bytes:(y + 1) * row_bytes], 'big'), f'0{row_bytes * 8}b')[:w] for y in range(h)]
    return w, h, rows


def components(rows: list[str]) -> list[list[int]]:
    """8-connected components as [x0, y0, x1, y1, pixels], joining runs from row to row."""
    parent: list[int] = []
    box: list[list[int]] = []

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    previous: list[tuple[int, int, int]] = []
    runs = re.compile('1+')
    for y, row in enumerate(rows):
        current = []
        if '1' in row:
            for m in runs.finditer(row):
                a, b = m.start(), m.end()
                index = len(parent)
                parent.append(index)
                box.append([a, y, b, y + 1, b - a])
                current.append((a, b, index))
            k = 0
            for a, b, index in current:
                while k < len(previous) and previous[k][1] < a:
                    k += 1
                j = k
                while j < len(previous) and previous[j][0] <= b:
                    ra, rb = find(index), find(previous[j][2])
                    if ra != rb:
                        parent[rb] = ra
                        p, q = box[ra], box[rb]
                        p[0] = min(p[0], q[0]); p[1] = min(p[1], q[1])
                        p[2] = max(p[2], q[2]); p[3] = max(p[3], q[3]); p[4] += q[4]
                    j += 1
        previous = current
    return [box[i] for i in range(len(parent)) if find(i) == i]


# --------------------------------------------------------------------------
# Split: one image per book page

def column_profile(job: tuple[str, bool]) -> tuple[int, int, list[int], int]:
    png, bilevel = job
    data = subprocess.run(['magick', png] + binarize_args(bilevel) + ['pbm:-'], capture_output=True, check=True).stdout
    w, h, d = parse_pbm(data)
    row_bytes = (w + 7) // 8
    cols = [0] * w
    black = 0
    for y in range(h):
        row = d[y * row_bytes:(y + 1) * row_bytes]
        if not any(row):
            continue
        for x, b in enumerate(row):
            if b:
                black += POPCOUNT[b]
                for k in range(8):
                    if b & (128 >> k):
                        px = x * 8 + k
                        if px < w:
                            cols[px] += 1
    return w, h, cols, black


def extract_images(pdf: Path, work: Path) -> tuple[list[Path], dict[int, bool], dict[int, int]]:
    """One image per PDF page: the scan itself when it can be taken out whole."""
    img = work / 'img'
    img.mkdir(parents=True, exist_ok=True)
    pages = int(re.search(r'Pages:\s+(\d+)', subprocess.run(['pdfinfo', str(pdf)], capture_output=True, text=True,
                                                            errors='replace').stdout)[1])
    listing = subprocess.run(['pdfimages', '-list', str(pdf)], capture_output=True, text=True, check=True).stdout
    per_page = collections.Counter()
    bilevel: dict[int, bool] = {}
    dpi: dict[int, int] = {}
    for line in listing.splitlines()[2:]:
        f = line.split()
        if len(f) < 13 or f[2] != 'image':
            continue
        page = int(f[0])
        per_page[page] += 1
        bilevel[page] = f[7] == '1'
        dpi[page] = int(f[12])
    if all(per_page[p] == 1 for p in range(1, pages + 1)):
        subprocess.run(['pdfimages', '-j', '-png', str(pdf), str(img / 'p')], check=True, capture_output=True)     # JPEG stays JPEG
    else:
        # strips, overlays or vector pages: render instead
        subprocess.run(['pdftoppm', '-png', '-r', '300', str(pdf), str(img / 'p')], check=True, capture_output=True)
        bilevel = {p: False for p in range(1, pages + 1)}
        dpi = {p: 300 for p in range(1, pages + 1)}
    return sorted(img.glob('p-*'), key=lambda p: int(p.stem.split('-')[1])), bilevel, dpi


def measure_unit(paths: list[Path]) -> float:
    """Body x-height over 15: the commonest glyph height is the height of small letters."""
    heights: collections.Counter = collections.Counter()
    for path in paths:
        _, _, rows = bit_rows(path)
        for c in components(rows):
            H, W = c[3] - c[1], c[2] - c[0]
            if 5 <= H <= 120 and W <= H * 2.5:
                heights[H] += 1
    if not heights:
        return 1.0
    best = max(heights, key=lambda H: heights[H - 1] + 2 * heights[H] + heights[H + 1])
    return max(.4, min(6.0, best / 15))


def measure_skew(path: Path) -> float:
    """Degrees that make the glyph bottoms line up; positive means the text falls to the right."""
    _, _, rows = bit_rows(path)
    points = [((c[0] + c[2]) / 2, c[3]) for c in components(rows)
              if u(9) <= c[3] - c[1] <= u(30) and c[2] - c[0] <= u(40)]
    if len(points) < 60:
        return 0.0
    size = max(u(2), 1)

    def score(angle: float) -> int:
        t = math.tan(math.radians(angle))
        bins = collections.Counter(int((y - x * t) / size) for x, y in points)
        return sum(v * v for v in bins.values())
    best = max((a / 10 for a in range(-40, 41)), key=score)
    return max((best + a / 100 for a in range(-10, 11)), key=score)


def straighten(job: tuple[str, dict]) -> tuple[str, float]:
    work, m = Path(job[0]), job[1]
    page = page_file(work, m['stem'])
    bw = work / 'bw' / f"{m['stem']}.pbm"
    angle = measure_skew(bw)
    if abs(angle) >= .08:
        subprocess.run(['magick', str(page), '-background', 'white', '-virtual-pixel', 'white', '-interpolate', 'Catrom',
                        '-distort', 'SRT', f'{-angle:.3f}'] + (['-colorspace', 'Gray'] if m['bilevel'] else ['-quality', '92']) + [str(page)], check=True)
        subprocess.run(['magick', str(page)] + binarize_args(m['bilevel']) + [f'pbm:{bw}'], check=True)
    return m['stem'], round(angle, 2)


def stage_split(pdf: Path, work: Path, only: set[int]) -> tuple[list[dict], float]:
    """With --only, just those PDF pages are prepared, so a trial costs what the trial reads."""
    path = work / 'manifest.json'
    stamp = [pdf.stat().st_size, int(pdf.stat().st_mtime)]
    if path.exists():
        saved = json.loads(path.read_text())
        if saved['source'] != stamp:
            sys.exit(f'{work} holds the work of another PDF: use a new --work folder')
        if set(saved.get('only') or []) == only or not saved.get('only'):
            return saved['pages'], saved['unit']
    images, bilevel, _ = extract_images(pdf, work)
    if only:
        images = {i: p for i, p in enumerate(images, 1) if i in only}
    else:
        images = dict(enumerate(images, 1))
    (work / 'pages').mkdir(exist_ok=True)
    (work / 'bw').mkdir(exist_ok=True)
    jobs = [(str(p), bilevel.get(i, False)) for i, p in images.items()]
    with cf.ProcessPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        profiles = list(ex.map(column_profile, jobs, chunksize=4))
    info, mids = [], []
    for w, h, cols, black in profiles:
        threshold = max(2, h * .004)
        band_start, best = None, (0, None)
        for x in range(int(w * .3), int(w * .7)):
            if cols[x] <= threshold:
                if band_start is None:
                    band_start = x
                if x - band_start > best[0]:
                    best = (x - band_start, (band_start + x) // 2)
            else:
                band_start = None
        left, right = sum(cols[:w // 2]), sum(cols[w // 2:])
        info.append(dict(w=w, h=h, band=best[0], mid=best[1], left=left, right=right))
        if w > h * 1.1 and best[1] and left > h * .5 and right > h * .5 and best[0] >= w * .015:
            mids.append(best[1] / w)
    typical = st.median(mids) if mids else .5
    manifest = []

    cuts = []
    for (i, png), inf in zip(images.items(), info):
        w, h = inf['w'], inf['h']
        both = inf['left'] > h * .5 and inf['right'] > h * .5
        if not (w > h * 1.1 and (not both or inf['band'] >= w * .015)):
            cuts.append((i, png, 'S', 0, w, inf))
            continue
        mid = inf['mid'] if both and inf['mid'] else int(typical * w)
        for side, x0, cw, ink in (('L', 0, mid, inf['left']), ('R', mid, w - mid, inf['right'])):
            if ink >= w * h * 2e-5:     # otherwise a blank half
                cuts.append((i, png, side, x0, cw, inf))
    manifest = [dict(stem=f'{i:04d}{side}', pdf=i, side=side, w=cw, h=inf['h'], x0=x0, src_w=inf['w'], bilevel=bilevel.get(i, False))
                for i, png, side, x0, cw, inf in cuts]

    def cut(job: tuple) -> None:
        i, png, side, x0, cw, inf = job
        stem = f'{i:04d}{side}'
        out = work / 'pages' / f'{stem}{png.suffix}'
        if side == 'S':
            shutil.copyfile(png, out)
        else:
            subprocess.run(['magick', str(png), '-crop', f"{cw}x{inf['h']}+{x0}+0", '+repage', '-quality', '92', str(out)], check=True)
        subprocess.run(['magick', str(out)] + binarize_args(bilevel.get(i, False)) + [f"pbm:{work / 'bw' / f'{stem}.pbm'}"], check=True)
    with cf.ThreadPoolExecutor(os.cpu_count() or 4) as ex:
        list(ex.map(cut, cuts))
    # the sample for the x-height: pages spread evenly over the book
    sample = manifest[::max(1, len(manifest) // 14)][:15]
    unit = measure_unit([work / 'bw' / f"{m['stem']}.pbm" for m in sample])
    angles = dict(pool_map(straighten, [(str(work), m) for m in manifest], unit))
    for m in manifest:
        m['skew'] = angles[m['stem']]
    path.write_text(json.dumps(dict(source=stamp, unit=unit, only=sorted(only), pages=manifest)))
    return manifest, unit


# --------------------------------------------------------------------------
# Read: the two engines

def helper(work: Path, name: str) -> Path | None:
    exe = work / name
    if exe.exists():
        return exe
    if sys.platform != 'darwin' or not shutil.which('swiftc'):
        return None
    done = subprocess.run(['swiftc', '-O', str(HERE / f'{name}.swift'), '-o', str(exe)], capture_output=True, text=True)
    return exe if done.returncode == 0 else None


def stage_read(work: Path, manifest: list[dict], lang_t: str, lang_v: str, use_vision: bool) -> bool:
    missing = [lang for lang in lang_t.split('+')
               if lang not in subprocess.run(['tesseract', '--list-langs'], capture_output=True, text=True).stdout.split()]
    if missing:
        sys.exit(f"Tesseract has no language data for {', '.join(missing)} (macOS: brew install tesseract-lang)")
    dt, dv = work / 'ocr_t', work / 'ocr_v'
    dt.mkdir(exist_ok=True)
    dv.mkdir(exist_ok=True)
    dpi = str(round(199 * U))

    def read_page(m: dict) -> None:
        out = dt / m['stem']
        if not out.with_suffix('.tsv').exists():
            subprocess.run(['tesseract', str(page_file(work, m['stem'])), str(out), '-l', lang_t, '--psm', '4', '--dpi', dpi, 'tsv'],
                           capture_output=True, env=dict(os.environ, OMP_THREAD_LIMIT='1'))
    with cf.ThreadPoolExecutor(os.cpu_count() or 4) as ex:
        list(ex.map(read_page, manifest))
    vision = helper(work, 'vision_ocr') if use_vision else None
    if vision:
        todo = [str(page_file(work, m['stem'])) for m in manifest if not (dv / f"{m['stem']}.jsonl").exists()]
        for i in range(0, len(todo), 40):
            subprocess.run([str(vision), str(dv), lang_v] + todo[i:i + 40], check=True, capture_output=True)
    return vision is not None


def read_tesseract(path: Path) -> list[dict]:
    rows = list(csv.DictReader(open(path, encoding='utf8'), delimiter='\t', quoting=csv.QUOTE_NONE))
    lines: dict = {}
    for r in rows:
        if r['level'] != '5' or not (r['text'] or '').strip():
            continue
        key = (r['block_num'], r['par_num'], r['line_num'])
        lines.setdefault(key, []).append(dict(t=r['text'], x0=int(r['left']), y0=int(r['top']),
                                              x1=int(r['left']) + int(r['width']), y1=int(r['top']) + int(r['height']),
                                              conf=float(r['conf'])))
    pieces = []
    for words in lines.values():
        words.sort(key=lambda w: w['x0'])
        # a wide gap means two columns read as one line
        current = [words[0]]
        typical = st.median([w['y1'] - w['y0'] for w in words])
        for w in words[1:]:
            if w['x0'] - current[-1]['x1'] > typical * 2.2:
                pieces.append(current)
                current = []
            current.append(w)
        pieces.append(current)
    result = [dict(words=ws, x0=min(w['x0'] for w in ws), x1=max(w['x1'] for w in ws),
                   y0=min(w['y0'] for w in ws), y1=max(w['y1'] for w in ws)) for ws in pieces]
    result.sort(key=lambda l: (l['y0'], l['x0']))
    return result


# Vision writes these for a note marker it cannot read
VISION_JUNK_CHARS = 'ạẠồỊịảẢấầậắằặẹẸệỆọỌổộợụỤứừựỳỵỷỹđĐ'
VISION_JUNK = re.compile(r'[iIaà]?[' + VISION_JUNK_CHARS + r'][' + VISION_JUNK_CHARS + r'iI]*')


def read_vision(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding='utf8').splitlines():
        if line.strip():
            d = json.loads(line)
            d['t'] = re.sub(r'\s+', ' ', VISION_JUNK.sub(' ', d['t'])).strip()
            if d['t']:
                rows.append(d)
    return rows


TAIL_MARKER = re.compile(r"^(.*?[^\W\d_][.,;:»)]{0,4})[\d°º*!?'’\"”“¹²³½¼¾]+$")


def core(token: str) -> str:
    m = TAIL_MARKER.match(token)
    if m:
        token = m[1]
    return re.sub(r"^[^\w]+|[^\w]+$", '', token, flags=re.U)


def norm(token: str) -> str:
    return core(token).casefold()


def stage_old_layer(pdf: Path, work: Path) -> dict:
    """The PDF's own text, as a third voter. Its words are in points, its pages in the file's order."""
    path = work / 'old_layer.json'
    if path.exists():
        return json.loads(path.read_text())
    text = subprocess.run(['pdftotext', '-bbox', str(pdf), '-'], capture_output=True, text=True, errors='replace').stdout
    pages = {}
    for n, page in enumerate(re.findall(r'<page .*?</page>', text, flags=re.S), 1):
        width = re.search(r'width="([\d.]+)"', page)
        pages[str(n)] = dict(width=float(width[1]) if width else 0, words=[
            (float(a), float(b), float(c), float(d), t) for a, b, c, d, t in
            re.findall(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>', page)])
    path.write_text(json.dumps(pages))
    return pages


def old_words(old: dict, m: dict, box) -> str:
    """The old layer's words over a box given in this page image's pixels."""
    page = old.get(str(m['pdf']))
    if not page or not page['words'] or not page['width']:
        return ''
    scale = page['width'] / m['src_w']
    x0, y0, x1, y1 = box
    x0, x1 = (x0 + m['x0']) * scale, (x1 + m['x0']) * scale
    y0, y1 = y0 * scale, y1 * scale
    found = []
    for a, b, c, d, t in page['words']:
        if a < x1 and c > x0 and b < y1 and d > y0 and min(c, x1) - max(a, x0) > (c - a) * .5:
            found.append((a, t))
    found.sort()
    return ' '.join(html.unescape(t) for _, t in found)


class Speller:
    def __init__(self, work: Path, lang: str, enabled: bool):
        self.exe = helper(work, 'spell_check') if enabled else None
        self.lang = lang
        self.cache: dict[str, bool] = {}

    def check(self, words) -> None:
        todo = sorted({w for w in words if w not in self.cache})
        if not todo:
            return
        if not self.exe:
            self.cache.update({w: True for w in todo})
            return
        rejected = set(subprocess.run([str(self.exe), self.lang], input='\n'.join(todo), capture_output=True, text=True).stdout.splitlines())
        for w in todo:
            self.cache[w] = w not in rejected

    def ok(self, word: str) -> bool:
        return self.cache.get(word, True)

    def rejected_by(self, lang: str, words) -> set[str]:
        if not self.exe or not words:
            return set()
        return set(subprocess.run([str(self.exe), lang], input='\n'.join(sorted(words)), capture_output=True, text=True).stdout.splitlines())


# --------------------------------------------------------------------------
# Vote

ROMAN = re.compile(r'^[IVXLCDM]+$')
HAS_DIGIT = re.compile(r'\d')
MARK_JUNK = "'’‘`\"”“!|°ºª*^~+®"


def align_line(line: dict, rows: list[dict]) -> list[dict]:
    t_words = line['words']
    v_tokens = ' '.join(r['t'] for r in sorted(rows, key=lambda r: r['x'])).split()
    matcher = difflib.SequenceMatcher(None, [norm(w['t']) for w in t_words], [norm(x) for x in v_tokens], autojunk=False)
    slots = []
    for tag, i, j, k, l in matcher.get_opcodes():
        if tag == 'equal':
            slots.extend(dict(t=[t_words[i + n]], v=[v_tokens[k + n]], same=True) for n in range(j - i))
        else:
            slots.append(dict(t=t_words[i:j], v=v_tokens[k:l], same=False))
    return slots


def parts(token: str) -> tuple[str, str, str]:
    """Lead, letters, tail: the tail is everything after the last letter, digits included."""
    m = re.match(r'^(\W*)(.*[^\W\d_])(.*)$', token, flags=re.S) or re.match(r'^(\W*)(.*?)(\W*)$', token, flags=re.S)
    return m[1], m[2], m[3]


def clean_tail(tail: str) -> str:
    return ''.join(c for c in tail if c not in MARK_JUNK and not c.isdigit()).replace('›', '»')


def settle_form(t: str, v: str, a: str) -> str:
    """Same letters, different case or punctuation. Vision drops guillemets and reads em
    dashes as hyphens; Tesseract confuses commas and full stops. The old layer votes."""
    lead_t, letters_t, tail_t = parts(t)
    lead_v, letters_v, tail_v = parts(v)
    _, letters_a, tail_a = parts(a) if a else ('', '', '')
    if letters_t == letters_v:
        letters = letters_t
    elif letters_a in (letters_t, letters_v):
        letters = letters_a
    else:
        letters = letters_t if letters_t.islower() or not letters_v.islower() else letters_v
    lead_v = lead_v.replace('‹', '«')
    if '«' in lead_t or '—' in lead_t:
        lead = lead_t
    elif '«' in lead_v or '—' in lead_v:
        lead = lead_v
    else:
        lead = lead_t or lead_v
    # marker junk stays for now: it is removed once the marker's position is known
    clean_t, clean_v, clean_a = clean_tail(tail_t), clean_tail(tail_v), clean_tail(tail_a)
    if clean_t == clean_v or ('»' in clean_t and '»' not in clean_v):
        tail = tail_t
    elif clean_a == clean_v and clean_a != clean_t:
        tail = tail_v.replace('-', '—') if '—' in tail_t else tail_v
    elif clean_a == clean_t:
        tail = tail_t
    else:
        tail = tail_v
    if tail_t != tail and any(c in MARK_JUNK for c in tail_t):
        tail += ''.join(c for c in tail_t if c in MARK_JUNK)
    return lead + letters + tail


def keep_punct(chosen: str, other: str) -> str:
    """Guillemets and dashes survive when the other engine's reading of the letters won."""
    lead_c, letters_c, tail_c = parts(chosen)
    lead_o, _, tail_o = parts(other)
    if '«' in lead_o and '«' not in lead_c:
        lead_c = lead_o
    if '»' in tail_o and '»' not in tail_c:
        tail_c = tail_o
    if '—' in lead_o and '-' in lead_c:
        lead_c = lead_o
    return lead_c + letters_c + tail_c


def resolve(t: str, v: str, a: str, valid, vocab: collections.Counter) -> tuple[str | None, str]:
    nt, nv, na = norm(t), norm(v), norm(a)
    if nt.replace(' ', '') == nv.replace(' ', ''):
        valid_t, valid_v = all(valid(x) for x in t.split()), all(valid(x) for x in v.split())
        if valid_t and not valid_v:
            return t, 'same letters, tesseract split valid'
        return v, 'same letters, vision split valid' if valid_v and not valid_t else 'same letters, vision split'
    third = t if na and na == nt else v if na and na == nv else None
    if HAS_DIGIT.search(t) or HAS_DIGIT.search(v) or ROMAN.match(core(t)) or ROMAN.match(core(v)):
        return (third, 'number or numeral: old layer settled') if third else (None, 'number or numeral: review')
    valid_t, valid_v = all(valid(x) for x in t.split()), all(valid(x) for x in v.split())
    if valid_t and not valid_v:
        return t, 'only tesseract valid'
    if valid_v and not valid_t:
        return v, 'only vision valid'
    if third:
        return third, 'old layer settled'
    count_t, count_v = vocab[core(t)], vocab[core(v)]
    if valid_t and valid_v:
        if count_t >= 20 and count_v == 0:
            return t, 'tesseract far commoner in the book'
        if count_v >= 20 and count_t == 0:
            return v, 'vision far commoner in the book'
        return None, 'both valid: review'
    return None, 'neither valid: review'


def flag_item(stem: str, line_no: int, w: dict, t: str, v: str, old: dict, m: dict) -> dict:
    return dict(id=f"{stem}-{line_no}-{w['x0']}", stem=stem, box=[w['x0'], w['y0'], w['x1'], w['y1']], t=t, v=v,
                a=old_words(old, m, (w['x0'], w['y0'], w['x1'], w['y1'])))


def stage_vote(work: Path, manifest: list[dict], old: dict, speller: Speller) -> tuple[dict, collections.Counter, list[dict]]:
    book = {}
    for m in manifest:
        lines = read_tesseract(work / 'ocr_t' / f"{m['stem']}.tsv")
        rows = read_vision(work / 'ocr_v' / f"{m['stem']}.jsonl")
        used = set()
        owners = collections.defaultdict(list)
        for i, r in enumerate(rows):       # a Vision line belongs to one Tesseract line only
            cy = r['y'] + r['h'] / 2
            best = None
            for k, line in enumerate(lines):
                h = line['y1'] - line['y0']
                if not (line['y0'] - h * .3 <= cy <= line['y1'] + h * .3):
                    continue
                if r['x'] >= line['x1'] + u(15) or r['x'] + r['w'] <= line['x0'] - u(15):
                    continue
                d = abs(cy - (line['y0'] + line['y1']) / 2)
                if best is None or d < best[0]:
                    best = (d, k)
            if best:
                owners[best[1]].append(r)
                used.add(i)
        for k, line in enumerate(lines):
            line['slots'] = align_line(line, owners.get(k, []))
            line['had_v'] = k in owners
        for i, r in enumerate(rows):       # lines only Vision saw
            if i in used:
                continue
            tokens = r['t'].split()
            total = sum(len(t) + 1 for t in tokens) or 1
            x = r['x']
            words = []
            for t in tokens:
                width = r['w'] * (len(t) + 1) / total
                words.append(dict(t=t, x0=int(x), x1=int(x + width * len(t) / (len(t) + 1)), y0=r['y'], y1=r['y'] + r['h'], conf=-1))
                x += width
            lines.append(dict(words=words, x0=r['x'], x1=r['x'] + r['w'], y0=r['y'], y1=r['y'] + r['h'],
                              slots=[dict(t=[w], v=[w['t']], same=True) for w in words], had_v=True, only_v=True))
        lines.sort(key=lambda l: (l['y0'], l['x0']))
        book[m['stem']] = lines
    # the book's vocabulary: what both engines read the same way
    vocab: collections.Counter = collections.Counter()
    candidates = set()
    for lines in book.values():
        for line in lines:
            for s in line['slots']:
                if s['same']:
                    vocab[core(s['t'][0]['t'])] += 1
                else:
                    for x in [w['t'] for w in s['t']] + s['v']:
                        candidates.update(core(p) for p in re.split(r'[-–—]', x))
    candidates.update(vocab)
    speller.check([c for c in candidates if c and not HAS_DIGIT.search(c)])
    stats: collections.Counter = collections.Counter()
    flagged: list[dict] = []

    def valid(word: str) -> bool:
        c = core(word)
        if not c:
            return True
        if HAS_DIGIT.search(c):
            return bool(re.fullmatch(r'\d+', c))
        pieces = [core(p) for p in re.split(r'[-–—]', c) if core(p)]
        if all(speller.ok(p) or vocab[p] >= 3 for p in pieces):
            return True
        joined = ''.join(pieces)
        return vocab[joined] >= 3 or bool(joined and speller.ok(joined) and joined in speller.cache)

    for m in manifest:
        stem = m['stem']
        for line_no, line in enumerate(book[stem]):
            out = []
            for s in line['slots']:
                if s['same']:
                    w = dict(s['t'][0])
                    if w['t'] != s['v'][0]:
                        stats['same letters, different form'] += 1
                        w['t'] = settle_form(w['t'], s['v'][0], old_words(old, m, (w['x0'], w['y0'], w['x1'], w['y1'])))
                    stats['agreed'] += 1
                    out.append(w)
                    continue
                t_text = ' '.join(w['t'] for w in s['t'])
                v_text = ' '.join(s['v'])
                if not s['t']:      # only Vision has it
                    stats['only vision'] += 1
                    if re.fullmatch(r"[\W\d]*", v_text) or len(core(v_text)) <= 1 or not line.get('had_v'):
                        continue
                    x = out[-1]['x1'] + u(4) if out else line['x0']
                    w = dict(t=v_text, x0=int(x), x1=int(x + u(8) * len(v_text)), y0=line['y0'], y1=line['y1'], conf=-1)
                    item = flag_item(stem, line_no, w, t_text, v_text, old, m)
                    flagged.append(item)
                    w['flag_id'] = item['id']
                    out.append(w)
                    continue
                box = (min(w['x0'] for w in s['t']), min(w['y0'] for w in s['t']),
                       max(w['x1'] for w in s['t']), max(w['y1'] for w in s['t']))
                a_text = old_words(old, m, box)
                if not s['v']:      # only Tesseract has it
                    stats['only tesseract'] += 1
                    if not line.get('had_v'):      # Vision saw no line here at all
                        if st.mean(w['conf'] for w in s['t']) < 50 and len(core(t_text)) <= 2:
                            continue
                        out.extend(dict(w) for w in s['t'])
                        continue
                    if len(core(t_text)) <= 2 or re.fullmatch(r"[\W\d]*", t_text):
                        continue
                    if norm(a_text) == norm(t_text):
                        out.extend(dict(w) for w in s['t'])
                        continue
                    w = dict(t=t_text, x0=box[0], x1=box[2], y0=box[1], y1=box[3], conf=s['t'][0]['conf'])
                    item = flag_item(stem, line_no, w, t_text, v_text, old, m)
                    flagged.append(item)
                    w['flag_id'] = item['id']
                    out.append(w)
                    continue
                choice, why = resolve(t_text, v_text, a_text, valid, vocab)
                stats['disagreed: ' + why] += 1
                if choice is None:
                    w = dict(t=t_text, x0=box[0], x1=box[2], y0=box[1], y1=box[3], conf=-2)
                    item = flag_item(stem, line_no, w, t_text, v_text, old, m)
                    flagged.append(item)
                    w['flag_id'] = item['id']
                    out.append(w)
                elif choice == t_text:
                    out.extend(dict(w) for w in s['t'])
                else:
                    if ' ' not in choice and ' ' not in t_text:
                        choice = keep_punct(choice, t_text)
                    out.extend(share_box(choice, box))
            line['final'] = out
            line.pop('slots', None)
            line.pop('words', None)
    return book, stats, flagged


def share_box(text: str, box) -> list[dict]:
    """A reading with a different number of words: share the box out among them."""
    tokens = text.split()
    total = sum(len(t) + 1 for t in tokens)
    gap = u(6)
    x = box[0]
    words = []
    for t in tokens:
        width = (box[2] - box[0] + gap) * (len(t) + 1) / total
        words.append(dict(t=t, x0=int(x), x1=int(x + width - gap / max(len(tokens), 1)), y0=box[1], y1=box[3], conf=-1))
        x += width
    return words


def apply_word_decisions(book: dict, decisions: dict, stats: collections.Counter) -> None:
    for lines in book.values():
        for line in lines:
            new = []
            for w in line.get('final', []):
                fid = w.get('flag_id')
                if not fid or fid not in decisions:
                    new.append(w)
                    continue
                stats['words decided on the sheets'] += 1
                if decisions[fid]:
                    tokens = decisions[fid].split()
                    if len(tokens) == 1:
                        new.append(dict(w, t=tokens[0], x1=max(w['x1'], w['x0'] + int(u(6)))))
                    else:
                        span = max(w['x1'], w['x0'] + int(u(6)) * len(tokens))
                        new.extend(dict(w, **piece) for piece in share_box(decisions[fid], (w['x0'], w['y0'], span, w['y1'])))
            line['final'] = new


# --------------------------------------------------------------------------
# Measure: what only the pixels can say

def find_figures(cc: list[list[int]], claimed: set[int], w: int, h: int) -> list[list[int]]:
    """Ink no text line claims, dense enough over a large enough area, is a picture. A rule
    is not, nor a thin frame around text, nor the dark strip a scanner leaves along the edge."""
    cell = max(6, int(u(24)))
    grid = collections.Counter()
    for c in cc:
        if id(c) in claimed or (c[3] - c[1] <= u(8) and c[2] - c[0] >= u(60)):
            continue
        if (c[0] <= 1 or c[1] <= 1 or c[2] >= w - 1 or c[3] >= h - 1) and min(c[2] - c[0], c[3] - c[1]) < u(60):
            continue
        if c[2] - c[0] >= u(150) and c[3] - c[1] >= u(100) and c[4] < (c[2] - c[0]) * (c[3] - c[1]) * .04:
            continue
        for gx in range(c[0] // cell, (c[2] - 1) // cell + 1):
            for gy in range(c[1] // cell, (c[3] - 1) // cell + 1):
                grid[(gx, gy)] += 1
    seen = set()
    figures = []
    for start in grid:
        if start in seen:
            continue
        stack, region = [start], []
        seen.add(start)
        while stack:
            x, y = stack.pop()
            region.append((x, y))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (x + dx, y + dy)
                    if n in grid and n not in seen:
                        seen.add(n)
                        stack.append(n)
        xs, ys = [p[0] for p in region], [p[1] for p in region]
        width, height = (max(xs) - min(xs) + 1) * cell, (max(ys) - min(ys) + 1) * cell
        if width >= u(150) and height >= u(100) and len(region) * cell * cell >= width * height * .3:
            figures.append([min(xs) * cell, min(ys) * cell, min((max(xs) + 1) * cell, w), min((max(ys) + 1) * cell, h)])
    return figures


def measure_page(job: tuple[str, dict, list[dict]]) -> dict:
    work, m, lines = Path(job[0]), job[1], job[2]
    w, h, rows = bit_rows(work / 'bw' / f"{m['stem']}.pbm")
    cc = components(rows)
    rules = [c for c in cc if c[2] - c[0] >= u(60) and c[3] - c[1] <= u(8)]
    lines = [l for l in lines if l.get('final')]
    widths = sorted(l['x1'] - l['x0'] for l in lines if len(l['final']) >= 5)
    text_width = widths[len(widths) * 3 // 4] if widths else u(860)
    # the rule above the notes: short, in the lower part of the page
    note_rule = [c for c in rules if text_width * .12 <= c[2] - c[0] <= text_width * .45 and c[1] > h * .25]
    if not note_rule:
        # a full-width rule counts only with smaller type under it
        below = sorted((c for c in rules if c[2] - c[0] >= text_width * .8 and c[1] > h * .4), key=lambda c: c[1])
        if below:
            under = [l for l in lines if l['y0'] > below[-1][1]]
            sizes = sorted(l['y1'] - l['y0'] for l in under)
            if under and sizes[len(sizes) // 2] < u(31):
                note_rule = below[-1:]
    note_rule.sort(key=lambda c: c[1])
    rule_y = note_rule[-1][1] if note_rule else None
    glyphs = [c for c in cc if 2 < c[3] - c[1] < u(75) and c[2] - c[0] < u(140)]
    for l in lines:
        l['g'] = []
    for g in glyphs:
        cy = (g[1] + g[3]) / 2
        best = None
        for l in lines:
            lh = l['y1'] - l['y0']
            if l['y0'] - lh * .35 <= cy <= l['y1'] + 1 and g[2] >= l['x0'] - u(6) and g[0] <= l['x1'] + u(45):
                d = abs(cy - (l['y0'] + lh * .55))
                if best is None or d < best[0]:
                    best = (d, l)
        if best:
            best[1]['g'].append(g)
    near = max(1, round(u(1)))
    for l in lines:
        gs = sorted(l['g'], key=lambda g: g[0])
        heights = sorted(g[3] - g[1] for g in gs if g[3] - g[1] >= u(9))
        l['cap'] = heights[int(len(heights) * .9)] if heights else None
        l['note'] = bool(rule_y and l['y0'] > rule_y)
        # the baseline is where most letters end; raised markers cannot outvote them
        bottoms: collections.Counter = collections.Counter()
        for g in gs:
            if g[3] - g[1] >= u(9):
                for d in range(-near, near + 1):
                    bottoms[g[3] + d] += 1
        l['base'] = bottoms.most_common(1)[0][0] if bottoms else l['y1']
        on_line = [g[3] - g[1] for g in gs if abs(g[3] - l['base']) <= u(2) and g[3] - g[1] >= u(9)]
        small = collections.Counter(H for H in on_line if l['cap'] and H <= l['cap'] * .8)
        l['xh'] = small.most_common(1)[0][0] if small else (min(on_line) if on_line else None)
        l['markers'] = []
    # A marker is a small glyph sitting high beside the text to its left, with nothing right
    # under it (an accent has its letter there). No line finding is needed, so a one-word
    # line at the end of a paragraph is treated like any other.
    ordered = sorted(glyphs, key=lambda g: g[0])
    candidates = []
    for c in glyphs:
        H, W = c[3] - c[1], c[2] - c[0]
        if not (u(8) <= H <= u(19) and u(2) <= W <= u(28)) or (rule_y and c[1] > rule_y):
            continue
        left = [g for g in ordered if g[2] <= c[0] + u(2) and c[0] - g[2] <= u(90) and g is not c
                and g[1] < c[3] + u(12) and g[3] > c[1] and g[3] - g[1] >= u(9)]
        if len(left) < 3:
            continue
        left.sort(key=lambda g: -g[2])
        nearest = left[:7]
        base = st.median(g[3] for g in nearest)
        xh = sorted(g[3] - g[1] for g in nearest)[len(nearest) // 3]
        up = base - c[3]
        if not (xh * .3 <= up <= xh * 1.1 and xh * .55 <= H <= xh * 1.25) or W > xh * 1.8:
            continue
        if any(o is not c and o[0] < c[2] - 1 and o[2] > c[0] + 1 and c[3] - u(2) <= o[1] <= c[3] + u(9) for o in glyphs):
            continue
        candidates.append((c, base))
    candidates.sort(key=lambda t: t[0][0])
    clusters = []
    for c, base in candidates:
        for cluster in clusters:
            k = cluster[0]
            if abs(cluster[1] - base) <= u(6) and 0 <= c[0] - k[2] <= u(7) and c[1] < k[3] and c[3] > k[1]:
                k[2] = max(k[2], c[2]); k[1] = min(k[1], c[1]); k[3] = max(k[3], c[3])
                break
        else:
            clusters.append((list(c[:4]), base))
    body_lines = [l for l in lines if not l['note']]
    for k, base in clusters:
        best = None
        for l in body_lines:
            if k[0] < l['x0'] - u(8) or k[0] > l['x1'] + u(60):
                continue
            d = abs(l['base'] - base)
            if d <= u(14) and (best is None or d < best[0]):
                best = (d, l)
        if best:
            xh = best[1]['xh'] or u(15)
            if best[1]['base'] - k[3] >= xh * .3 and k[3] - k[1] >= xh * .62:
                best[1]['markers'].append(k)
    for l in body_lines:
        l['markers'].sort(key=lambda k: k[0])
    # pictures, then ink nobody read: nothing may vanish silently
    claimed = {id(g) for l in lines for g in l['g']}
    figures = find_figures(cc, claimed, w, h)
    inside = lambda b, f: b[0] >= f[0] - u(6) and b[1] >= f[1] - u(6) and b[2] <= f[2] + u(6) and b[3] <= f[3] + u(6)
    for l in lines:
        # what the engines made of a picture; well-read text stays, whatever surrounds it
        confidence = [wd['conf'] for wd in l['final'] if wd.get('conf', -1) >= 0]
        if figures and any(inside((l['x0'], l['y0'], l['x1'], l['y1']), f) for f in figures) and (not confidence or st.mean(confidence) < 70):
            l['final'] = []
    loose = [g for g in glyphs if id(g) not in claimed and g[3] - g[1] >= u(9) and g[2] - g[0] >= u(2)
             and not (rule_y and abs(g[1] - rule_y) < u(4)) and not any(inside(g, f) for f in figures)]
    loose.sort(key=lambda g: (g[1], g[0]))
    orphans: list[list[int]] = []
    for g in loose:
        for o in orphans:
            if g[0] - o[2] <= u(40) and o[0] - g[2] <= u(40) and g[1] - o[3] <= u(12) and o[1] - g[3] <= u(12):
                o[0] = min(o[0], g[0]); o[1] = min(o[1], g[1]); o[2] = max(o[2], g[2]); o[3] = max(o[3], g[3]); o[4] += 1
                break
        else:
            orphans.append([g[0], g[1], g[2], g[3], 1])
    lines = [l for l in lines if l['final']]
    score_slant(rows, [wd for l in lines for wd in l['final']])
    return dict(w=w, h=h, rule=rule_y, rule_x=note_rule[-1][0] if note_rule else None, rules=[c[:4] for c in rules],
                orphans=orphans, figures=figures, lines=lines)


def score_slant(rows: list[str], words: list[dict]) -> None:
    """Italic letters line up when sheared back; a Viterbi over the page's words means one
    short word cannot flip a phrase. Evidence grows with the word's length."""
    evidence = []
    for wd in words:
        x0, x1, y0, y1 = max(wd['x0'], 0), wd['x1'], max(wd['y0'], 0), wd['y1']
        letters = len(re.sub(r'\W', '', wd['t']))
        if x1 - x0 < u(6) or y1 <= y0 or letters == 0:
            evidence.append(0.0)
            continue
        scores = []
        for shear in (0, .08, .16, .24, .32):
            cols: dict[int, int] = {}
            for y in range(y0, min(y1, len(rows))):
                offset = int(round(shear * (y1 - 1 - y)))
                for m in re.finditer('1', rows[y][x0:x1]):
                    k = m.start() - offset
                    cols[k] = cols.get(k, 0) + 1
            scores.append(sum(v * v for v in cols.values()))
        evidence.append((max(scores[2:]) / max(scores[0], 1) - 1.09) * min(letters, 8) * 2.2)
    switch = 1.15
    cost_roman, cost_italic = 0.0, 0.0
    back = []
    for e in evidence:
        from_roman = (cost_roman, 0) if cost_roman <= cost_italic + switch else (cost_italic + switch, 1)
        from_italic = (cost_italic, 1) if cost_italic <= cost_roman + switch else (cost_roman + switch, 0)
        back.append((from_roman[1], from_italic[1]))
        cost_roman, cost_italic = from_roman[0] + max(e, 0), from_italic[0] + max(-e, 0)
    state = 0 if cost_roman <= cost_italic else 1
    for i in range(len(words) - 1, -1, -1):
        words[i]['italic'] = bool(state)
        state = back[i][state]


# --------------------------------------------------------------------------
# Notes: markers become part of the text, numbers come from the printed sequence

def read_marker(work: Path, stem: str, box) -> str:
    x0, y0, x1, y1 = box
    pad = max(2, int(u(4)))
    crop = subprocess.run(['magick', str(page_file(work, stem)), '-crop', f'{x1 - x0 + 2 * pad}x{y1 - y0 + 2 * pad}+{x0 - pad}+{y0 - pad}',
                           '+repage', '-resize', f'{400 / U:.0f}%', '-bordercolor', 'white', '-border', '30', 'png:-'], capture_output=True).stdout
    return subprocess.run(['tesseract', 'stdin', 'stdout', '--psm', '7', '-c', 'tessedit_char_whitelist=0123456789'],
                          input=crop, capture_output=True).stdout.decode().strip()


def note_starts(notes: list[dict], rule_x: int, expected: int | None) -> list[tuple[dict, int | None]]:
    """A note's first line is indented; in a book whose notes are flush, its number is the next one."""
    starts = []
    following = expected
    for l in notes:
        m = re.match(r'^\(?(\d{1,3})[.,:;)]?$', l['final'][0]['t'])
        n = int(m[1]) if m else None
        if l['x0'] >= rule_x + u(20) or (n is not None and (n == following or (n == 1 and not starts))):
            starts.append((l, n))
            following = n + 1 if n is not None else (following + 1 if following is not None else None)
    return starts


def attach_markers(pages: dict, manifest: list[dict], stats: collections.Counter, work: Path, lang: str) -> list:
    expected = None
    problems = []
    openings = collections.Counter(w['t'][0] for pg in pages.values() for l in pg['lines'] for w in l.get('final') or [] if w['t'][:1] in '«“"')
    keep_quotes = openings['“'] + openings['"'] > openings['«']
    for m in manifest:
        stem = m['stem']
        pg = pages[stem]
        lines = [l for l in pg['lines'] if l.get('final')]
        body = [l for l in lines if not l.get('note')]
        notes = [l for l in lines if l.get('note')]
        rule_x = pg.get('rule_x')
        if rule_x is None:
            rule_x = min((l['x0'] for l in notes), default=0)
        marks = [(l, c) for l in body for c in l.get('markers', [])]
        if not notes:       # no notes on the page: a raised shape here is not a marker
            stats['raised shapes ignored on pages without notes'] += len(marks)
            continue
        starts = note_starts(notes, rule_x, expected)
        # a paragraph inside a note is indented too, but starts with no number
        if len(starts) > len(marks):
            starts = [s for s in starts if s[1] is not None or re.match(r"^[\dlIOoSsZzBAgbGi(]{1,3}[.,:;]?$", s[0]['final'][0]['t'])] or starts
        numeric = [(i, n) for i, (_, n) in enumerate(starts) if n is not None]
        if numeric:
            base = collections.Counter(n - i for i, n in numeric).most_common(1)[0][0]
            if expected is None or base == 1 or abs(base - expected) > 3:
                if expected is not None and base != 1:
                    problems.append((stem, 'note numbers jump', expected, base))
                expected = base
        numbers = list(range(expected, expected + len(starts))) if expected is not None else []
        for (l, n), want in zip(starts, numbers):
            if n != want:
                stats['note numbers corrected from the sequence'] += 1
            first = l['final'][0]
            if n is None and not re.match(r"^[\dlIOoSsZzBAgbGi(]{1,3}[.,:;]?$", first['t']):
                stats['note numbers restored'] += 1
                l['final'].insert(0, dict(first, t=f'{want}.', x0=int(first['x0'] - u(30)), x1=int(first['x0'] - u(6)), italic=False))
            else:
                first['t'] = f'{want}.'
                first['italic'] = False
        if numbers and len(marks) > len(starts) and starts:
            # a stain or pen stroke can look raised: keep what reads as the expected numbers
            read = [read_marker(work, stem, c) for _, c in marks]
            wanted = {str(n) for n in numbers}
            kept = [mark for mark, r in zip(marks, read) if r in wanted]
            if len(kept) == len(numbers):
                stats['raised shapes rejected after reading them'] += len(marks) - len(kept)
                marks = kept
        ok = len(marks) == len(starts)
        stats['pages with notes: markers = notes' if ok else 'pages with notes: markers != notes'] += 1
        if not ok:
            problems.append((stem, 'markers and notes differ', len(marks), [n for _, n in starts]))
        for i, (l, c) in enumerate(marks):
            if ok and numbers:
                label = str(numbers[i])
            elif ok:       # symbols, not numbers
                label = core(starts[i][0]['final'][0]['t']) or '*'
            else:
                r = read_marker(work, stem, c)
                label = r if r and str(r) in {str(n) for n in numbers} else '?'
            at = body.index(l)
            context = ' '.join(w['t'] for o in body[max(at - 4, 0):at + 1] for w in o['final'])
            insert_marker(l, c, label, stats, keep_quotes, lang == 'es' and '¿' not in context and '¡' not in context)
        if expected is not None:
            expected += len(starts)
    return problems


def insert_marker(line: dict, cluster, label: str, stats: collections.Counter, keep_quotes: bool, no_opening_mark: bool) -> None:
    x0, y0, x1, y1 = cluster
    words = line['final']
    index = 0
    for i, w in enumerate(words):
        if w['x0'] < x0:
            index = i
    w = words[index]
    base = line.get('base', 0)
    xh = line.get('xh') or u(15)
    if w['x1'] >= x0 - u(2):
        # an engine read the marker as junk on the end of the word. In a book that quotes with
        # “ ” a real closing mark shows in the pixels as a small raised shape before the marker.
        raised = keep_quotes and any(g[2] <= x0 + 1 and g[0] >= w['x0'] and x0 - g[2] <= u(16) and base - g[3] >= xh * .35 and g[3] - g[1] < xh * .62
                                     for g in line.get('g', []))
        after_punctuation = re.match(r"^(.*?[.,;:»)])[\d'’‘`\"”“|°ºª*^~+®!?]+$", w['t'], flags=re.S)
        cleaned = after_punctuation[1] if after_punctuation else re.sub(r"[\d'’‘`\"”“|°ºª*^~+®]+$", '', w['t'])
        if raised and cleaned != w['t'] and re.search(r"[\"”’]", w['t']):
            cleaned += '”'
        # a full stop and a marker read together as ! or ?: Spanish would have opened the
        # question, and elsewhere the shape before the marker decides
        before = [g for g in line.get('g', []) if g[2] <= x0 + 1 and g[0] >= w['x0'] and g[3] - g[1] >= 2]
        if re.search(r'[!?]+$', cleaned):
            last = max(before, key=lambda g: g[2]) if before else None
            if no_opening_mark or (last and last[3] - last[1] <= u(8) and last[2] - last[0] <= u(7)):
                cleaned = re.sub(r'[!?]+$', '', cleaned)
        if cleaned and cleaned != w['t']:
            stats['marker junk removed from a word'] += 1
            w['t'] = cleaned
        w['x1'] = min(w['x1'], x0 - 1)
        if w['x1'] <= w['x0']:
            w['x1'] = w['x0'] + int(u(4))
    # the engines often lose a full stop or comma beside a marker; the pixels still show it
    if not re.search(r'[.,;:»)\]!?"”]$', w['t']):
        for g in line.get('g', []):
            if g[2] <= x0 + 1 and x0 - g[2] <= u(14) and g[3] - g[1] <= u(8) and g[2] - g[0] <= u(7) and abs(g[3] - base) <= u(6) and g[0] >= w['x0']:
                w['t'] += ',' if g[3] > base + u(2) else '.'
                stats['punctuation recovered beside a marker'] += 1
                break
    if index + 1 < len(words):
        following = words[index + 1]
        if following['x0'] <= x1 + u(2) and re.fullmatch(r"[\d'’`\"!|°ºª*^~.,]+", following['t']):
            words.pop(index + 1)
    words.insert(index + 1, dict(t=label, x0=x0, x1=x1, y0=y0, y1=y1, conf=100, sup=True, italic=False, sup_bottom=y1))
    stats['markers placed'] += 1


def keep_author_rules(pages: dict, manifest: list[dict], stats: collections.Counter) -> None:
    """A rule at the start of a line stands for the author of the entry above: it is text."""
    for m in manifest:
        pg = pages[m['stem']]
        for x0, y0, x1, _ in pg.get('rules', []):
            if not u(60) <= x1 - x0 <= u(160) or (pg.get('rule') and abs(y0 - pg['rule']) < u(8)):
                continue
            for l in pg['lines']:
                if l['y0'] - u(4) <= y0 <= l['y1'] + u(4) and l['x0'] >= x1 - u(12) and l.get('final'):
                    # an engine may have read the rule as a dash on the next word
                    l['final'][0]['t'] = l['final'][0]['t'].lstrip('-–—_') or l['final'][0]['t']
                    l['final'].insert(0, dict(t='———', x0=x0, x1=x1, y0=l['y0'], y1=l['y1'], conf=100, italic=False))
                    l['x0'] = x0
                    stats['repeated-author rules kept'] += 1
                    break


def keep_figures(work: Path, manifest: list[dict], pages: dict, decisions: dict) -> list[dict]:
    """Each picture gets a crop to look at, named by its id; "" for that id in the answers drops it."""
    kept = []
    for m in manifest:
        pg = pages[m['stem']]
        remaining = []
        for f in pg.get('figures', []):
            fid = f"fig-{m['stem']}-{f[0]}-{f[1]}"
            if decisions.get(fid) == '':
                continue
            remaining.append(f)
            kept.append(dict(id=fid, stem=m['stem'], box=f))
            (work / 'figures').mkdir(exist_ok=True)
            subprocess.run(['magick', str(page_file(work, m['stem'])), '-crop', f'{f[2] - f[0]}x{f[3] - f[1]}+{f[0]}+{f[1]}', '+repage',
                            '-resize', '500x500>', str(work / 'figures' / f'{fid}.png')], capture_output=True)
        pg['figures'] = remaining
    return kept


def tidy_tokens(pages: dict) -> None:
    for pg in pages.values():
        for l in pg['lines']:
            f = l.get('final') or []
            i = 0
            while i < len(f) - 1:
                if f[i]['t'] == '«' and not f[i + 1].get('sup'):      # a lone guillemet belongs to the next word
                    f[i + 1]['t'] = '«' + f[i + 1]['t']
                    f[i + 1]['x0'] = f[i]['x0']
                    f.pop(i)
                i += 1


# --------------------------------------------------------------------------
# What needs an eye

def section_numerals(manifest: list[dict], pages: dict, stats: collections.Counter, decisions: dict) -> list[dict]:
    """A centred line of one short token is a section numeral. Both engines misread these
    (II as 11 or tl, III as MI), so every one is read on a sheet."""
    items = []
    for m in manifest:
        pg = pages[m['stem']]
        ls = [l for l in pg['lines'] if l.get('final') and not l.get('note')]
        longs = [l for l in ls if len(l['final']) >= 5]
        if not longs:
            continue
        left = sorted(l['x0'] for l in longs)[len(longs) // 5]
        right = sorted(l['x1'] for l in longs)[-max(len(longs) // 5, 1)]
        centre = (left + right) / 2
        for l in ls:
            text = ' '.join(w['t'] for w in l['final'])
            if len(l['final']) != 1 or len(text) > 6 or l['y0'] < pg['h'] * .055 or abs((l['x0'] + l['x1']) / 2 - centre) > u(40):
                continue
            if not re.fullmatch(r"[IiLl1!|VvXxUuHhMmTtNnYy.'´0O]+", text):
                continue
            fid = f"num-{m['stem']}-{l['y0']}"
            w = l['final'][0]
            l['numeral'] = True
            if fid in decisions:
                w['t'] = decisions[fid]
                stats['section numerals read on the sheets'] += 1
            else:
                items.append(dict(id=fid, stem=m['stem'], box=[w['x0'], w['y0'], w['x1'], w['y1']], t=text, v='(numeral)', a=''))
    return items


def volume_numerals(manifest: list[dict], pages: dict, stats: collections.Counter, decisions: dict) -> list[dict]:
    """'vol. 1,' or 'Op. Cit., 1, p. 40': a volume in roman type that both engines read as digits."""
    items = []
    volume = re.compile(r'^(vol|vols|t|ts|tomo|tomos)\.?$', re.I)
    pages_word = re.compile(r'^(pp?|ff?|fs|cap|caps|nos?)\b', re.I)
    for m in manifest:
        for l in pages[m['stem']]['lines']:
            f = l.get('final') or []
            for i, w in enumerate(f):
                text = w['t'].strip('.,;:()')
                if not re.fullmatch(r'[1lI]{1,3}|[1lI]?[VX][1lI]{0,3}', text) or re.fullmatch(r'I{1,3}|I?[VX]I{0,3}', text):
                    continue
                before = f[i - 1]['t'] if i else ''
                after = f[i + 1]['t'] if i + 1 < len(f) else ''
                if not (volume.match(before) or (before.endswith(',') and (pages_word.match(after) or before.endswith('Cit.,')))):
                    continue
                fid = f"vol-{m['stem']}-{w['x0']}-{w['y0']}"
                if fid in decisions:
                    w['t'] = decisions[fid]
                    stats['volume numerals read on the sheets'] += 1
                else:
                    items.append(dict(id=fid, stem=m['stem'], box=[w['x0'], w['y0'], w['x1'], w['y1']], t=w['t'], v='(volume?)',
                                      a=before[-8:] + ' _ ' + after[:6]))
    return items


def one_edit_apart(a: str, b: str) -> bool:
    a, b = a.casefold(), b.casefold()
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return (a[i:] == b[i + 1:] or a[i + 1:] == b[i + 1:]
            or (len(a) == len(b) and i + 1 < len(a) and a[i] == b[i + 1] and a[i + 1] == b[i] and a[i + 2:] == b[i + 2:]))


def unvouched_words(manifest: list[dict], pages: dict, stats: collections.Counter, decisions: dict, speller: Speller, lang: str) -> list[dict]:
    """Words both engines agree on but no dictionary, and not the book itself, vouches for.
    A shared misreading can only be caught here: rare, rejected, not plain English."""
    counts: collections.Counter = collections.Counter()
    where = []
    for m in manifest:
        carry = None
        for l in pages[m['stem']]['lines']:
            tokens = [w for w in (l.get('final') or []) if not w.get('sup')]
            for i, w in enumerate(tokens):
                text = w['t']
                if carry is not None and i == 0:      # the second half of a word divided at the line end
                    text = re.sub(r'-$', '', carry['t']) + re.sub(r'^\W+', '', text)
                    counts.update(re.findall(r"[^\W\d_]+", text))
                    where.append((m, carry, text))
                    carry = None
                    continue
                if i == len(tokens) - 1 and re.search(r'[^\W\d_]-$', text):
                    carry = w
                    continue
                counts.update(re.findall(r"[^\W\d_]+", text))
                where.append((m, w, text))
    words = [c for c in counts if len(c) >= 3]
    speller.check(words)
    rejected = {c for c in words if not speller.ok(c)}
    english = set() if lang == 'en' else rejected - speller.rejected_by('en_GB', rejected) if speller.exe else set()
    by_length = collections.defaultdict(list)
    for c, n in counts.items():
        if n >= 5 and len(c) >= 4 and speller.ok(c):
            by_length[len(c)].append(c)

    def near_common(word: str) -> bool:
        return any(one_edit_apart(word, c) for size in (len(word) - 1, len(word), len(word) + 1) for c in by_length.get(size, []))

    items = []
    for m, w, text in where:
        for part in re.findall(r"[^\W\d_]+", text):
            if part not in rejected or part in english or counts[part] > 2 or (part[0].isupper() and not near_common(part)):
                continue
            fid = f"sp-{m['stem']}-{w['x0']}-{w['y0']}"
            if fid in decisions:
                if decisions[fid] != w['t']:
                    stats['words corrected on the sheets'] += 1
                    w['t'] = decisions[fid]
                else:
                    stats['words confirmed on the sheets (printed that way)'] += 1
            else:
                items.append(dict(id=fid, stem=m['stem'], box=[w['x0'], w['y0'], w['x1'], w['y1']], t=w['t'], v='(unvouched)', a=text if text != w['t'] else ''))
            break
    return items


def recover_ink(manifest: list[dict], pages: dict, stats: collections.Counter, decisions: dict) -> list[dict]:
    items = []
    for m in manifest:
        pg = pages[m['stem']]
        for o in pg.get('orphans', []):
            fid = f"ink-{m['stem']}-{o[0]}-{o[1]}"
            if fid not in decisions:
                items.append(dict(id=fid, stem=m['stem'], box=o[:4], t='(no engine)', v=f'{o[4]} shapes', a=''))
            elif decisions[fid]:
                stats['lines recovered from ink no engine read'] += 1
                pg['lines'].append(dict(x0=o[0], x1=o[2], y0=o[1], y1=o[3], base=o[3], xh=None, cap=None, markers=[], only_v=True,
                                        note=bool(pg['rule'] and o[1] > pg['rule']),
                                        final=[dict(t=decisions[fid], x0=o[0], x1=o[2], y0=o[1], y1=o[3], conf=-1, italic=False)]))
                pg['lines'].sort(key=lambda l: (l['y0'], l['x0']))
    return items


def crop_label(i: int, item: dict) -> str:
    return f"{i}: {item['t'] or '-'} | {item['v'] or '-'} | {item['a'] or '-'}"[:58].replace('%', 'pct').replace('\\', '/')


def build_sheet_crop(job: tuple[str, int, dict, float]) -> str:
    work, i, item, unit = Path(job[0]), job[1], job[2], job[3]
    x0, y0, x1, y1 = item['box']
    pad, margin = int(70 * unit), int(7 * unit)
    out = work / 'review' / f'c{i:05d}.png'
    font = next((['-font', f] for f in LABEL_FONTS if os.path.exists(f)), [])
    subprocess.run(['magick', str(page_file(work, item['stem'])),
                    '-crop', f'{x1 - x0 + 2 * pad}x{y1 - y0 + 2 * margin}+{max(x0 - pad, 0)}+{max(y0 - margin, 0)}', '+repage',
                    '-resize', f'{100 / unit:.1f}%', '-gravity', 'West', '-background', 'white', '-extent', '440x56',
                    '-bordercolor', '#ddd', '-border', '1', '-gravity', 'North', '-background', '#eef', '-splice', '0x20']
                   + font + ['-pointsize', '14', '-annotate', '+0+2', crop_label(i, item), str(out)], capture_output=True)
    return str(out)


def build_sheets(work: Path, items: list[dict], unit: float, per_sheet: int = 48) -> list[Path]:
    review = work / 'review'
    review.mkdir(exist_ok=True)
    for old_sheet in review.glob('sheet*.png'):
        old_sheet.unlink()
    with cf.ThreadPoolExecutor(os.cpu_count() or 4) as ex:
        crops = list(ex.map(build_sheet_crop, [(str(work), i, item, unit) for i, item in enumerate(items)]))
    sheets = []
    for s in range(0, len(crops), per_sheet):
        out = review / f'sheet{s // per_sheet:03d}.png'
        subprocess.run(['magick', 'montage'] + crops[s:s + per_sheet] + ['-tile', '3x', '-geometry', '+3+3', str(out)], capture_output=True)
        sheets.append(out)
    for c in crops:
        Path(c).unlink(missing_ok=True)
    return sheets


def merge_answers(work: Path, answers: Path, accept_rest: bool) -> None:
    """Sheet numbers (or item ids) to text; "" deletes. --accept-rest keeps the first reading of the rest."""
    items = json.loads((work / 'items.json').read_text())
    path = work / 'decisions.json'
    decisions = json.loads(path.read_text()) if path.exists() else {}
    given = json.loads(answers.read_text()) if answers else {}
    for i, item in enumerate(items):
        if str(i) in given:
            decisions[item['id']] = given[str(i)]
        elif item['id'] in given:
            decisions[item['id']] = given[item['id']]
        elif accept_rest:
            decisions[item['id']] = '' if item['t'] == '(no engine)' else item['t']
    decisions.update({k: v for k, v in given.items() if not k.isdigit()})
    path.write_text(json.dumps(decisions, ensure_ascii=False, indent=0))


# --------------------------------------------------------------------------
# Type sizes

def assign_sizes(pages: dict) -> dict:
    """Snap each line to a size class, from its leading as well as its letters: a scan's
    thickening moves the x-height a pixel or two from page to page, the leading never moves."""
    body_xh: collections.Counter = collections.Counter()
    note_xh: collections.Counter = collections.Counter()
    for pg in pages.values():
        for l in pg['lines']:
            if l.get('final') and l.get('xh') and len(l['final']) >= 4:
                (note_xh if l.get('note') else body_xh)[l['xh']] += 1
    bxh = body_xh.most_common(1)[0][0] if body_xh else u(15)
    nxh = note_xh.most_common(1)[0][0] if note_xh else u(12)
    caps: collections.Counter = collections.Counter()
    for pg in pages.values():
        for l in pg['lines']:
            if l.get('final') and not l.get('note') and l.get('cap') and l.get('xh') == bxh:
                caps[l['cap']] += 1
    bcap = caps.most_common(1)[0][0] if caps else u(21)
    body_px, note_px = bxh / .45, nxh / .45
    quote_px = (note_px + body_px) / 2
    pitches: collections.Counter = collections.Counter()

    def ordered(pg: dict) -> list[dict]:
        return sorted((l for l in pg['lines'] if l.get('final') and not l.get('note')), key=lambda l: l['base'])

    for pg in pages.values():
        ls = ordered(pg)
        for a, b in zip(ls, ls[1:]):
            if b['x0'] < a['x1'] and a['x0'] < b['x1'] and u(20) <= b['base'] - a['base'] <= u(60):
                pitches[round(b['base'] - a['base'])] += 1
    body_pitch = pitches.most_common(1)[0][0] if pitches else u(41)
    for pg in pages.values():
        ls = ordered(pg)
        for i, l in enumerate(ls):
            ds = []
            for o in (ls[i - 1] if i else None, ls[i + 1] if i + 1 < len(ls) else None):
                if o is not None and o['x0'] < l['x1'] and l['x0'] < o['x1'] and u(20) <= abs(o['base'] - l['base']) <= u(60):
                    ds.append(abs(o['base'] - l['base']))
            pitch = min(ds) if ds else None
            letters = re.sub(r'[\W\d_]', '', ' '.join(w['t'] for w in l['final']))
            if letters and letters.upper() == letters and len(letters) >= 3 and l.get('cap'):
                ratio = l['cap'] / bcap
                l['size_px'] = body_px * (round(ratio * 8) / 8 if ratio > 1.12 else 1)
            elif pitch is not None:
                l['size_px'] = body_px if pitch >= body_pitch - u(3) else quote_px
            elif l.get('xh') and l['xh'] <= bxh - u(3):
                l['size_px'] = quote_px
            else:
                l['size_px'] = body_px
        # one odd line inside a run of the other class is noise
        for i in range(1, len(ls) - 1):
            a, b, c = ls[i - 1]['size_px'], ls[i]['size_px'], ls[i + 1]['size_px']
            if a == c != b and a in (body_px, quote_px) and b in (body_px, quote_px):
                ls[i]['size_px'] = a
        for l in pg['lines']:
            if l.get('final') and l.get('note'):
                l['size_px'] = note_px
    return dict(body_xh=bxh, note_xh=nxh, body_cap=bcap, body_pitch=body_pitch)


# --------------------------------------------------------------------------
# Write

def page_offsets(manifest: list[dict], pages: dict) -> tuple[dict, int, int]:
    """The crop at the gutter must not leak into the layout: every text block starts at the
    same place, so pdf_to_html.py measures one margin for the whole book."""
    lefts, tops = {}, {}
    for m in manifest:
        pg = pages[m['stem']]
        ls = [l for l in pg['lines'] if l.get('final') and not l.get('only_v')]
        xs = sorted(l['x0'] for l in ls if len(l['final']) >= 5)
        left = pg.get('rule_x')
        if left is None:
            for x in xs:       # the smallest x several lines share: an indented epigraph cannot move it
                if sum(1 for o in xs if x <= o <= x + u(8)) >= 3:
                    left = x
                    break
        lefts[m['stem']] = left if left is not None else (xs[len(xs) // 5] if xs else None)
        tops[m['stem']] = min((l['y0'] for l in ls if l['y0'] < pg['h'] * .12), default=None)
    known = [v for v in lefts.values() if v is not None]
    target_x = max(known) if known else 0
    known_tops = [v for v in tops.values() if v is not None]
    target_y = st.median(known_tops) if known_tops else 0
    offsets = {}
    for stem in lefts:
        dx = target_x - lefts[stem] if lefts[stem] is not None else 0
        dy = target_y - tops[stem] if tops[stem] is not None and abs(target_y - tops[stem]) < u(60) else 0
        offsets[stem] = (dx, dy)
    width = int(max(pages[m['stem']]['w'] + offsets[m['stem']][0] for m in manifest) + u(20))
    height = int(max(pages[m['stem']]['h'] + abs(offsets[m['stem']][1]) for m in manifest) + u(20))
    return offsets, width, height


def pdf_text(text: str) -> bytes:
    raw = text.encode('cp1252', 'replace')
    return raw.replace(b'\\', b'\\\\').replace(b'(', b'\\(').replace(b')', b'\\)')


def is_foot_folio(line: dict, pg: dict, body_pitch: float) -> bool:
    """A lone number at the foot of a page, set apart from the text above it."""
    if len(line['final']) != 1 or not re.fullmatch(r'\d{1,4}|[ivxlc]{1,7}', line['final'][0]['t']) or line['y1'] < pg['h'] * .85:
        return False
    above = [l['y1'] for l in pg['lines'] if l.get('final') and l is not line and l['y1'] <= line['y0'] + u(2)]
    return not above or line['y0'] - max(above) >= body_pitch * .8


def write_pdf(work: Path, manifest: list[dict], pages: dict, out: Path, offsets: dict, canvas: tuple[int, int],
              sizes: dict, with_page_images: bool) -> None:
    """Hidden text over the page. Glyphs are all one width in the font dictionary, and each word
    is stretched to the width it has on the page, so the positions pdf_to_html.py reads are real."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    canvas_w, canvas_h = canvas
    scale = 72 / (199 * U)
    widths = b'[' + b' '.join(b'500' for _ in range(224)) + b']'
    font_ids = {}
    for key, base in (('FR', b'Times-Roman'), ('FI', b'Times-Italic')):
        font_ids[key] = add(b'<< /Type /Font /Subtype /Type1 /BaseFont /' + base + b' /Encoding /WinAnsiEncoding /FirstChar 32 /LastChar 255 /Widths ' + widths + b' >>')
    page_objects = []
    for m in manifest:
        stem = m['stem']
        pg = pages[stem]
        dx, dy = offsets[stem]
        ops: list[bytes] = []
        for l in pg['lines']:
            if not l.get('final') or (not with_page_images and is_foot_folio(l, pg, sizes['body_pitch'])):
                continue
            size_px = l.get('size_px') or sizes['body_xh'] / .45
            fs = size_px * scale
            base_y = canvas_h - (l['base'] + dy)
            for wd in l['final']:
                t = wd['t']
                if not t:
                    continue
                if wd.get('sup'):
                    fs2 = fs * .62
                    tz = max(40, min(300, (wd['x1'] - wd['x0'] + 1) * scale / (len(t) * .5 * fs2) * 100))
                    ops.append(b'BT /FR %.2f Tf %.1f Tz 1 0 0 1 %.2f %.2f Tm 3 Tr (' % (fs2, tz, (wd['x0'] + dx) * scale, (canvas_h - (wd['sup_bottom'] + dy)) * scale)
                               + pdf_text(t) + b') Tj ET')
                    continue
                # keep a clear gap to the next word, or the reader glues them
                following = [o['x0'] for o in l['final'] if o['x0'] > wd['x0'] and not o.get('sup')]
                right = wd['x1']
                if following and not re.search(r'[-—]$', t):
                    right = min(right, min(following) - size_px * .3)
                tz = max(25, min(400, max(right - wd['x0'], u(3)) * scale / (len(t) * .5 * fs) * 100))
                font = b'FI' if wd.get('italic') else b'FR'
                ops.append(b'BT /' + font + b' %.2f Tf %.1f Tz 1 0 0 1 %.2f %.2f Tm 3 Tr (' % (fs, tz, (wd['x0'] + dx) * scale, base_y * scale) + pdf_text(t) + b') Tj ET')
        xobjects = []
        before = b''
        if with_page_images:
            w, h, bits = load_bits(work / 'bw' / f'{stem}.pbm')
            data = zlib.compress(bits, 6)
            image = add(b'<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray /BitsPerComponent 1 /Decode [1 0] /Filter /FlateDecode /Length %d >>\nstream\n' % (w, h, len(data)) + data + b'\nendstream')
            xobjects.append(b'/Im0 %d 0 R' % image)
            before = b'q %.3f 0 0 %.3f %.3f %.3f cm /Im0 Do Q\n' % (w * scale, h * scale, dx * scale, (canvas_h - h - dy) * scale)
        else:
            for i, f in enumerate(pg.get('figures', [])):
                fw, fh = f[2] - f[0], f[3] - f[1]
                raw = subprocess.run(['magick', str(page_file(work, stem)), '-crop', f'{fw}x{fh}+{f[0]}+{f[1]}', '+repage', '-colorspace', 'sRGB', '-depth', '8', 'rgb:-'],
                                     capture_output=True).stdout
                data = zlib.compress(raw, 6)
                image = add(b'<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n' % (fw, fh, len(data)) + data + b'\nendstream')
                xobjects.append(b'/Im%d %d 0 R' % (i, image))
                before += b'q %.3f 0 0 %.3f %.3f %.3f cm /Im%d Do Q\n' % (fw * scale, fh * scale, (f[0] + dx) * scale, (canvas_h - f[3] - dy) * scale, i)
        resources = b'<< /Font << /FR %d 0 R /FI %d 0 R >>' % (font_ids['FR'], font_ids['FI'])
        if xobjects:
            resources += b' /XObject << ' + b' '.join(xobjects) + b' >>'
        resources += b' >>'
        data = zlib.compress(before + b'\n'.join(ops), 6)
        contents = add(b'<< /Filter /FlateDecode /Length %d >>\nstream\n' % len(data) + data + b'\nendstream')
        page_objects.append((resources, contents))
    parent = len(objects) + len(page_objects) + 1
    kids = []
    for resources, contents in page_objects:
        kids.append(add(b'<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Resources ' % (parent, canvas_w * scale, canvas_h * scale)
                        + resources + b' /Contents %d 0 R >>' % contents))
    add(b'<< /Type /Pages /Count %d /Kids [' % len(kids) + b' '.join(b'%d 0 R' % k for k in kids) + b'] >>')
    root = add(b'<< /Type /Catalog /Pages %d 0 R >>' % parent)
    with open(out, 'wb') as f:
        f.write(b'%PDF-1.5\n%\xe2\xe3\xcf\xd3\n')
        positions = []
        for i, body in enumerate(objects, 1):
            positions.append(f.tell())
            f.write(b'%d 0 obj\n' % i + body + b'\nendobj\n')
        xref = f.tell()
        f.write(b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1))
        for p in positions:
            f.write(b'%010d 00000 n \n' % p)
        f.write(b'trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objects) + 1, root, xref))


def suggest_config(work: Path, manifest: list[dict], pages: dict, offsets: dict, sizes: dict, lang: str) -> None:
    """Settings pdf_to_html.py needs for this PDF. Written to config.json once: edit that file."""
    to_units = 72 / (199 * U) * 1.5      # pdf_to_html.py reads the PDF at zoom 1.5
    heads, bodies = [], []
    numerals = False
    for m in manifest:
        pg = pages[m['stem']]
        dy = offsets[m['stem']][1]
        ls = sorted((l for l in pg['lines'] if l.get('final')), key=lambda l: l['y0'])
        numerals = numerals or any(l.get('numeral') for l in ls)
        if not ls or ls[0]['y0'] > pg['h'] * .15:
            continue
        # a running head: the first line (folio and title may be two pieces), set well apart
        tops = [l for l in ls if l['y0'] <= ls[0]['y0'] + u(12)]
        rest = [l for l in ls if l['y0'] > ls[0]['y0'] + u(12)]
        if rest and rest[0]['y0'] - max(l['y1'] for l in tops) >= sizes['body_pitch'] * .8:
            heads.append(max(l['y1'] for l in tops) + dy)
            bodies.append(rest[0]['y0'] + dy)
    config = dict(lang=lang, footer_band=99999, chapter_title_caps=True, keep_hyphen_words=[], replacements=[],
                  heading_patterns={r'^(CAP[IÍ]TULO|CHAPTER|CHAPITRE|LIBRO|BOOK|PARTE|PART)\s+[IVXLC\d]+$': 1})
    if numerals:
        config['heading_patterns'][r'^[IVX]{1,6}$'] = 2
    if len(heads) >= len(manifest) * .2:
        config['header_band'] = round((sorted(heads)[len(heads) * 9 // 10] + sorted(bodies)[len(bodies) // 10]) / 2 * to_units)
    suggested = json.dumps(config, ensure_ascii=False, indent=1)
    (work / 'config.suggested.json').write_text(suggested)
    if not (work / 'config.json').exists():
        (work / 'config.json').write_text(suggested)


def write_page_map(work: Path, manifest: list[dict], pages: dict) -> list:
    """Page numbers of the rebuilt PDF, for toc_pages and the like, and every line in capitals."""
    rows, capitals = ['page\tscan\tfirst text'], []
    for i, m in enumerate(manifest, 1):
        ls = sorted((l for l in pages[m['stem']]['lines'] if l.get('final')), key=lambda l: l['y0'])
        first = next((' '.join(w['t'] for w in l['final']) for l in ls if l['y0'] >= pages[m['stem']]['h'] * .1), '')
        rows.append(f"{i}\t{m['stem']}\t{first[:70]}")
        for l in ls:
            text = ' '.join(w['t'] for w in l['final'] if not w.get('sup'))
            letters = re.sub(r'[\W\d_]', '', text)
            if len(letters) >= 4 and letters.upper() == letters and l['y0'] >= pages[m['stem']]['h'] * .055:
                capitals.append((i, text[:80]))
    (work / 'pages.tsv').write_text('\n'.join(rows) + '\n')
    return capitals


def write_audit(work: Path, manifest: list[dict], text_pdf: Path, count: int, seed: int) -> None:
    """Random pages to compare by eye: the only check that can see what both engines misread."""
    audit = work / 'audit'
    audit.mkdir(exist_ok=True)
    numbered = list(enumerate(manifest, 1))
    random.Random(seed).shuffle(numbered)
    for i, m in sorted(numbered[:count], key=lambda t: t[0]):
        image = audit / f"{m['stem']}.png"
        subprocess.run(['magick', str(page_file(work, m['stem'])), '-resize', f'{78 / U:.0f}%', '-colors', '8', str(image)], check=True)
        text = subprocess.run(['pdftotext', '-f', str(i), '-l', str(i), '-layout', str(text_pdf), '-'], capture_output=True, text=True).stdout
        (audit / f"{m['stem']}.txt").write_text('\n'.join(re.sub(r' {2,}', ' ', l.strip()) for l in text.splitlines() if l.strip()) + '\n')
        print(f'  audit: {image} and its .txt')


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pdf', required=True, type=Path)
    ap.add_argument('--work', required=True, type=Path, help='folder for everything this script makes; keep it between runs')
    ap.add_argument('--out', type=Path, help='searchable PDF with the page images (default: WORK/book.searchable.pdf)')
    ap.add_argument('--lang', default='es', help='book language: ' + ', '.join(LANGUAGES))
    ap.add_argument('--skip', default='', help='PDF pages to leave out, comma-separated (the cover)')
    ap.add_argument('--only', default='', help='work on these PDF pages only, comma-separated with ranges (a trial)')
    ap.add_argument('--tesseract-lang', help='Tesseract language, when not the default for --lang (spa+eng)')
    ap.add_argument('--vision-lang', help='Vision language, when not the default for --lang')
    ap.add_argument('--spell-lang', help='spell checker language, when not the default for --lang')
    ap.add_argument('--no-vision', action='store_true', help='Tesseract and the old text layer only')
    ap.add_argument('--no-spell', action='store_true', help='without the spell checker')
    ap.add_argument('--sheets', action='store_true', help='build review sheets of what is still in doubt')
    ap.add_argument('--answers', type=Path, help='JSON {"sheet number": "text"} from reading the sheets; "" deletes')
    ap.add_argument('--accept-rest', action='store_true', help='with --answers: the sheets\' other items keep their first reading')
    ap.add_argument('--audit', type=int, default=0, help='write this many random pages to compare by eye')
    ap.add_argument('--seed', type=int, default=1, help='for --audit')
    args = ap.parse_args()
    for tool in ('magick', 'pdfimages', 'pdftotext', 'pdfinfo', 'tesseract'):
        if not shutil.which(tool):
            sys.exit(f'{tool} is required (macOS: brew install imagemagick poppler tesseract tesseract-lang)')
    default = LANGUAGES.get(args.lang, (args.lang, args.lang, args.lang))
    lang_t, lang_v, lang_s = args.tesseract_lang or default[0], args.vision_lang or default[1], args.spell_lang or default[2]
    work = args.work
    work.mkdir(parents=True, exist_ok=True)
    if args.answers or args.accept_rest:
        merge_answers(work, args.answers, args.accept_rest)
    keep: set[int] = set()
    for piece in filter(None, args.only.split(',')):
        a, _, b = piece.partition('-')
        keep.update(range(int(a), int(b or a) + 1))
    manifest, unit = stage_split(args.pdf, work, keep)
    set_unit(unit)
    skip = {int(x) for x in args.skip.split(',') if x}
    manifest = [m for m in manifest if m['pdf'] not in skip and (not keep or m['pdf'] in keep)]
    has_vision = stage_read(work, manifest, lang_t, lang_v, not args.no_vision)
    old = stage_old_layer(args.pdf, work)
    speller = Speller(work, lang_s, not args.no_spell)
    book, stats, flagged = stage_vote(work, manifest, old, speller)
    path = work / 'decisions.json'
    decisions = json.loads(path.read_text()) if path.exists() else {}
    apply_word_decisions(book, decisions, stats)
    measured = pool_map(measure_page, [(str(work), m, book[m['stem']]) for m in manifest], unit)
    pages = {m['stem']: pg for m, pg in zip(manifest, measured)}
    figures = keep_figures(work, manifest, pages, decisions)
    keep_author_rules(pages, manifest, stats)
    problems = attach_markers(pages, manifest, stats, work, args.lang)
    items = [f for f in flagged if f['id'] not in decisions]
    items += section_numerals(manifest, pages, stats, decisions)
    items += volume_numerals(manifest, pages, stats, decisions)
    items += unvouched_words(manifest, pages, stats, decisions, speller, args.lang)
    items += recover_ink(manifest, pages, stats, decisions)
    (work / 'items.json').write_text(json.dumps(items, ensure_ascii=False, indent=0))
    sheets = build_sheets(work, items, unit) if args.sheets else []
    tidy_tokens(pages)
    sizes = assign_sizes(pages)
    offsets, canvas_w, canvas_h = page_offsets(manifest, pages)
    out = args.out or work / 'book.searchable.pdf'
    text_pdf = work / 'text_layer.pdf'
    write_pdf(work, manifest, pages, text_pdf, offsets, (canvas_w, canvas_h), sizes, with_page_images=False)
    write_pdf(work, manifest, pages, out, offsets, (canvas_w, canvas_h), sizes, with_page_images=True)
    suggest_config(work, manifest, pages, offsets, sizes, args.lang)
    capitals = write_page_map(work, manifest, pages)
    (work / 'report.json').write_text(json.dumps(dict(
        pages=len(manifest), unit=unit, vision=has_vision, spell=bool(speller.exe), sizes=sizes, stats=dict(stats), review_items=len(items),
        problems=problems, figures=figures, lines_in_capitals=capitals), ensure_ascii=False, indent=1, default=str))
    if args.audit:
        write_audit(work, manifest, text_pdf, args.audit, args.seed)
    # the summary is the gate: see section 1b of SKILL.md
    total = sum(v for k, v in stats.items() if k.startswith('pages with notes'))
    disagreed = sum(v for k, v in stats.items() if k.startswith('disagreed'))
    print(f'{len(manifest)} book pages, x-height {unit * 15:.0f} px; engines: tesseract' + (' + vision' if has_vision else ' ONLY')
          + ('' if old and any(p['words'] for p in old.values()) else '; no old text layer')
          + ('' if speller.exe else '; no spell checker'))
    print(f"  words: {stats['agreed']} agreed, {disagreed} disagreed")
    print(f"  pages where markers = notes: {stats['pages with notes: markers = notes']} of {total}")
    for key in ('markers placed', 'note numbers corrected from the sequence', 'note numbers restored', 'raised shapes rejected after reading them',
                'raised shapes ignored on pages without notes', 'punctuation recovered beside a marker', 'repeated-author rules kept',
                'lines recovered from ink no engine read'):
        if stats[key]:
            print(f'  {key}: {stats[key]}')
    print(f'  figures kept: {len(figures)}')
    for f in figures[:40]:
        print(f"    {f['id']}")
    print(f'  still to read: {len(items)}' + (f' (on {len(sheets)} sheets in {work / "review"})' if sheets else ' (run with --sheets)' if items else ''))
    for p in problems[:40]:
        print(f'  PROBLEM: {p}')
    print(f'  for pdf_to_html.py: {text_pdf} with {work / "config.json"}; page numbers in {work / "pages.tsv"}')
    print(f'  searchable PDF: {out}')


if __name__ == '__main__':
    main()
