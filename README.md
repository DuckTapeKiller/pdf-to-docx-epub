# PDF to DOCX and EPUB conversion skill

A Pi skill and scripts for converting book-length PDFs (monographs, edited volumes,
scholarly books) into:

- an editable DOCX with real Word footnotes, book paragraph styles, chapters on new pages
  and note numbers that restart per chapter like the print edition; and
- a reflowable EPUB 3 whose footnotes are clickable pop-ups (Apple Books, Kobo, Kindle,
  Thorium) and link back to the text in every other reader.

The PDF is first reconstructed into semantic HTML, with paragraphs, headings, notes,
quotations, small caps, figures, bibliography and index recovered from Poppler's
positioned text. Pandoc then writes both outputs from that one source, and each is
validated against it and against the PDF's own text layer.

## Use in Pi

```text
/skill:pdf-to-docx-epub
```

Copy the repository into Pi's skill directory so that `SKILL.md` sits at the root of the
skill directory. `SKILL.md` has the complete workflow.

## Pipeline

```text
PDF ─ pdf_to_html.py ─> book.html ─ check_text.py (against the PDF)
book.html ─ pandoc ─> raw.docx ─ format_docx.py ─> final.docx ─ validate_docx.py
book.html ─ pandoc ─> raw.epub ─ polish_epub.py ─> book.epub  ─ validate_epub.py + epubcheck
```

## Scripts

| Script | Purpose |
|---|---|
| `scripts/pdf_to_html.py` | Reconstructs semantic HTML from the PDF and writes a JSON report of every decision to review (garbled fonts, removed running heads, headings, notes, hyphenation). |
| `scripts/check_text.py` | Compares the HTML with `pdftotext` output to catch dropped, duplicated or glued text. |
| `scripts/format_docx.py` | Applies book styles, chapter sections, per-chapter note numbering and custom note marks to Pandoc's DOCX without breaking Word's XML. |
| `scripts/validate_docx.py` | Checks package integrity, schema order, footnotes, bookmarks and text. |
| `scripts/polish_epub.py` | Normalises EPUB notes for pop-ups and back links, applies the printed labels, removes Pandoc's empty styles. |
| `scripts/validate_epub.py` | Checks package, links, notes in both directions, labels, TOC and text (complements epubcheck). |
| `assets/epub.css` | Reflowable book stylesheet. |

The scripts use only the Python 3 standard library.

## Requirements

Python 3.9+, Pandoc 3.x and Poppler (macOS: `brew install pandoc poppler`).
Optional: epubcheck, Calibre, LibreOffice, and ocrmypdf for scanned PDFs.

## Scope

The supplied PDF is the only source. The workflow does not substitute another edition and
does not discard difficult pages; anything it cannot reconstruct is reported.
