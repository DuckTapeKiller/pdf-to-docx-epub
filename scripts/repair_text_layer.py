#!/usr/bin/env python3
"""Repair a PDF's text layer from its fonts' glyph names.

Text extraction trusts each font's ToUnicode map; screen rendering uses the
glyphs.  When the two disagree, the page looks right and the text is wrong:
"Cartagena" extracts as "cartagena" (capitals mapped to the small-cap
letters), "C(p;+&l%" as "Capítulo", "/e" as "The".  For simple fonts (Type 1,
TrueType) whose encoding names its glyphs, this script compares every code's
ToUnicode value with the character its glyph name stands for, rebuilds the
maps that disagree, and writes a repaired copy as an incremental update (the
original bytes are kept; only fonts and new maps are appended).

Small-capital glyphs (c.sc, Asmall, ...) are mapped into the Private Use
Area (U+F700 + the lowercase letter, Adobe's convention) so pdf_to_html.py
can tell small caps from lowercase; it maps them back.

Usage:
  repair_text_layer.py --pdf book.pdf --out repaired.pdf [--report repairs.json]
Exit status: 0 (repaired or nothing to repair), 2 (cannot parse: encrypted
or damaged; use the original).
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import zlib
from pathlib import Path

SMALLCAP_BASE = 0xF700
SMALLCAP_SUFFIXES = ('.sc', '.smcp', '.c2sc', '.small', '.sc1', '.scalt')
FIGURE_SUFFIXES = ('.taboldstyle', '.oldstyle', '.osf', '.tosf', '.lf', '.tf', '.tlf', '.pnum',
                   '.tnum', '.onum', '.lnum', '.alt', '.alt1', '.alt2', '.swash', '.ss01', '.ss02',
                   '.cap', '.case', '.ital', '.salt', '.calt', '.liga', '.dlig', '.hlig')

# Adobe Glyph List names likely in book fonts (the rest come from uniXXXX,
# uXXXXX, single letters, and suffix/underscore rules below).
AGL = {
    'space': ' ', 'exclam': '!', 'quotedbl': '"', 'numbersign': '#', 'dollar': '$', 'percent': '%',
    'ampersand': '&', 'quotesingle': "'", 'quoteright': '’', 'parenleft': '(', 'parenright': ')',
    'asterisk': '*', 'plus': '+', 'comma': ',', 'hyphen': '-', 'period': '.', 'slash': '/',
    'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5', 'six': '6',
    'seven': '7', 'eight': '8', 'nine': '9', 'colon': ':', 'semicolon': ';', 'less': '<',
    'equal': '=', 'greater': '>', 'question': '?', 'at': '@', 'bracketleft': '[', 'backslash': '\\',
    'bracketright': ']', 'asciicircum': '^', 'underscore': '_', 'grave': '`', 'quoteleft': '‘',
    'braceleft': '{', 'bar': '|', 'braceright': '}', 'asciitilde': '~', 'exclamdown': '¡',
    'cent': '¢', 'sterling': '£', 'currency': '¤', 'yen': '¥', 'brokenbar': '¦', 'section': '§',
    'dieresis': '¨', 'copyright': '©', 'ordfeminine': 'ª', 'guillemotleft': '«', 'logicalnot': '¬',
    'registered': '®', 'macron': '¯', 'degree': '°', 'plusminus': '±', 'acute': '´', 'mu': 'µ',
    'paragraph': '¶', 'periodcentered': '·', 'cedilla': '¸', 'ordmasculine': 'º',
    'guillemotright': '»', 'onequarter': '¼', 'onehalf': '½', 'threequarters': '¾',
    'questiondown': '¿', 'multiply': '×', 'divide': '÷', 'germandbls': 'ß', 'AE': 'Æ', 'ae': 'æ',
    'OE': 'Œ', 'oe': 'œ', 'Oslash': 'Ø', 'oslash': 'ø', 'Eth': 'Ð', 'eth': 'ð', 'Thorn': 'Þ',
    'thorn': 'þ', 'dotlessi': 'ı', 'Lslash': 'Ł', 'lslash': 'ł', 'florin': 'ƒ', 'circumflex': 'ˆ',
    'caron': 'ˇ', 'breve': '˘', 'dotaccent': '˙', 'ring': '˚', 'ogonek': '˛', 'tilde': '˜',
    'hungarumlaut': '˝', 'endash': '–', 'emdash': '—', 'quotesinglbase': '‚',
    'quotedblleft': '“', 'quotedblright': '”', 'quotedblbase': '„', 'dagger': '†',
    'daggerdbl': '‡', 'bullet': '•', 'ellipsis': '…', 'perthousand': '‰', 'guilsinglleft': '‹',
    'guilsinglright': '›', 'fraction': '⁄', 'trademark': '™', 'minus': '−', 'Euro': '€',
    'nbspace': ' ', 'sfthyphen': '­', 'uni00A0': ' ', 'figuredash': '‒',
    'fi': 'fi', 'fl': 'fl', 'ff': 'ff', 'ffi': 'ffi', 'ffl': 'ffl', 'st': 'st',
    'alpha': 'α', 'beta': 'β', 'gamma': 'γ', 'delta': 'δ', 'epsilon': 'ε', 'zeta': 'ζ', 'eta': 'η',
    'theta': 'θ', 'iota': 'ι', 'kappa': 'κ', 'lambda': 'λ', 'nu': 'ν', 'xi': 'ξ', 'omicron': 'ο',
    'pi': 'π', 'rho': 'ρ', 'sigma': 'σ', 'sigma1': 'ς', 'sigmafinal': 'ς', 'tau': 'τ',
    'upsilon': 'υ', 'phi': 'φ', 'chi': 'χ', 'psi': 'ψ', 'omega': 'ω', 'Alpha': 'Α', 'Beta': 'Β',
    'Gamma': 'Γ', 'Delta': 'Δ', 'Epsilon': 'Ε', 'Zeta': 'Ζ', 'Eta': 'Η', 'Theta': 'Θ', 'Iota': 'Ι',
    'Kappa': 'Κ', 'Lambda': 'Λ', 'Mu': 'Μ', 'Nu': 'Ν', 'Xi': 'Ξ', 'Omicron': 'Ο', 'Pi': 'Π',
    'Rho': 'Ρ', 'Sigma': 'Σ', 'Tau': 'Τ', 'Upsilon': 'Υ', 'Phi': 'Φ', 'Chi': 'Χ', 'Psi': 'Ψ',
    'Omega': 'Ω',
}
ACCENTS = {'acute': '́', 'grave': '̀', 'circumflex': '̂', 'tilde': '̃',
           'dieresis': '̈', 'ring': '̊', 'cedilla': '̧', 'caron': '̌',
           'macron': '̄', 'breve': '̆', 'ogonek': '̨', 'dotaccent': '̇',
           'hungarumlaut': '̋', 'commaaccent': '̦'}


def glyph_unicode(name: str) -> tuple[str, bool] | None:
    """(text, smallcap) for a glyph name, or None when the name says nothing."""
    import unicodedata
    smallcap = False
    base = name
    for suffix in SMALLCAP_SUFFIXES:
        if base.endswith(suffix):
            base, smallcap = base[: -len(suffix)], True
    if base.endswith('small') and len(base) > 5:
        base, smallcap = base[:-5], True
    changed = True
    while changed:
        changed = False
        for suffix in FIGURE_SUFFIXES:
            if base.endswith(suffix):
                base, changed = base[: -len(suffix)], True
        if base.endswith('oldstyle') and len(base) > 8:
            base, changed = base[:-8], True
        if '.' in base:
            base, changed = base.split('.', 1)[0], True
    if '_' in base:
        parts = [glyph_unicode(p) for p in base.split('_')]
        if all(parts):
            return ''.join(p[0] for p in parts), smallcap
        return None
    if base in AGL:
        text = AGL[base]
    elif re.fullmatch(r'uni([0-9A-Fa-f]{4})+', base):
        text = ''.join(chr(int(base[i:i + 4], 16)) for i in range(3, len(base), 4))
    elif re.fullmatch(r'u[0-9A-Fa-f]{4,6}', base):
        text = chr(int(base[1:], 16))
    elif re.fullmatch(r'[A-Za-z]', base):
        text = base
    else:
        match = re.fullmatch(r'([A-Za-z]|AE|ae|OE|oe)(acute|grave|circumflex|tilde|dieresis|ring|'
                             r'cedilla|caron|macron|breve|ogonek|dotaccent|hungarumlaut|commaaccent)', base)
        if not match:
            return None
        letter = AGL.get(match.group(1), match.group(1))
        text = unicodedata.normalize('NFC', letter + ACCENTS[match.group(2)])
    if smallcap and len(text) == 1 and text.isalpha():
        lower = text.lower()
        if ord(lower) < 0x100:
            return chr(SMALLCAP_BASE + ord(lower)), True
        return lower, True
    return text, smallcap


# --------------------------------------------------------------------------
# Minimal PDF reader (classic and stream cross-reference tables)

class PDF:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offsets: dict[int, tuple[int, int]] = {}   # num -> (type, value)
        self.objstm_cache: dict[int, dict[int, bytes]] = {}
        self.trailer = b''
        start = int(re.findall(rb'startxref\s+(\d+)', data)[-1])
        self.last_xref = start
        seen = set()
        while start is not None and start not in seen:
            seen.add(start)
            start = self._read_xref(start)

    def _read_xref(self, pos: int):
        data = self.data
        if data[pos:pos + 4] == b'xref':
            match = re.compile(rb'trailer\s*<<').search(data, pos)
            table = data[pos + 4:match.start()]
            lines = table.split(b'\n')
            number = None
            for line in lines:
                parts = line.split()
                if len(parts) == 2:
                    number = int(parts[0])
                elif len(parts) == 3 and number is not None:
                    if parts[2].startswith(b'n') and number not in self.offsets:
                        self.offsets[number] = (1, int(parts[0]))
                    number += 1
            trailer = self._dict_at(match.end() - 2)
            if not self.trailer:
                self.trailer = trailer
            hybrid = re.search(rb'/XRefStm\s+(\d+)', trailer)
            if hybrid:
                self._read_xref(int(hybrid.group(1)))
            prev = re.search(rb'/Prev\s+(\d+)', trailer)
            return int(prev.group(1)) if prev else None
        # cross-reference stream
        head = re.compile(rb'\d+\s+\d+\s+obj').match(data, pos)
        if not head:
            raise ValueError('cannot find the cross-reference table')
        obj = self._object_at(head.end())
        dictionary, stream = obj
        if not self.trailer:
            self.trailer = dictionary
        widths = [int(x) for x in re.search(rb'/W\s*\[([^\]]*)\]', dictionary).group(1).split()]
        size = int(re.search(rb'/Size\s+(\d+)', dictionary).group(1))
        index = re.search(rb'/Index\s*\[([^\]]*)\]', dictionary)
        ranges = [int(x) for x in index.group(1).split()] if index else [0, size]
        raw = decode_stream(dictionary, stream)
        row = sum(widths)
        cursor = 0
        for first, count in zip(ranges[0::2], ranges[1::2]):
            for number in range(first, first + count):
                fields, offset = [], cursor
                for width in widths:
                    fields.append(int.from_bytes(raw[offset:offset + width], 'big') if width else None)
                    offset += width
                cursor += row
                kind = fields[0] if widths[0] else 1
                # type 1: (1, byte offset); type 2: (2, number of the object stream)
                if number not in self.offsets and kind in (1, 2):
                    self.offsets[number] = (kind, fields[1])
        prev = re.search(rb'/Prev\s+(\d+)', dictionary)
        return int(prev.group(1)) if prev else None

    def _dict_at(self, pos: int) -> bytes:
        depth, i, data = 0, pos, self.data
        while i < len(data):
            if data[i:i + 2] == b'<<':
                depth += 1
                i += 2
                continue
            if data[i:i + 2] == b'>>':
                depth -= 1
                i += 2
                if depth == 0:
                    return data[pos:i]
                continue
            if data[i:i + 1] == b'(':
                i = skip_string(data, i)
                continue
            i += 1
        raise ValueError('unterminated dictionary')

    def _object_at(self, pos: int) -> tuple[bytes, bytes | None]:
        data = self.data
        while data[pos:pos + 1] in b' \r\n\t':
            pos += 1
        if data[pos:pos + 2] == b'<<':
            dictionary = self._dict_at(pos)
            after = pos + len(dictionary)
            match = re.compile(rb'\s*stream\r?\n').match(data, after)
            if match:
                length = re.search(rb'/Length\s+(\d+)(\s+\d+\s+R)?', dictionary)
                start = match.end()
                if length and not length.group(2):
                    stream = data[start:start + int(length.group(1))]
                else:
                    end = data.find(b'endstream', start)
                    stream = data[start:end].rstrip(b'\r\n')
                return dictionary, stream
            return dictionary, None
        end = data.find(b'endobj', pos)
        return data[pos:end].strip(), None

    def get(self, number: int) -> tuple[bytes, bytes | None] | None:
        entry = self.offsets.get(number)
        if entry is None:
            return None
        kind, value = entry
        if kind == 1:
            head = re.compile(rb'\s*\d+\s+\d+\s+obj').match(self.data, value)
            if not head:
                return None
            return self._object_at(head.end())
        objects = self.objstm_cache.get(value)
        if objects is None:
            container = self.get(value)
            if container is None:
                return None
            dictionary, stream = container
            raw = decode_stream(dictionary, stream)
            count = int(re.search(rb'/N\s+(\d+)', dictionary).group(1))
            first = int(re.search(rb'/First\s+(\d+)', dictionary).group(1))
            header = [int(x) for x in raw[:first].split()]
            objects = {}
            for k in range(count):
                num, off = header[2 * k], header[2 * k + 1]
                end = header[2 * k + 3] if k + 1 < count else len(raw) - first
                objects[num] = raw[first + off:first + end].strip()
            self.objstm_cache[value] = objects
        body = objects.get(number)
        return (body, None) if body is not None else None

    def all_numbers(self):
        return sorted(self.offsets)


def skip_string(data: bytes, i: int) -> int:
    depth = 0
    while i < len(data):
        c = data[i:i + 1]
        if c == b'\\':
            i += 2
            continue
        if c == b'(':
            depth += 1
        elif c == b')':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def decode_stream(dictionary: bytes, stream: bytes) -> bytes:
    filters = re.findall(rb'/(FlateDecode|Fl|ASCIIHexDecode|AHx|ASCII85Decode|A85)', dictionary)
    data = stream
    for name in filters:
        if name in (b'FlateDecode', b'Fl'):
            data = zlib.decompressobj().decompress(data)
        elif name in (b'ASCIIHexDecode', b'AHx'):
            data = bytes.fromhex(re.sub(rb'[^0-9A-Fa-f]', b'', data.split(b'>')[0]).decode())
        else:
            text = data.strip()
            if not text.startswith(b'<~'):
                text = b'<~' + text
            data = base64.a85decode(text, adobe=True)
    predictor = re.search(rb'/Predictor\s+(\d+)', dictionary)
    if predictor and int(predictor.group(1)) >= 10:   # PNG predictors, one filter byte per row
        columns_match = re.search(rb'/Columns\s+(\d+)', dictionary)
        columns = int(columns_match.group(1)) if columns_match else 1
        out, previous, i = bytearray(), bytearray(columns), 0
        while i < len(data):
            kind, row = data[i], bytearray(data[i + 1:i + 1 + columns])
            for k in range(len(row)):
                left = row[k - 1] if k else 0
                up = previous[k] if k < len(previous) else 0
                upper_left = previous[k - 1] if k and k - 1 < len(previous) else 0
                if kind == 1:
                    row[k] = (row[k] + left) & 0xFF
                elif kind == 2:
                    row[k] = (row[k] + up) & 0xFF
                elif kind == 3:
                    row[k] = (row[k] + (left + up) // 2) & 0xFF
                elif kind == 4:
                    p = left + up - upper_left
                    pa, pb, pc = abs(p - left), abs(p - up), abs(p - upper_left)
                    row[k] = (row[k] + (left if pa <= pb and pa <= pc else up if pb <= pc else upper_left)) & 0xFF
            out += row
            previous = row
            i += 1 + columns
        data = bytes(out)
    return data


# --------------------------------------------------------------------------
# Fonts

BASE_CODECS = {b'WinAnsiEncoding': 'cp1252', b'MacRomanEncoding': 'mac_roman', b'StandardEncoding': 'latin-1'}


def parse_cmap(text: str) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for block in re.findall(r'beginbfchar(.*?)endbfchar', text, re.S):
        for source, target in re.findall(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>', block):
            mapping[int(source, 16)] = bytes.fromhex(target).decode('utf-16-be', 'replace') if target else ''
    for block in re.findall(r'beginbfrange(.*?)endbfrange', text, re.S):
        for first, last, target in re.findall(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]+>|\[[^\]]*\])', block):
            a, b = int(first, 16), int(last, 16)
            if target.startswith('['):
                for k, item in enumerate(re.findall(r'<([0-9A-Fa-f]*)>', target)):
                    mapping[a + k] = bytes.fromhex(item).decode('utf-16-be', 'replace')
            else:
                value = bytes.fromhex(target[1:-1]).decode('utf-16-be', 'replace')
                for k in range(b - a + 1):
                    mapping[a + k] = value[:-1] + chr(ord(value[-1]) + k) if value else ''
    return mapping


def encoding_names(pdf: PDF, font: bytes) -> tuple[dict[int, str], str | None]:
    """Glyph names by code from /Differences, and the base codec."""
    match = re.search(rb'/Encoding\s*(/\w+|\d+\s+\d+\s+R|<<)', font)
    if not match:
        return {}, None
    token = match.group(1)
    if token.startswith(b'/'):
        return {}, BASE_CODECS.get(token[1:])
    if token == b'<<':
        body = font[match.start(1):]
        body = body[:body.find(b'>>') + 2]
    else:
        obj = pdf.get(int(token.split()[0]))
        if obj is None:
            return {}, None
        body = obj[0]
    base = re.search(rb'/BaseEncoding\s*/(\w+)', body)
    codec = BASE_CODECS.get(base.group(1)) if base else 'latin-1'
    names: dict[int, str] = {}
    differences = re.search(rb'/Differences\s*\[(.*?)\]', body, re.S)
    if differences:
        code = 0
        for token in re.findall(rb'\d+|/[^\s/\[\]<>()]+', differences.group(1)):
            if token.startswith(b'/'):
                names[code] = token[1:].decode('latin-1')
                code += 1
            else:
                code = int(token)
    return names, codec


def expected_text(code: int, names: dict[int, str], codec: str | None):
    name = names.get(code)
    if name is not None:
        return glyph_unicode(name)
    if codec and 0x20 <= code <= 0xFF:
        try:
            text = bytes([code]).decode(codec)
        except UnicodeDecodeError:
            return None
        return (text, False) if text.isprintable() else None
    return None


def build_cmap(mapping: dict[int, str]) -> bytes:
    entries = [f'<{code:02X}> <{text.encode("utf-16-be").hex().upper()}>' for code, text in sorted(mapping.items()) if text]
    blocks = []
    for i in range(0, len(entries), 100):
        chunk = entries[i:i + 100]
        blocks.append(f'{len(chunk)} beginbfchar\n' + '\n'.join(chunk) + '\nendbfchar')
    return ('/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n'
            '/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n'
            '/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n'
            '1 begincodespacerange\n<00> <FF>\nendcodespacerange\n'
            + '\n'.join(blocks) + '\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n').encode('latin-1')


def repair(data: bytes):
    if b'/Encrypt' in data[-4096:] or re.search(rb'/Encrypt\s+\d+\s+\d+\s+R', data):
        raise ValueError('encrypted PDF')
    pdf = PDF(data)
    report = {'fonts_checked': 0, 'fonts_repaired': [], 'codes_changed': 0, 'smallcap_codes': 0}
    updates: dict[int, bytes] = {}
    next_number = max(pdf.all_numbers()) + 1
    size_match = re.search(rb'/Size\s+(\d+)', pdf.trailer)
    next_number = max(next_number, int(size_match.group(1)) if size_match else 0)
    cmap_cache: dict[tuple, int] = {}
    for number in pdf.all_numbers():
        obj = pdf.get(number)
        if obj is None:
            continue
        font, _ = obj
        if not re.search(rb'/Type\s*/Font\b', font) or not re.search(rb'/Subtype\s*/(Type1|MMType1|TrueType)\b', font):
            continue
        tounicode = re.search(rb'/ToUnicode\s+(\d+)\s+(\d+)\s+R', font)
        names, codec = encoding_names(pdf, font)
        if not tounicode or not (names or codec):
            continue
        report['fonts_checked'] += 1
        stream_obj = pdf.get(int(tounicode.group(1)))
        if stream_obj is None or stream_obj[1] is None:
            continue
        current = parse_cmap(decode_stream(*stream_obj).decode('latin-1'))
        fixed = dict(current)
        changes = []
        smallcaps = 0
        for code in set(current) | set(names):
            expected = expected_text(code, names, codec)
            if expected is None:
                continue
            text, smallcap = expected
            have = current.get(code)
            if smallcap and text != have:
                fixed[code] = text
                smallcaps += 1
                continue
            if have is None or have == text or not text.strip():
                continue
            if len(text) == 1 and text.isalpha() and len(have or '') <= 1:
                fixed[code] = text            # wrong letter or wrong case
                changes.append((code, have, text))
            elif len(have or '') == 1 and not have.isalnum() and text.isalnum():
                fixed[code] = text            # punctuation standing for a letter or digit
                changes.append((code, have, text))
        if not changes and not smallcaps:
            continue
        key = tuple(sorted(fixed.items()))
        if key not in cmap_cache:
            cmap = build_cmap(fixed)
            cmap_cache[key] = next_number
            updates[next_number] = b'<< /Length %d >>\nstream\n' % len(cmap) + cmap + b'\nendstream'
            next_number += 1
        new_font = font[:tounicode.start()] + b'/ToUnicode %d 0 R' % cmap_cache[key] + font[tounicode.end():]
        updates[number] = new_font
        base = re.search(rb'/BaseFont\s*/([^\s/<>\[\]]+)', font)
        report['fonts_repaired'].append({
            'object': number, 'font': base.group(1).decode('latin-1') if base else '?',
            'changes': [f'{chr(c) if 32 <= c < 127 else hex(c)}: {h!r} -> {t!r}' for c, h, t in changes[:40]],
            'smallcap_codes': smallcaps})
        report['codes_changed'] += len(changes)
        report['smallcap_codes'] += smallcaps
    return pdf, updates, report


def write_update(data: bytes, pdf: PDF, updates: dict[int, bytes]) -> bytes:
    out = bytearray(data)
    if not out.endswith(b'\n'):
        out += b'\n'
    offsets = {}
    for number in sorted(updates):
        offsets[number] = len(out)
        out += b'%d 0 obj\n' % number + updates[number] + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n'
    for number in sorted(offsets):
        out += b'%d 1\n%010d 00000 n \n' % (number, offsets[number])
    size = max(max(updates) + 1, int(re.search(rb'/Size\s+(\d+)', pdf.trailer).group(1)))
    keep = b''.join(m.group(0) + b' ' for m in re.finditer(rb'/(Root|Info|ID)\s*(\d+\s+\d+\s+R|\[[^\]]*\])', pdf.trailer))
    out += b'trailer\n<< /Size %d %s/Prev %d >>\nstartxref\n%d\n%%%%EOF\n' % (size, keep, pdf.last_xref, xref)
    return bytes(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--pdf', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    data = args.pdf.read_bytes()
    try:
        pdf, updates, report = repair(data)
    except (ValueError, KeyError, IndexError, zlib.error, AttributeError) as error:
        print(f'cannot repair {args.pdf}: {error}; extract from the original', file=sys.stderr)
        sys.exit(2)
    if updates:
        args.out.write_bytes(write_update(data, pdf, updates))
    else:
        args.out.write_bytes(data)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'{args.out}: {report["fonts_checked"]} font(s) checked, {len(report["fonts_repaired"])} repaired, '
          f'{report["codes_changed"]} wrong code(s) corrected, {report["smallcap_codes"]} small-cap code(s) marked')


if __name__ == '__main__':
    main()
