# Arabic RTL terminal-display spike

Date: 2026-11-03. Branch: `spike/arabic-rtl`. Baseline main: `3835b40dc2f3ace93f9c7e48985f73cc4e1407a3` (0.7.0). This is a feasibility experiment, not a release or a GUI decision already made.

## Verdict

**A display-only TUI fix is technically feasible for this filename-browsing problem:** the true-monospaced independent-cell proof shows correct Arabic visual order, connected contextual forms, correct digits/Latin/parentheses, and a single-cell lam-alef, without changing real paths. This removes “a TUI cannot display these Arabic names” as a technical reason for a GUI. It does **not yet prove owner acceptance on Windows Terminal**: no Windows Terminal/conhost or owner's font/fallback chain was available, and no Windows result is claimed.

For browsing/selecting existing files, try this small workaround on the owner's Windows Terminal **before building a GUI solely for Arabic filename display**. [INFERENCE] With a presentation-form-capable monospace font such as the proven DejaVu Sans Mono, it can plausibly remove that specific need. If the requirement includes normal Arabic cursor movement/rename editing, this spike still does **not** meet it; a GUI using a real text engine remains justified. Owner/font validation is the decision gate, not a claim that all terminal configurations are solved.

## What changed

`pdf_display.display_name(str) -> str` performs `arabic_reshaper.reshape` followed by **`bidi.algorithm.get_display`**. Pure ASCII and strings without Arabic return the original string before reading terminal settings or calling either library. Reshaping preserves harakat (`delete_harakat=False`, `shift_harakat_position=True`); combining-mark placement is still renderer-sensitive.

Names are standalone bidi paragraphs, with direction inferred from their first strong character. Arabic-first filenames therefore display the extension at the **visual left** (`pdf.`); Latin-first `Ahmed عقد.pdf` keeps `.pdf` at the right. This is intentional natural bidi ordering, not an attempt to make every filename LTR. `/`, `\\`, and line breaks are retained in original order while each component is independently transformed. For example, `C:\\folder\\Arabic-folder\\Arabic-file.pdf` keeps its drive/folder traversal order. Do not apply bidi to an entire path as a single paragraph.

Only presentation callsites change: DirectoryTree labels (including folders/root), Queue/Recent prompts, Info names and metadata, topbar path, card names/suggestions/picks, Compare border titles, Pages title, prompt border titles, filename-bearing confirmation messages, notifications and activity log messages. DirectoryTree's icon prefix/click metadata is retained; name styling is rebuilt because ligatures invalidate logical string offsets. Filename fragments are transformed independently before interpolation so two Arabic names separated by an arrow do not swap places.

**Unchanged:** `Path` objects; tree node data and logical labels; Option IDs; sorting keys; queue/recent identity; filenames written to disk; output PDF/bookmark names; JSON settings; editable `Input.value`. No CLI output or disk content is reshaped. The integration test exercises actual Arabic-file rename and logical settings/queue/recent values.

### Toggle

Set `POWERPDF_BIDI=on|off|auto` before starting PowerPDF (default `auto`). In PowerShell:

```powershell
$env:POWERPDF_BIDI = 'on'   # force visual-cell Arabic compensation
powerpdf
$env:POWERPDF_BIDI = 'off'  # native-bidi terminal, or compare without compensation
powerpdf
$env:POWERPDF_BIDI = 'auto'
```

`on`/`off` always override detection. `auto` defaults on for unknown/non-bidi terminals, including WT/conhost, kitty/alacritty and iTerm2. It defaults off for `KONSOLE_VERSION`, `VTE_VERSION`, `TERM_PROGRAM=mintty`, or `TERM` beginning `mlterm`. Unknown/invalid values follow auto policy. These environment variables are **identity hints, not negotiated capabilities**; profile settings, old versions/builds, nested terminals, SSH and multiplexers can invalidate the guess. Force `off` where the terminal performs native bidi, or the visual string can be reordered twice. Force `on` in a native-capable terminal whose active alternate-screen/profile settings disable bidi. iTerm2 is not classified as native bidi merely because it runs on macOS.

## Evidence: terminal cells, not browser screenshots

Generated real blank PDF fixtures in `/tmp/rtl/fixtures`:

- `رخصة المركبة.pdf`
- `شهادة 2026.pdf`
- `Ahmed عقد.pdf`
- `تقرير الترخيص 2026 (2).pdf`
- `لا لالا.pdf`
- folder `مستندات السيارات`

The baseline was snapshotted **before** application edits. A Textual pilot ran the real app headlessly at 180×48 cells. The proof extracts `screen._compositor.render_strips()`: actual output strips after Textual layout/cropping. Each Unicode glyph is drawn separately at its assigned cell column with Pillow's **BASIC** layout engine, clipped to its Rich cell width, using Noto Naskh Arabic and Noto Sans Mono. No browser, SVG, whole-line text drawing, RAQM bidi, or implicit paragraph shaping is used. This follows the allowed headless-pilot route; pyte/PTY was not needed and was not claimed.

The proof intentionally isolates display behavior: the main preview stays on the logo, and Info is populated through the app's real `show_info` method with sample details. Queue/Recent/tree are real widgets populated with real fixture paths. A separate calibration screen makes names and lam-alef large enough to inspect. Both captures inherited `NO_COLOR=1`/`TERM=dumb`, so they are identically monochrome; the proof is about cell order/forms, not theme colors. Some unrelated UI icons lack glyph coverage in the proof fonts.

| View | Before (main snapshot) | After (spike) |
|---|---|---|
| Tree, Queue, Info | `/tmp/rtl/before-tree-queue-info.png` | `/tmp/rtl/after-tree-queue-info.png` |
| Recent | `/tmp/rtl/before-recent.png` | `/tmp/rtl/after-recent.png` |
| Cell calibration, paths and lam-alef | `/tmp/rtl/before-calibration.png` | `/tmp/rtl/after-calibration.png` |
| **True monospace calibration (strongest joining evidence)** | `/tmp/rtl/before-calibration-mono.png` | `/tmp/rtl/after-calibration-mono.png` |

I inspected both baseline/after calibration pairs and the application grids. Before has logical-order, isolated Arabic letters; after has expected visual order/contextual forms, correct digits/Latin, correct `(2)`, and a single-cell lam-alef. The initial Noto proof has inter-cell gaps because it centers **proportional** glyphs inside fixed cells; this must **not** be mistaken for an unavoidable terminal limitation. A second proof reuses the exact captured Textual cell mappings with **DejaVu Sans Mono 2.37**, 24px BASIC layout, each glyph drawn individually at its cell origin on a common baseline. Every required glyph has exactly 14px advance, matching `font.getlength('M')`; no line shaping/bidi is performed. In the second after-PNG I see continuous strokes across the intended connecting sequences in `المركبة`, `الترخيص`, and `مستندات`; ordinary Arabic nonjoining-letter breaks remain normal. This is real cell-based evidence of joined Arabic with a suitable mono font. It is still not a Windows Terminal screenshot or font-fallback test. No real Linux terminal window was opened.

Actual font cmap probes show **29/29 emitted presentation-form codepoints covered**, including U+FEFB, in both Noto Naskh Arabic and the mono DejaVu font; no required Arabic mapping is missing. `/tmp/rtl/font-coverage.json` and `monospace-calibration-metadata.json` contain mappings/measurements. The DejaVu font was downloaded locally under `/tmp/rtl` from [the official 2.37 distribution](https://downloads.sourceforge.net/project/dejavu/dejavu/2.37/dejavu-fonts-ttf-2.37.tar.bz2); its file hash is recorded in metadata. Coverage is proven for these samples, not the entirety of Forms-A/B and not the owner's Cascadia/fallback chain.

Reproduction harnesses: `/tmp/rtl/capture_cells.py` and `/tmp/rtl/monospace_calibration.py` (temporary, not installed). Adjacent `.txt` and `.cells.json` files preserve the grid, exact Unicode codepoints and cell coordinates. `/tmp/rtl/before-metadata.json`, `after-metadata.json`, and `monospace-calibration-metadata.json` record environment, font paths/hashes, source snapshot hashes and dimensions. Frozen sources: `/tmp/rtl/main_powerpdf.py`, `after_powerpdf.py`, `after_pdf_display.py`. These `/tmp` artifacts are local and ephemeral, not committed image assets.

## Important discovery: the Rust API is not interchangeable

Locked dependencies: `python-bidi==0.6.12`, `arabic-reshaper==3.1.0` (installed by `uv run --frozen`). Since 0.5, `from bidi import get_display` selects the Rust-backed API. Its signature is `get_display(str_or_bytes, encoding='utf-8', base_dir=None, debug=False)`; `base_dir` is `L`/`R`, bytes are supported, and it has no Python implementation's `upper_is_rtl` option. Since 0.6 the older Python implementation remains explicitly available as `bidi.algorithm.get_display`.

Observed on the required mixed sample, the Rust API with LTR base returned literal `)2(`. Rust's `unicode-bidi::reorder_line` explicitly does **not** implement UAX #9 L3/L4 (combining-character treatment/glyph mirroring); its caller must do that. The Python implementation includes an embedding-level-aware `apply_mirroring` pass. The spike uses that API with natural paragraph direction, yielding `pdf.(2) 2026 …` in the cell grid. Globally swapping all brackets would be incorrect and was not used.

Trade-off: the Python implementation is the older Unicode-v5 algorithm; modern explicit bidi isolate/control sequences are not a supported filename case for this spike. This is not full UAX #9 support for arbitrary hostile/control-bearing Unicode strings. See [python-bidi #25](https://github.com/MeirKriheli/python-bidi/issues/25), [Rust wrapper source](https://github.com/MeirKriheli/python-bidi/blob/master/src/lib.rs), [unicode-bidi omission of L3/L4](https://docs.rs/unicode-bidi/0.3.18/src/unicode_bidi/lib.rs.html#587-594), and [Python mirroring source](https://github.com/MeirKriheli/python-bidi/blob/master/bidi/algorithm.py#L582-L661).

## Sorting, width, truncation, editing, and cost

- **Sorting:** unchanged logical `Path.name` keys; this avoids reordering the filesystem list according to presentation glyphs. Queue order and Option IDs remain logical.
- **Cell width:** measured `rich.cells.cell_len` and `wcwidth.wcswidth` agree on all six checked outputs: `رخصة المركبة.pdf` 16 cells, `شهادة 2026.pdf` 14, `Ahmed عقد.pdf` 13, mixed report 26, `لا.pdf` 5, Windows path sample 34. Every emitted presentation-form glyph in the basic unit samples is one Rich cell. Width need not equal the logical width: `لا.pdf` goes from 6 to 5 because lam-alef is one glyph. Textual receives the visual string before measuring/cropping, so highlight/layout use its actual display width. Arbitrary combining-mark names were not proven on Windows.
- **Truncation:** remains a real limitation, deliberately documented rather than adding a new list widget. Textual crops labels at the right edge. After visual reordering, the rightmost Arabic text is often the **logical beginning** of the filename, so narrow trees/queues can hide the most useful part while leaving the extension/numeric suffix visible. This is visible for the long report in the app screenshot. Wider terminals/Info/Recent help but do not solve it. A shipping design could truncate logical names before shaping or deliberately crop from the other side; that needs a UI decision and tests.
- **Rename/search/go/save/card-name Input:** stays **logical**. Arabic in the editable field therefore still appears wrong in a non-bidi terminal; caret motion/selection are not RTL-aware. The rename dialog border can show the transformed name while the Input shows its original logical content. Pre-reordering `Input.value` would corrupt editing/filenames and was not done. There is no new search/filter feature in this spike; any existing Input keeps the same logical editing behavior.
- **Copy/accessibility:** screen-copy yields presentation-form/visual text, not a reusable logical filename. Use the actual filesystem or logical Input value rather than copying rendered labels into commands.
- **Measured microbenchmark:** on this Linux Python 3.13 environment, warm `timeit`, 100,000 ASCII calls ≈0.030 μs/call; 10,000 mixed-Arabic report calls ≈55.4 μs/call. This is a local observation, not a Windows performance guarantee. Non-Arabic labels do not invoke either transform. No sort algorithm or PDF operation changes.

## Primary-source research

### Windows Terminal and fonts

- [microsoft/terminal #538](https://github.com/microsoft/terminal/issues/538) remains open. A maintainer's 2026-03-20 comment says AtlasEngine still needs RTL analysis/directional-marker handling (`AnalyzeBidi`, UAX #9). This supports the owner's report; it does not establish an identical behavior matrix for every WT build.
- Historical renderer fixes [#1873](https://github.com/microsoft/terminal/pull/1873) (RTL drawing origin) and [#7190](https://github.com/microsoft/terminal/pull/7190) (consecutive RTL glyph-run order) were partial rendering improvements, not complete console bidi semantics. Do not infer a full solution from those closed PRs.
- Unicode defines presentation forms in [Forms-A U+FB50–U+FDFF](https://www.unicode.org/charts/PDF/UFB50.pdf) and [Forms-B U+FE70–U+FEFF](https://www.unicode.org/charts/PDF/UFE70.pdf). Their encoded glyphs are exactly what the display workaround needs to draw; native Arabic script shaping support alone does not establish coverage.
- [Cascadia #717](https://github.com/microsoft/cascadia-code/issues/717) reports missing Forms-B in Cascadia Mono 2111.01 static OTF on Linux and remains open. That is **not** proof of current Windows Cascadia's complete coverage or lack thereof. [Cascadia upstream](https://github.com/microsoft/cascadia-code) has Arabic design resources, but no complete current Forms-A/B guarantee was established here.
- [WT profile appearance docs](https://learn.microsoft.com/en-us/windows/terminal/customize-settings/profile-appearance) name Cascadia Mono as the default and Consolas when the requested font is unavailable. This is not the same as per-codepoint fallback. [WT #2664](https://github.com/microsoft/terminal/issues/2664) discusses DirectWrite/system missing-glyph fallback. No specific fallback chain or coverage is guaranteed for the owner's machine. Font fallback, glyph clipping, and continuous joins **must be confirmed there**. Local Noto evidence is not Cascadia evidence.

### Textual

- [Official text alignment docs](https://textual.textualize.io/css_types/text_align/) say `start`/`end` currently alias left/right and may change “when RTL languages become supported by Textual.” Right alignment does not perform bidi or shaping.
- [Textual discussion #6291](https://github.com/Textualize/textual/discussions/6291) reports unsupported `direction: rtl` and RTL TextArea editing against 6.11.0. It is an unanswered user report about that version, not a complete source audit of PowerPDF's newer Textual. This spike is explicitly a display adapter, not framework RTL support.

### Library maintenance and licensing

- [`python-bidi` current PyPI metadata](https://pypi.org/pypi/python-bidi/json), [changelog](https://github.com/MeirKriheli/python-bidi/blob/master/CHANGELOG.rst), and [wrapper](https://github.com/MeirKriheli/python-bidi/blob/master/bidi/wrapper.py) establish current 0.6.12, Python ≥3.10, API split, and Rust/Python differences. The Python algorithm header is **LGPL-3.0-or-later**, not MIT. Shipping the bundled application needs corresponding license materials and review of LGPL distribution obligations; this branch is not a release.
- [`arabic-reshaper` 3.1.0](https://pypi.org/project/arabic-reshaper/), [upstream README](https://github.com/mpcabd/python-arabic-reshaper), and [MIT license](https://github.com/mpcabd/python-arabic-reshaper/blob/master/LICENSE). Recent publication/packaging modernization makes “abandoned” inaccurate; it does not guarantee future maintenance. Retain its copyright/permission notice when distributing. Default `delete_harakat=True` is deliberately overridden here.

### Terminals with native bidi (double-processing risk)

| Terminal | Evidence / active-mode caveat |
|---|---|
| mintty | [Manual](https://mintty.github.io/mintty.1.html): bidi/Arabic shaping; default `Bidi=2`. `Bidi=0` disables; `Bidi=1` disables on alternate screen only. Identity cannot tell active mode. |
| Konsole | [Complex text rendering](https://docs.kde.org/stable_kf6/en/konsole/konsole/complex-text-rendering.html): configurable bidi/shaping, forced-LTR/table overrides; styles can split words and affect shaping. |
| GNOME Terminal / VTE | [`enable-bidi`](https://gnome.pages.gitlab.gnome.org/vte/gtk3/property.Terminal.enable-bidi.html): since 0.58, default TRUE, writable by embedding app. VTE identity does not prove enabled. |
| mlterm | [Upstream bidi README](https://github.com/arakiken/mlterm/blob/master/doc/en/README.bidi): FriBidi build support, bidi by default in UTF-8, Arabic shaping and optional OpenType; build/font settings matter. |
| iTerm2 | [Official RTL request #1611](https://gitlab.com/gnachman/iterm2/-/issues/1611) remains open. Not assumed native bidi; no claim about every partial shaping behavior. |

[WT #840](https://github.com/microsoft/terminal/issues/840) documents `WT_SESSION` and inheritance into nested terminals. [GNOME source-history](https://mail.gnome.org/archives/commits-list/2020-April/msg11861.html) documents `VTE_VERSION` injection. Other identity variables used here are best-effort conventions, not experimentally proven capability flags. [Terminal bidi working-group rationale](https://terminal-wg.pages.freedesktop.org/bidi/bidi-intro/why-terminals-are-special.html) explains why terminal cell addressing and application-level text cropping complicate bidi, and why independently reordering at both layers is fragile.

## Verification and shipping estimate

Requested command, run once after code/test changes:

```text
uv run --frozen pytest -q
52 passed, 4 skipped in 33.98s
```

The skipped cases require Windows ACLs or Ghostscript. Focused tests cover exact logical→visual strings, ASCII/non-Arabic passthrough, digits/parentheses, mixed Latin, lam-alef width, slash/backslash/line segmentation, toggle/detection, logical tree node/Option IDs, logical rename Input, real renamed files and settings identity. Existing tests remain passing. This is Linux verification; no installer build, Windows test, native-bidi-terminal matrix, or release was performed.

**Estimated effort to ship as 0.7.1: 2–4 focused engineering days after the owner can test**, not counting waiting for access/feedback: approximately one day for owner WT/font validation and policy; one day for RTL-aware logical truncation and a more usable rename display strategy; one to two days for dependency/license notices, Windows wheel/PyInstaller packaging, installer rehearsal, regression/cell-width cases and release preparation. This is an estimate, not observed work. If actual WT fallback cannot draw connected forms acceptably, the workaround should not be marketed as solving Arabic rendering; use a proper GUI text engine instead. No version bump/tag/push/release is part of this spike.
