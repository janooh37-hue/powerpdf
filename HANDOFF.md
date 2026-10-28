# PowerPDF — handoff

Date: 2026-10-27. Owner: GSSG (Windows 11, PowerShell 7). Version **0.6.0**.

## Goal

A personal PDF toolkit for Windows. The main job: take scanned licenses, ID cards and certificates and make
**small, readable, card-only** copies for web-app uploads and sending to people. It should feel premium.

`PLAN.md` (all four tiers + folder rename + single source of truth) is **done**; one git commit per item. Branch `main`, pushed to the
private GitHub repo `janooh37-hue/powerpdf` (`origin`, account `janooh37-hue`, `gh` auth over https).
The open backlog is at the bottom. **Don't build from it until the user picks by number.**

## Where the code lives (one copy)

| Location | What it is |
|---|---|
| `C:\Users\GSSG\projects\powerpdf\` | Git repo. Textual app, package `powerpdf` 0.6.0, entry point `powerpdf`; modules `powerpdf`, `pdf_merge`, `pdf_split`, `pdf_card`, `pdf_compress`, `pdf_image`, plus `pdf_print.ps1`. uv `.venv`, `uv.lock`, `test_powerpdf.py` (**33 passed**, `uv run --frozen pytest -q`, 40–200 s; OCR is stubbed by the autouse `no_ocr` fixture) |
| `C:\Users\GSSG\Documents\PowerShell\Microsoft.PowerShell_profile.ps1` lines 17–21 | `pdfSplit`, `pdfMerge`, `pdfCompress`, `pdfImage` run `projects\powerpdf\.venv\Scripts\python.exe projects\powerpdf\pdf_<x>.py`; `pdfPrint` runs `projects\powerpdf\pdf_print.ps1` |
| `C:\Users\GSSG\.local\bin\powerpdf.exe` | Global `powerpdf` command, an editable `uv tool` install (env in `%APPDATA%\uv\tools\powerpdf`), so code edits apply at once. `uv tool` **ignores `uv.lock`**, so install it constrained to the lock: `uv export --frozen --no-dev --no-emit-project --no-hashes -o $env:TEMP\c.txt; uv tool install --editable C:\Users\GSSG\projects\powerpdf --force --constraints $env:TEMP\c.txt`. Then tool and venv run the same libs (0.6.0: numpy 2.5.4, Pillow 13.0.0, pypdf 6.21.2, pypdfium2 5.14.0, opencv-headless 5.0.0.93, textual 8.2.8, textual-image 0.14.2). Rerun it when the repo moves, the lock changes, or a module is added to `py-modules` (the editable install only maps the modules listed when it was built) |

## Shell commands (all have `-h` with flags and examples)

| Command | Script | Behavior |
|---|---|---|
| `pdfSplit [paths]` | `pdf_split.py` (pypdf) | Each page becomes its own file in `<name>_pages\<name>_page_001.pdf…`. Files or folders; default is the current folder. Staged in a temp folder, then swapped in |
| `pdfMerge [paths] [-o out.pdf] [-f]` | `pdf_merge.py` (pypdf `writer.append`) | Merges in argument order; folders sorted by name. Default `merged.pdf`; needs ≥2 PDFs; never merges the output into itself. Existing output without `-f` → argparse error, **exit 2** |
| `pdfPrint [paths] [-p] [-h]` | `pdf_print.ps1` | Named files print directly. No args or a folder: fzf multi-select (Tab mark, Ctrl-A all). `-p` picks the printer in fzf. Ghostscript `-sDEVICE=mswinpr2`, one job per file. A nonexistent path throws "Cannot find path" (line 42) |
| `pdfCompress [paths] [-q screen\|ebook\|printer] [-c]` | `pdf_compress.py` | Writes `<name>_small.pdf` next to each original; skips `*_small.pdf`. Plain: Ghostscript `pdfwrite /<q>`, original shipped if not smaller. `-c`: render, crop to content, JPEG q80 `optimize=True` via Pillow. **Every output is stripped of `/Info` and XMP** (no Title, Producer, dates) |
| `pdfImage [paths] [-f jpg\|png] [-r dpi]` | `pdf_image.py` (pypdfium2 + Pillow) | Every page as an image, 150 dpi default (50–600). One page → `<name>_image.jpg` next to the PDF; more → `<name>_images\<name>_page_001.jpg…`, staged in a temp folder, then swapped in; that format's old pages are removed, the other format is kept. JPG q80 `optimize=True`; no EXIF or PNG text chunks. Pages larger than A4 render as A4 (same rule as `-c`), so phone scans don't make 3900×5500 px images |

Exit codes: 0 success, 1 nothing to do or failure, 2 argparse usage errors.

## App keys (`powerpdf [folder]`)

Main: `space` queue · `o` open · `m` merge · `s` split · `p` print · `k` card · `c` compress · **`i` images** · **`d` compare** ·
**`t` pages** · `r` recent · `?` keys · `q` quit · `[ ]` flip preview · `v` pixel mode · `g` go to folder · `e` Explorer ·
`F2` rename · `Del` Recycle Bin · `a` queue folder · `x` clear queue · `f` all files · `ctrl+r` refresh · `ctrl+t` theme.

New in 0.5.0:
- **Drag and drop:** dropping files on Windows Terminal pastes their quoted paths. On the main screen (no Input focused) PDFs are queued (one toast with the count) and a dropped folder is opened. Pastes into an Input behave as normal.
- **`d` Compare:** on `X.pdf` (with `X_small.pdf` next to it) or on `X_small.pdf`. Side by side, each with its size, plus `−N% (A → B)`; `[ ]` flip both (up to the shorter doc); `Esc` closes.
- **`t` Pages:** thumbnail grid. `space` select · `r` rotate 90° · `Del` delete · `shift+←/→` reorder · `x` extract to `<stem>_extract.pdf` · `Enter` save `<stem>_edited.pdf` · `Esc` close (asks if unsaved). Keys act on the selection, or the cursor page if none. Original is never changed.
- **Card screen (`k`):** `Enter` saves `<stem>_card.pdf` = **the cards only**, 2 card-sized pages (front, back), 150 dpi, q80, no metadata. **`j`** saves `<stem>_front.jpg` / `<stem>_back.jpg` (150 dpi, q80, no EXIF; confirms before overwriting). **`b`** adds a black box on the current side; `space` steps crop → box 1 → … → crop; arrows / `shift`+arrows move/resize the selected box (or the crop); `backspace` removes the selected box; `tab` switches side. Boxes are painted into the pixels, grown to 8-px JPEG blocks and saved 4:4:4 so they decode to exactly (0,0,0).

Unreleased (after 0.6.0) — **many cards per scan** (`k`):
- `pdf_card.pair_cards` finds **every** card on up to `MAX_PAGES` = 12 pages (`detect_cards`: card-shaped, ≥80% filled, ≥½ the largest card's area) and groups them into rows (center within half a card height; real scans tilt rows by up to ~50 px, which broke the old `round(cy/50)` sort).
  - **Side by side** (a row with 2+ cards): **left = back, right = front** (the user's arranged batches, e.g. `vehicle_licenses_batch_3_arranged.pdf`: 2 pages × 4 rows = 8 cards).
  - **One per row**: paired in reading order, front first: front above back on one page, or page 1 then page 2 (old 2-page scans and `make_scan`).
  - A side with no partner gets a **guess** mirrored across the page (left↔right, or top↔bottom), flagged ⚠ until its crop is moved. Pages with no cards are skipped; none anywhere → the old page/half-page fallback, flagged ⚠.
- Card screen: top strip `Card 3 of 8 · page 1, row 3`, numbered chips (current, ~~skipped~~, ⚠ to check) and `Enter saves N cards`; a **Scan** map panel (the edited side's page, current card filled: edited side primary, other side accent; skipped darkened; redrawn 0.25 s after arrow nudges stop). `PgUp`/`PgDn` card · `s` swap front/back of this card · `x` skip/keep · `o` **one PDF** with every kept card (`<stem>_cards.pdf`, front, back, front, …). With 2+ cards, `Enter` / `j` ask for a folder (default `<stem>_cards\`) and write `<stem>_card_01.pdf` or `<stem>_card_01_front.jpg` + `_back.jpg` per kept card (numbers = strip numbers, so skips leave gaps). Encoded in a staging folder inside it, then swapped in; saves run in a thread from a snapshot of the cards. One card: exactly the old behavior and file names.
- Rendering takes `PDFIUM_LOCK` per page (opened from bytes); detection runs outside the lock.
- **Card names (`n`):** a name is the plate as `code-number` plus an optional English word: `16-16402 Station` (the user's choice: plate code = category; `/` typed becomes `-`). Named cards save as `<name>.pdf` / `<name>_front.jpg`, a name used twice gets ` (2)`; unnamed cards keep `<stem>_card_01`. `o` puts a bookmark per card in the one PDF. Words after the number are remembered in settings (`card_labels`, newest first, 12) and offered as picks (Tab) in the name dialog.
- **Name suggestions (OCR):** on opening, a worker reads every card with Tesseract, 4 at a time (`pdf_card.suggest_name`): background removed by the brightest channel (black print stays on any card colour), 5 passes per side (whole card ×3, plate band 22–45% ×2), front first, back only if the front has no name. A name counts only if **2+ passes agree and none disagrees**: single passes drop digits (21/13695 → 21/3695). Recognised: UAE plates `NN/NNNNN`, Emirates ID `784-…`. Suggestions show as `16-16402? n confirms` and are **never used until confirmed**; a plate read on the Back says `s swaps?`.
  Chips: `✓` named, `?` a suggestion waiting for `n` + Enter. Measured in the app: batch_3 5 of 8 suggested (raw page 1 2 of 4, raw page 2 0 of 4); **all 7 correct**; 17–28 s for 8 cards in the background. No Tesseract → no suggestions, naming still works.
- **Raw page 2 of a duplex scan lies front left / back right** (the flipped side), so position pairing swaps every card there: press `s` per card. Not auto-detected.

New in 0.6.0:
- **`i` Images:** queued (or highlighted) PDFs → one image per page, same layout as `pdfImage`. Choices: JPG 150 (default), PNG 150, JPG 300, PNG 300. The log shows each output and its total size. pdfium renders under `PDFIUM_LOCK` one page at a time; encoding runs outside the lock, so the preview stays responsive.

## Environment (verified)

- Python 3.14.8 (global): pypdf 6.17, pypdfium2 5.9, Pillow 12.2, numpy 2.4.6, opencv-python 4.13. The app venv and the uv tool env have their own, the same versions as `uv.lock` (Pillow 13.0.0, numpy 2.5.4, pypdf 6.21.2, opencv-python-headless 5.0.0.93).
  Not installed: pikepdf, pymupdf, ocrmypdf, pytesseract, reportlab, pdfplumber, img2pdf.
- Ghostscript 10.08 (`gswin64c`, `mswinpr2` device), fzf 0.74.4, Tesseract 5.4 at `C:\Program Files\Tesseract-OCR\`
  (`ara`, `eng`, `osd`; not on PATH), ImageMagick (`magick`), `pdftotext` (from Git). Not installed: qpdf, mutool, pdfimages, pngquant, jbig2, unpaper.
- No `.pdf` file association, so printing goes through Ghostscript.
- Printers: **`SHARP BP-50C31 PCL6` (default, real paper)**, `Microsoft Print to PDF`, `OneNote (Desktop)`. 12 logical CPUs.
- The app runs inside Herdr panes fine; `herdr pane send-keys` drives it for real-TUI smoke tests. Under Herdr the preview starts in **pixel mode** (see gotchas).

## Gotchas (each one has bitten already)

- **Console encoding is `ibm850`.** Arabic names from native tools garble. `pdf_print.ps1` sets `[Console]::OutputEncoding` to UTF-8 in try/finally; new PowerShell code reading native output needs the same.
- **Never stage outputs in `tempfile.mkdtemp` / `TemporaryDirectory` and move them out.** Python 3.12.4+ gives that folder an owner-only, non-inherited ACL (SYSTEM, Administrators, OWNER RIGHTS) and moved files keep it. The user's PowerShell always runs **elevated** (for SSH work), so the owner is `BUILTIN\Administrators` and Photos/Explorer (not elevated) get "no permission". Use `pdf_split.staging_dir()` (plain `mkdir`, inherits the folder's ACL). Fix a broken file from an elevated shell: `icacls <file> /reset`.
- **Never send test jobs to the SHARP.** Use a throwaway printer and remove it in `finally`:
  `Add-Printer -Name 'pdfPrint test' -DriverName 'Microsoft Print To PDF' -PortName 'nul:'`.
  Windows manages the default printer (`LegacyDefaultPrinterMode` = 0), so printing elsewhere can move the default. Check `Win32_Printer Default=TRUE` is the SHARP before and after.
- **fzf in tests:** `$env:FZF_DEFAULT_OPTS='--exact --filter test'` (fuzzy "test" also matches `OneNote (Desktop)`).
- Writing an empty string to a process's input sends **Enter**; in interactive fzf that prints. Kill the session instead.
- bash → `pwsh -Command '…'` quoting breaks with embedded Python or `"`. Write a temporary `.ps1`.
- WhatsApp/iOS scans claim ~26×37″ pages (72 dpi), so `/ebook` doesn't downsample; the saving is JPEG re-encoding.
- `-q screen` makes license small print **unreadable**; `ebook` is readable. `-c -q printer` can come out larger.
- pdfium is not thread-safe: every call is under `PDFIUM_LOCK`. Preview/compare/grid open PDFs **from bytes**: a path-opened pdfium doc keeps a Windows file handle that blocks rename, Recycle Bin and overwrite (WinError 32).
- **Card pairing:** see "many cards per scan" above. Front/back is decided by **position only** (no content check): left = back, right = front; stacked = front on top. A batch arranged the other way round (e.g. the flipped duplex page `20261028_121333_page_002.pdf`) needs `s` per card. The old reference (`20261007_102850.pdf`) now gives 2 cards (front top, back bottom on each page), not two backs.
- **OCR on cards:** plain Tesseract on a card is unreliable (yellow security pattern; 3–4 of 8 plates, wrong digits). Windows' own OCR (`Windows.Media.Ocr`, en-US + ar-SA installed, PowerShell 5.1 only) is worse here (`6402` for `16/16402`). The back's PDF417 barcode (zxing-cpp) decodes 8/8 but holds only the owner's T.C. No., the same on every card. Arabic fields (category) read only when cropped exactly; not used.
- The preview LRU holds 16 renders; opening the page grid on a long PDF flushes it (the main preview re-renders on demand).
- `import powerpdf` still loads numpy and PIL because `textual_image` (sixel) needs them; cv2/pypdf are deferred.
- **Preview resolution must not follow the panel size.** Sizing the render from the panel's current cells (tried in 0.5.0) gave a tiny, blurry page whenever the panel wasn't laid out yet (splash on top, window resized or font zoomed later), and the cache kept it. `textual_image` already resizes to the widget with Pillow; just hand it ≤1.5× / ≤1600 px. Windows Terminal doesn't answer the cell-size query, so textual_image assumes 10×20 px cells.
- **Herdr can't show sharp images in Windows Terminal.** In a Herdr pane textual_image probes `sixel=False, tgp=True`: Herdr answers the Kitty graphics query itself but only re-draws Kitty images when the *outer* terminal speaks Kitty graphics (`terminal.kitty_graphics`, default on); Windows Terminal doesn't, and Herdr has no Sixel passthrough (forced Sixel never reaches the Herdr client). So `sharp_preview_works()` (`HERDR_ENV=1` + TGP renderable) starts the app in pixel mode, the subtitle says `pixels (v)`, and `v` to sharp warns it will be blank. Plain Windows Terminal probes `sixel=True` and starts sharp. Herdr inside a Kitty-graphics outer terminal would draw sharp, but still starts in pixel mode (press `v`).
- **Pixel mode** (`v`, `HalfcellImage`) = one `▀` per cell, so 1×2 truecolor pixels per cell: the page layout and colours, never readable small print. It's the fallback for terminals without image support.
- **Agent shells leak `NO_COLOR=1` and `TERM=dumb`** into anything they launch (`wt.exe`, `herdr --session`). Textual then draws monochrome and pixel mode looks grey. That, not the app, was the "grey in WT" seen before. Clear them in test windows (`cmd /c "set NO_COLOR=&& set TERM=&& set HERDR_ENV=&& …"`; in a Herdr test pane run `$env:NO_COLOR=$null` first). `wt.exe` also inherits `HERDR_ENV=1` from a Herdr pane.
- `test_preview_pages` presses `v` and expects `HalfcellImage`; it assumes the app starts sharp. Under pytest's output capture there's no TTY, so that holds; `pytest -s` inside a Herdr pane would start in pixel mode and fail.

## Reference files (read-only; never modify originals in Downloads)

- License scan (2 pages, each page front + back of one vehicle license, 1654×2338 JPEG per page): `C:\Users\GSSG\Downloads\Scans\20261007_102850.pdf`. Split pages and old A4 `_card.pdf` files are in `…\20261007_102850_pages\`.
- Certificate (WhatsApp/iOS scan, 1,288 KB, iOS Producer + dates): `C:\Users\GSSG\Downloads\WhatsApp Scan 2026-09-14 at 14.55.52.pdf`.
- Card batch (2 A4 pages × 4 rows, back left / front right, 8 vehicles): `C:\Users\GSSG\Downloads\Scans\20261028_121333_pages\vehicle_licenses_batch_3_arranged.pdf`; the raw scans `20261028_121333_page_001.pdf` / `_002.pdf` (4 vehicles each, true 85×54 mm) are next to it.

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
| `pdfImage`, 150 dpi, JPG / PNG | — | scan page 249 KB / 1,350 KB (PNG ~5×); vector form 130 / 132 KB; 1241×1754 px per A4 page; fake phone scan (26×37″ page) 1241×1755 instead of 3900×5514 |
| Card batch (`batch_3_arranged`, unreleased) | `k` saw 2 cards | 8 cards, all front right / back left, none guessed; render 0.6–0.7 s + detect 64 ms/page; per-card PDFs 79–86 KB, one PDF with 8 cards 641 KB. Headless pilot, 180×48, pixel mode: 5 crop nudges 1.9 s (old screen 3.1 s), `PgDn` ~0.6 s (three half-cell images; the cost is textual_image's per-cell Rich styles, not the cuts: 33–50 ms + map 52–108 ms) |

Older numbers still valid: `-c screen` 57 KB (unreadable); one card at JPEG q85/q70/q60 = 47/32/27 KB;
OpenCV finds the license cards at 86×54 / 85×54 mm, tilted ~0.8° / ~2.3°. Tesseract on whole cards is unreliable; see the OCR gotcha.

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

Done in 0.6.0: PDF pages to JPG / PNG (`pdfImage`, `i`). Done in 0.5.0: 1, 2, 4, 6, 16 (deferred imports), 17 (1600 px preview cap), 18 (preview cache), 19 (drag and drop),
21 (compare), 24 (page grid), 25 (redact), 30 (folder rename), CLI single source of truth.

Rejected, with reasons: linearization (needs qpdf/pikepdf, no gain at ~100 KB), grayscale (~12% smaller, loses colour),
JBIG2/OCRmyPDF (extra installs; scans are colour), pypdfium2 rendering for compress (no measured gain).

## How to verify changes

- CLI: run in a `$env:TEMP` copy folder with real scans, including an Arabic-named file. Check exit codes 0/1/2.
- Look at outputs: `PdfReader(f).pages[0].images[0].image.save(...)` and view it. Readability of the license card is the acceptance bar.
- App: `cd ~/projects/powerpdf; uv run --frozen pytest -q` (33 tests, end-to-end through Textual's pilot).
- Real TUI: in Herdr, split a pane, `pane run` the app in a temp copy folder, `pane send-keys` keys, `pane read --source visible`.
  Text reads never show images: judge previews by a picture of the window. Don't take over the user's own Herdr view; start an isolated
  session in its own window (`wt.exe -w new --maximized -d <dir> cmd /c "set HERDR_ENV=&& set HERDR_PANE_ID=&& set HERDR_SOCKET_PATH=&& herdr --session <name>"`,
  then `herdr --session <name> pane run w1:p1 …`) and capture just that window with `PrintWindow(hwnd, hdc, 2)` (works while it's behind other
  windows; `CopyFromScreen` shows whatever is in front, or the lock screen). Check plain Windows Terminal too, with a clean env (see gotchas).
  Stop the test session (`herdr session stop <name>`) and close the windows afterwards.

## Releases

GitHub releases on `janooh37-hue/powerpdf`, tag `v<version>`, asset `powerpdf-<version>-windows-x64.zip` (the app only, not the CLIs;
Ghostscript is not bundled). Build a **one-folder** exe; one-file took 10–14 s to start (it unpacks ~200 MB to `%TEMP%` every run) vs 0.55 s warm:

```
uv run --frozen --with pyinstaller pyinstaller powerpdf.py --onedir --name powerpdf --console --noconfirm --clean `
  --specpath build --workpath build --distpath dist --collect-submodules textual --collect-data textual `
  --hidden-import pdf_merge --hidden-import pdf_split --hidden-import pdf_card --hidden-import pdf_compress --hidden-import pdf_image
```

Zip `dist\powerpdf\`, then `gh release create v<version> <zip> --title … --notes …`. `build/` and `dist/` are git-ignored.
Smoke the exe before uploading: `dist\powerpdf\powerpdf.exe <temp folder>`, `a` (queue), `i` Enter (images), `k` (card, loads cv2).
