---
name: pdf-to-docx-epub
description: Convert book-length PDFs (monographs, edited volumes, scholarly books), including scans and two-page spreads, into an editable Word DOCX with real footnotes and a reflowable EPUB 3 with pop-up footnotes, preserving headings, chapter authors, block quotations, italics and small caps, figures, bibliography and index. Use for PDF-to-DOCX, PDF-to-EPUB, DOCX-to-EPUB with working notes, or a complete PDF book conversion.
compatibility: macOS or Linux with Python 3.9+ (standard library only), Pandoc 3.x, and Poppler (pdftohtml, pdftotext, pdftoppm). Scanned books also need ImageMagick and Tesseract, and read best on macOS, which adds Apple Vision and the system spell checker. Optional - epubcheck, Calibre, LibreOffice, mutool or qpdf for booklets.
metadata:
  workflow: pdf-reconstruction-docx-epub
  validation: text-structure-footnote-and-package-validation
---

# PDF book to DOCX and EPUB

The PDF the user supplied is the only source. Never substitute another edition, never
drop pages because they are hard to extract, and never call an output complete,
"validated" or "perfect" until the gates in section 8 pass. If an application test
(Word, Apple Books, Kindle Previewer) could not be run, say so.

## 0. Setup

`SKILL_DIR` below means the directory that contains this `SKILL.md`; set it first:

```sh
SKILL_DIR="/absolute/path/to/pi-pdf-to-docx-epub"
```

Requirements (macOS: `brew install pandoc poppler`; optional `brew install epubcheck`):

- Pandoc 3.x. Check `pandoc --version`. Pandoc 3.9+ no longer writes a number or a
  back link inside EPUB notes; `polish_epub.py` restores both, so always run it.
- Poppler: `pdfinfo`, `pdffonts`, `pdftohtml`, `pdftotext`, `pdftoppm`, `pdfimages`.
- Python 3 standard library only. Use a scratch working directory for each book.
- Scanned books (section 1b): `brew install imagemagick tesseract tesseract-lang`. On macOS,
  `xcode-select --install` as well, so `swiftc` can build the helpers for Apple Vision and
  the spell checker.

Pipeline:

```text
PDF ──pdf_to_html.py──> book.html (semantic, notes as epub:type)
         (repairs the PDF's text layer first: repair_text_layer.py)
book.html ──check_text.py + check_structure.py (both against the PDF itself)
book.html ──pandoc──> raw.docx ──format_docx.py──> final.docx ──validate_docx.py
book.html ──pandoc──> raw.epub ──polish_epub.py──> book.epub ──validate_epub.py + epubcheck
(final.docx edited by the user ──pandoc -f docx+styles──> EPUB ──polish_epub.py──> same result)
a scan first goes through: PDF ──scan_to_pdf.py──> text_layer.pdf, which pdf_to_html.py then reads
```

Why Pandoc and not `ebook-convert book.pdf`: Calibre's own manual ranks PDF last as an
input format; it cannot recover paragraphs, notes, links or the TOC from a book PDF.
Calibre is useful afterwards (viewer, AZW3, Check Book), not for reconstruction.

## 1. Inspect the PDF

```sh
pdfinfo book.pdf
pdffonts book.pdf
pdftotext -layout -f 15 -l 16 book.pdf - | head -80
pdftoppm -png -r 80 -f 15 -l 16 book.pdf page    # look at the pages themselves
```

Record the page count, encryption, the pages of the cover, half-title, series page,
title page, copyright page, printed contents and list of illustrations, and how notes,
running heads and chapter openings look. Note the book's language: set it in the config
(`"lang": "es-ES"`); if you leave it out it is inferred from the text and printed, and
`check_structure.py` fails when the declared language is not the language of the text.

- Landscape pages holding two book pages side by side (a booklet, a 2-up scan) are read
  left half, then right half (`two_up_pages` in the report). A booklet printed in
  imposition order (last page beside the first) must be split and reordered first, e.g.
  `mutool poster -x 2 book.pdf split.pdf` and `qpdf split.pdf --pages split.pdf 1,4,... -- ordered.pdf`.

- Fonts with `uni no` or `Custom` encoding, or a PDF re-saved by macOS Preview
  ("Quartz PDFContext" producer), often extract as garbage (section 3).
- A scan: `pdfimages -list` shows one full-page image per page, whether or not there is a
  text layer, and `pdffonts` shows a single font or none. Go to section 1b first. Do not
  give a scan straight to `pdf_to_html.py`: it finds no type sizes, italics or note markers.

## 1b. Scanned books

`scan_to_pdf.py` rebuilds the book from the page images: it splits two-page spreads at the
gutter, straightens each page, reads it with two engines (Apple Vision and Tesseract), votes
word by word with the old text layer and the spell checker as tie-breakers, and takes from
the pixels what no engine reports: note rules, raised markers, type sizes, italics, figures
and rules standing for a repeated author. It writes `text_layer.pdf`, which the rest of this
skill reads like any other PDF.

```sh
python3 "$SKILL_DIR/scripts/scan_to_pdf.py" --pdf book.pdf --work /abs/path/scan --lang es --skip 1 --sheets
```

`--skip` leaves out the cover (take the cover image from `scan/img/`). A whole book takes a
long time, so run it in the background (`nohup ... > scan.log 2>&1 &`) and read the log.
Later runs reuse what is in `--work`, so keep that folder. `--only 40-50` tries a few PDF
pages first.

Then work through the summary it prints:

1. **Engines.** The first line must say `tesseract + vision`. `tesseract ONLY` or `no spell
   checker` means nothing could catch most misreadings: say so in the final response.
2. **Sheets.** Read every sheet in `scan/review/` with the read tool. Each crop is labelled
   `number: first reading | second reading | old text`, or `(numeral)`, `(volume?)`,
   `(unvouched)` or `(no engine)`. The labels are hints; the crop decides. For each crop whose
   first reading is wrong, write the text as printed, without its note marker, in a JSON file
   of `{"12": "II:", "47": ""}`, where `""` deletes (a speck, or a marker read as a word).
   Keep the book's own misprints: the page is the source, and `(unvouched)` words are often
   printed that way. Then run the same command with
   `--answers answers.json --accept-rest`, which keeps the first reading of every crop not
   listed, and repeat until `still to read: 0`.
3. **`pages where markers = notes`** must cover every page. A `PROBLEM` line names the page;
   look at it and repair the note in `book.html` after step 5 (a `?` marks a marker it could
   not number). Many `raised shapes ignored on pages without notes` means this book's notes
   have no rule above them, which the script cannot yet separate: say so.
4. **`figures kept`.** Look at the crops in `scan/figures/`. A false one is removed by adding
   its id with `""` to the answers. Pictures the script missed are inserted by hand.
5. **Reconstruct.** Edit `scan/config.json`, which already holds the bands, language and title
   joining the book needs: add `title`, `toc_pages` and `front_matter_until`, with page numbers
   from `scan/pages.tsv`, and `heading_patterns` for the headings in `lines_in_capitals` in
   `scan/report.json` (front and back matter). Then follow section 2 with
   `--pdf scan/text_layer.pdf --config scan/config.json`, and run `check_text.py` and
   `check_structure.py` against `scan/text_layer.pdf`. Put later corrections in `replacements`
   in the config, not in `book.html`, so they survive a re-run.
6. **Audit.** Add `--audit 8` to write random pages to `scan/audit/`: the page image and its
   text. Compare them word by word. It is the only check that can see what both engines
   misread, because every other check compares against the same reading. An error found here
   usually has siblings, so search the book for its class, then audit new pages.

`book.searchable.pdf` in the same folder (or `--out`) has the page images under the corrected
text, one book page per PDF page. Give it to the user as well.

Not handled: pages scanned sideways, booklets in imposition order, and tables, which come out
as lines of text as in section 4. Report anything left open.

## 2. Reconstruct the HTML

```sh
python3 "$SKILL_DIR/scripts/pdf_to_html.py" --print-config > config.json   # then edit
python3 "$SKILL_DIR/scripts/pdf_to_html.py" --pdf book.pdf --out book.html \
  --config config.json --media media
```

It first repairs the PDF's text layer in a temporary copy (`repair_text_layer.py`, also
usable on its own): fonts whose Unicode map contradicts their own glyph names, the usual
cause of lowercase headings ("capítulo 1" printed "CAPÍTULO 1") and of small capitals
that extract as plain lowercase, are corrected from the glyph names (`--no-repair` skips
this; `text_layer_repair` in the report lists every change). It then reads Poppler's
positioned XML and writes `book.html` plus `book.report.json`.

It measures the body, note and quotation sizes, margins, indent and line pitch, then:
removes running heads, folios and printer slugs (but keeps a title set high on its page
in display type); reads two-column pages (indexes) column by column; joins paragraphs
across lines and pages; repairs line-end hyphenation from the book's own vocabulary, the
language's rules (Spanish and Portuguese repeat a real hyphen on the next line) and a
word list (keeping "Quispe-Agnoli", "Anglo-Norman"); restores spaces lost at font
changes, after note markers and after drop caps; keeps only what lies inside each page's
crop box (a printer's PDF also carries slug lines, crop marks and, on page 1, the whole
cover spread); drops text printed twice for a bold effect and rotated text (a spine); detects chapters (with their "Chapter 1" label),
sections (including small-caps and wrapped headings) and chapter authors, following the
PDF's own bookmarks when it has them; keeps section numbers printed in the margin;
reads footnotes from the foot of each page, including notes continued on the next page,
and links each to its marker, never letting a note run into an index or bibliography;
builds block quotations, lists, bibliography and abbreviation lists (hanging indents),
index entries (wrapped entries joined, small-caps headwords kept), figures with captions
and front matter; skips printed contents pages and the cover page.

Config keys you will use most:

| key | use |
|---|---|
| `lang`, `title` | language (drives hyphenation in readers and the hyphen rules) and HTML title; both are inferred when absent |
| `skip_pages` | cover, half-title, series page, title page (Pandoc makes its own title page), blank pages |
| `toc_pages` | printed contents pages, if not auto-detected (they are always skipped) |
| `front_matter_until` | last PDF page of front matter, if the guess is wrong |
| `font_maps` | per-font character maps for garbled fonts (section 3) |
| `smallcaps_fonts`, `heading_fonts` | force a font to small caps, or to heading level N (`"FAMILY@SIZE": 2`) |
| `body_size`, `note_size`, `quote_sizes`, `header_band`, `footer_band` | override measurements |
| `keep_hyphen_words`, `hanging_sections` | real compounds split at a line end; extra hanging-indent sections |
| `dictionary` | a word list or Hunspell `.dic` for the book's language, to decide line-end hyphens (macOS has `/usr/share/dict/words` for English) |
| `heading_patterns` | `{"regex": level}` for headings the type alone does not reveal |
| `min_figure_area` | smallest image kept as a figure, as a share of the page (default 0.015) |
| `per_page_margin`, `indent` | measure the left margin on each page, and set the paragraph indent, when text blocks shift from page to page |
| `chapter_title_caps` | capitals right after a "Chapter 3" label are the rest of its title |
| `replacements` | `[["old", "new"], ...]`: exact corrections applied to the finished HTML, listed in the report when they match nothing |

### Review loop (mandatory)

Read the summary it prints and `book.report.json`, fix the config, re-run, until clean:

1. `suspect_fonts`, `mixed_script_words`: garbled text; go to section 3.
   `text_layer_repair`: fonts corrected from their glyph names.
2. `headings`: compare with the printed contents page, level by level. `outline`: the
   PDF's bookmarks and which of them have a printed heading (a bookmark that only points
   into running text, such as "Epítome 1", has none; that is fine).
3. `chapter_authors`, `display_text`, `removed_margin_text`, `margin_lines`: nothing of
   the book proper may be in the removed lists.
4. `notes`: `linked` must equal `found`; every `note_problems` entry must be resolved
   (a marker the script missed, a note continued oddly, a page wrongly treated as notes).
5. `hyphen_guessed`: line-end hyphens removed without evidence; add real compounds to
   `keep_hyphen_words`, or give a `dictionary`.
6. `figures`, `images_skipped`, `cover_page`, `two_column_pages`, `two_up_pages`,
   `toc_pages_skipped`, `skipped_pages`.
7. `table_pages`: tables come out as lines of text; rebuild each as a `<table>` (with
   its own notes, if it has a separate note series) or insert an image of it.
8. `marginal_numbers`, `lists`, `sized_small_caps`, `rotated_text`, `overprinted_text`.

Then compare with the PDF's own text layer:

```sh
python3 "$SKILL_DIR/scripts/check_text.py" --pdf book.pdf --html book.html
```

It must exit 0: no GLUED words, no NOTE MARKERS followed by a letter, and every
UNEXPLAINED word inspected on its PDF page (it lists the pages). Differences explained
by running heads, folios, decoded fonts and pdftotext's own hyphen removal are fine.

Then check the structure against the PDF:

```sh
python3 "$SKILL_DIR/scripts/check_structure.py" --pdf book.pdf --html book.html
```

The validators only compare files the pipeline wrote, so they cannot see a section that
was never recognised. This check reads the PDF again and fails (ERROR) when a printed
contents entry has no heading, when the PDF prints a bibliography or index that is not
its own section with its entries in it, when a note holds an index or the text after it
(the last note of a chapter swallowing what follows), when a page's words are mostly
missing, when a page skipped as "contents" is really a table, or when the declared
language is not the language of the text. WARN lines (bookmarks without a heading, note
numbers that jump, large images skipped, table pages, headings that read like body text)
each need a look at the page. Resolve every ERROR; explain every WARN you accept.

Finally spot-check a chapter opening, a page with a long note, a block quotation, the
start of the bibliography and of the index, and the end of a chapter against `pdftoppm`
renders.

## 3. Garbled fonts

Subset fonts with a custom encoding often extract as a substitution cipher: "C(p;+&l% <"
for "Capítulo 1", "93ii" for small-caps "xvii", "λὺρφίάφ" for Greek εὐνοῦχος, "/e" for a
"Th" ligature. Each font is consistent, so it is repaired per font:

1. The report's `suspect_fonts` lists each font's characters and every distinct run with
   its page. Render those pages (`pdftoppm -png -r 150 -f N -l N book.pdf p`) and read
   the real text from the image.
2. Write pairs of equal length and derive the map:

   ```json
   [{"font": "AAAAAE+StempelGaramondLTStd", "garbled": "(p;+&l% <", "text": "apítulo 1"},
    {"font": "AAAAAE+StempelGaramondLTStd", "garbled": "93ii", "text": "xvii"}]
   ```

   ```sh
   python3 "$SKILL_DIR/scripts/pdf_to_html.py" --pdf book.pdf --out x \
     --font-map-pairs pairs.json        # prints {"font_maps": ...}; merge into config.json
   ```

3. Re-run until the font is no longer suspect and every character in its `chars` list is
   mapped. Never guess a character you have not seen on the page. `mixed_script_words`
   ("eunμchus") catches single wrong characters inside otherwise normal words.

If the user still has the original publisher PDF, compare one heading: a copy made with
macOS Preview "Export as PDF" can lose correct text that the original still has.

## 4. The HTML contract

`book.html` is the single source for both outputs. Keep this markup when you edit it:

- One `<h1>` per chapter, preface, introduction, bibliography or index: Pandoc starts a
  new EPUB file and (via `format_docx.py`) a new Word section at each `<h1>`, and note
  numbers restart there. Label and title stay in one heading:
  `<h1 id="ch03"><span class="chapter-label">Chapter 1</span><br />Raised Voices</h1>`.
  Sections are `<h2>`/`<h3>`; add `class="unlisted"` to keep one out of the TOC.
- Footnotes are the hard requirement. Each marker is a link, each note an aside; the
  note may sit anywhere in the same file and may hold several paragraphs:

  ```html
  ... text.<a epub:type="noteref" href="#ch03-n12">12</a> Next sentence ...
  <aside epub:type="footnote" id="ch03-n12"><p>Note text.</p></aside>
  ```

  The link text is the printed label ("12", "*"); IDs are unique in the file. Pandoc,
  reading with `--from=html+epub_html_exts`, turns these into real Word footnotes and
  EPUB notes. Never leave a bare `<sup>12</sup>`; a superscript that is not a note
  (siglum W², exponent) is wrapped as `<span class="not-a-note"><sup>2</sup></span>`.
- Paragraph roles are `<div>` wrappers, because Pandoc drops classes on `<p>`; the
  `data-custom-style` value becomes the Word paragraph style and the class styles the EPUB:

  | class | data-custom-style | for |
  |---|---|---|
  | `noindent` | `First Paragraph` | flush first paragraph not after a heading, continuation after a quote |
  | `indent` | `Body Text` | indented new paragraph after a block quotation |
  | `chapter-author` | `Chapter Author` | contributor's name under a chapter title |
  | `bibliography` | `Bibliography` | hanging-indent entries (also abbreviation lists) |
  | `hanging`, `hanging-sub` | `Hanging Entry`, `Hanging Subentry` | numbered entries that are not a list (a chronology), and their sub-entries |
  | `index`, `index-sub`, `index-letter` | `Index Entry`, `Index Subentry`, `Index Letter` | index |
  | `epigraph`, `epigraph-source`, `dedication` | `Epigraph`, `Epigraph Source`, `Dedication` | front matter, chapter openings |
  | `centered`, `right`, `front-matter` | `Centered`, `Right`, `Front Matter` | title, copyright and other front matter |

- `<blockquote><p>` for quotations, `<figure><img alt="..." /><figcaption>` for figures,
  `<ol start="n">`/`<ul>` with `<li><p>` for lists, `<i>`/`<b>`,
  `<span class="smallcaps">` for small caps (the text keeps the letters' real case:
  "Abante" for a capital A followed by small capitals), `<span dir="rtl">` for Hebrew or
  Arabic words in a left-to-right text.
- Tables (the report's `table_pages`) are rebuilt by hand from the page, with word
  positions from `pdftotext -tsv -f N -l N book.pdf -`: a `<table>` with `<caption>`,
  `<thead>` and `<tbody>`, wrapped in `<div class="table" data-custom-style="Table Block">`,
  followed by its own notes in `<div class="table-notes" data-custom-style="Table Note">`.
  A table's note markers are `<span class="not-a-note"><sup>1</sup></span>`: they are not
  footnotes of the chapter, and their notes sit right below the table. Remove the
  flattened rows and any table notes the script took for chapter notes.
- Section or line numbers printed in the margin (classical texts, editions) are kept where
  the section starts: `<span class="marginal" data-custom-style="Marginal Number">35</span>`
  (floated into the margin in the EPUB, a small bold number in Word).

## 5. Metadata and cover

Write `metadata.yaml` from the title page and its verso, in the book's language:

```yaml
---
title: Castration and Culture in the Middle Ages
subtitle: (if any)
author: Larissa Tracy (ed.)
publisher: D. S. Brewer
date: 2013
lang: en-GB
identifier:
  - scheme: ISBN-13
    text: 978-1-84384-351-1
rights: © Contributors 2013
toc-title: Contents
---
```

Cover: prefer the publisher's cover file. Otherwise render the cover page with
`-cropbox` (a printer's PDF puts the whole wraparound cover in the media box and only
the front panel in the crop box), and crop the front panel by hand if page 1 is still a
spread (look at the render first):
`pdftoppm -cropbox -jpeg -jpegopt quality=90 -r 240 -f 1 -l 1 [-x X -y Y -W W -H H] -singlefile book.pdf cover`.
Do not pass an image extracted by `pdfimages`/`pdftohtml` directly: those JPEGs often
lack a JFIF header and Pandoc cannot read their size. Aim for at least 1400 px wide.

## 6. DOCX

```sh
pandoc book.html --from=html+epub_html_exts --to=docx --toc --toc-depth=2 \
  --metadata-file=metadata.yaml --resource-path=. -o raw.docx
python3 "$SKILL_DIR/scripts/format_docx.py" --docx raw.docx --out final.docx --source book.html
python3 "$SKILL_DIR/scripts/validate_docx.py" --docx final.docx --source book.html
```

`format_docx.py` never edits the input. It sets Body Text to a 0.25 in first-line indent
with no paragraph spacing, unindents First Paragraph and every style built on Body Text,
gives the book styles their indents (hanging bibliography, Chicago-style index), sets
footnotes to 10 pt, starts every chapter on a new page in its own section, restarts note
numbers per chapter when the source does (`--note-restart auto|chapter|never`), and turns
symbol labels such as "*" into Word custom marks. It parses and re-serialises each part
with its original namespace declarations and inserts elements in schema order, so it is
safe with a Word-made `--reference-doc`. Pandoc's TOC field is marked dirty: Word fills
it on opening.

LibreOffice shows custom marks twice in the note area and cannot render per-chapter note
restarts written per section, which is why the restart is declared document-wide; Word
honours it. Test in Word when available; otherwise say it was not tested.

## 7. EPUB

```sh
pandoc book.html --from=html+epub_html_exts --to=epub3 --toc --toc-depth=2 --split-level=1 \
  --css="$SKILL_DIR/assets/epub.css" --epub-cover-image=cover.jpg \
  --metadata-file=metadata.yaml --resource-path=. -o raw.epub
python3 "$SKILL_DIR/scripts/polish_epub.py" --epub raw.epub --out book.epub --source book.html
python3 "$SKILL_DIR/scripts/validate_epub.py" --epub book.epub --source book.html
epubcheck book.epub        # or: --epubcheck /path/to/epubcheck.jar on the line above
```

`polish_epub.py` makes notes work everywhere: every marker is `<sup>` inside
`<a epub:type="noteref" role="doc-noteref">`, every note is an
`<aside epub:type="footnote" role="doc-footnote">` that starts with its label linked back
to the marker. Apple Books, Kobo, Thorium and Kindle (Send to Kindle) show these as
pop-ups; readers without pop-ups jump to the note at the end of the chapter and back.
With `--source`, the labels are the printed ones (per-chapter or continuous numbering,
"*"). It also removes Pandoc's empty `<style>` elements, gives chapter files real titles
and keeps `mimetype` first and uncompressed. `assets/epub.css` is a reflowable book
stylesheet (indents, no paragraph spacing, hyphenation, small-caps sections, notes,
hanging bibliography, index) that leaves font and size to the reader and passes
Calibre's stylelint rules; adapt it per book if the user asks.

From an edited DOCX (when the user corrects the Word file and wants a new EPUB):

```sh
pandoc final.docx --from=docx+styles --to=epub3 --toc --toc-depth=2 --split-level=1 \
  --css="$SKILL_DIR/assets/epub.css" --epub-cover-image=cover.jpg \
  --metadata-file=metadata.yaml -o raw.epub
python3 "$SKILL_DIR/scripts/polish_epub.py" --epub raw.epub --out book.epub   # [--numbering continuous]
```

Word footnotes become EPUB notes, Word styles become the same classes, and custom marks
("*") are recovered; without `--source` numbering restarts in every chapter unless
`--numbering continuous`.

## 8. Validation gates

Report exactly which of these ran:

0. For a scan, `scan_to_pdf.py`: both engines ran, markers match notes on every page,
   `still to read: 0`, and the audit pages were compared and how many errors they held.
1. `pdf_to_html.py` report reviewed: no suspect fonts left, notes linked = found, no
   unresolved note problems, headings match the printed contents.
2. `check_text.py` exits 0, and `check_structure.py` exits 0 with every WARN explained.
3. `validate_docx.py` exits 0 (ZIP, XML, content types, relationships, mc:Ignorable
   prefixes, schema order, footnotes vs source, bookmarks, TOC field, text vs source).
4. `validate_epub.py` exits 0 (package, metadata, links, notes in both directions,
   labels, no bare superscript markers, no note markers in the TOC, text vs source).
5. `epubcheck` reports 0 errors. It does not check notes: gates 1-4 do.
6. A look at the result: render a chapter opening and its notes (Calibre viewer, Apple
   Books, Kindle Previewer, or a browser on the unzipped XHTML), and open the DOCX in
   Word if available.

## 9. Known problems and fixes

| Symptom | Cause | Fix |
|---|---|---|
| Headings or words like "C(p;+&l%", "93ii", "/e" | font without a usable Unicode map | section 3 |
| Notes missing or merged | notes and quotes share a size, unusual note numbering | check `note_problems`; set `note_size`/`quote_sizes` |
| "word³Next" or "hijirasof" | space lost at a marker or font change | fixed by the script; `check_text.py` reports any left |
| Paragraph split after a chapter opening | drop cap | handled; check the report and the first paragraph |
| Index read across columns | two-column page not detected | `two_column_pages`; set `skip_pages` or fix by hand |
| Bibliography entry per line | heading not recognised as bibliography | add its text to `hanging_sections` |
| Vector figures missing | `pdftohtml` extracts raster images only | render the area with `pdftoppm -x -y -W -H` and insert a `<figure>` |
| Tables flattened into paragraphs | no table reconstruction | `table_pages` lists them; rebuild as `<table>` by hand |
| Note numbers jump (70 → 1) in a chapter | a table with its own note series | rebuild the table and link its notes by hand |
| Page halves in the wrong order | booklet printed in imposition order | split and reorder the pages first (section 1) |
| Headings missing, bookmarks present | headings in body type | `outline` in the report; they are followed automatically when they match |
| Index page numbers meaningless | reflowable EPUB has no pages | keep (fidelity) or drop the index on the user's instruction |
| Calibre "Unexpected empty source" | empty `<style>` from Pandoc | `polish_epub.py` |
| Calibre "Expected selector ... to come before" | CSS specificity order | keep generic selectors first, as `assets/epub.css` does |
| Calibre "unknown property hanging-indent" | non-standard CSS | negative `text-indent` + `padding-left` |
| Word "unreadable content" | XML edited with regex or ElementTree dropping namespaces | only `format_docx.py`; `validate_docx.py` checks |

## 10. Final response

Give the DOCX and EPUB paths, the gates that ran and their results, what could not be
tested, and anything that needs the user's eye (decoded fonts, unresolved notes, figures
or tables rebuilt by hand, pages deliberately skipped, hyphens guessed without evidence,
the language if it was inferred).
