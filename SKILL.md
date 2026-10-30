---
name: pdf-to-docx-epub
description: Convert book-length PDFs into an editable, Word-compatible DOCX and a Kindle-friendly EPUB while preserving structure, notes, bibliography, index, figures, captions, metadata, and navigation. Use when a user asks for PDF-to-DOCX, PDF-to-EPUB, or a complete PDF book conversion workflow.
compatibility: macOS with Python 3, Pandoc, Poppler, xmllint, unzip, and optionally Calibre or epubcheck.
metadata:
  workflow: pdf-reconstruction-docx-epub
  validation: structural-and-link-validation
---

# PDF book conversion: PDF to DOCX and EPUB

Use this skill for long, structured books. Treat the supplied PDF as the only source. Do not silently substitute another edition, search for a replacement, or discard pages that are difficult to extract.

## Core rule

Do not call a file "complete", "Word-compatible", "Kindle-friendly", or "validated" until the relevant validation gates below have passed. If native Word or Calibre cannot be run, say so explicitly; do not claim that it was tested.

## Tool choice: Pandoc versus Calibre

The current official Calibre documentation makes the decision clear for this workflow:

- Calibre supports PDF, DOCX, and EPUB conversion, but its FAQ ranks PDF last among preferred source formats.
- Calibre's PDF guidance says PDFs have fixed page size and text placement, making paragraph boundaries difficult to recover. It warns that complex, multi-column, and image-based PDFs are unsupported, that vector images and tables may not be extracted, and that PDF links and tables of contents are not supported.
- Calibre is therefore not the primary PDF reconstruction tool for a scholarly book with running heads, footnotes, figures, bibliography, index, and a functional TOC.
- Calibre is useful after semantic reconstruction: it can convert a clean DOCX/EPUB, generate device-specific formats such as AZW3, split oversized EPUB flows, inspect the book in its viewer, and perform optional polishing.
- Pandoc is the better core engine here because the input to Pandoc is controlled semantic HTML. Its `--reference-doc` mechanism controls DOCX styles, and its `--css` mechanism controls EPUB styling.

Recommended pipeline:

```text
PDF -> positioned PDF XML -> reconstructed semantic HTML
    -> Pandoc + XML-aware style pass -> DOCX
    -> Pandoc + CSS + EPUB polish -> EPUB
    -> optional Calibre inspection/polish/device conversion
```

Do not replace the reconstruction stage with `ebook-convert input.pdf output.docx` merely because Calibre can technically perform that conversion.

Official references:

- Calibre conversion guidance: https://manual.calibre-ebook.com/en/conversion.html
- Calibre FAQ and supported formats: https://manual.calibre-ebook.com/en/faq.html
- Calibre `ebook-convert` options: https://manual.calibre-ebook.com/generated/en/ebook-convert.html
- Pandoc User's Guide: https://pandoc.org/MANUAL.html

## 1. Establish the workspace and exact source

Use a project directory under the user's home directory and keep temporary extraction under `/tmp` or another disposable directory. Resolve the absolute source path before processing it.

Record:

- exact source PDF path;
- page count;
- encryption status;
- whether text is extractable;
- whether images and fonts are available;
- output paths;
- commands and validation results.

For a local PDF, inspect it first:

```sh
pdfinfo "$PDF"
pdftotext "$PDF" /tmp/book-plain.txt
```

Do not use a plain text dump as the reconstruction source when layout matters. It is only a comparison and diagnostics source.

## 2. Reconstruct semantic HTML from positioned PDF XML

For a book-length PDF, prefer `pdftohtml -xml` and a Python reconstruction pass over a single `pdftotext` conversion. The reconstruction should preserve:

- title page and publisher information;
- contributors and acknowledgements;
- abbreviations;
- chapter and subsection hierarchy;
- italics and superscripts;
- body paragraphs and block quotations;
- footnotes and their numbering;
- figures, captions, and image-disclaimer text;
- bibliography and its subdivisions;
- index entries;
- trailing source fragments rather than silently dropping them.

Keep the source HTML semantic. Use `h1`, `h2`, and `h3` for hierarchy, `p` for paragraphs, `blockquote` for quotations, `figure`/`figcaption` for figures, and an ordered list for notes.

### Paragraph typography

The book convention is:

- first paragraph after a chapter or subsection heading: no first-line indent;
- first prose paragraph after a chapter author line: no first-line indent;
- later body paragraphs: first-line indent;
- block quotations, front matter, bibliography, index, captions, and notes: no ordinary body indent.

Mark the first prose paragraphs in the source HTML with `class="noindent"`. Do not rely on arbitrary paragraph classes surviving Pandoc's EPUB writer; the DOCX formatter and EPUB CSS must also enforce the rule independently.

### Footnotes are a hard requirement

Never leave a footnote marker as a bare `<sup>1</sup>`. It is not usable in an EPUB.

For every body reference, create a unique, chapter-prefixed link:

```html
<a class="noteref" href="#chapter-1-note-1" id="chapter-1-ref-1"><sup>1</sup></a>
```

Give every note a target and a return link:

```html
<li id="chapter-1-note-1">
  <span class="note-number">1.</span> Note text.
  <a class="noteback" href="#chapter-1-ref-1">↩</a>
</li>
```

Prefix IDs with a chapter or section identifier because note numbers normally restart in every chapter. Check footnotes printed in chapter titles as well as those printed in body paragraphs. A note with no body reference is a reconstruction defect unless the PDF proves it is intentionally unreferenced.

## 3. Build the DOCX safely

Generate the first DOCX with Pandoc:

```sh
pandoc book.html \
  --from=html \
  --to=docx \
  --toc \
  --toc-depth=2 \
  --metadata title="BOOK TITLE" \
  --metadata author="AUTHOR OR EDITOR" \
  --metadata lang="en-GB" \
  -o raw.docx
```

Then apply only a controlled style pass. Use the bundled `scripts/format_docx.py` when available:

```sh
python3 "$SKILL_DIR/scripts/format_docx.py" \
  --docx raw.docx \
  --html book.html \
  --out final.docx
```

The style pass must:

- set `BodyText` to a 360-twip (0.25-inch) first-line indent;
- set `FirstParagraph` to `w:firstLine="0"` so it overrides `BodyText` inheritance;
- assign `FirstParagraph` to the first prose paragraph after `Heading1`/`Heading2` and after a chapter author line;
- leave later body paragraphs as `BodyText`;
- keep front matter, bibliography, index, captions, and quotations in their appropriate no-indent styles;
- preserve all hyperlinks and text.

### DOCX failure that must not recur

Do not insert Word XML with broad regular expressions. In particular:

- `w:pageBreakBefore` must not be inserted before `w:pStyle`; that element order makes Word reject the document;
- do not append `w:updateFields` at an arbitrary location in `settings.xml`;
- do not add a footer unless its relationship, content-type override, and section reference are all added in valid order;
- do not patch an already-patched DOCX repeatedly.

The safest default is a clean Pandoc DOCX with a Pandoc TOC field and a minimal, XML-aware style pass. Do not add page-number footers merely for appearance if they risk package validity.

## 4. Build the EPUB safely

Create an EPUB input HTML without the cover image if the cover will be passed separately to Pandoc. This avoids duplicate cover/title-page content.

```sh
pandoc book-epub.html \
  --from=html \
  --to=epub3 \
  --toc \
  --toc-depth=2 \
  --css=assets/epub.css \
  --epub-cover-image=assets/cover-front.jpg \
  --resource-path=. \
  --metadata title="BOOK TITLE" \
  --metadata author="AUTHOR OR EDITOR" \
  --metadata lang="en-GB" \
  -o book.epub
```

After Pandoc, run the bundled EPUB polish pass:

```sh
python3 "$SKILL_DIR/scripts/polish_epub.py" --epub book.epub
```

### EPUB CSS rules learned from Calibre

- Use `text-indent: -1.2em` plus `padding-left: 1.2em` for hanging bibliographic entries. Never use the nonstandard `hanging-indent` property.
- Keep generic selectors before more specific selectors. Calibre's stylelint checks `no-descending-specificity` and reports otherwise harmless-looking order errors.
- Pandoc may emit an empty inline `<style></style>` element into every XHTML file. Remove empty style elements after conversion. Calibre reports this misleadingly as `Unexpected empty source` at line 1 of each file.
- Keep the external stylesheet linked after removing the empty inline style block.
- Preserve footnote `href` links and target IDs through the Pandoc conversion.

## 5. Validation gates

### DOCX gate

Run all of these before handoff:

```sh
unzip -t final.docx
rm -rf /tmp/docx-check && mkdir /tmp/docx-check
unzip -q final.docx -d /tmp/docx-check
find /tmp/docx-check -type f \( -name '*.xml' -o -name '*.rels' \) -print0 |
  xargs -0 -n1 xmllint --noout
```

Also verify:

- every relationship target exists;
- there is exactly one intended `word/document.xml` and no accidental duplicate footer parts;
- `BodyText` has the intended indent and `FirstParagraph` overrides it with zero indent;
- the TOC field exists;
- plain-text extraction from DOCX matches the reconstructed source after accounting only for generated TOC text and navigation-only link labels;
- no PDF running heads, page labels, or extraction artefacts have leaked into the body.

If Microsoft Word is installed, perform a native open-and-close test. If the environment blocks GUI automation, report that limitation instead of claiming a Word test.

### EPUB gate

Run:

```sh
unzip -t book.epub
```

Then verify:

- the first ZIP entry is an uncompressed `mimetype` containing exactly `application/epub+zip`;
- every XHTML/XML file passes `xmllint --noout`;
- no XHTML file contains an empty inline `<style>` block;
- the stylesheet contains no `hanging-indent` property;
- Calibre's CSS linter produces zero warnings when Calibre is available;
- every `noteref` link resolves to a note target in the correct XHTML file;
- every note has a return link, and every note target has at least one incoming body/title reference;
- the EPUB navigation document is listed in the OPF with `properties="nav"`;
- the cover is declared as a cover image and the cover XHTML exists;
- all figures and referenced image assets exist.

Use `scripts/validate_epub.py` for the structural/link checks when available. `epubcheck` is an optional additional gate; it does not replace the footnote-link audit.

## 6. Calibre error map

| Calibre message | Actual cause | Required fix |
|---|---|---|
| `Unexpected empty source` on every XHTML file | Empty inline `<style></style>` emitted by the converter | Remove empty style elements; keep the external stylesheet link |
| `Expected selector "p" to come before selector ".cover-copy p"` | CSS specificity order | Move generic `p` before more specific selectors |
| `Expected selector "h1 + p" ...` | CSS specificity order | Order type-only structural rules before class/ID rules |
| `Expected selector "blockquote p" ...` | Same specificity-order lint | Put `blockquote p` before the other `... p` structural rules |
| `Unexpected unknown property "hanging-indent"` | Nonstandard CSS property | Use negative `text-indent` and matching `padding-left` |
| body superscript does not open a note | Bare `<sup>` with no `href` | Add a noteref anchor, note target, and return link |

## 7. Final response

Give the user:

- the final DOCX path;
- the final EPUB path;
- a short list of validations that actually ran;
- any native-application test that could not run.

Do not claim a file is perfect based only on `unzip -t`. Package integrity, XML validity, text order, styles, navigation, and footnote link resolution are separate checks.
