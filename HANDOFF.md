# PowerPDF — handoff

Date: 2026-10-07. Owner: GSSG (Windows 11, PowerShell 7). Version **0.5.0**.

## Goal

A personal PDF toolkit for Windows. The main job: take scanned licenses, ID cards and certificates and make
**small, readable, card-only** copies for web-app uploads and sending to people. It should feel premium.

`PLAN.md` (all four tiers + folder rename + single source of truth) is **done**; one git commit per item, no remote.
The open backlog is at the bottom. **Don't build from it until the user picks by number.**

## Where the code lives (one copy)

| Location | What it is |
|---|---|
| `C:\Users\GSSG\projects\powerpdf\` | Git repo. Textual app, package `powerpdf` 0.5.0, entry point `powerpdf`; modules `powerpdf`, `pdf_merge`, `pdf_split`, `pdf_card`, `pdf_compress`, plus `pdf_print.ps1`. uv `.venv`, `uv.lock`, `test_powerpdf.py` (**18 passed**, `uv run --frozen pytest -q`, 40–75 s) |
| `C:\Users\GSSG\Documents\PowerShell\Microsoft.PowerShell_profile.ps1` lines 17–20 | `pdfSplit`, `pdfMerge`, `pdfCompress` run `projects\powerpdf\.venv\Scripts\python.exe projects\powerpdf\pdf_<x>.py`; `pdfPrint` runs `projects\powerpdf\pdf_print.ps1` |
| `C:\Users\GSSG\.local\bin\powerpdf.exe` | Global `powerpdf` command: `uv tool install --editable C:\Users\GSSG\projects\powerpdf` (env in `%APPDATA%\uv\tools\powerpdf`). Editable, so code edits apply at once, but `uv tool` **ignores `uv.lock`**: it resolves its own (newer) libs, e.g. opencv-headless 5.0, pypdf 6.19 vs the venv's. If the repo moves, rerun with `--force` |
| `C:\Users\GSSG\Scripts\` | Old CLI copies **retired** (moved to the Recycle Bin 2026-10-07). Only `__pycache__` is left |
| `C:\Users\GSSG\projects\powerdpf\` (typo) | Leftover old `.venv` only, waiting to be recycled by the setup agent once the terminal using it closes. Not code |

## Shell commands (all have `-h` with flags and examples)

| Command | Script | Behavior |
|---|---|---|
| `pdfSplit [paths]` | `pdf_split.py` (pypdf) | Each page becomes its own file in `<name>_pages\<name>_page_001.pdf…`. Files or folders; default is the current folder. Staged in a temp folder, then swapped in |
| `pdfMerge [paths] [-o out.pdf] [-f]` | `pdf_merge.py` (pypdf `writer.append`) | Merges in argument order; folders sorted by name. Default `merged.pdf`; needs ≥2 PDFs; never merges the output into itself. Existing output without `-f` → argparse error, **exit 2** |
| `pdfPrint [paths] [-p] [-h]` | `pdf_print.ps1` | Named files print directly. No args or a folder: fzf multi-select (Tab mark, Ctrl-A all). `-p` picks the printer in fzf. Ghostscript `-sDEVICE=mswinpr2`, one job per file. A nonexistent path throws "Cannot find path" (line 42) |
| `pdfCompress [paths] [-q screen\|ebook\|printer] [-c]` | `pdf_compress.py` | Writes `<name>_small.pdf` next to each original; skips `*_small.pdf`. Plain: Ghostscript `pdfwrite /<q>`, original shipped if not smaller. `-c`: render, crop to content, JPEG q80 `optimize=True` via Pillow. **Every output is stripped of `/Info` and XMP** (no Title, Producer, dates) |

Exit codes: 0 success, 1 nothing to do or failure, 2 argparse usage errors.

## App keys (`powerpdf [folder]`)

Main: `space` queue · `o` open · `m` merge · `s` split · `p` print · `k` card · `c` compress · **`d` compare** ·
**`t` pages** · `r` recent · `?` keys · `q` quit · `[ ]` flip preview · `v` pixel mode · `g` go to folder · `e` Explorer ·
`F2` rename · `Del` Recycle Bin · `a` queue folder · `x` clear queue · `f` all files · `ctrl+r` refresh · `ctrl+t` theme.

New in 0.5.0:
- **Drag and drop:** dropping files on Windows Terminal pastes their quoted paths. On the main screen (no Input focused) PDFs are queued (one toast with the count) and a dropped folder is opened. Pastes into an Input behave as normal.
- **`d` Compare:** on `X.pdf` (with `X_small.pdf` next to it) or on `X_small.pdf`. Side by side, each with its size, plus `−N% (A → B)`; `[ ]` flip both (up to the shorter doc); `Esc` closes.
- **`t` Pages:** thumbnail grid. `space` select · `r` rotate 90° · `Del` delete · `shift+←/→` reorder · `x` extract to `<stem>_extract.pdf` · `Enter` save `<stem>_edited.pdf` · `Esc` close (asks if unsaved). Keys act on the selection, or the cursor page if none. Original is never changed.
- **Card screen (`k`):** `Enter` saves `<stem>_card.pdf` = **the cards only**, 2 card-sized pages (front, back), 150 dpi, q80, no metadata. **`j`** saves `<stem>_front.jpg` / `<stem>_back.jpg` (150 dpi, q80, no EXIF; confirms before overwriting). **`b`** adds a black box on the current side; `space` steps crop → box 1 → … → crop; arrows / `shift`+arrows move/resize the selected box (or the crop); `backspace` removes the selected box; `tab` switches side. Boxes are painted into the pixels, grown to 8-px JPEG blocks and saved 4:4:4 so they decode to exactly (0,0,0).

## Environment (verified)

- Python 3.14.8 (global): pypdf 6.17, pypdfium2 5.9, Pillow 12.2, numpy 2.4.6, opencv-python 4.13. The app venv has its own (Pillow 12.3, numpy 2.5.3, opencv-python-headless).
  Not installed: pikepdf, pymupdf, ocrmypdf, pytesseract, reportlab, pdfplumber, img2pdf.
- Ghostscript 10.08 (`gswin64c`, `mswinpr2` device), fzf 0.74.4, Tesseract 5.4 at `C:\Program Files\Tesseract-OCR\`
  (`ara`, `eng`, `osd`; not on PATH), ImageMagick (`magick`), `pdftotext` (from Git). Not installed: qpdf, mutool, pdfimages, pngquant, jbig2, unpaper.
- No `.pdf` file association, so printing goes through Ghostscript.
- Printers: **`SHARP BP-50C31 PCL6` (default, real paper)**, `Microsoft Print to PDF`, `OneNote (Desktop)`. 12 logical CPUs.
- The app runs inside Herdr panes fine; `herdr pane send-keys` drives it for real-TUI smoke tests.

## Gotchas (each one has bitten already)

- **Console encoding is `ibm850`.** Arabic names from native tools garble. `pdf_print.ps1` sets `[Console]::OutputEncoding` to UTF-8 in try/finally; new PowerShell code reading native output needs the same.
- **Never send test jobs to the SHARP.** Use a throwaway printer and remove it in `finally`:
  `Add-Printer -Name 'pdfPrint test' -DriverName 'Microsoft Print To PDF' -PortName 'nul:'`.
  Windows manages the default printer (`LegacyDefaultPrinterMode` = 0), so printing elsewhere can move the default. Check `Win32_Printer Default=TRUE` is the SHARP before and after.
- **fzf in tests:** `$env:FZF_DEFAULT_OPTS='--exact --filter test'` (fuzzy "test" also matches `OneNote (Desktop)`).
- Writing an empty string to a process's input sends **Enter**; in interactive fzf that prints. Kill the session instead.
- bash → `pwsh -Command '…'` quoting breaks with embedded Python or `"`. Write a temporary `.ps1`.
- WhatsApp/iOS scans claim ~26×37″ pages (72 dpi), so `/ebook` doesn't downsample; the saving is JPEG re-encoding.
- `-q screen` makes license small print **unreadable**; `ebook` is readable. `-c -q printer` can come out larger.
- pdfium is not thread-safe: every call is under `PDFIUM_LOCK`. Preview/compare/grid open PDFs **from bytes**: a path-opened pdfium doc keeps a Windows file handle that blocks rename, Recycle Bin and overwrite (WinError 32).
- **Card pairing:** `pair_cards` takes one card from each of the first two pages, or two cards from a single page. The license reference has front + back of a *different vehicle* on each page, so running `k` on the 2-page file gives two backs. Run it on one page (split first) for a real front + back.
- The preview LRU holds 16 renders; opening the page grid on a long PDF flushes it (the main preview re-renders on demand).
- `import powerpdf` still loads numpy and PIL because `textual_image` (sixel) needs them; cv2/pypdf are deferred.
- **Preview resolution must not follow the panel size.** Sizing the render from the panel's current cells (tried in 0.5.0) gave a tiny, blurry page whenever the panel wasn't laid out yet (splash on top, window resized or font zoomed later), and the cache kept it. `textual_image` already resizes to the widget with Pillow; just hand it ≤1.5× / ≤1600 px. Windows Terminal doesn't answer the cell-size query, so textual_image assumes 10×20 px cells.

## Reference files (read-only; never modify originals in Downloads)

- License scan (2 pages, each page front + back of one vehicle license, 1654×2338 JPEG per page): `C:\Users\GSSG\Downloads\Scans\20261007_102850.pdf`. Split pages and old A4 `_card.pdf` files are in `…\20261007_102850_pages\`.
- Certificate (WhatsApp/iOS scan, 1,288 KB, iOS Producer + dates): `C:\Users\GSSG\Downloads\WhatsApp Scan 2026-09-14 at 14.55.52.pdf`.

## Measurements (0.5.0, 2026-10-07)

| Item | Before (0.4.0) | After (0.5.0) |
|---|---|---|
| `import powerpdf`, warm, median of 5 interleaved runs | 0.74 s (cv2, pypdf, numpy, PIL) | 0.59 s (no cv2, no pypdf) |
| Certificate preview render, page 1 | 2852×4047, 121–154 ms | 1128×1600 (scale ≤1.5, long side ≤1600 px), 35 ms. License unchanged: 894×1263 |
| License preview page flip | reopen + render every time | cached revisit 2–4 ms, 0 renders; next page prefetched |
| Card output (license, one page) | A4 2480×3508, 300 dpi q92, 467 KB | 2 pages 85.2×54.4 / 85.5×54.5 mm, 83 KB |
| Card JPGs | — | front 41 KB, back 40 KB (back with a black box: 46 KB, 4:4:4) |
| Card PDF with a black box | — | 99 KB (4:4:4 costs ~19%; only when boxes exist) |
| License `-c ebook` | 188 KB, `/Title '.…_small.tmp'` + dates | 186 KB, no metadata |
| Certificate plain `ebook` / `-c ebook` | 396 / 257 KB, iOS Producer + dates | 395 / 250 KB, no metadata |
| License plain `ebook` | 232 KB (original shipped) | 232 KB (original, metadata stripped) |
| Page grid, 50-page scan | — | all thumbnails 1.3–1.8 s; key latency median 2 ms while rendering |

Older numbers still valid: `-c screen` 57 KB (unreadable); one card at JPEG q85/q70/q60 = 47/32/27 KB;
OpenCV finds the license cards at 86×54 / 85×54 mm, tilted ~0.8° / ~2.3°; Tesseract on cards is garbage.

## Backlog (open; the user picks by number)

3. Watermark "Copy for <purpose> only – <date>" across the card.
5. Size limit `--max 200KB`: step the quality down until the file fits.
7. `pdfInfo`: pages, page size, image dpi, scan or text, encrypted, metadata.
9. `pdfLock` / `pdfUnlock`: AES-256 password with pypdf.
10. `pdfOcr` for normal documents (Arabic + English), not cards.
11. `imgToPdf` for phone photos.
12. Process folders in parallel (12 cores; currently one file at a time).
13. A single `pdf <command>` with PowerShell Tab completion.
14. Explorer right-click menu entries under HKCU (no admin).
15. `pdfPrint` page ranges (`-sPageList`) and copies (copies with `mswinpr2` **unverified**).
20. Progress bar and live sizes during batch jobs, with `Esc` to cancel.
22. Type to filter the file list, like fzf.
23. Refresh the list by itself when new scans arrive.
26. Remove blank pages, for duplex scans (reuses the `INK` threshold).
27. Searchable PDF using Tesseract's own `pdf` output (`ara+eng`); the implementation of #10.
28. Phone photo to clean PDF: crop and straighten with the existing `pdf_card` code; the implementation of #11.
29. Copy a PDF's text to the clipboard (pypdfium2).

Done in 0.5.0: 1, 2, 4, 6, 16 (deferred imports), 17 (1600 px preview cap), 18 (preview cache), 19 (drag and drop),
21 (compare), 24 (page grid), 25 (redact), 30 (folder rename), CLI single source of truth.

Rejected, with reasons: linearization (needs qpdf/pikepdf, no gain at ~100 KB), grayscale (~12% smaller, loses colour),
JBIG2/OCRmyPDF (extra installs; scans are colour), pypdfium2 rendering for compress (no measured gain).

## How to verify changes

- CLI: run in a `$env:TEMP` copy folder with real scans, including an Arabic-named file. Check exit codes 0/1/2.
- Look at outputs: `PdfReader(f).pages[0].images[0].image.save(...)` and view it. Readability of the license card is the acceptance bar.
- App: `cd ~/projects/powerpdf; uv run --frozen pytest -q` (18 tests, end-to-end through Textual's pilot).
- Real TUI: in Herdr, split a pane, `pane run` the app in a temp copy folder, `pane send-keys` keys, `pane read --source visible`.
  Herdr panes don't draw sharp (Sixel) images, so judge preview quality in a plain Windows Terminal window (`wt.exe -w new`) plus a desktop screenshot.
