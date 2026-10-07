# PowerPDF — handoff

Date: 2026-10-07. Owner: GSSG (Windows 11, PowerShell 7).

## Goal

A personal PDF toolkit for Windows. The main job: take scanned licenses, ID cards and certificates and make
**small, readable, card-only** copies for web-app uploads and sending to people. It should feel premium.
The user tested `pdfCompress -c` on a real vehicle license and said it "works flawless".

**Open state: the user asked for research only and said "do not add before telling me".** The improvement
backlog below is waiting for them to pick by number. Do not build anything from it until they choose.

## Where the code lives (two copies; check before editing)

| Location | What it is |
|---|---|
| `C:\Users\GSSG\Scripts\` | CLI scripts behind the shell commands (source of truth for the CLI) |
| `C:\Users\GSSG\Documents\PowerShell\Microsoft.PowerShell_profile.ps1` lines 17–20 | `pdfSplit`, `pdfMerge`, `pdfPrint`, `pdfCompress` functions |
| `C:\Users\GSSG\projects\powerdpf\` (**note the typo: "dpf"**) | Textual terminal app, package `powerpdf` 0.4.0, entry point `powerpdf`. Has `.venv` (uv), `uv.lock`, `test_powerpdf.py` (**11 passed**, `uv run --frozen pytest -q`, ~20 s) |
| `C:\Users\GSSG\projects\powerpdf\` (this folder) | Only this handoff. Ask the user whether the app folder should be renamed or moved here |

The app imports **its own copies** of the scripts (`pyproject.toml` lists the modules `powerpdf`, `pdf_merge`,
`pdf_split`, `pdf_card`, `pdf_compress`). How the copies compare, checked 2026-10-07:
- `pdf_split.py`, `pdf_compress.py`, `pdf_print.ps1`: identical to `Scripts\`.
- `pdf_merge.py`: the app copy also has a `merge_pdfs(pdfs, output, log=print)` function, which `main()` uses. Otherwise the same.
- `pdf_card.py`: exists only in the app.

Risk: the two copies can drift apart. Pick one source of truth, e.g. make the profile functions call the app's
modules, or install the package and point the profile at it. Don't fix one copy and forget the other.

## Shell commands (all have `-h` with flags and examples)

| Command | Script | Behavior |
|---|---|---|
| `pdfSplit [paths]` | `pdf_split.py` (pypdf) | Each page becomes its own file in `<name>_pages\<name>_page_001.pdf…`. Files or folders; default is the current folder. Staged in a temp folder, then swapped in |
| `pdfMerge [paths] [-o out.pdf] [-f]` | `pdf_merge.py` (pypdf `writer.append`) | Merges in argument order; folders are sorted by name. Default `merged.pdf`; refuses to overwrite without `-f`; never merges the output into itself; needs ≥2 PDFs. Checks the inputs before the output-exists check |
| `pdfPrint [paths] [-p] [-h]` | `pdf_print.ps1` | Named files print directly. With no args or a folder, opens an fzf multi-select (Tab mark, Ctrl-A all). `-p` picks the printer in fzf (default printer first). Prints with Ghostscript `-sDEVICE=mswinpr2 -sOutputFile=%printer%<name>`, one job per file |
| `pdfCompress [paths] [-q screen\|ebook\|printer] [-c]` | `pdf_compress.py` | Writes `<name>_small.pdf` next to each original; originals are never touched; skips `*_small.pdf` inputs. Plain mode: Ghostscript `pdfwrite -dPDFSETTINGS=/<q>`; if the result isn't smaller, it ships the original. `-c`: renders each page, crops to content (gray < 235, ignores specks and lines under ¼″, bridges gaps under ¼″), saves the pages as JPEG q80 through Pillow. Pages larger than A4 (WhatsApp scans claim 26×37″) are rendered as if A4 |

## Environment (verified)

- Python 3.14.8 (global): pypdf 6.17, pypdfium2 5.9, Pillow 12.2, numpy 2.4.6, opencv-python 4.13.
  Not installed: pikepdf, pymupdf, ocrmypdf, pytesseract, reportlab, pdfplumber, img2pdf.
- Ghostscript 10.08 (`gswin64c`, scoop shim; has the `mswinpr2` device), fzf 0.74.4, Tesseract 5.4 at
  `C:\Program Files\Tesseract-OCR\` (languages: `ara`, `eng`, `osd`; not on PATH), ImageMagick (`magick`),
  `pdftotext` (from Git). Not installed: qpdf, mutool, pdfimages, pngquant, jbig2, unpaper.
- No `.pdf` file association on Windows, so `Start-Process -Verb Print` won't work. That's why printing goes through Ghostscript.
- Printers: **`SHARP BP-50C31 PCL6` (default, real paper, port 192.168.15.106)**, `Microsoft Print to PDF`
  (asks for a file name), `OneNote (Desktop)`. 12 logical CPUs.

## Gotchas (each one has bitten already)

- **Console encoding is `ibm850`.** Arabic file names coming back from fzf were garbled, so `pdfPrint`
  failed with "Cannot find path". The fix in `pdf_print.ps1`: set `[Console]::OutputEncoding` to UTF-8 inside
  try/finally and restore it afterwards. Any new PowerShell code that reads native-tool output needs the same.
  Many of the user's files have Arabic names (e.g. `SC-1383_طلب مواد.pdf`).
- **Never send test jobs to the SHARP.** To test printing, make a throwaway printer whose output is discarded
  (no admin needed), and remove it in `finally`:
  `Add-Printer -Name 'pdfPrint test' -DriverName 'Microsoft Print To PDF' -PortName 'nul:'`.
  To test the default-printer path, set it as the default temporarily
  (`Invoke-CimMethod -MethodName SetDefaultPrinter`) and restore the SHARP in `finally`.
- **fzf in tests:** `$env:FZF_DEFAULT_OPTS='--exact --filter test'` makes picks non-interactive. Use `--exact`,
  because a fuzzy "test" also matches `OneNote (Desktop)`. A two-printer pick became one bogus printer name and Ghostscript hung.
- Writing an empty string to a process's input sends **Enter**. In an interactive fzf that would print the
  highlighted file. Kill the session instead of trying to "send Esc".
- bash → `pwsh -Command '…'` quoting breaks with embedded Python or `"`. Write a temporary `.ps1` instead.
- WhatsApp/iOS scans set the page to about 26×37″ (effectively 72 dpi), so Ghostscript `/ebook` doesn't
  downsample them. The roughly 70% saving comes from re-encoding the JPEG.
- `-q screen` (72 dpi) makes the small print on a license **unreadable**. `ebook` (150 dpi) is readable; I checked visually.
- `-c -q printer` can come out larger than the input (license: 232 → 572 KB). The output says "larger".
- pdfium is not thread-safe (the app already uses `PDFIUM_LOCK`).

## Reference files (read-only; never modify originals in Downloads)

- License scan (vehicle license, front + back on one A4 page, 2 pages, 1654×2338 JPEG per page):
  `C:\Users\GSSG\Downloads\Scans\20261007_102850.pdf`. Its split pages are in `…\20261007_102850_pages\`,
  and so are `…_card.pdf` (468 KB) files from the app's card tool: straightened cards on a white A4 page at 300 dpi, q92.
- Full-page certificate (WhatsApp/iOS scan, 1,288 KB): `C:\Users\GSSG\Downloads\WhatsApp Scan 2026-09-14 at 14.55.52.pdf`.
  Its metadata leaks `Producer: iOS Version 26.6.1 … Quartz PDFContext` plus creation and modification dates.

## Measurements

| Item | Result |
|---|---|
| License, plain `ebook` | 232 → 232 KB (can't shrink, original shipped) |
| License, `-c` `ebook` | 232 → 188 KB, pages 89×126 mm, readable |
| License, `-c` `screen` | 57 KB, card text unreadable |
| Certificate, plain `ebook` | 1,288 → 396 KB |
| Certificate, `-c` `ebook` | 1,288 → 257 KB, margins trimmed, content intact |
| One cropped card, JPEG q85 / q80 / q70 / q60 | 47 / 40 / 32 / 27 KB (grayscale only ~12% smaller; `optimize=True` ~5%) |
| OpenCV contours on the license page | 2 cards, 86×54 and 85×54 mm (ID-1 is 85.6×54), tilted ~0.8° and ~2.3° |
| Tesseract `ara+eng` on the card at 300 dpi | mostly garbage, 0.5 s. OCR isn't useful for cards, only for normal documents |
| Timings | split 0.2 s, merge (3 files) 0.3 s, compress 0.75 s, `-c` 0.9–1.2 s; numpy+PIL+pypdf import ~0.4 s warm |

## Backlog (proposed; the user hasn't picked yet)

My recommendation: **1 + 2 + 4 + 6** first, then **12, 7, 3**.

1. **Separate the cards and straighten them.** `pdf_card.py` already detects, straightens (`find_cards`, `cut`)
   and pairs (`pair_cards`) cards, but `compose()` puts them back on a **white A4 page at 300 dpi q92**. That's
   what the user said they *don't* want. Missing: output just the cards, each on its own page, at about 150 dpi
   q80 (~40 KB per card versus 94 KB per page today). Reuse `pdf_card`; don't write a second detector.
2. JPG/PNG export (`<name>_front.jpg`, `<name>_back.jpg`) for web apps that only accept images.
3. Watermark "Copy for <purpose> only – <date>" across the card (common advice when sharing ID copies).
4. Strip metadata (Producer, dates) from compressed copies.
5. Size limit `--max 200KB`: step the quality down until the file fits.
6. JPEG `optimize=True` (~5% smaller, lossless).
7. `pdfInfo`: pages, page size, image dpi, scan or text, encrypted, metadata.
8. `pdfPages`: keep, delete, reorder and rotate pages (`1,3-5`).
9. `pdfLock` / `pdfUnlock`: AES-256 password with pypdf.
10. `pdfOcr` for normal documents (Arabic + English), not cards.
11. `imgToPdf` for phone photos.
12. Process folders in parallel (12 cores; currently one file at a time).
13. A single `pdf <command>` with PowerShell Tab completion.
14. Explorer right-click menu entries under HKCU (no admin).
15. `pdfPrint` page ranges (`-sPageList`) and copies (whether copies works with `mswinpr2` is **unverified**).

Rejected, with reasons: linearization ("fast web view"; needs qpdf/pikepdf, no gain at ~100 KB), grayscale
(only ~12% smaller and loses colour), JBIG2/OCRmyPDF (extra installs; the scans are colour), switching
rendering to pypdfium2 (no clear speed gain measured).

## How to verify changes

- CLI: run in a temporary copy folder (`$env:TEMP\…`) with real scans copied in, including an Arabic-named file.
  Check exit codes: 0 for success, 1 for nothing to do or failure, 2 for argparse usage errors.
- Look at outputs: extract the page image with `PdfReader(f).pages[0].images[0].image.save(...)` and view it.
  Readability of the license card is the acceptance bar.
- App: `cd ~/projects/powerdpf; uv run --frozen pytest -q` (11 tests, end-to-end through Textual's pilot).
