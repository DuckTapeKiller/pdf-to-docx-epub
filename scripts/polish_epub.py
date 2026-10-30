#!/usr/bin/env python3
"""Remove converter artefacts from an EPUB without changing its reading order."""
from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path

EMPTY_STYLE = re.compile(r"\s*<style\b[^>]*>\s*</style>\s*", re.IGNORECASE)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--epub', required=True, type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()

    source = args.epub.resolve()
    output = (args.out or source).resolve()
    temporary = output.with_suffix(output.suffix + '.tmp')
    removed = 0

    with zipfile.ZipFile(source, 'r') as zin, zipfile.ZipFile(temporary, 'w') as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.lower().endswith(('.xhtml', '.html')):
                text = data.decode('utf-8')
                text, count = EMPTY_STYLE.subn('\n', text)
                removed += count
                data = text.encode('utf-8')
            zout.writestr(item, data)

    temporary.replace(output)
    print(f'{output}: removed {removed} empty inline style element(s)')


if __name__ == '__main__':
    main()
