#!/usr/bin/env python3
"""PowerPDF: a pixel-vibe terminal UI for exploring files and merging, splitting, and printing PDFs."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import threading
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Callable

import pypdfium2 as pdfium
from rich.text import Text
from send2trash import send2trash

# textual_image probes the terminal (sixel / kitty graphics / half-blocks) on import,
# so it must be imported before the app starts.
from textual_image.widget import HalfcellImage, Image as SharpImage
from textual_image._terminal import get_cell_size
from textual import events, on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Center, Horizontal, Middle, Vertical
from textual.markup import escape
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import (
    DirectoryTree, Footer, Input, Label, OptionList, RichLog, Static, TabbedContent, TabPane,
)
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

VERSION = "0.4.0"
SETTINGS = Path(os.environ.get("APPDATA") or Path.home() / ".config") / "powerpdf" / "settings.json"
RECENT_MAX = 30
BAD_NAME_CHARS = set('\\/:*?"<>|')
PDFIUM_LOCK = threading.Lock()  # pdfium is not thread-safe

THEMES = [
    Theme(
        name="powerpdf",
        primary="#FF4F81",
        secondary="#7B5CFF",
        accent="#FFB000",
        warning="#FFB000",
        error="#FF3B3B",
        success="#3DDC84",
        foreground="#F2E9E4",
        background="#0E0B16",
        surface="#17121F",
        panel="#241B33",
        dark=True,
    ),
    Theme(
        name="powerpdf-gameboy",
        primary="#9BBC0F",
        secondary="#8BAC0F",
        accent="#E0F8D0",
        warning="#E0F8D0",
        error="#FF6B6B",
        success="#9BBC0F",
        foreground="#C4E39A",
        background="#0F380F",
        surface="#173F17",
        panel="#306230",
        dark=True,
    ),
]

GLYPHS = {
    "P": ["████ ", "█   █", "████ ", "█    ", "█    "],
    "O": [" ███ ", "█   █", "█   █", "█   █", " ███ "],
    "W": ["█   █", "█   █", "█ █ █", "██ ██", "█   █"],
    "E": ["█████", "█    ", "████ ", "█    ", "█████"],
    "R": ["████ ", "█   █", "████ ", "█  █ ", "█   █"],
    "D": ["████ ", "█   █", "█   █", "█   █", "████ "],
    "F": ["█████", "█    ", "████ ", "█    ", "█    "],
}


def logo() -> str:
    """POWERPDF in a 5-row pixel font, colored by the active theme."""
    rows = []
    for row in range(5):
        power = " ".join(GLYPHS[c][row] for c in "POWER")
        pdf = " ".join(GLYPHS[c][row] for c in "PDF")
        rows.append(f"[$primary]{power}[/]  [$accent]{pdf}[/]")
    return "\n".join(rows)


def is_pdf(path: Path) -> bool:
    return path.suffix.lower() == ".pdf" and path.is_file()


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def details(rows: list[tuple[str, object]]) -> str:
    return "\n".join(f"[$foreground-muted]{k:<9}[/]{escape(str(v))}" for k, v in rows)


def list_printers() -> list[str]:
    """Windows printer names, default printer first."""
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_Printer | Sort-Object { -not $_.Default } | ForEach-Object Name"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def print_pdf(path: Path, printer: str, timeout: float = 300) -> bool:
    """Print through Ghostscript's Windows printer device; True on success.

    Ghostscript waits forever on a printer that doesn't exist, hence the timeout."""
    try:
        result = subprocess.run(
            ["gswin64c", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dNoCancel",
             "-sDEVICE=mswinpr2", f"-sOutputFile=%printer%{printer}", str(path)],
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def output_checks(inputs: list[Path]) -> tuple[Callable[[str], str | None], Callable[[str], str | None]]:
    """PromptDialog check/confirm for a new PDF written from inputs."""
    def check(value: str) -> str | None:
        output = Path(value).expanduser().resolve()
        if output.suffix.lower() != ".pdf":
            return "Output must end in .pdf"
        if output in inputs:
            return "Output is one of the input files"
        return None

    exists = lambda value: "File exists. Enter again to overwrite" if Path(value).expanduser().exists() else None
    return check, exists


class FileTree(DirectoryTree):
    """Folders and PDFs (every file with show_all); other files are dimmed, queued PDFs get a dot."""

    BINDINGS = [
        Binding("space", "mark", "Queue"),
        Binding("enter", "open", "Open", show=False),
        Binding("backspace", "up", "Up", key_display="⌫"),
    ]
    show_all = False

    def filter_paths(self, paths):
        return [
            p for p in paths
            if not p.name.startswith(".") and (self.show_all or p.is_dir() or p.suffix.lower() == ".pdf")
        ]

    def render_label(self, node, base_style, style):
        label = super().render_label(node, base_style, style)
        path = node.data.path if node.data else None
        if path is None or path.is_dir():
            return label
        if path.suffix.lower() != ".pdf":
            label.stylize("dim")
        elif path in self.app.queue:
            label = Text("● ", style="bold") + label
        return label

    def action_mark(self) -> None:
        node = self.cursor_node
        if node is None or node.data is None:
            return
        path = node.data.path
        if path.is_dir():
            self.action_toggle_node()
        elif is_pdf(path):
            self.app.toggle_queued(path)
        else:
            self.app.notify("Only PDFs can be queued.", severity="warning")

    def action_open(self) -> None:
        node = self.cursor_node
        if node and node.data and node.data.path.is_dir():
            self.action_toggle_node()
        else:
            self.app.action_open()

    def action_up(self) -> None:
        self.app.go_to(Path(self.path).parent)


class PathList(OptionList):
    """An OptionList whose option ids are file paths."""

    def path_at_cursor(self) -> Path | None:
        if self.highlighted is None:
            return None
        return Path(self.get_option_at_index(self.highlighted).id)


class QueueList(PathList):
    BINDINGS = [
        Binding("shift+up", "move(-1)", "Move up"),
        Binding("shift+down", "move(1)", "Move down"),
        Binding("delete,backspace", "drop", "Remove"),
    ]

    def action_move(self, step: int) -> None:
        self.app.move_queued(self.highlighted, step)

    def action_drop(self) -> None:
        self.app.drop_queued(self.highlighted)


class RecentList(PathList):
    BINDINGS = [
        Binding("space", "mark", "Queue"),
        Binding("enter", "app.open", "Open", show=False),
        Binding("delete,backspace", "forget", "Forget"),
    ]

    def action_mark(self) -> None:
        if path := self.path_at_cursor():
            self.app.toggle_queued(path)

    def action_forget(self) -> None:
        if path := self.path_at_cursor():
            self.app.forget(path)


class Splash(Screen):
    """Opening title card; any key skips it."""

    def compose(self) -> ComposeResult:
        with Middle():
            with Center():
                yield Static(logo(), id="logo")
            with Center():
                yield Static(f"[$foreground-muted]explore · merge · split · print     v{VERSION}[/]")
            with Center():
                yield Static("[$foreground-muted]press any key[/]", id="skip")

    def on_mount(self) -> None:
        self.set_timer(1.5, self.close)

    def on_key(self) -> None:
        self.close()

    def close(self) -> None:
        # No await and no return value: the timer would await dismiss() inside this screen's own handler.
        if self.is_current:
            self.dismiss()


class PromptDialog(ModalScreen[str | None]):
    """One-line input. check() returns an error to show; confirm() a warning a second Enter accepts."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(
        self,
        title: str,
        value: str,
        check: Callable[[str], str | None] = lambda value: None,
        confirm: Callable[[str], str | None] = lambda value: None,
    ) -> None:
        super().__init__()
        self.title_text, self.value = title, value
        self.check, self.confirm = check, confirm
        self.armed: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as dialog:
            dialog.border_title = self.title_text
            yield Input(self.value)
            yield Label("[$foreground-muted]Enter to confirm · Esc to cancel[/]", id="hint")

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        hint = self.query_one("#hint", Label)
        if error := self.check(value):
            hint.update(f"[$error]{escape(error)}[/]")
        elif (warning := self.confirm(value)) and self.armed != value:
            self.armed = value
            hint.update(f"[$warning]{escape(warning)}[/]")
        else:
            self.dismiss(value)


class ConfirmDialog(ModalScreen[bool]):
    BINDINGS = [
        Binding("y,enter", "dismiss(True)", "Yes"),
        Binding("n,escape", "dismiss(False)", "No"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as dialog:
            dialog.border_title = "Confirm"
            yield Label(self.question)
            yield Label("[$foreground-muted]y / Enter = yes · n / Esc = no[/]", id="hint")


class PrinterDialog(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as dialog:
            dialog.border_title = "Printer"
            yield OptionList()
            yield Label("[$foreground-muted]Loading printers…[/]", id="hint")

    def on_mount(self) -> None:
        self.load()

    @work(thread=True)
    def load(self) -> None:
        try:
            printers = list_printers()
        except (OSError, subprocess.CalledProcessError) as error:
            self.app.call_from_thread(self.show, [], f"[$error]{escape(str(error))}[/]")
            return
        hint = "[$foreground-muted]Enter to print · Esc to cancel[/]" if printers else "[$error]No printers found[/]"
        self.app.call_from_thread(self.show, printers, hint)

    def show(self, printers: list[str], hint: str) -> None:
        options = self.query_one(OptionList)
        options.add_options(printers)
        options.highlighted = 0 if printers else None
        options.focus()
        self.query_one("#hint", Label).update(hint)

    @on(OptionList.OptionSelected)
    def pick(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.prompt))


class ChoiceDialog(ModalScreen[str | None]):
    """Pick one option; dismisses with its id."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, choices: list[Option]) -> None:
        super().__init__()
        self.title_text, self.choices = title, choices

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as dialog:
            dialog.border_title = self.title_text
            yield OptionList(*self.choices)
            yield Label("[$foreground-muted]Enter to choose · Esc to cancel[/]", id="hint")

    def on_mount(self) -> None:
        self.query_one(OptionList).highlighted = 0

    @on(OptionList.OptionSelected)
    def pick(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class CardScreen(ModalScreen):
    """Front and back cut out of scans: nudge each crop, then save both as a card-sized 2-page PDF or two JPGs."""

    BINDINGS = [
        Binding("enter", "save", "Save PDF"),
        Binding("j", "save_jpg", "Save JPG"),
        Binding("tab", "switch", "Front/Back"),
        Binding("left_square_bracket", "change(0, 0, 0, 0, 0.5)", "Tilt", key_display="[ ]"),
        Binding("right_square_bracket", "change(0, 0, 0, 0, -0.5)", "Tilt", show=False),
        Binding("r", "change(0, 0, 0, 0, 180)", "Rotate 180°"),
        Binding("left", "change(-10, 0)", "Move", key_display="←↑↓→"),
        Binding("right", "change(10, 0)", "Move", show=False),
        Binding("up", "change(0, -10)", "Move", show=False),
        Binding("down", "change(0, 10)", "Move", show=False),
        Binding("shift+right", "change(0, 0, 10, 0)", "Resize", key_display="shift+←↑↓→"),
        Binding("shift+left", "change(0, 0, -10, 0)", "Resize", show=False),
        Binding("shift+up", "change(0, 0, 0, 10)", "Resize", show=False),
        Binding("shift+down", "change(0, 0, 0, -10)", "Resize", show=False),
        Binding("escape", "app.pop_screen", "Cancel"),
    ]

    def __init__(self, sources: list[Path], cards: list) -> None:
        super().__init__()
        self.sources = sources
        self.pages = [page for page, _ in cards]
        self.rects = [rect for _, rect in cards]
        self.side = 0

    def compose(self) -> ComposeResult:
        image = HalfcellImage if self.app.pixel_mode else SharpImage
        with Horizontal():
            for i, name in enumerate(("Front", "Back")):
                with Vertical(id=f"side{i}", classes="panel card") as panel:
                    panel.border_title = name
                    yield image(id=f"cut{i}", classes="cut")
        yield Footer()

    def on_mount(self) -> None:
        for i in range(2):
            self.show(i)
        self.action_switch(0)

    def show(self, i: int) -> None:
        from pdf_card import DPI, cut
        (_, _), (w, h), angle = self.rects[i]
        self.query_one(f"#cut{i}").image = cut(self.pages[i], self.rects[i])
        mm = lambda px: px / DPI * 25.4
        self.query_one(f"#side{i}").border_subtitle = f"{mm(w):.1f} × {mm(h):.1f} mm · angle {angle:+.1f}°"

    def action_switch(self, side: int | None = None) -> None:
        self.side = 1 - self.side if side is None else side
        for i in range(2):
            self.query_one(f"#side{i}").set_class(i == self.side, "-active")

    def action_change(self, dx: float, dy: float, dw: float = 0, dh: float = 0, da: float = 0) -> None:
        (cx, cy), (w, h), angle = self.rects[self.side]
        self.rects[self.side] = ((cx + dx, cy + dy), (max(20, w + dw), max(20, h + dh)), angle + da)
        self.show(self.side)

    def action_save(self) -> None:
        default = self.sources[0].with_name(f"{self.sources[0].stem}_card.pdf")
        self.app.push_screen(
            PromptDialog("Save front + back as", str(default), *output_checks(self.sources)),
            lambda value: value and self.save(Path(value).expanduser().resolve()),
        )

    def action_save_jpg(self) -> None:
        outputs = [self.sources[0].with_name(f"{self.sources[0].stem}_{side}.jpg") for side in ("front", "back")]
        if existing := [o.name for o in outputs if o.exists()]:
            self.app.push_screen(
                ConfirmDialog(f"Overwrite [b]{escape(' and '.join(existing))}[/]?"),
                lambda ok: ok and self.save(*outputs),
            )
        else:
            self.save(*outputs)

    def save(self, *outputs: Path) -> None:
        """One output: a 2-page PDF. Two: front and back JPGs."""
        from pdf_card import cut, save_cards, save_jpgs
        try:
            cards = [cut(page, rect) for page, rect in zip(self.pages, self.rects)]
            save_cards(cards, *outputs) if len(outputs) == 1 else save_jpgs(cards, list(outputs))
        except OSError as error:
            self.notify(f"Can't save: {error}", severity="error")
            return
        self.app.pop_screen()
        self.app.saved_card(*outputs)


class CompareScreen(ModalScreen):
    """A PDF and its _small copy side by side, with sizes and the percent saved. [ ] flips both, up to the shorter one's last page."""

    BINDINGS = [
        Binding("left_square_bracket", "page(-1)", "Prev page", key_display="[ ]"),
        Binding("right_square_bracket", "page(1)", "Next page", show=False),
        Binding("escape", "dismiss(None)", "Close"),
    ]

    def __init__(self, original: Path, small: Path) -> None:
        super().__init__()
        self.paths = (original, small)
        self.sizes = [path.stat().st_size for path in self.paths]
        self.page = 0
        self.count = 1  # pages both docs have; known after the first render

    def compose(self) -> ComposeResult:
        image = HalfcellImage if self.app.pixel_mode else SharpImage
        before, after = self.sizes
        change = round((after - before) / max(before, 1) * 100)
        verdict = f"[b $success]−{-change}%[/]" if change <= 0 else f"[b $error]+{change}% (larger)[/]"
        yield Static(f"{verdict} ({human_size(before)} → {human_size(after)})", id="saved")
        with Horizontal():
            for i, (label, path) in enumerate(zip(("Original", "Small"), self.paths)):
                with Vertical(id=f"side{i}", classes="panel compare") as panel:
                    panel.border_title = f"{label} · {path.name}"
                    yield image(id=f"shot{i}", classes="shot")
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self.load, 0)  # after layout, so the panels have a size

    def action_page(self, step: int) -> None:
        if 0 <= self.page + step < self.count:
            self.load(self.page + step)

    @work(thread=True, exclusive=True, group="compare")
    def load(self, page: int) -> None:
        """Render both sides. Each PDF is opened from bytes and closed again, so no pdfium handle outlives a render."""
        shots = []
        try:
            for i, path in enumerate(self.paths):
                with PDFIUM_LOCK:
                    mtime = path.stat().st_mtime_ns
                    pdf = pdfium.PdfDocument(path.read_bytes())
                    try:
                        shots.append((self.app.render_cached(pdf, path, mtime, page, self.query_one(f"#side{i}")), len(pdf)))
                    finally:
                        pdf.close()
        except (pdfium.PdfiumError, OSError) as error:
            self.app.call_from_thread(self.notify, f"Can't compare: {error}", severity="error")
            return
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self.show, page, shots)

    def show(self, page: int, shots: list) -> None:
        self.page, self.count = page, min(count for _, count in shots)
        for i, (image, count) in enumerate(shots):
            self.query_one(f"#shot{i}").image = image
            self.query_one(f"#side{i}").border_subtitle = f"{human_size(self.sizes[i])} · page {page + 1}/{count}"


class PowerPDF(App):
    TITLE = "PowerPDF"
    CSS = """
    Screen { background: $background; }
    Splash { align: center middle; }
    Splash Static { width: auto; }
    Splash #logo { padding: 1 2; }
    Splash #skip { padding-top: 2; }

    #topbar { dock: top; height: 1; background: $panel; padding: 0 1; }

    .panel {
        border: round $panel-lighten-2;
        border-title-color: $foreground-muted;
        border-subtitle-color: $foreground-muted;
        background: $surface;
    }
    .panel:focus, .panel:focus-within {
        border: round $primary;
        border-title-color: $primary;
        border-title-style: bold;
    }

    #left { width: 32; }
    #left TabPane { padding: 0; }
    #left FileTree, #left RecentList { height: 1fr; border: none; }
    #preview { width: 1fr; align: center middle; }
    #preview #page { width: auto; height: 1fr; }
    #empty { width: auto; height: auto; }
    #side { width: 34; }
    #info { height: auto; min-height: 9; padding: 0 1; }
    #queue { height: 1fr; }
    #activity { height: 8; }

    FileTree, PathList, RichLog { background: $surface; }

    PromptDialog, ConfirmDialog, PrinterDialog, ChoiceDialog { align: center middle; background: $background 60%; }
    CardScreen { background: $background; }
    CompareScreen { background: $background; }
    CompareScreen #saved { height: 1; padding: 0 1; background: $panel; }
    CompareScreen .compare { width: 1fr; align: center middle; }
    CompareScreen .shot { width: auto; height: 1fr; }
    CardScreen .card { width: 1fr; align: center middle; }
    CardScreen .card.-active { border: round $primary; border-title-color: $primary; border-title-style: bold; }
    CardScreen .cut { width: 1fr; height: auto; }
    .dialog {
        width: 64; height: auto; max-height: 20; padding: 1 2;
        border: thick $primary; border-title-color: $accent; border-title-style: bold;
        background: $surface;
    }
    .dialog OptionList { height: auto; max-height: 12; }
    .dialog #hint { padding-top: 1; }
    """

    BINDINGS = [
        Binding("o", "open", "Open"),
        Binding("m", "merge", "Merge"),
        Binding("s", "split", "Split"),
        Binding("p", "print", "Print"),
        Binding("k", "card", "Card"),
        Binding("c", "compress", "Compress"),
        Binding("d", "compare", "Compare"),
        Binding("r", "toggle_recent", "Recent"),
        Binding("question_mark", "show_help_panel", "Keys", key_display="?"),
        Binding("q", "quit", "Quit"),
        # in the help panel (?) and command palette (ctrl+p), not the footer
        Binding("e", "reveal", "Show in Explorer", show=False),
        Binding("f2", "rename", "Rename", show=False),
        Binding("delete", "delete", "Delete to Recycle Bin", show=False),
        Binding("g", "go", "Go to folder", show=False),
        Binding("tilde", "home", "Home folder", show=False),
        Binding("f", "toggle_all", "All files / PDFs only", show=False),
        Binding("ctrl+r", "reload", "Refresh", show=False),
        Binding("a", "queue_folder", "Queue folder", show=False),
        Binding("x", "clear_queue", "Clear queue", show=False),
        Binding("left_square_bracket", "page(-1)", "Prev page", show=False),
        Binding("right_square_bracket", "page(1)", "Next page", show=False),
        Binding("v", "toggle_pixels", "Pixel mode", show=False),
        Binding("ctrl+t", "change_theme", "Theme", show=False),
    ]

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.queue: list[Path] = []
        self.current: Path | None = None  # PDF in the preview
        self.page = 0
        self.page_count = 0
        self.doc: tuple[Path, int, pdfium.PdfDocument] | None = None  # open preview PDF; guarded by PDFIUM_LOCK
        self.page_cache: OrderedDict = OrderedDict()  # (path, mtime_ns, page, scale) -> image, 16 newest; guarded by PDFIUM_LOCK
        self.pixel_mode = False
        try:
            self.settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.settings = {}
        self.recent = [p for p in map(Path, self.settings.get("recent", [])) if p.is_file()]

    def compose(self) -> ComposeResult:
        yield Static(self.topbar(), id="topbar")
        with Horizontal():
            with TabbedContent(id="left", classes="panel"):
                with TabPane("Files", id="files-tab"):
                    yield FileTree(self.root, id="files")
                with TabPane("Recent", id="recent-tab"):
                    yield RecentList(id="recent")
            with Vertical(id="preview", classes="panel") as preview:
                preview.border_title = "Preview"
                yield Static(logo(), id="empty")
                yield SharpImage(id="page")
            with Vertical(id="side"):
                info = Static("[$foreground-muted]Highlight a file to see its details.[/]", id="info", classes="panel")
                info.border_title = "Info"
                yield info
                queue = QueueList(id="queue", classes="panel")
                queue.border_title = "Queue"
                yield queue
        log = RichLog(id="activity", classes="panel", wrap=True)
        log.border_title = "Activity"
        yield log
        yield Footer()

    def on_mount(self) -> None:
        for theme in THEMES:
            self.register_theme(theme)
        saved = self.settings.get("theme", "powerpdf")
        self.theme = saved if saved in self.available_themes else "powerpdf"
        self.theme_changed_signal.subscribe(self, self.save_theme)
        self.query_one("#page").display = False
        self.refresh_queue()
        self.refresh_recent()
        self.query_one(FileTree).focus()
        self.push_screen(Splash())

    def topbar(self) -> str:
        return (
            f"[b $primary]▐█▌[/] [b]Power[$accent]PDF[/][/]  "
            f"[$foreground-muted]v{VERSION} · {escape(str(self.root))}[/]"
        )

    def save_settings(self, **changes) -> None:
        self.settings |= changes
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")

    def save_theme(self, theme: Theme) -> None:
        self.save_settings(theme=theme.name)

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        for title, help_text, action in [
            ("Open", "Open the highlighted file in its default app, or enter the folder", self.action_open),
            ("Show in Explorer", "Reveal the highlighted file in Windows Explorer", self.action_reveal),
            ("Rename", "Rename the highlighted file or folder", self.action_rename),
            ("Delete", "Move the highlighted file or folder to the Recycle Bin", self.action_delete),
            ("Go to folder", "Type a folder or drive to explore", self.action_go),
            ("Home folder", "Explore your home folder", self.action_home),
            ("Toggle all files", "Show every file, or PDFs only", self.action_toggle_all),
            ("Merge queue", "Merge queued PDFs into one file", self.action_merge),
            ("Split", "Split queued (or highlighted) PDFs into single pages", self.action_split),
            ("Print", "Print queued (or highlighted) PDFs", self.action_print),
            ("Card front + back", "Cut a scanned card's front and back into a small card-sized PDF", self.action_card),
            ("Compare", "Show the highlighted PDF and its _small copy side by side", self.action_compare),
            ("Compress", "Small, readable copies (<name>_small.pdf) to upload or send", self.action_compress),
            ("Queue folder", "Queue every PDF in the highlighted folder", self.action_queue_folder),
            ("Clear queue", "Empty the queue", self.action_clear_queue),
            ("Toggle pixel mode", "Switch preview between sharp and half-block pixels", self.action_toggle_pixels),
        ]:
            yield SystemCommand(title, help_text, action)

    def say(self, line: str, tone: str = "foreground") -> None:
        color = self.get_css_variables()[tone]
        self.query_one("#activity", RichLog).write(Text(line, style=color))

    # ── what's under the cursor ──────────────────────────────────────────────

    def focused_path(self) -> Path | None:
        """The file or folder under the cursor of the focused list."""
        widget = self.focused
        if isinstance(widget, FileTree):
            node = widget.cursor_node
            return node.data.path if node and node.data else None
        if isinstance(widget, PathList):
            return widget.path_at_cursor()
        return None

    def targets(self) -> list[Path]:
        """The queue, or the highlighted PDF when the queue is empty."""
        if self.queue:
            return list(self.queue)
        path = self.focused_path()
        return [path] if path and is_pdf(path) else []

    @on(DirectoryTree.NodeHighlighted)
    @on(OptionList.OptionHighlighted)
    def follow_cursor(self, event) -> None:
        if event.control.has_focus:
            self.show_path(self.focused_path())

    def on_descendant_focus(self) -> None:
        self.show_path(self.focused_path())

    def show_path(self, path: Path | None) -> None:
        if path is None or path == self.current or not path.exists():
            return
        if is_pdf(path):
            self.current, self.page = path, 0
            self.load_preview(path, 0)
            return
        self.current = None
        with PDFIUM_LOCK:
            self.close_doc()
        self.query_one("#page").display = False
        self.query_one("#empty").display = True
        self.query_one("#preview").border_subtitle = ""
        stat = path.stat()
        rows = [("type", "folder" if path.is_dir() else path.suffix[1:].upper() or "file")]
        if path.is_file():
            rows.append(("size", human_size(stat.st_size)))
        rows.append(("modified", datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")))
        self.show_info(path, details(rows))

    # ── preview ──────────────────────────────────────────────────────────────

    def action_page(self, step: int) -> None:
        if self.current and 0 <= self.page + step < self.page_count:
            self.page += step
            self.load_preview(self.current, self.page)

    def preview_scale(self, width: float, height: float, panel=None) -> float:
        """Scale at which a page just fills the panel's pixels (the preview's by default), never above 1.5.
        Half-block mode shows fewer pixels than sixel, so sizing for sixel is sharp in both."""
        box, cell = (panel or self.query_one("#preview")).content_size, get_cell_size()
        max_w, max_h = (box.width * cell.width, box.height * cell.height) if box.width and box.height else (1600, 1600)
        return min(1.5, max_w / width, max_h / height)

    def close_doc(self) -> None:
        """Close the open preview PDF. The caller holds PDFIUM_LOCK."""
        if self.doc:
            self.doc[2].close()
            self.doc = None

    def open_doc(self, path: Path, mtime: int) -> pdfium.PdfDocument:
        """The preview PDF, reopened only when the selection or the file changed. The caller holds PDFIUM_LOCK.
        Opens from bytes: a path would make pdfium keep a file handle, and Windows then blocks rename, delete and overwrite."""
        if not self.doc or self.doc[:2] != (path, mtime):
            self.close_doc()
            self.doc = (path, mtime, pdfium.PdfDocument(path.read_bytes()))
        return self.doc[2]

    def render_cached(self, pdf: pdfium.PdfDocument, path: Path, mtime: int, page: int, panel=None):
        """Rendered page image, from the LRU if shown before. The caller holds PDFIUM_LOCK."""
        page_obj = pdf[page]
        try:
            scale = self.preview_scale(*page_obj.get_size(), panel)
            key = (path, mtime, page, scale)
            if key in self.page_cache:
                self.page_cache.move_to_end(key)
                return self.page_cache[key]
            image = self.page_cache[key] = page_obj.render(scale=scale).to_pil()
        finally:
            page_obj.close()
        if len(self.page_cache) > 16:
            self.page_cache.popitem(last=False)
        return image

    @work(thread=True, exclusive=True, group="preview")
    def load_preview(self, path: Path, page: int) -> None:
        worker = get_current_worker()
        try:
            with PDFIUM_LOCK:
                mtime = path.stat().st_mtime_ns
                pdf = self.open_doc(path, mtime)
                count = len(pdf)
                image = self.render_cached(pdf, path, mtime, page)
                meta = pdf.get_metadata_dict(skip_empty=True)
        except (pdfium.PdfiumError, OSError) as error:
            self.call_from_thread(self.show_info, path, f"[$error]Can't open: {escape(str(error))}[/]")
            return
        if worker.is_cancelled:
            return
        self.call_from_thread(self.show_preview, path, page, count, image, meta)
        if page + 1 < count and not worker.is_cancelled:  # warm the next flip
            try:
                with PDFIUM_LOCK:
                    if self.doc and self.doc[:2] == (path, mtime):
                        self.render_cached(self.doc[2], path, mtime, page + 1)
            except pdfium.PdfiumError:
                pass

    def on_unmount(self) -> None:
        with PDFIUM_LOCK:
            self.close_doc()

    def show_preview(self, path: Path, page: int, count: int, image, meta: dict) -> None:
        if path != self.current:
            return
        self.page_count = count
        self.query_one("#empty").display = False
        page_widget = self.query_one("#page")
        page_widget.display = True
        page_widget.image = image
        self.query_one("#preview").border_subtitle = f"page {page + 1}/{count} · [ ] flip"
        stat = path.stat()
        rows = [("pages", count), ("size", human_size(stat.st_size)),
                ("modified", datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"))]
        rows += [(key.lower(), meta[key]) for key in ("Title", "Author", "Producer") if key in meta]
        self.show_info(path, details(rows))

    def show_info(self, path: Path, body: str) -> None:
        self.query_one("#info", Static).update(f"[b $accent]{escape(path.name)}[/]\n\n{body}")

    async def action_toggle_pixels(self) -> None:
        self.pixel_mode = not self.pixel_mode
        old = self.query_one("#page")
        new = (HalfcellImage if self.pixel_mode else SharpImage)(old.image, id="page")
        new.display = old.display
        await old.remove()
        await self.query_one("#preview").mount(new)
        self.notify("Pixel mode on" if self.pixel_mode else "Sharp mode on", timeout=2)

    # ── explore ──────────────────────────────────────────────────────────────

    def go_to(self, folder: Path) -> None:
        self.root = folder
        self.query_one("#topbar", Static).update(self.topbar())
        tree = self.query_one(FileTree)
        tree.path = folder
        self.query_one(TabbedContent).active = "files-tab"
        tree.focus()

    def action_go(self) -> None:
        check = lambda value: None if Path(value).expanduser().is_dir() else "Not a folder"
        self.push_screen(
            PromptDialog("Go to folder", str(self.root), check),
            lambda value: value and self.go_to(Path(value).expanduser().resolve()),
        )

    def action_home(self) -> None:
        self.go_to(Path.home())

    def on_paste(self, event: events.Paste) -> None:
        """A file dropped on Windows Terminal arrives as pasted, quoted paths. An Input takes (and stops)
        its own pastes, so this only sees the rest; dialogs and the splash ignore them."""
        if self.screen is not self.screen_stack[0]:
            return
        try:
            tokens = shlex.split(event.text, posix=False)
        except ValueError:  # unbalanced quote: not a dropped path
            return
        paths = [Path(t.strip("\"'")) for t in tokens]
        folders = [p for p in paths if p.is_dir()]
        pdfs = [p.resolve() for p in paths if is_pdf(p)]
        new = [p for p in dict.fromkeys(pdfs) if p not in self.queue]
        if new:
            self.queue += new
            self.refresh_queue(len(self.queue) - 1)
            self.notify(f"Queued {len(new)} PDF{'s' * (len(new) > 1)}", timeout=2)
        if folders:  # a mix queues the PDFs, then goes to the last folder
            self.go_to(folders[-1].resolve())

    def action_toggle_all(self) -> None:
        tree = self.query_one(FileTree)
        tree.show_all = not tree.show_all
        tree.reload()
        self.notify("Showing all files" if tree.show_all else "Showing PDFs only", timeout=2)

    def action_reload(self) -> None:
        self.query_one(FileTree).reload()

    def action_toggle_recent(self) -> None:
        tabs = self.query_one(TabbedContent)
        tabs.active = "files-tab" if tabs.active == "recent-tab" else "recent-tab"
        self.query_one(FileTree if tabs.active == "files-tab" else RecentList).focus()

    # ── file actions ─────────────────────────────────────────────────────────

    def action_open(self) -> None:
        path = self.focused_path()
        if path is None:
            return
        if path.is_dir():
            self.go_to(path)
            return
        try:
            os.startfile(path)  # Windows: the file's default app
        except OSError as error:
            self.notify(f"Can't open {path.name}: {error}", severity="error")
            return
        self.say(f"Opened {path.name}")
        self.remember(path)

    def action_reveal(self) -> None:
        if path := self.focused_path():
            subprocess.Popen(f'explorer /select,"{path}"')

    def action_rename(self) -> None:
        path = self.focused_path()
        if path is None or path == self.root:
            return

        def check(name: str) -> str | None:
            if not name or name in (".", "..") or BAD_NAME_CHARS & set(name):
                return 'Not a valid name (no \\ / : * ? " < > |)'
            if name.lower() != path.name.lower() and (path.parent / name).exists():
                return "That name is taken"
            return None

        self.push_screen(
            PromptDialog(f"Rename {path.name}", path.name, check),
            lambda name: name and name != path.name and self.rename(path, name),
        )

    def rename(self, path: Path, name: str) -> None:
        new = path.with_name(name)
        try:
            path.rename(new)
        except OSError as error:
            self.say(f"✗ rename failed: {error}", "error")
            return
        self.moved(path, new)
        self.say(f"Renamed {path.name} → {name}")

    def action_delete(self) -> None:
        path = self.focused_path()
        if path is None or path == self.root:
            return
        self.push_screen(
            ConfirmDialog(f"Move [b]{escape(path.name)}[/] to the Recycle Bin?"),
            lambda ok: ok and self.trash(path),
        )

    def trash(self, path: Path) -> None:
        try:
            send2trash(path)
        except OSError as error:
            self.say(f"✗ delete failed: {error}", "error")
            return
        self.moved(path, None)
        self.say(f"Moved {path.name} to the Recycle Bin")

    def moved(self, old: Path, new: Path | None) -> None:
        """Keep queue and recent in step with a renamed (new) or deleted (None) file or folder."""
        def fix(path: Path) -> Path | None:
            if not path.is_relative_to(old):
                return path
            return None if new is None else new / path.relative_to(old)

        self.queue = [p for p in map(fix, self.queue) if p]
        self.recent = [p for p in map(fix, self.recent) if p]
        if self.current and self.current.is_relative_to(old):
            self.current = None
            with PDFIUM_LOCK:
                self.close_doc()
        self.save_settings(recent=[str(p) for p in self.recent])
        self.refresh_queue()
        self.refresh_recent()
        self.query_one(FileTree).reload()

    # ── recent ───────────────────────────────────────────────────────────────

    def remember(self, *paths: Path) -> None:
        """Put PDFs at the top of the Recent tab."""
        pdfs = [p for p in paths if p.suffix.lower() == ".pdf"]
        self.recent = list(dict.fromkeys(pdfs + self.recent))[:RECENT_MAX]
        self.save_settings(recent=[str(p) for p in self.recent])
        self.refresh_recent()

    def forget(self, path: Path) -> None:
        self.recent.remove(path)
        self.save_settings(recent=[str(p) for p in self.recent])
        self.refresh_recent()

    def refresh_recent(self) -> None:
        options = self.query_one(RecentList)
        highlighted = options.highlighted
        options.clear_options()
        options.add_options(
            Option(f"{escape(p.name)}\n[$foreground-muted]{escape(str(p.parent))}[/]", id=str(p))
            for p in self.recent
        )
        if self.recent:
            options.highlighted = min(highlighted or 0, len(self.recent) - 1)

    # ── queue ────────────────────────────────────────────────────────────────

    def refresh_queue(self, highlight: int | None = None) -> None:
        options = self.query_one(QueueList)
        options.clear_options()
        options.add_options(
            Option(f"{i:>2}. {escape(p.name)}", id=str(p)) for i, p in enumerate(self.queue, 1)
        )
        options.border_title = f"Queue · {len(self.queue)}"
        options.border_subtitle = "shift+↑↓ move · del remove" if self.queue else "space to add"
        if self.queue:
            options.highlighted = min(highlight or 0, len(self.queue) - 1)
        self.query_one(FileTree).refresh()

    def toggle_queued(self, path: Path) -> None:
        if path in self.queue:
            self.queue.remove(path)
        else:
            self.queue.append(path)
        self.refresh_queue(len(self.queue) - 1)

    def move_queued(self, index: int | None, step: int) -> None:
        if index is None or not 0 <= index + step < len(self.queue):
            return
        q = self.queue
        q[index], q[index + step] = q[index + step], q[index]
        self.refresh_queue(index + step)

    def drop_queued(self, index: int | None) -> None:
        if index is not None:
            del self.queue[index]
            self.refresh_queue(index)

    def action_queue_folder(self) -> None:
        path = self.focused_path()
        if path is None:
            return
        folder = path if path.is_dir() else path.parent
        pdfs = sorted((p for p in folder.iterdir() if is_pdf(p)), key=lambda p: p.name.lower())
        self.queue += [p for p in pdfs if p not in self.queue]
        self.refresh_queue()

    def action_clear_queue(self) -> None:
        self.queue.clear()
        self.refresh_queue()

    # ── PDF jobs ─────────────────────────────────────────────────────────────

    def action_merge(self) -> None:
        if len(self.queue) < 2:
            self.notify("Queue at least 2 PDFs (space) to merge.", severity="warning")
            return
        queue = list(self.queue)
        self.push_screen(
            PromptDialog(f"Merge {len(queue)} PDFs into", str(queue[0].parent / "merged.pdf"), *output_checks(queue)),
            lambda value: value and self.merge(queue, Path(value).expanduser().resolve()),
        )

    @work(thread=True, group="jobs")
    def merge(self, queue: list[Path], output: Path) -> None:
        from pdf_merge import merge_pdfs
        log = lambda line: self.call_from_thread(self.say, line)
        try:
            pages = merge_pdfs(queue, output, log)
        except Exception as error:  # any pypdf/IO failure goes to the log, not a crash
            self.call_from_thread(self.say, f"✗ merge failed: {error}", "error")
            return
        self.call_from_thread(self.say, f"✓ {output} ({pages} pages)", "success")
        self.call_from_thread(self.notify, f"Merged → {output.name}")
        self.call_from_thread(self.remember, output, *queue)
        self.call_from_thread(self.query_one(FileTree).reload)

    def action_split(self) -> None:
        if not (targets := self.targets()):
            self.notify("Highlight or queue a PDF to split.", severity="warning")
            return
        self.split(targets)

    @work(thread=True, group="jobs")
    def split(self, targets: list[Path]) -> None:
        from pdf_split import split_pdf
        done = []
        for pdf in targets:
            try:
                pages, folder = split_pdf(pdf)
            except Exception as error:  # report and continue with the rest, like pdfSplit
                self.call_from_thread(self.say, f"✗ {pdf.name}: {error}", "error")
                continue
            done.append(pdf)
            self.call_from_thread(self.say, f"✓ {pdf.name} → {folder.name}/ ({pages} pages)", "success")
        self.call_from_thread(self.notify, f"Split {len(done)}/{len(targets)} PDFs",
                              severity="information" if len(done) == len(targets) else "error")
        self.call_from_thread(self.remember, *done)
        self.call_from_thread(self.query_one(FileTree).reload)

    def action_print(self) -> None:
        if not (targets := self.targets()):
            self.notify("Highlight or queue a PDF to print.", severity="warning")
            return
        if not shutil.which("gswin64c"):
            self.notify("Ghostscript (gswin64c) not found on PATH.", severity="error")
            return
        self.push_screen(PrinterDialog(), lambda printer: printer and self.print_files(targets, printer))

    def action_compress(self) -> None:
        if not (targets := self.targets()):
            self.notify("Highlight or queue a PDF to compress.", severity="warning")
            return
        if not (gs := shutil.which("gswin64c") or shutil.which("gs")):
            self.notify("Ghostscript (gswin64c) not found on PATH.", severity="error")
            return
        choices = [Option(label, id=choice) for choice, label in [
            ("ebook", "ebook          150 dpi, readable (recommended)"),
            ("ebook+cut", "ebook + cut    card only, white page cut away"),
            ("screen", "screen         smallest, small print unreadable"),
            ("screen+cut", "screen + cut   smallest card, text unreadable"),
            ("printer", "printer        300 dpi print quality"),
            ("printer+cut", "printer + cut  card at 300 dpi, can be larger"),
        ]]
        self.push_screen(
            ChoiceDialog(f"Compress {len(targets)} PDF{'s' * (len(targets) > 1)} → <name>_small.pdf", choices),
            lambda choice: choice and self.compress_files(gs, targets, choice),
        )

    @work(thread=True, group="jobs")
    def compress_files(self, gs: str, targets: list[Path], choice: str) -> None:
        from pdf_compress import compress
        quality, _, crop = choice.partition("+")
        done = []
        for pdf in targets:
            try:
                output = compress(gs, pdf, quality, bool(crop))
            except Exception as error:  # Ghostscript/IO failure: report and continue, like pdfCompress
                self.call_from_thread(self.say, f"✗ {pdf.name}: {error}", "error")
                continue
            before, after = pdf.stat().st_size, output.stat().st_size
            self.call_from_thread(self.say, f"✓ {pdf.name} → {output.name} ({human_size(before)} → "
                                  f"{human_size(after)}{', larger' if after > before else ''})",
                                  "success" if after <= before else "warning")
            done.append(output)
        self.call_from_thread(self.notify, f"Compressed {len(done)}/{len(targets)} PDFs",
                              severity="information" if len(done) == len(targets) else "error")
        self.call_from_thread(self.remember, *done)
        self.call_from_thread(self.query_one(FileTree).reload)

    @work(thread=True, group="jobs")
    def print_files(self, targets: list[Path], printer: str) -> None:
        done = []
        for pdf in targets:
            self.call_from_thread(self.say, f"Printing {pdf.name} → {printer}")
            if print_pdf(pdf, printer):
                done.append(pdf)
            else:
                self.call_from_thread(self.say, f"✗ failed: {pdf}", "error")
        self.call_from_thread(self.notify, f"Sent {len(done)}/{len(targets)} to {printer}",
                              severity="information" if len(done) == len(targets) else "error")
        self.call_from_thread(self.remember, *done)

    def action_compare(self) -> None:
        path = self.focused_path()
        if path and is_pdf(path):
            stem = path.stem
            original, small = (path.with_name(stem[:-6] + path.suffix), path) if stem.endswith("_small") else (path, path.with_name(f"{stem}_small.pdf"))
            if is_pdf(original) and is_pdf(small):
                self.push_screen(CompareScreen(original, small))
                return
        self.notify("Highlight a PDF that has a _small copy next to it (X.pdf and X_small.pdf).", severity="warning")

    def action_card(self) -> None:
        if not (targets := self.targets()):
            self.notify("Highlight a scan with front and back pages, or queue two scans.", severity="warning")
            return
        self.load_cards(targets)

    @work(thread=True, group="jobs")
    def load_cards(self, targets: list[Path]) -> None:
        from pdf_card import pair_cards, render_pages
        try:
            with PDFIUM_LOCK:
                pages = render_pages(targets)
        except (pdfium.PdfiumError, OSError) as error:
            self.call_from_thread(self.say, f"✗ card: {error}", "error")
            return
        if pages:
            self.call_from_thread(self.push_screen, CardScreen(targets, pair_cards(pages)))

    def saved_card(self, *outputs: Path) -> None:
        self.say(f"✓ {' + '.join(map(str, outputs))} (front + back)", "success")
        self.notify(f"Saved → {', '.join(o.name for o in outputs)}")
        self.remember(*outputs)
        self.query_one(FileTree).reload()


def main() -> None:
    parser = argparse.ArgumentParser(prog="powerpdf", description="Explore folders and merge, split, and print PDFs.")
    parser.add_argument("folder", nargs="?", default=".", help="folder to open (default: current folder)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args()
    root = Path(args.folder).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"not a folder: {root}")
    PowerPDF(root).run()


if __name__ == "__main__":
    main()
