# PDF to DOCX and EPUB conversion skill

A reusable Pi skill and supporting scripts for converting book-length PDFs into:

- an editable, Word-compatible DOCX; and
- a Kindle-friendly EPUB with navigation, figures, metadata, and linked footnotes.

The workflow reconstructs the PDF into semantic HTML before conversion. It uses positioned PDF XML rather than a plain-text dump so that headings, paragraphs, figures, captions, notes, bibliography entries, index entries, and trailing source fragments can be recovered deliberately.

## Use in Pi

Invoke the skill with:

```text
/skill:pdf-to-docx-epub
```

Install or copy the repository contents into Pi's skill directory so that `SKILL.md` is at the root of the skill directory.

## Pipeline

```text
PDF -> positioned PDF XML -> semantic HTML
    -> Pandoc + DOCX style pass -> DOCX
    -> Pandoc + CSS + EPUB polish -> EPUB
    -> optional Calibre inspection or device conversion
```

Calibre is treated as a post-processing and inspection tool. It is not the primary reconstruction engine for complex scholarly PDFs.

## Included scripts

- `scripts/format_docx.py` applies the book paragraph styles to a Pandoc-generated DOCX without unsafe broad XML substitutions.
- `scripts/polish_epub.py` removes converter artefacts such as empty inline style elements while preserving EPUB reading order.

See `SKILL.md` for the complete workflow, typography rules, footnote-link requirements, validation gates, and Calibre error map.

## Requirements

The workflow expects macOS with Python 3, Pandoc, Poppler, `xmllint`, and `unzip`. Calibre and `epubcheck` are optional validation or post-processing tools.

## Scope

The supplied PDF is the only source. The workflow does not silently substitute another edition or discard difficult-to-classify pages.
