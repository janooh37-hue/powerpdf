# PowerPDF — handoff

Date: 2026-11-04. Owner: GSSG (Windows 11, PowerShell 7). Version **0.7.0**.

## Goal

A personal PDF toolkit for Windows. The main job: take scanned licenses, ID cards and certificates and make
**small, readable, card-only** copies for web-app uploads and sending to people. It should feel premium.

`PLAN.md` (all four tiers + folder rename + single source of truth) is **done**; one git commit per item. Branch `main`, pushed to the
private GitHub repo `janooh37-hue/powerpdf` (`origin`, account `janooh37-hue`, `gh` auth over https).
The open backlog is at the bottom. **Don't build from it until the user picks by number.**

## Desktop product: shared-engine phase (`gui`)

The product direction is a full PySide6/Qt Widgets desktop GUI because terminals
cannot reliably display/edit Arabic RTL names. Pass logical Unicode strings to
Qt; never pre-reorder Arabic text. Keep the TUI and command entry points working.
PowerPDF is **AGPL-3.0-or-later** (full `LICENSE`, dependency summary in `NOTICE`);
the owner chose open-source, unsigned bundles for Windows, macOS and Linux.
This supersedes the commercial-Ghostscript/signing gates in the untracked GUI
research documents. Do not commit or delete `docs/gui/`.

Phase 1 extracts `powerpdf_core`: `files`/`outputs`, `merge`, `split`, `compress`,
`images`, `render`, `cards`/`ocr`, `pages`, `workspace`/`settings`, and `jobs`.
Core has no Qt, Textual or Rich dependencies. `pdf_split`, `pdf_merge`,
`pdf_compress`, and `pdf_image` are argparse adapters, not importable algorithm
homes; the obsolete `pdf_card` module is removed. `powerpdf_cli` remains the
frozen CLI/TUI dispatcher. The native shell is now `powerpdf_gui:main`;
`powerpdf-gui` is a windowed GUI script, also available as `python -m powerpdf_gui`.

`RenderSession` alone owns all PDFium resources/calls under one process-wide
mutex, opens byte snapshots, and returns owned RGB images. Preview and thumbnail
caches have separate 128/32 MiB budgets. TUI preview remains ≤1.5×/1600 px;
image export/crop-compression own A4 normalization, not the renderer. Card analysis is limited to
12 pages, 32 million pixels/page and 384 MiB of shared RGB scan arrays.
Card/page edits capture source revisions; stale sources cannot be exported.
Card OCR suggestions carry IDs/revisions/evidence side and require confirmation.

Outputs stage with inherited destination ACLs; merge no longer truncates an
existing destination on encoding failure. Split cleanup uses literal stems and
numeric page suffixes; named-card outputs are globally case-insensitively unique.
Cancellation is cooperative between native calls and before commits; multi-file
replacement failures report exactly which outputs committed, not fake atomicity.
Destination reservations also serialize independent TUI/GUI processes through
cross-platform locks in PowerPDF's cache, without leaving lockfiles in outputs.
Shared settings use platformdirs (`PowerPDF/settings.json`), schema-versioned
atomic JSON and cross-process merge locking. The former APPDATA or
`~/.config/powerpdf/settings.json` is imported once and never destroyed.

Runtime environment isolation is integrated from the packaging engineer's
`6b213d4`: discovery no longer mutates `os.environ`. GS/Tesseract subprocesses
receive `helper_env(executable)`; host PowerShell/Explorer processes receive
`host_env()`. The remaining packaging work must collect `powerpdf_core` and
remove obsolete `pdf_card` collection references.

Phase 1 acceptance exercised on Linux: `uv run --frozen pytest -q` →
**151 passed, 4 skipped in 33.76s** (three Windows ACL cases and the existing
Ghostscript integration case requiring `gswin64c`).
No print job was submitted. Native desktop/bundle and Windows ACL qualification
remain platform acceptance work; headless tests do not establish those results.

Phases 2 and 3 were approved for the native shell and bulk workflows below.
Phase 4 (cards) and Phase 5 (pages/compare) still require separate owner approval.
The TUI/CLI Windows print commands remain available independently of the GUI's
Qt print adapter; never send test jobs to the SHARP.
The parallel `gui-packaging` engineer owns runtime discovery, `packaging/` and
`.github/workflows/`; new assets belong under `assets/` when introduced.

## Native desktop shell — Phase 2

Launch from source with `uv run --frozen powerpdf-gui [files-or-folders]`.
The window has a native Unicode file/folder browser, ordered drag/keyboard queue
dock, recent documents, document metadata, activity, and a real page-image preview.
Open Files/Open Folder use native dialogs. Files dropped from outside are only
queued; folders navigate; a mixed drop queues PDFs in order then opens the last
folder. No source file is moved by a drop.

Preview has page flip, zoom, fit-page/width and drag-to-pan. One coalescing worker
owns its core `RenderSession`; generation IDs reject stale completions, image
transfers own their pixels, and the physical resolution follows zoom/viewport/DPR.
Screen rasters have a 12-million-pixel budget with an explicit resolution-limit
message. Card/image/print physical-size policies are not changed.

Rename uses native `QLineEdit` with normal Qt Arabic shaping, including IME input.
Rename/Trash remap queued/recent descendants. Trash failures never become permanent
deletion. Reveal uses Explorer shell selection, Finder `open -R`, or Linux
FileManager1; unsupported Linux selection reports that only the parent opened.
External open checks for PowerPDF self-association loops where the OS exposes the
handler. OS dialog/association/reveal qualification remains platform-specific.

Menus and the command palette expose the working shell actions; single-letter
TUI browser keys apply only outside text entry. Queue Shift+Up/Down and Delete
reorder/remove without touching files. PowerPDF dark is default; Light, System,
Game Boy and High contrast are available. Window geometry/docks/folder/theme are
stored under the shared settings `gui` object, separately from TUI presentation.
Later-phase operation buttons are not represented by disabled or fake controls.

Noto Sans and Noto Sans Arabic are bundled under `assets/fonts/`, unmodified,
with both full OFL licenses and pinned URL/hash provenance in `SOURCES.json`.
Qt shapes logical text itself; no terminal bidi workaround is used.
`resources.asset_path` resolves source `assets/`, frozen `_MEIPASS/assets/`, or
wheel data at `sys.prefix/share/powerpdf/assets/`. The app icon is a local PNG.

Runtime commits `3b2f5b6` and `1eaaa9f` are cherry-picked. Every GS/Tesseract
process creation uses `helper_launch_scope` plus `helper_env`. Host launches use
`host_launch_scope` plus sanitized environments where the API supports them.
The shared launch lock prevents frozen Windows DLL-directory resets racing a
helper launch. The DLL-directory setting is restored immediately after creation,
not held for the lifetime of an OCR/compression/print child.

Acceptance: `QT_QPA_PLATFORM=offscreen uv run --frozen pytest -q` →
**224 passed, 4 skipped in 40.55s**. Skips remain three Windows ACL cases and the
existing `gswin64c` integration case. Qt tests exercise real PDFs, source changes,
HiDPI/zoom/page navigation, queue interactions, Arabic IME rename, descendant
remapping, native-dialog adapters, desktop dispatch and worker shutdown.
Native dialogs/desktop dispatch are mocked only for adapter tests, not claimed
as installed-platform integration evidence.

Inspected actual Qt offscreen captures:
- `/tmp/gui/main-window-arabic.png`: browser with all four specified Arabic/mixed
  filenames and `مستندات السيارات`, queue, real PDFium image and metadata.
- `/tmp/gui/rename-dialog-arabic.png`: Arabic committed through `QInputMethodEvent`
  into the native editor.
The PDF shown is an explicitly labelled synthetic local document, not owner data.
Regenerate with `QT_QPA_PLATFORM=offscreen uv run --frozen pytest -q
test_gui.py::test_capture_arabic_main_and_rename_dialog`.

Filename presentation follows the application's layout direction, not a filename's
first strong Arabic character. Labels/window titles/log paragraphs use LRI or RLI
plus PDI around presentation text; Qt still shapes the Arabic runs. `FilenameEdit`
uses a native editor paragraph mark (LRM/RLM), never returned in `.text()` or used
in filesystem operations, because `QLineEdit.setLayoutDirection` alone still
auto-detects an Arabic paragraph. The stem is initially selected with the cursor
at its end. The same editor serves the browser path field. Direction tests cover
both LTR and RTL application layouts; raw logical filenames remain unchanged.
Corrected main/rename captures retain the paths above.

### Packaging collection contract

- Entry: `powerpdf_gui:main` (windowed). Include `powerpdf_gui` and modules
  `__main__`, `application`, `window`, `navigation`, `preview`, `resources`,
  `desktop`, `themes`; include the existing `powerpdf_core` package and runtime.
- Qt Python imports: `PySide6.QtCore`, `PySide6.QtGui`, `PySide6.QtWidgets`.
  No QtPdf, QtWebEngine, QtSvg, QtTest or QtPrintSupport dependency in this phase.
  Keep native platform plugins/dependencies for each supported OS; offscreen is
  an automated-test plugin, not the user's desktop platform.
- Collect `assets/powerpdf.png`, `assets/fonts/NotoSans[wdth,wght].ttf`,
  `assets/fonts/NotoSansArabic[wdth,wght].ttf`, `NotoSans-OFL.txt`,
  `NotoSansArabic-OFL.txt`, `SOURCES.json` preserving the `assets/fonts/` layout.
  Collect root `LICENSE` and `NOTICE` plus the exact helper/dependency notices.
- Deferred core hidden imports if the freezer does not traverse them:
  `powerpdf_core.cards`, `.ocr`, `.pages`, `.merge`, `.split`, `.compress`,
  `.images`, `.render`, `.jobs`, `.workspace`, `.settings`, `.files`, `.outputs`.
  Do not collect obsolete `pdf_card`. GUI-only builds need not pull in the TUI's
  Textual/Rich/textual-image payload; retained TUI/CLI builds still need theirs.
- Linux reveal can use host `dbus-send`; absence/unsupported FileManager1 uses the
  honest parent-folder-open path. No new Python DBus or QtDBus module is imported.

## Bulk document workflows — Phase 3

`Operations` exposes Merge (`m`), Split (`s`), Compress (`c`), Images (`i`) and
Print (`p`, Ctrl/Cmd+P). Merge requires at least two queued PDFs; other operations
use a snapshot of the queue or selected PDF. Changing the queue afterward cannot
change an open operation's target set.

Merge/split/compress/images share `OperationDialog`: review exact ordered inputs
and destinations, select native Save/folder destinations, confirm all existing
outputs and stale numbered pages, then execute core jobs off the GUI thread.
Destination reservations are acquired before reviewed revisions are checked and
held through execution, preventing another local job from silently replacing an
unconfirmed revision. Source/self/hardlink and cross-input output collisions fail
before writing. Encoding/commit/error/cancel outcomes and actual output bytes are
reported per input; completed outputs survive cancellation of later work.

Compression defaults to ebook, supports crop and printer/screen quality, and warns
that screen can destroy card fine print. Image export supports JPEG/PNG, quick
150/300 DPI presets and the full 50–600 DPI range. All operations preserve inputs.
Progress/cancel use real core events and cooperative cancellation; native calls
are not forcibly interrupted. Closing joins owned workers safely.
Print review lists exact inputs, scaling and copies; the actual `QPrintDialog`
must be explicitly accepted before submission. Its native paper/orientation,
duplex and printer settings are retained. A dedicated worker exclusively owns
the accepted `QPrinter`, paints one uncached 300-dpi PDFium raster at a time, and
uses PDF points for fit/actual-size placement inside printable margins.
Oversized pages beyond 40 million pixels are rejected explicitly, not silently
normalized to A4. Combined review page ranges, copies and collation are applied
once; native printers receive one job per source.

Explicit PDF printing is an intentionally selected output, not an automatic
fallback for missing printers. It combines selected documents into one PDF,
confirms existing-output replacement and stages destination-locally until the
entire job succeeds. Cancel/error preserves an existing destination. Printer
progress counts pages handed to the print engine, not physically printed pages;
already-spooled output cannot be recalled.

Packaging delta: include `powerpdf_gui.text`, `powerpdf_gui.operations`,
`powerpdf_gui.printing`, `PySide6.QtPrintSupport` and its target-native print
support plugin/dependencies. The Phase 2 asset list is unchanged; no QtPdf,
QtWebEngine or QtSvg renderer is introduced. Native print-driver/dialog
qualification remains necessary on each target OS.

Phase 3 acceptance on Linux:
`QT_QPA_PLATFORM=offscreen uv run --frozen pytest -q` →
**275 passed, 4 skipped in 94.88s**. The unchanged skips are three Windows ACL
cases and the legacy `gswin64c` integration case. New compression/crop tests use
the real local Ghostscript (`/usr/bin/gs`); print tests accept an explicit
`QPrinter.PdfFormat` destination, then run real PDFium/QPainter/QPrinter code.
Lossless image rendering avoids an extra JPEG pass through Qt's PDF engine.
Physical point sizes, clipping/margins, 300-dpi raster dimensions, copies/ranges,
cancel/error preservation, overwrite races and source revisions are exercised.
No physical printer job was submitted; OS dialogs are adapter-mocked in tests.

Reviewed operation captures under `/tmp/gui/`, with real progress events:
- `merge-dialog.png`, `merge-progress.png`, `merge-result.png`
- `split-dialog.png`, `split-progress.png`, `split-result.png`
- `compress-dialog.png`, `compress-progress.png`, `compress-result.png`
- `images-dialog.png`, `images-progress.png`, `images-result.png`
- `print-dialog.png`, `print-progress.png`, `print-result.png`
`phase3-overview.png` assembles these receipts. The print receipt capture waits
for Qt's wrapped status label to finish layout; its focused screenshot test also
passed after this assertion was added. Bulk results contain real output paths
and bytes, not simulated operations. Printing evidence is explicit PDF output,
not proof of native printer drivers, duplex hardware or paper delivery.



## Where the code lives (one copy)

| Location | What it is |
|---|---|
| `C:\Users\GSSG\projects\powerpdf\` | Owner's source checkout. Package `powerpdf` 0.7.0; shared `powerpdf_core`, TUI `powerpdf`, dispatcher `powerpdf_cli`, runtime `pdf_runtime`, CLI adapters `pdf_merge`, `pdf_split`, `pdf_compress`, `pdf_image`, plus `pdf_print.ps1`. uv `.venv`, `uv.lock`; `test_powerpdf.py`, `test_runtime.py`, and `test_core_*.py`. Installer builds run in target-native CI |
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

New in 0.7.0 — **many cards per scan** (`k`), OCR names, and the complete Windows installer (see Releases):
- `powerpdf_core.cards.pair_cards` finds **every** card on up to `MAX_PAGES` = 12 pages (`detect_cards`: card-shaped, ≥80% filled, ≥½ the largest card's area) and groups them into rows (center within half a card height; real scans tilt rows by up to ~50 px, which broke the old `round(cy/50)` sort).
  - **Side by side** (a row with 2+ cards): **left = back, right = front** (the user's arranged batches, e.g. `vehicle_licenses_batch_3_arranged.pdf`: 2 pages × 4 rows = 8 cards).
  - **One per row**: paired in reading order, front first: front above back on one page, or page 1 then page 2 (old 2-page scans and `make_scan`).
  - A side with no partner gets a **guess** mirrored across the page (left↔right, or top↔bottom), flagged ⚠ until its crop is moved. Pages with no cards are skipped; none anywhere → the old page/half-page fallback, flagged ⚠.
- Card screen: top strip `Card 3 of 8 · page 1, row 3`, numbered chips (current, ~~skipped~~, ⚠ to check) and `Enter saves N cards`; a **Scan** map panel (the edited side's page, current card filled: edited side primary, other side accent; skipped darkened; redrawn 0.25 s after arrow nudges stop). `PgUp`/`PgDn` card · `s` swap front/back of this card · `x` skip/keep · `o` **one PDF** with every kept card (`<stem>_cards.pdf`, front, back, front, …). With 2+ cards, `Enter` / `j` ask for a folder (default `<stem>_cards\`) and write `<stem>_card_01.pdf` or `<stem>_card_01_front.jpg` + `_back.jpg` per kept card (numbers = strip numbers, so skips leave gaps). Encoded in a staging folder inside it, then swapped in; saves run in a thread from a snapshot of the cards. One card: exactly the old behavior and file names.
- Rendering takes `PDFIUM_LOCK` per page (opened from bytes); detection runs outside the lock.
- **Card names (`n`):** a name is the plate as `code-number` plus an optional English word: `16-16402 Station` (the user's choice: plate code = category; `/` typed becomes `-`). Named cards save as `<name>.pdf` / `<name>_front.jpg`, a name used twice gets ` (2)`; unnamed cards keep `<stem>_card_01`. `o` puts a bookmark per card in the one PDF. Words after the number are remembered in settings (`card_labels`, newest first, 12) and offered as picks (Tab) in the name dialog.
- **Name suggestions (OCR):** on opening, a worker reads every card with Tesseract, four at a time (`powerpdf_core.ocr`): background removed by the brightest channel, five passes per side (whole card ×3, plate band 22–45% ×2), front first, back only if the front has no evidence. A suggestion requires **2+ agreeing passes and none disagreeing**; never use it until confirmed. Results from an older edit revision are rejected. Missing OCR does not block manual names.
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
- **Never stage outputs in `tempfile.mkdtemp` / `TemporaryDirectory` and move them out.** Python 3.12.4+ gives that folder an owner-only, non-inherited ACL (SYSTEM, Administrators, OWNER RIGHTS) and moved files keep it. The user's PowerShell always runs **elevated** (for SSH work), so the owner is `BUILTIN\Administrators` and Photos/Explorer (not elevated) get "no permission". Use `powerpdf_core.outputs.staging_dir()` (plain `mkdir`, inherits the folder's ACL). Fix a broken file from an elevated shell: `icacls <file> /reset`.
- **Never send test jobs to the SHARP.** Use a throwaway printer and remove it in `finally`:
  `Add-Printer -Name 'pdfPrint test' -DriverName 'Microsoft Print To PDF' -PortName 'nul:'`.
  Windows manages the default printer (`LegacyDefaultPrinterMode` = 0), so printing elsewhere can move the default. Check `Win32_Printer Default=TRUE` is the SHARP before and after.
- **fzf in tests:** `$env:FZF_DEFAULT_OPTS='--exact --filter test'` (fuzzy "test" also matches `OneNote (Desktop)`).
- Writing an empty string to a process's input sends **Enter**; in interactive fzf that prints. Kill the session instead.
- bash → `pwsh -Command '…'` quoting breaks with embedded Python or `"`. Write a temporary `.ps1`.
- WhatsApp/iOS scans claim ~26×37″ pages (72 dpi), so `/ebook` doesn't downsample; the saving is JPEG re-encoding.
- `-q screen` makes license small print **unreadable**; `ebook` is readable. `-c -q printer` can come out larger.
- pdfium is not thread-safe: **only `powerpdf_core.render` may call it**, including bitmap conversion/closing/finalizers. Sessions open PDFs **from bytes**, avoiding Windows file handles that block rename, Trash and overwrite (WinError 32).
- **Card pairing:** see "many cards per scan" above. Front/back is decided by **position only** (no content check): left = back, right = front; stacked = front on top. A batch arranged the other way round (e.g. the flipped duplex page `20261028_121333_page_002.pdf`) needs `s` per card. The old reference (`20261007_102850.pdf`) now gives 2 cards (front top, back bottom on each page), not two backs.
- **OCR on cards:** plain Tesseract on a card is unreliable (yellow security pattern; 3–4 of 8 plates, wrong digits). Windows' own OCR (`Windows.Media.Ocr`, en-US + ar-SA installed, PowerShell 5.1 only) is worse here (`6402` for `16/16402`). The back's PDF417 barcode (zxing-cpp) decodes 8/8 but holds only the owner's T.C. No., the same on every card. Arabic fields (category) read only when cropped exactly; not used.
- Separate byte-budgeted preview and thumbnail caches prevent the page grid from flushing main-preview images.
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
28. Phone photo to clean PDF: crop and straighten with the existing `powerpdf_core.cards` code; the implementation of #11.
29. Copy a PDF's text to the clipboard (pypdfium2).

Done in 0.6.0: PDF pages to JPG / PNG (`pdfImage`, `i`). Done in 0.5.0: 1, 2, 4, 6, 16 (deferred imports), 17 (1600 px preview cap), 18 (preview cache), 19 (drag and drop),
21 (compare), 24 (page grid), 25 (redact), 30 (folder rename), CLI single source of truth.

Rejected, with reasons: linearization (needs qpdf/pikepdf, no gain at ~100 KB), grayscale (~12% smaller, loses colour),
JBIG2/OCRmyPDF (extra installs; scans are colour), pypdfium2 rendering for compress (no measured gain).

## How to verify changes

- CLI: run in a `$env:TEMP` copy folder with real scans, including an Arabic-named file. Check exit codes 0/1/2.
- Look at outputs: `PdfReader(f).pages[0].images[0].image.save(...)` and view it. Readability of the license card is the acceptance bar.
- App and core: `cd ~/projects/powerpdf; uv run --frozen pytest -q` (existing Textual/runtime cases plus real-PDF core behavior edges).
- Real TUI: in Herdr, split a pane, `pane run` the app in a temp copy folder, `pane send-keys` keys, `pane read --source visible`.
  Text reads never show images: judge previews by a picture of the window. Don't take over the user's own Herdr view; start an isolated
  session in its own window (`wt.exe -w new --maximized -d <dir> cmd /c "set HERDR_ENV=&& set HERDR_PANE_ID=&& set HERDR_SOCKET_PATH=&& herdr --session <name>"`,
  then `herdr --session <name> pane run w1:p1 …`) and capture just that window with `PrintWindow(hwnd, hdc, 2)` (works while it's behind other
  windows; `CopyFromScreen` shows whatever is in front, or the lock screen). Check plain Windows Terminal too, with a clean env (see gotchas).
  Stop the test session (`herdr session stop <name>`) and close the windows afterwards.

## Releases

Releases on `janooh37-hue/powerpdf` are built on **Windows GitHub Actions**, never cross-compiled on Linux.
`.github/workflows/release.yml` runs on a `v*` tag or `workflow_dispatch` (manual rehearsals do not publish).
The tag must match `pyproject.toml`. The workflow installs uv/Python 3.13, syncs `uv.lock`, runs
`uv run --frozen pytest -q`, then `packaging/build-windows.ps1` builds a **one-folder** PyInstaller
runtime and Inno Setup installer. One-file is deliberately not used: it took 10–14 s per launch.
Textual data/submodules and every `pdf_*` module are collected explicitly.

Download **`powerpdf-<version>-windows-x64-setup.exe`** for a complete installation:
- Default: per-user `%LOCALAPPDATA%\Programs\PowerPDF`, no admin rights; optional all-users mode.
- `bin` goes on the appropriate user/machine PATH; setup/uninstall broadcast `WM_SETTINGCHANGE`.
- Commands in any new terminal: `powerpdf`, `pdfsplit`, `pdfmerge`, `pdfcompress`, `pdfimage`, `pdfprint`.
  The CLIs are thin `.cmd` shims into the same frozen executable, without importing Textual for CLI work.
- Start Menu shortcut launches in Windows Terminal if present, otherwise a normal console; desktop shortcut optional.
- Add/Remove Programs uninstall removes the PATH entry. The fixed AppId upgrades an existing installation in place.
- Includes **Ghostscript 10.08.0**, **Tesseract 5.5.0.20241111** plus `eng`, `ara`, `osd`
  (`tessdata_fast` 4.1.0), and **fzf 0.74.4**. No extra runtime installations or PowerShell 7 are needed.
  Downloads are version-pinned and SHA-256 checked. Ghostscript includes its DLL, lib, Resource and ICC profiles.
- `bin\licenses` and `bin\THIRD-PARTY-NOTICES.txt` contain licenses/source links: Ghostscript AGPL-3.0,
  Tesseract/models Apache-2.0, fzf MIT. The exact Ghostscript source archive is a separate release asset.

`pdf_runtime.resolve_tool` prefers bundled `vendor` directories under `sys._MEIPASS`, then beside the
frozen exe, then PATH, then known install directories. It sets `TESSDATA_PREFIX` / `GS_LIB` for bundled
tools. `pdf_print.ps1` applies the same order and also works on Windows PowerShell 5.1.
Source development (`uv run`) still uses local tools when no bundle is present.

The workflow silently installs a lower-version rehearsal installer, upgrades it, then starts a fresh shell
with registry-refreshed PATH and poisoned external-tool PATH commands. It checks `--version`, bundled tool
resolution and versions, OCR languages, generated-PDF compress/image/split/merge output, `pdfprint -h`,
and a cancelled bundled-fzf picker (**no physical print jobs**). Silent uninstall must remove the executable and PATH entry. Only then are
assets uploaded and the tag's release published. Actions logs contain the `SMOKE` evidence lines.

Second asset: **`powerpdf-<version>-windows-x64.zip`**, the same complete runtime for portable use;
extract the whole folder (no PATH/shortcuts automatically). Third: `ghostscript-10.08.0-source.tar.xz`,
corresponding source offered alongside its binary. Build scripts: `packaging/`; release notes are in
`packaging/release-notes.txt`.

To release: check `gh release list` / `git tag`, bump `pyproject.toml` and `powerpdf.py VERSION`, run
`uv lock`, update this handoff, commit/push main, then push `v<version>`. Watch with `gh run watch`.
Run a manual workflow rehearsal before tagging if changing packaging. Never publish an un-smoked build.
