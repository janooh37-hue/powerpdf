#!/usr/bin/env python3
"""PowerPDF: a pixel-vibe terminal UI to explore, preview, merge, split, print, compress, card, pages, redact, compare and save PDFs as images."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from rich.text import Text
from send2trash import send2trash

# textual_image probes the terminal (sixel / kitty graphics / half-blocks) on import,
# so it must be imported before the app starts.
from textual_image.renderable import Image as AutoRenderable, TGPImage as TGPRenderable
from textual_image.widget import HalfcellImage, Image as SharpImage
from textual import events, on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Center, Grid, Horizontal, Middle, Vertical
from textual.markup import escape
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import (
    DirectoryTree, Footer, Input, Label, OptionList, RichLog, Static, TabbedContent, TabPane,
)
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from pdf_runtime import resolve_tool, helper_env, host_env, helper_launch_scope, host_launch_scope
from powerpdf_core import files, render, settings as core_settings
from powerpdf_core.workspace import Workspace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from powerpdf_core.pages import PageEditSession
from powerpdf_core.files import compare_pair, is_pdf
from powerpdf_core.jobs import CancelToken, JobCancelled, JobSpec, run_job

VERSION = "0.7.0"
SETTINGS = core_settings.settings_path()
CELL_W, CELL_H = 24, 14  # one page cell of the Pages grid, in terminal cells


class WorkerCancel(CancelToken):
    """Bridge Textual's worker cancellation into toolkit-independent jobs."""

    def __init__(self, worker):
        super().__init__()
        self.worker = worker

    @property
    def is_cancelled(self) -> bool:
        return self.worker.is_cancelled or super().is_cancelled


def sharp_preview_works(env=os.environ, renderable=AutoRenderable) -> bool:
    """False under Herdr when textual_image picked Kitty graphics: Herdr answers the Kitty query itself but only draws
    images in a Kitty-capable outer terminal (not Windows Terminal), so the sharp preview would be blank."""
    return not (renderable is TGPRenderable and env.get("HERDR_ENV") == "1")


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
    with host_launch_scope():
        process = subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=host_env(),
        )
    with process:
        stdout, stderr = process.communicate()
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, process.args, stdout, stderr)
    return [line for line in stdout.splitlines() if line.strip()]


def print_pdf(path: Path, printer: str, timeout: float = 300) -> bool:
    """Print through Ghostscript's Windows printer device; True on success.

    Ghostscript waits forever on a printer that doesn't exist, hence the timeout."""
    executable = resolve_tool("ghostscript") or "gswin64c"
    with helper_launch_scope():
        process = subprocess.Popen(
            [executable, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dNoCancel",
             "-sDEVICE=mswinpr2", f"-sOutputFile=%printer%{printer}", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=helper_env(executable),
        )
    with process:
        try:
            process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return False
    return process.returncode == 0


def output_checks(inputs: list[Path]) -> tuple[Callable[[str], str | None], Callable[[str], str | None]]:
    """Adapt shared destination validation to the terminal prompt."""
    check = lambda value: files.output_error(Path(value).expanduser().resolve(), inputs)
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
                yield Static(f"[$foreground-muted]explore · preview · merge · split · print · compress · card · pages · redact · compare · images     v{VERSION}[/]")
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


class NameDialog(ModalScreen[str | None]):
    """A card's name for its files. Enter keeps what's typed, made file-safe (16/16402 → 16-16402); empty clears it.
    Tab reaches the picks: what OCR read off the card (replaces the number) and labels used before (go after the number)."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, value: str, read: str | None, labels: list[str]) -> None:
        super().__init__()
        self.title_text, self.value, self.read, self.labels = title, value, read, labels

    def compose(self) -> ComposeResult:
        picks = [Option(f"{escape(self.read)}  [$foreground-muted]read off the card: check it[/]", id="read")] if self.read else []
        picks += [Option(f"… {escape(label)}", id=f"label:{label}") for label in self.labels]
        with Vertical(classes="dialog") as dialog:
            dialog.border_title = self.title_text
            yield Input(self.value, placeholder="plate or number, then a word: 16-16402 Station")
            if picks:
                yield OptionList(*picks)
            tab = " · Tab picks" if picks else ""
            yield Label(f"[$foreground-muted]Enter save{tab} · empty = no name · Esc cancel[/]", id="hint")

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        from powerpdf_core.cards import card_file_name
        self.dismiss(card_file_name(event.value))

    @on(OptionList.OptionSelected)
    def pick(self, event: OptionList.OptionSelected) -> None:
        from powerpdf_core.cards import card_label
        field = self.query_one(Input)
        value = field.value.strip()
        if event.option.id == "read":
            field.value = f"{self.read} {card_label(value)}".strip()
        else:  # the label goes after the number, replacing the old one; a name without a number keeps all its words
            base = value.partition(" ")[0] if card_label(value) else value
            field.value = f"{base} {event.option.id.removeprefix('label:')}".strip()
        field.focus()
        field.cursor_position = len(field.value)


class CardScreen(ModalScreen):
    """Every card found in the scans, one at a time: front and back side by side, next to the scan page with each card outlined.
    Nudge a crop, black out private details, swap front and back, skip a card, name it, then save. One card: a 2-page PDF or two JPGs.
    Many: a folder with a PDF (or front and back JPGs) per card, or one PDF with them all. Named cards save under their names.
    Names are suggested in the background (OCR of plates and Emirates ID numbers) but only used once confirmed with n.
    Arrows, shift+arrows move/resize the selected black box, or the crop when none is selected; b adds a box (selected), space steps crop → box → … → crop."""

    BATCH = {"step", "skip", "save_one"}  # actions that need more than one card; hidden otherwise

    BINDINGS = [
        Binding("enter", "save", "Save PDF"),
        Binding("j", "save_jpg", "Save JPG"),
        Binding("o", "save_one", "One PDF"),
        Binding("pagedown", "step(1)", "Card", key_display="PgUp PgDn"),
        Binding("pageup", "step(-1)", "Card", show=False),
        Binding("tab", "switch", "Front/Back"),
        Binding("s", "swap", "Swap"),
        Binding("x", "skip", "Skip"),
        Binding("n", "name", "Name"),
        Binding("b", "add_box", "Black box"),
        Binding("space", "cycle", "Crop/Box"),
        Binding("backspace", "remove_box", "Remove box"),
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

    def __init__(self, session) -> None:
        super().__init__()
        self.sources = list(session.sources)
        self.session = session
        self.thumbs: dict[int, tuple] = {}
        self.saving = False
        self.map_timer = None

    cards = property(lambda self: self.session.cards)
    card_boxes = property(lambda self: self.session.card_boxes)
    skipped = property(lambda self: self.session.skipped)
    names = property(lambda self: self.session.names)
    suggested = property(lambda self: self.session.suggested)
    swapped = property(lambda self: self.session.swapped)
    index = property(lambda self: self.session.index, lambda self, value: setattr(self.session, "index", value))
    side = property(lambda self: self.session.side, lambda self, value: setattr(self.session, "side", value))
    active = property(lambda self: self.session.active, lambda self, value: setattr(self.session, "active", value))
    card = property(lambda self: self.session.card)
    pages = property(lambda self: self.session.pages)
    rects = property(lambda self: self.session.rects)
    boxes = property(lambda self: self.session.boxes)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        return len(self.cards) > 1 or action not in self.BATCH

    def compose(self) -> ComposeResult:
        image = HalfcellImage if self.app.pixel_mode else SharpImage
        yield Static(id="strip")
        with Horizontal():
            with Vertical(id="map", classes="panel map") as panel:
                panel.border_title = "Scan"
                yield image(id="scan", classes="scan")
            for i in range(2):
                with Vertical(id=f"side{i}", classes="panel card"):
                    yield image(id=f"cut{i}", classes="cut")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#side0").add_class("-active")
        self.show_card()
        self.read_names(self.session.ocr_cards())

    @work(thread=True, group="card-names")
    def read_names(self, cards) -> None:
        """Adapt immutable core OCR results to the terminal worker lifecycle."""
        from powerpdf_core import ocr
        worker = get_current_worker()
        try:
            for found in ocr.read_names(cards, cancel=WorkerCancel(worker)):
                if worker.is_cancelled:
                    return
                self.app.call_from_thread(self.suggest, found)
        except JobCancelled:
            return

    def suggest(self, found) -> None:
        if self.is_attached and self.session.apply_suggestion(found):
            self.show_strip()

    def show_card(self) -> None:
        for i in range(2):
            self.show(i)
        self.show_marks()

    def show_marks(self) -> None:
        """Skipped or kept on the panels, the map and the strip; the cuts themselves don't change."""
        skipped = self.index in self.skipped
        for i in range(2):
            panel = self.query_one(f"#side{i}")
            panel.set_class(skipped, "-skipped")
            panel.border_title = ("Front", "Back")[i] + (" · skipped (x keeps it)" if skipped else "")
        self.show_map()
        self.show_strip()

    def show(self, i: int) -> None:
        from PIL import ImageDraw
        from powerpdf_core.cards import DPI, cut
        (_, _), (w, h), angle = self.rects[i]
        image = cut(self.pages[i], self.rects[i])
        draw = ImageDraw.Draw(image)
        for n, (cx, cy, bw, bh) in enumerate(self.boxes[i]):  # the selected box gets a red outline (preview only)
            selected = i == self.side and n == self.active
            draw.rectangle((cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2), fill=(0, 0, 0), outline=(255, 0, 0) if selected else None, width=3)
        self.query_one(f"#cut{i}").image = image
        mm = lambda px: px / DPI * 25.4
        box = f" · box {self.active + 1}/{len(self.boxes[i])}" if i == self.side and self.active is not None else ""
        guessed = " · ⚠ not found: move it onto the card" if self.card.guessed[i] else ""
        self.query_one(f"#side{i}").border_subtitle = f"{mm(w):.1f} × {mm(h):.1f} mm · angle {angle:+.1f}°{box}{guessed}"

    def show_map(self) -> None:
        """The scan page of the current side, every card on it numbered (F front, B back); the current card filled, the side
        being edited in primary (like its panel's border), the other in accent; skipped ones darkened. Fills, not just
        outlines: they survive pixel mode's downscale."""
        import cv2
        from PIL import Image, ImageDraw, ImageFont
        from textual.color import Color
        page = self.pages[self.side]
        if id(page) not in self.thumbs:
            height, width = page.shape[:2]
            scale = 1000 / max(height, width)
            size = (round(width * scale), round(height * scale))
            self.thumbs[id(page)] = Image.fromarray(page).resize(size, Image.Resampling.BILINEAR, reducing_gap=2.0), scale
        thumb, scale = self.thumbs[id(page)]
        css = self.app.get_css_variables()
        tone = lambda name, alpha: (*Color.parse(css[name]).rgb, alpha)
        overlay = Image.new("RGBA", thumb.size)
        draw = ImageDraw.Draw(overlay)
        font = ImageFont.load_default(size=40)
        for k, card in enumerate(self.cards):
            for s, (card_page, rect) in enumerate(zip(card.pages, card.rects)):
                if card_page is not page:
                    continue
                current, color = k == self.index, "primary" if s == self.side else "accent"
                fill = (0, 0, 0, 160) if k in self.skipped else tone(color, 110) if current else None
                outline = tone(color, 255) if current else tone("foreground-muted", 220)
                draw.polygon([tuple(point) for point in cv2.boxPoints(rect) * scale], fill=fill, outline=outline, width=8 if current else 3)
                (cx, cy), _, _ = rect
                draw.text((cx * scale, cy * scale), f"{k + 1}{'FB'[s]}", font=font, anchor="mm",
                          fill=(255, 255, 255, 255), stroke_width=4, stroke_fill=(0, 0, 0, 255))
        self.query_one("#scan").image = Image.alpha_composite(thumb.convert("RGBA"), overlay).convert("RGB")
        self.query_one("#map").border_subtitle = self.card.where

    def show_strip(self) -> None:
        """Which card and its name, a chip per card (current, skipped, ✓ named, ? a name to confirm, ⚠ a side not found), and what Enter saves."""
        count, kept = len(self.cards), len(self.cards) - len(self.skipped)
        if count == 1:
            text = f"[b]{escape(self.card.where)}[/] · {self.name_note()}"
        else:
            first = max(0, min(self.index - 12, count - 25))  # a window of 25 chips around the current card
            last = min(count, first + 25)
            chips = []
            for k in range(first, last):
                warn, named = any(self.cards[k].guessed), self.names[k] is not None
                style = ("b reverse $primary" if k == self.index else "strike $foreground-muted" if k in self.skipped
                         else "$warning" if warn else "$success" if named else "$foreground")
                mark = "⚠" if warn else "✓" if named else "?" if self.suggested[k] else ""
                chips.append(f"[{style}] {k + 1}{mark} [/]")
            more = "[$foreground-muted]…[/]"
            text = (f"[b]Card {self.index + 1} of {count}[/] [$foreground-muted]· {escape(self.card.where)} ·[/] {self.name_note()}  "
                    f"{more if first else ''}{''.join(chips)}{more if last < count else ''}")
        notes = []
        if unsure := sum(any(card.guessed) for card in self.cards):
            notes.append(f"[$warning]⚠ {unsure} to check[/]")
        if self.skipped:
            notes.append(f"[$error]{len(self.skipped)} skipped[/]")
        if count > 1 and (named := sum(name is not None for name in self.names)):
            notes.append(f"[$success]{named} named[/]")
        notes.append(f"[$success]Enter saves {kept} cards[/]" if count > 1 else "[$success]Enter saves front + back[/]")
        self.query_one("#strip", Static).update(f"{text}   {' · '.join(notes)}")

    def name_note(self) -> str:
        """The current card's name: confirmed, read by OCR (and the side it was read on), or how to give one."""
        k = self.index
        if name := self.names[k]:
            return f"[b $success]{escape(name)}[/]"
        if found := self.suggested[k]:
            name, side = found
            swapped = " [$warning]read on the Back: s swaps?[/]" if side else ""
            return f"[$warning]{escape(name)}?[/] [$foreground-muted]n confirms[/]{swapped}"
        return "[$foreground-muted]n names it[/]"

    def action_step(self, step: int) -> None:
        previous = self.index
        self.session.step(step)
        if self.index != previous:
            self.show_card()

    def action_swap(self) -> None:
        self.session.swap()
        self.show_card()

    def action_name(self) -> None:
        k, found = self.index, self.suggested[self.index]
        value = self.names[k] or (found[0] if found else "")
        labels = self.app.settings.get("card_labels", [])
        self.app.push_screen(NameDialog(f"Name card {k + 1}", value, found and found[0], labels), lambda name: self.named(k, name))

    def named(self, k: int, name: str | None) -> None:
        """The dialog's answer for card k: a name, "" to clear it, None if cancelled. A label after the number is
        remembered (newest first, 12 kept) and offered for the next cards."""
        from powerpdf_core.cards import card_label
        if name is None:
            return
        self.session.name(k, name)
        if label := card_label(name):
            self.app.workspace.remember_label(label)
            self.app.save_settings(card_labels=self.app.workspace.card_labels)
        self.show_strip()

    def action_skip(self) -> None:
        self.session.skip()
        self.show_marks()

    def action_switch(self, side: int | None = None) -> None:
        previous, selected = self.side, self.active is not None
        self.session.switch(side)
        if selected:
            self.show(previous)
        for i in range(2):
            self.query_one(f"#side{i}").set_class(i == self.side, "-active")
        self.show_map()  # the edited side's color moves, and the sides can be on different pages

    def action_add_box(self) -> None:
        self.session.add_box()
        self.show(self.side)

    def action_cycle(self) -> None:
        self.session.cycle()
        self.show(self.side)

    def action_remove_box(self) -> None:
        if self.active is not None:
            self.session.remove_box()
            self.show(self.side)

    def action_change(self, dx: float, dy: float, dw: float = 0, dh: float = 0, da: float = 0) -> None:
        moving_box = self.active is not None and not da
        self.session.change(dx, dy, dw, dh, da)
        self.show(self.side)
        if moving_box:
            return
        self.show_strip()
        if self.map_timer:  # redraw the map once the arrows stop: a held arrow then redraws only the card
            self.map_timer.stop()
        self.map_timer = self.set_timer(0.25, self.show_map)

    def kept(self) -> list[int]:
        return self.session.kept()

    def ready(self) -> bool:
        if self.saving:
            return False
        if not self.kept():
            self.notify("Every card is skipped: x keeps one.", severity="warning")
        return bool(self.kept())

    def action_save(self) -> None:
        if len(self.cards) > 1:
            self.ask_folder("pdf")
            return
        default = self.sources[0].with_name(f"{self.names[0] or self.sources[0].stem + '_card'}.pdf")
        self.app.push_screen(
            PromptDialog("Save front + back as", str(default), *output_checks(self.sources)),
            lambda value: value and self.save(Path(value).expanduser().resolve()),
        )

    def action_save_jpg(self) -> None:
        if len(self.cards) > 1:
            self.ask_folder("jpg")
            return
        outputs = [self.sources[0].with_name(f"{self.names[0] or self.sources[0].stem}_{side}.jpg") for side in ("front", "back")]
        if existing := [o.name for o in outputs if o.exists()]:
            self.app.push_screen(
                ConfirmDialog(f"Overwrite [b]{escape(' and '.join(existing))}[/]?"),
                lambda ok: ok and self.save(*outputs),
            )
        else:
            self.save(*outputs)

    def save(self, *outputs: Path) -> None:
        """The one card. One output: a 2-page PDF. Two: front and back JPGs."""
        try:
            self.session.snapshot().export([list(outputs)])
        except Exception as error:
            self.notify(f"Can't save: {error}", severity="error")
            return
        self.app.pop_screen()
        self.app.saved_card(*outputs)

    def file_stems(self) -> dict[int, str]:
        """Globally unique output stems from confirmed names."""
        return self.session.file_stems()

    def batch_paths(self, folder: Path, kind: str) -> list[list[Path]]:
        return self.session.batch_paths(folder, kind)

    def ask_folder(self, kind: str) -> None:
        if not self.ready():
            return
        folder = lambda value: Path(value).expanduser().resolve()
        check = lambda value: "That's a file, not a folder" if folder(value).is_file() else None

        def confirm(value: str) -> str | None:
            existing = sum(path.exists() for paths in self.batch_paths(folder(value), kind) for path in paths)
            return f"{existing} of the files exist. Enter again to overwrite" if existing else None

        what = "a PDF" if kind == "pdf" else "front + back JPGs"
        default = self.sources[0].with_name(f"{self.sources[0].stem}_cards")
        self.app.push_screen(
            PromptDialog(f"Save {len(self.kept())} cards, {what} each, in folder", str(default), check, confirm),
            lambda value: value and self.start_save(self.batch_paths(folder(value), kind), folder(value)),
        )

    def action_save_one(self) -> None:
        if not self.ready():
            return
        default = self.sources[0].with_name(f"{self.sources[0].stem}_cards.pdf")
        self.app.push_screen(
            PromptDialog(f"Save {len(self.kept())} cards in one PDF (front, back, front, …)", str(default), *output_checks(self.sources)),
            lambda value: value and self.start_save([[Path(value).expanduser().resolve()]], None, list(self.file_stems().values())),
        )

    def start_save(self, paths: list[list[Path]], folder: Path | None, titles: list[str] | None = None) -> None:
        """Saves in a thread so the screen stays live; it works from a copy of the kept cards, so edits meanwhile don't leak in.
        folder: one output set per kept card in it (save_batch); None: all kept cards in paths[0][0], one PDF, a bookmark per card (titles)."""
        self.saving = True
        snapshot = self.session.snapshot()
        self.notify(f"Saving {len(snapshot.cards)} cards…", timeout=2)
        self.write(snapshot, paths, folder, titles)

    @work(thread=True, group="cards")
    def write(self, snapshot, paths: list[list[Path]], folder: Path | None, titles: list[str] | None) -> None:
        try:
            outputs = snapshot.export(paths, folder=folder, titles=titles, cancel=WorkerCancel(get_current_worker()))
        except Exception as error:
            self.app.call_from_thread(self.save_failed, error)
            return
        self.app.call_from_thread(self.saved, outputs, len(snapshot.cards))

    def save_failed(self, error: Exception) -> None:
        self.saving = False
        self.notify(f"Can't save: {error}", severity="error")

    def saved(self, outputs: list[Path], count: int) -> None:
        self.saving = False
        if self.app.screen is self:  # not if Esc closed it while saving
            self.app.pop_screen()
        self.app.saved_cards(outputs, count)


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
            for path in self.paths:
                with render.RenderSession(path) as session:
                    shots.append((session.render(page), session.info.page_count))
        except (render.RenderError, OSError, ValueError) as error:
            self.app.call_from_thread(self.notify, f"Can't compare: {error}", severity="error")
            return
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self.show, page, shots)

    def show(self, page: int, shots: list) -> None:
        self.page, self.count = page, min(count for _, count in shots)
        for i, (image, count) in enumerate(shots):
            self.query_one(f"#shot{i}").image = image
            self.query_one(f"#side{i}").border_subtitle = f"{human_size(self.sizes[i])} · page {page + 1}/{count}"


class PagesScreen(ModalScreen):
    """Thumbnails of every page: pick, rotate, delete and reorder them, then save the result or extract the picks.
    Every key acts on the picked pages, or on the cursor page when none are picked."""

    BINDINGS = [
        Binding("left", "cursor(-1)", "Move", key_display="←↑↓→"),
        Binding("right", "cursor(1)", "Move", show=False),
        Binding("up", "cursor(-1, True)", "Move", show=False),
        Binding("down", "cursor(1, True)", "Move", show=False),
        Binding("space", "pick", "Select"),
        Binding("r", "rotate", "Rotate 90°"),
        Binding("delete", "drop", "Delete"),
        Binding("shift+left", "reorder(-1)", "Reorder", key_display="shift+←→"),
        Binding("shift+right", "reorder(1)", "Reorder", show=False),
        Binding("x", "extract", "Extract"),
        Binding("enter", "save", "Save"),
        Binding("escape", "close", "Close"),
    ]

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.session: PageEditSession | None = None
        self.cells: dict[int, Vertical] = {}
        self.thumbs: dict[int, object] = {}  # original page -> unrotated PIL image
        self.cols = 1

    order = property(lambda self: self.session.order if self.session else [])
    turns = property(lambda self: self.session.turns if self.session else {})
    picked = property(lambda self: self.session.picked if self.session else set())
    cursor = property(lambda self: self.session.cursor if self.session else 0,
                      lambda self, value: setattr(self.session, "cursor", value))
    edited = property(lambda self: self.session.edited if self.session else False)

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield Grid(id="grid")
        yield Footer()

    def on_mount(self) -> None:
        self.load()

    def on_resize(self) -> None:
        self.cols = max(1, self.size.width // CELL_W)
        self.query_one("#grid").styles.grid_size_columns = self.cols

    @work(thread=True)
    def load(self) -> None:
        """Render thumbnails from the same immutable snapshot as the page editor."""
        from powerpdf_core.pages import PageEditSession
        worker, app = get_current_worker(), self.app
        try:
            session = PageEditSession(self.path)
            with render.RenderSession(session.source_bytes) as renderer:
                app.call_from_thread(self.build, session)
                for page, (width, height) in enumerate(renderer.info.page_sizes):
                    if worker.is_cancelled:
                        return
                    scale = min(1.5, 320 / max(width, height))
                    image = renderer.render(page, scale=scale, purpose="thumbnail")
                    app.call_from_thread(self.show_thumb, page, image)
        except (render.RenderError, OSError, ValueError) as error:
            app.call_from_thread(self.notify, f"Can't show pages: {error}", severity="error")
            if not self.cells:
                app.call_from_thread(self.dismiss)

    def build(self, session: PageEditSession) -> None:
        self.session = session
        for page in self.order:  # halfcell: sixel images don't survive scrolling and clipping in a grid
            self.cells[page] = Vertical(
                Center(HalfcellImage(classes="thumb"), classes="frame"), Label(self.caption(page), classes="num"),
                classes="cell -cursor" if page == 0 else "cell",
            )
        self.query_one("#grid").mount(*self.cells.values())
        self.status()

    def caption(self, page: int) -> str:
        turns = self.turns.get(page, 0)
        return f"{'✓ ' if page in self.picked else ''}{page + 1}{f' ↻{90 * turns}°' if turns else ''}"

    def show_thumb(self, page: int, image=None) -> None:
        """Show a page's thumbnail, turned by its rotation (image is new from the worker, else the stored one)."""
        if image:
            self.thumbs[page] = image
        if page in self.cells and page in self.thumbs:
            self.cells[page].query_one(".thumb").image = self.thumbs[page].rotate(-90 * self.turns.get(page, 0), expand=True)

    def paint(self, *pages: int) -> None:
        for page in pages:
            cell = self.cells[page]
            cell.query_one(".num", Label).update(self.caption(page))
            cell.set_class(page in self.picked, "-picked")
            cell.set_class(page == self.order[self.cursor], "-cursor")

    def status(self) -> None:
        self.query_one("#status", Static).update(
            f"[b $accent]{escape(self.path.name)}[/]  [$foreground-muted]{len(self.order)} pages · "
            f"{len(self.picked)} selected{' · [$warning]unsaved[/]' if self.edited else ''}[/]"
        )

    def targets(self) -> list[int]:
        return self.session.targets() if self.session else []

    def action_cursor(self, step: int, rows: bool = False) -> None:
        if not self.order:
            return
        old = self.order[self.cursor]
        self.cursor = max(0, min(len(self.order) - 1, self.cursor + step * (self.cols if rows else 1)))
        self.paint(old, self.order[self.cursor])
        self.cells[self.order[self.cursor]].scroll_visible()

    def action_pick(self) -> None:
        if self.order:
            page = self.order[self.cursor]
            self.session.pick()
            self.paint(page)
            self.status()

    def action_rotate(self) -> None:
        targets = self.targets()
        self.session.rotate()
        for page in targets:
            self.show_thumb(page)
            self.paint(page)
        self.status()

    def action_drop(self) -> None:
        if not (gone := self.targets()):
            return
        self.session.delete()
        for page in gone:
            self.cells.pop(page).remove()
        if self.order:
            self.paint(self.order[self.cursor])
        self.status()

    def action_reorder(self, step: int) -> None:
        self.session.reorder(step)
        grid = self.query_one("#grid")
        for index in range(1, len(self.order)):
            grid.move_child(self.cells[self.order[index]], after=self.cells[self.order[index - 1]])
        if self.order:
            self.call_after_refresh(self.cells[self.order[self.cursor]].scroll_visible)
        self.status()

    def action_save(self) -> None:
        if not self.order:
            self.notify("No pages left to save.", severity="warning")
            return
        default = self.path.with_name(f"{self.path.stem}_edited.pdf")
        self.app.push_screen(
            PromptDialog("Save pages as", str(default), *output_checks([self.path])),
            lambda value: value and self.write(Path(value).expanduser().resolve(), list(self.order), close=True),
        )

    def action_extract(self) -> None:
        if not (pages := self.targets()):
            self.notify("No pages left to extract.", severity="warning")
            return
        default = self.path.with_name(f"{self.path.stem}_extract.pdf")
        self.app.push_screen(
            PromptDialog(f"Extract {len(pages)} page{'s' * (len(pages) > 1)} to", str(default), *output_checks([self.path])),
            lambda value: value and self.write(Path(value).expanduser().resolve(), pages),
        )

    def write(self, output: Path, pages: list[int], close: bool = False) -> None:
        """Write original pages (numbers from 0) in the given order, each turned by its rotation on top of its own."""
        try:
            self.session.write(output, pages)
        except Exception as error:  # any pypdf/IO failure is shown, not a crash
            self.notify(f"Can't save: {error}", severity="error")
            return
        self.app.saved_pages(output, len(pages))
        if close:
            self.dismiss()

    def action_close(self) -> None:
        if self.edited:
            self.app.push_screen(
                ConfirmDialog(f"Discard your changes to [b]{escape(self.path.name)}[/]?"),
                lambda ok: ok and self.dismiss(),
            )
        else:
            self.dismiss()


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

    PromptDialog, ConfirmDialog, PrinterDialog, ChoiceDialog, NameDialog { align: center middle; background: $background 60%; }
    CardScreen { background: $background; }
    CompareScreen { background: $background; }
    CompareScreen #saved { height: 1; padding: 0 1; background: $panel; }
    CompareScreen .compare { width: 1fr; align: center middle; }
    CompareScreen .shot { width: auto; height: 1fr; }
    PagesScreen { background: $background; }
    PagesScreen #status { height: 1; padding: 0 1; background: $panel; }
    PagesScreen #grid { grid-rows: 14; height: 1fr; overflow-y: auto; }
    PagesScreen .cell { height: 14; border: round $panel-lighten-2; background: $surface; }
    PagesScreen .cell.-picked { border: round $accent; background: $accent 25%; }
    PagesScreen .cell.-cursor { border: heavy $primary; }
    PagesScreen .frame { height: 1fr; }
    PagesScreen .thumb { width: auto; height: 1fr; }
    PagesScreen .num { width: 100%; text-align: center; }
    CardScreen #strip { height: 1; padding: 0 1; background: $panel; }
    CardScreen .map { width: 24%; align: center middle; }
    CardScreen .scan { width: 1fr; height: auto; }
    CardScreen .card { width: 1fr; align: center middle; }
    CardScreen .card.-active { border: round $primary; border-title-color: $primary; border-title-style: bold; }
    CardScreen .card.-skipped { border: round $error; border-title-color: $error; }
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
        Binding("i", "image", "Image"),
        Binding("d", "compare", "Compare"),
        Binding("t", "pages", "Pages"),
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
        self.current: Path | None = None
        self.page = 0
        self.page_count = 0
        self.doc: render.RenderSession | None = None
        self.preview_lock = threading.RLock()
        self.pixel_mode = not sharp_preview_works()
        self.settings_store = core_settings.SettingsStore() if SETTINGS == core_settings.settings_path() else core_settings.SettingsStore(path=SETTINGS)
        self.settings = self.settings_store.load()
        self.workspace = Workspace(recent=self.settings.get("recent", []),
                                   card_labels=self.settings.get("card_labels", []))

    queue = property(lambda self: self.workspace.queue,
                     lambda self, value: setattr(self.workspace, "queue", value))
    recent = property(lambda self: self.workspace.recent)

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
                yield (HalfcellImage if self.pixel_mode else SharpImage)(id="page")
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
        self.settings = self.settings_store.update(changes)

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
            ("Pages", "Thumbnail grid of the highlighted PDF: rotate, delete, reorder, extract pages", self.action_pages),
            ("Compress", "Small, readable copies (<name>_small.pdf) to upload or send", self.action_compress),
            ("Save as images", "Every page of queued (or highlighted) PDFs as JPG or PNG", self.action_image),
            ("Queue folder", "Queue every PDF in the highlighted folder", self.action_queue_folder),
            ("Clear queue", "Empty the queue", self.action_clear_queue),
            ("Toggle pixel mode", "Switch preview between sharp and half-block pixels", self.action_toggle_pixels),
        ]:
            yield SystemCommand(title, help_text, action)

    def say(self, line: str, tone: str = "foreground") -> None:
        color = self.get_css_variables()[tone]
        self.screen_stack[0].query_one("#activity", RichLog).write(Text(line, style=color))  # the main screen, whatever modal is on top

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
        return self.workspace.targets(self.focused_path())

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
        with self.preview_lock:
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

    def close_doc(self) -> None:
        """Release the preview session; callers serialize session selection."""
        if self.doc:
            self.doc.close()
            self.doc = None

    @work(thread=True, exclusive=True, group="preview")
    def load_preview(self, path: Path, page: int) -> None:
        worker = get_current_worker()
        try:
            with self.preview_lock:
                stat = path.stat()
                if self.doc is None or self.doc.source_path != path.resolve() or not self.doc.source_unchanged():
                    self.close_doc()
                    self.doc = render.RenderSession(path)
                count = self.doc.info.page_count
                image = self.doc.render(page)
                meta = self.doc.info.metadata
        except (render.RenderError, OSError, ValueError) as error:
            self.call_from_thread(self.show_info, path, f"[$error]Can't open: {escape(str(error))}[/]")
            return
        if worker.is_cancelled:
            return
        self.call_from_thread(self.show_preview, path, page, count, image, meta, stat)
        if page + 1 < count and not worker.is_cancelled:  # warm the next flip
            try:
                with self.preview_lock:
                    if self.doc and self.current == path:
                        self.doc.render(page + 1)
            except (render.RenderError, OSError, ValueError):
                pass

    def on_unmount(self) -> None:
        with self.preview_lock:
            self.close_doc()

    def show_preview(self, path: Path, page: int, count: int, image, meta: dict, stat: os.stat_result) -> None:
        """stat comes from the worker: a second stat here raised if the file was deleted while its preview loaded."""
        if path != self.current:
            return
        self.page_count = count
        self.query_one("#empty").display = False
        page_widget = self.query_one("#page")
        page_widget.display = True
        page_widget.image = image
        self.query_one("#preview").border_subtitle = self.preview_subtitle(page, count)
        rows = [("pages", count), ("size", human_size(stat.st_size)),
                ("modified", datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"))]
        rows += [(key.lower(), meta[key]) for key in ("Title", "Author", "Producer") if key in meta]
        self.show_info(path, details(rows))

    def preview_subtitle(self, page: int, count: int) -> str:
        return f"page {page + 1}/{count} · [ ] flip" + (" · pixels (v)" if self.pixel_mode else "")

    def show_info(self, path: Path, body: str) -> None:
        self.query_one("#info", Static).update(f"[b $accent]{escape(path.name)}[/]\n\n{body}")

    async def action_toggle_pixels(self) -> None:
        self.pixel_mode = not self.pixel_mode
        old = self.query_one("#page")
        new = (HalfcellImage if self.pixel_mode else SharpImage)(old.image, id="page")
        new.display = old.display
        await old.remove()
        await self.query_one("#preview").mount(new)
        preview = self.query_one("#preview")
        if preview.border_subtitle:  # a page is shown
            preview.border_subtitle = self.preview_subtitle(self.page, self.page_count)
        if self.pixel_mode:
            self.notify("Pixel mode on", timeout=2)
        elif not sharp_preview_works():
            self.notify("Sharp mode on: Herdr can't draw it in Windows Terminal, press v for pixels", timeout=4)
        else:
            self.notify("Sharp mode on", timeout=2)

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
        added = self.workspace.add(pdfs)
        if added:
            self.refresh_queue(len(self.queue) - 1)
            self.notify(f"Queued {added} PDF{'s' * (added > 1)}", timeout=2)
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
            with host_launch_scope():
                os.startfile(path)  # Windows: the file's default app
        except OSError as error:
            self.notify(f"Can't open {path.name}: {error}", severity="error")
            return
        self.say(f"Opened {path.name}")
        self.remember(path)

    def action_reveal(self) -> None:
        if path := self.focused_path():
            with host_launch_scope():
                subprocess.Popen(["explorer", "/select,", str(path)], env=host_env())

    def action_rename(self) -> None:
        path = self.focused_path()
        if path is None or path == self.root:
            return

        check = lambda name: files.rename_error(path, name)

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
        self.workspace.moved(old, new)
        if self.current and self.current.is_relative_to(old):
            self.current = None
            with self.preview_lock:
                self.close_doc()
        self.save_settings(recent=[str(p) for p in self.recent])
        self.refresh_queue()
        self.refresh_recent()
        self.query_one(FileTree).reload()

    # ── recent ───────────────────────────────────────────────────────────────

    def remember(self, *paths: Path) -> None:
        self.workspace.remember(*paths)
        self.save_settings(recent=[str(p) for p in self.recent])
        self.refresh_recent()

    def forget(self, path: Path) -> None:
        self.workspace.forget(path)
        self.save_settings(recent=[str(p) for p in self.recent])
        self.refresh_recent()

    def refresh_recent(self) -> None:
        options = self.screen_stack[0].query_one(RecentList)
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
        self.workspace.toggle(path)
        self.refresh_queue(len(self.queue) - 1)

    def move_queued(self, index: int | None, step: int) -> None:
        moved = self.workspace.move(index, step)
        if moved is not None:
            self.refresh_queue(moved)

    def drop_queued(self, index: int | None) -> None:
        if index is not None:
            self.workspace.drop(index)
            self.refresh_queue(index)

    def action_queue_folder(self) -> None:
        path = self.focused_path()
        if path is None:
            return
        folder = path if path.is_dir() else path.parent
        self.workspace.add_folder(folder)
        self.refresh_queue()

    def action_clear_queue(self) -> None:
        self.workspace.clear()
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
        self.execute_job(JobSpec("merge", tuple(queue), output), "Merged")

    def action_split(self) -> None:
        if not (targets := self.targets()):
            self.notify("Highlight or queue a PDF to split.", severity="warning")
            return
        self.split(targets)

    @work(thread=True, group="jobs")
    def split(self, targets: list[Path]) -> None:
        self.execute_job(JobSpec("split", tuple(targets)), "Split")

    def action_print(self) -> None:
        if not (targets := self.targets()):
            self.notify("Highlight or queue a PDF to print.", severity="warning")
            return
        if not resolve_tool("ghostscript"):
            self.notify("Ghostscript not found. Install the bundled Windows release.", severity="error")
            return
        self.push_screen(PrinterDialog(), lambda printer: printer and self.print_files(targets, printer))

    def action_compress(self) -> None:
        if not (targets := self.targets()):
            self.notify("Highlight or queue a PDF to compress.", severity="warning")
            return
        if not (gs := resolve_tool("ghostscript")):
            self.notify("Ghostscript not found. Install the bundled Windows release.", severity="error")
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
        quality, _, crop = choice.partition("+")
        self.execute_job(JobSpec("compress", tuple(targets), options={"gs": gs, "quality": quality, "crop": bool(crop)}), "Compressed")

    def action_image(self) -> None:
        if not (targets := self.targets()):
            self.notify("Highlight or queue a PDF to save as images.", severity="warning")
            return
        choices = [Option(label, id=choice) for choice, label in [
            ("jpg-150", "JPG 150 dpi   small, for uploads (recommended)"),
            ("png-150", "PNG 150 dpi   lossless text; scans ~5x larger"),
            ("jpg-300", "JPG 300 dpi   print quality"),
            ("png-300", "PNG 300 dpi   lossless print quality, largest"),
        ]]
        self.push_screen(
            ChoiceDialog(f"Save {len(targets)} PDF{'s' * (len(targets) > 1)} as images (one per page)", choices),
            lambda choice: choice and self.image_files(targets, *choice.split("-")),
        )

    @work(thread=True, group="jobs")
    def image_files(self, targets: list[Path], fmt: str, dpi: str) -> None:
        self.execute_job(JobSpec("images", tuple(targets), options={"fmt": fmt, "dpi": int(dpi)}), "Saved images of")

    def execute_job(self, spec: JobSpec, label: str) -> None:
        """UI presentation only: iteration, failures and cancellation live in core."""
        def report(item):
            if item.error:
                partial = f" ({len(item.outputs)} outputs committed)" if item.outputs else ""
                self.call_from_thread(self.say, f"✗ {item.inputs[0].name}: {item.error}{partial}", "error")
            elif item.success:
                where = item.outputs[0].name if len(item.outputs) == 1 else f"{item.outputs[0].parent.name}/" if item.outputs else ""
                note = f" ({item.pages} pages)" if item.pages is not None else ""
                sizes = f" · {human_size(item.input_bytes)} → {human_size(item.output_bytes)}" if spec.action == "compress" else ""
                self.call_from_thread(self.say, f"✓ {item.inputs[0].name} → {where}{note}{sizes}", "success")

        result = run_job(spec, cancel=WorkerCancel(get_current_worker()), on_outcome=report)
        done = [item for item in result.outcomes if item.success]
        total = 1 if spec.action == "merge" else len(spec.inputs)
        suffix = " · canceled" if result.cancelled else ""
        self.call_from_thread(self.notify, f"{label} {len(done)}/{total} PDFs{suffix}",
                              severity="information" if result.success else "error")
        remembered = [path for item in done for path in (item.outputs if spec.action in {"merge", "compress"} else item.inputs)]
        self.call_from_thread(self.remember, *remembered)
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
        if path and (pair := compare_pair(path)):
            self.push_screen(CompareScreen(*pair))
            return
        self.notify("Highlight a PDF that has a _small copy next to it (X.pdf and X_small.pdf).", severity="warning")

    def action_card(self) -> None:
        if not (targets := self.targets()):
            self.notify("Highlight a scan with cards on it, or queue scans (e.g. fronts, then backs).", severity="warning")
            return
        self.notify("Finding cards…", timeout=2)
        self.load_cards(targets)

    @work(thread=True, group="jobs")
    def load_cards(self, targets: list[Path]) -> None:
        from powerpdf_core.cards import MAX_PAGES, load_cards
        try:
            session, total = load_cards(targets, labels=self.workspace.card_labels,
                                        cancel=WorkerCancel(get_current_worker()))
        except (render.RenderError, OSError, ValueError, JobCancelled) as error:
            self.call_from_thread(self.say, f"✗ card: {error}", "error")
            return
        cards = session.cards
        self.call_from_thread(self.push_screen, CardScreen(session))
        unsure = sum(any(card.guessed) for card in cards)
        if len(cards) > 1 or unsure or total > MAX_PAGES:
            found = f"Found {len(cards)} card{'s' if len(cards) > 1 else ''}"
            found += f" on the first {MAX_PAGES} of {total} pages" if total > MAX_PAGES else ""
            found += f" · ⚠ {unsure} with a side not found: check it" if unsure else ""
            self.call_from_thread(self.notify, found, severity="warning" if unsure or total > MAX_PAGES else "information")

    def action_pages(self) -> None:
        path = self.focused_path()
        if path and is_pdf(path):
            self.push_screen(PagesScreen(path))
        else:
            self.notify("Highlight a PDF to see its pages.", severity="warning")

    def saved_card(self, *outputs: Path) -> None:
        self.say(f"✓ {' + '.join(map(str, outputs))} (front + back)", "success")
        self.notify(f"Saved → {', '.join(o.name for o in outputs)}")
        self.remember(*outputs)
        self.query_one(FileTree).reload()

    def saved_cards(self, outputs: list[Path], count: int) -> None:
        """A CardScreen saved count cards: one PDF with them all, or a folder of files."""
        where = outputs[0] if len(outputs) == 1 else outputs[0].parent
        size = human_size(sum(path.stat().st_size for path in outputs))
        self.say(f"✓ {count} cards → {where} ({len(outputs)} file{'s' if len(outputs) > 1 else ''}, {size})", "success")
        self.notify(f"Saved {count} cards → {where.name}")
        if len(outputs) == 1:
            self.remember(*outputs)
        self.screen_stack[0].query_one(FileTree).reload()

    def saved_pages(self, output: Path, count: int) -> None:
        """A PagesScreen wrote output. It may still be open on top, so reach the main screen explicitly."""
        self.say(f"✓ {output} ({count} pages)", "success")
        self.notify(f"Saved → {output.name}")
        self.remember(output)
        self.screen_stack[0].query_one(FileTree).reload()


def main() -> None:
    parser = argparse.ArgumentParser(prog="powerpdf", description="Explore, preview, merge, split, print, compress, card, pages, redact and compare PDFs, and save them as images.")
    parser.add_argument("folder", nargs="?", default=".", help="folder to open (default: current folder)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args()
    root = Path(args.folder).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"not a folder: {root}")
    PowerPDF(root).run()


if __name__ == "__main__":
    main()
