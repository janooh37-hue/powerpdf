"""Native card workspace: core edit commands, revision-tagged OCR and snapshot saves.

Workers own immutable requests, never widgets. Closing cancels and joins owned
workers; the shared renderer remains the only PDFium owner.
"""
from __future__ import annotations

import os
import threading
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QProgressBar, QPushButton, QVBoxLayout,
)

from powerpdf_core import cards, files, ocr
from powerpdf_core.jobs import CancelToken, JobCancelled, JobOutcome, JobResult
from powerpdf_core.outputs import OutputCommitError, destination_lock
from powerpdf_core.settings import SettingsStore
from .card_canvas import CardEditor
from .operations import _signature
from .text import FilenameEdit, filename_text


KINDS = (
    ("Current card · two-sided PDF", "pdf"),
    ("Current card · front and back JPGs", "jpg"),
    ("Current side · JPG", "jpg-side"),
    ("Current side · PDF", "pdf-side"),
    ("All kept cards · PDF folder", "pdf-folder"),
    ("All kept cards · JPG folder", "jpg-folder"),
    ("All kept cards · combined PDF with bookmarks", "combined"),
)


def _label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _edit_state(session, index):
    card = session.cards[index]
    return (tuple(id(page) for page in card.pages), tuple(card.rects), tuple(card.guessed),
            tuple(tuple(side) for side in session.card_boxes[index]), session.names[index])


@dataclass(frozen=True)
class _ExportPlan:
    snapshot: cards.ExportSnapshot
    paths: tuple[tuple[Path, ...], ...]
    folder: Path | None
    titles: tuple[str, ...] | None
    signatures: tuple[tuple[Path, object], ...]
    states: tuple[tuple[int, tuple], ...]
    skipped: frozenset[int] | None
    side: int | None

    @property
    def outputs(self):
        return tuple(path for group in self.paths for path in group)


def _export_plan(session, kind, destination):
    """Capture exactly what the user reviewed, with no scan-array copies."""
    if kind not in dict((value, title) for title, value in KINDS):
        raise ValueError("Unknown card save variant")
    destination = Path(destination).expanduser().absolute()
    all_cards = kind.endswith("-folder") or kind == "combined"
    if all_cards:
        snapshot = session.snapshot()
        indices = session.kept()
        if not indices:
            raise ValueError("Every card is skipped. Keep a card before saving.")
    else:
        if session.index in session.skipped:
            raise ValueError("This card is skipped. Keep it before saving.")
        indices = [session.index]
        snapshot = cards.ExportSnapshot(tuple(session.sources), session.source_revisions,
                                        (session._snapshot_card(session.index),))
    folder, titles = None, None
    if kind.endswith("-folder"):
        folder = destination.resolve()
        paths = session.batch_paths(folder, kind.split("-")[0])
    elif kind == "jpg":
        # The chooser selects a pair's stem, not an independently renamed back.
        stem = destination.stem
        if stem.endswith("_front"):
            stem = stem[:-6]
        paths = [[destination.with_name(stem + "_front.jpg"), destination.with_name(stem + "_back.jpg")]]
    else:
        suffix = ".jpg" if kind == "jpg-side" else ".pdf"
        destination = destination.with_suffix(suffix)
        paths = [[destination]]
        if kind.endswith("-side"):
            card, = snapshot.cards
            side = session.side
            card = replace(card, pages=(card.pages[side],), rects=(card.rects[side],),
                           guessed=(card.guessed[side],), boxes=(card.boxes[side],))
            snapshot = replace(snapshot, cards=(card,))
        if kind == "combined":
            titles = tuple(session.file_stems().values())
    reviewed = []
    validated = []
    for group in paths:
        output_group = []
        for path in group:
            reviewed.append(path)
            output = files.validate_output(path, session.sources, suffix=path.suffix)
            if output.exists() and not output.is_file():
                raise ValueError(f"Output is not a file: {output}")
            output_group.append(output)
        validated.append(tuple(output_group))
    outputs = [path for group in validated for path in group]
    if len({str(path).casefold() for path in outputs}) != len(outputs):
        raise ValueError("More than one card has the same output destination")
    parents = {path.parent for path in outputs}
    for parent in parents:
        if not parent.is_dir() and (parent != folder or not parent.parent.is_dir()):
            raise ValueError(f"Choose an existing destination folder: {parent}")
    signatures = tuple((path, _signature(path)) for path in dict.fromkeys((*reviewed, *outputs)))
    return _ExportPlan(snapshot, tuple(validated), folder, titles, signatures,
                       tuple((i, _edit_state(session, i)) for i in indices),
                       frozenset(session.skipped) if all_cards else None,
                       session.side if kind.endswith("-side") else None)


def _execute_export(plan, cancel, progress):
    outputs, error, status = (), None, "success"
    try:
        reservations = set(plan.outputs)
        if plan.folder is not None:
            reservations.add(plan.folder)
        # Sorted, reentrant core reservations cover review checks through commit.
        with ExitStack() as locks:
            for path in sorted(reservations, key=lambda item: os.path.normcase(str(item))):
                locks.enter_context(destination_lock(path))
            cancel.raise_if_cancelled()
            for path, signature in plan.signatures:
                if _signature(path) != signature:
                    raise ValueError(f"Destination changed after review; review again: {path}")
            plan.snapshot.check_sources()
            if plan.folder is not None:
                plan.folder.mkdir(exist_ok=True)
            outputs = tuple(plan.snapshot.export([list(group) for group in plan.paths], folder=plan.folder,
                                                titles=list(plan.titles) if plan.titles else None,
                                                cancel=cancel, progress=progress))
    except Exception as exc:
        outputs = tuple(exc.committed) if isinstance(exc, OutputCommitError) else ()
        error = str(exc)
        status = "cancelled" if isinstance(exc, JobCancelled) else "error"
    size = 0
    for path in outputs:
        try:
            size += path.stat().st_size
        except OSError:
            pass  # Keep the original commit error and its actual receipts.
    return JobResult((JobOutcome(plan.snapshot.sources, outputs, output_bytes=size,
                                 error=error, status=status),), status == "cancelled")


class _Signals(QObject):
    loaded = Signal(object, int, str)
    progress = Signal(object)
    suggestion = Signal(object)
    ocr_done = Signal(str)
    saved = Signal(object, object)


class _Worker:
    def __init__(self, name):
        self.name = name
        self.thread = None
        self.cancel = CancelToken()

    def start(self, function):
        self.join()
        self.cancel = CancelToken()
        self.thread = threading.Thread(target=function, args=(self.cancel,), name=self.name)
        self.thread.start()

    def join(self):
        if self.thread is not None:
            self.thread.join()
            self.thread = None

    def shutdown(self):
        self.cancel.cancel()
        self.join()


class NameDialog(QDialog):
    """Qt shapes logical Arabic; history and suggestions never confirm themselves."""
    def __init__(self, session, parent=None, *, labels=None):
        super().__init__(parent)
        index = session.index
        self.setWindowTitle(f"Name card {index + 1}")
        self.resize(530, 320)
        found = session.suggested[index]
        self.reading = found[0] if found else None
        layout = QVBoxLayout(self)
        layout.addWidget(_label("Confirmed filename · empty clears the name; / becomes -"))
        self.editor = FilenameEdit(session.names[index] or (self.reading or ""))
        self.editor.setAccessibleName("Confirmed card filename")
        layout.addWidget(self.editor)
        self.evidence = _label("OCR is only a suggestion. Confirm explicitly to use it in filenames." +
                               (" Evidence was on the back; check whether front/back need swapping." if found and found[1] else ""))
        layout.addWidget(self.evidence)
        self.history = QListWidget()
        self.history.setAccessibleName("Remembered card labels, newest first")
        for label in session.labels if labels is None else labels:
            item = QListWidgetItem(filename_text(label))
            item.setData(Qt.ItemDataRole.UserRole, label)
            self.history.addItem(item)
        self.history.itemClicked.connect(self.use_label)
        layout.addWidget(self.history)
        if self.reading:
            use = QPushButton("Use OCR number (still requires Confirm)")
            use.clicked.connect(self.use_reading)
            layout.addWidget(use)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Confirm name")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.editor.selectAll()
        self.editor.setFocus()

    def use_label(self, item):
        value = self.editor.text().strip()
        base = value.partition(" ")[0] if cards.card_label(value) else value
        self.editor.setText(f"{base} {item.data(Qt.ItemDataRole.UserRole)}".strip())
        self.editor.setFocus()
        self.editor.setCursorPosition(len(self.editor.text()))

    def use_reading(self):
        self.editor.setText(f"{self.reading} {cards.card_label(self.editor.text())}".strip())
        self.editor.setFocus()


class CardsWindow(QDialog):
    completed = Signal(object)

    def __init__(self, targets, parent=None, *, settings_path=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("PowerPDF · Cards")
        self.resize(1440, 940)
        self.targets = tuple(Path(path).resolve() for path in targets)
        self.session = None
        self.editor = None
        self.job_result = None
        self._closed = False
        self._loading = True
        self._saving = False
        self._ocr_running = False
        self._ocr_unavailable = False
        self._ocr_seen = set()
        self._saved_states = {}
        self._saved_skipped = frozenset()
        self._store = SettingsStore(settings_path)
        self._signals = _Signals(self)
        self._worker = _Worker("PowerPDF-cards")
        self._ocr_worker = _Worker("PowerPDF-card-ocr")
        self._signals.loaded.connect(self._loaded, Qt.ConnectionType.QueuedConnection)
        self._signals.progress.connect(self._progress, Qt.ConnectionType.QueuedConnection)
        self._signals.suggestion.connect(self._suggestion, Qt.ConnectionType.QueuedConnection)
        self._signals.ocr_done.connect(self._ocr_finished, Qt.ConnectionType.QueuedConnection)
        self._signals.saved.connect(self._saved, Qt.ConnectionType.QueuedConnection)
        self._ocr_timer = QTimer(self)
        self._ocr_timer.setSingleShot(True)
        self._ocr_timer.setInterval(500)
        self._ocr_timer.timeout.connect(self._start_ocr)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self.summary = _label("Cards · analyzing up to the first 12 pages across the exact inputs below")
        header.addWidget(self.summary, 1)
        self.previous_button = self._button("Previous", lambda: self.step(-1), "Previous card · Page Up")
        self.next_button = self._button("Next", lambda: self.step(1), "Next card · Page Down")
        for button in (self.previous_button, self.next_button):
            header.addWidget(button)
        layout.addLayout(header)
        self.inputs = _label("\n".join(filename_text(path) for path in self.targets))
        layout.addWidget(self.inputs)
        self.strip = QListWidget()
        self.strip.setAccessibleName("All detected cards, numbered with confirmed, suggested, guessed and skipped markers")
        self.strip.setFlow(QListWidget.Flow.LeftToRight)
        self.strip.setWrapping(False)
        self.strip.setMaximumHeight(86)
        self.strip.currentRowChanged.connect(self.select_card)
        layout.addWidget(self.strip)
        controls = QHBoxLayout()
        self.front_button = self._button("Focus front", lambda: self.focus_side(0), "Edit the front side")
        self.back_button = self._button("Focus back", lambda: self.focus_side(1), "Edit the back side")
        self.swap_button = self._button("Swap front/back", lambda: self.command("swap"), "Manual swap · S; sides are not automatically identified")
        self.skip_button = self._button("Skip card", lambda: self.command("skip"), "Skip / keep the selected card · X")
        self.undo_button = self._button("Undo", lambda: self.command("undo"), "Undo card edit · Ctrl/Cmd+Z")
        self.redo_button = self._button("Redo", lambda: self.command("redo"), "Redo card edit · Ctrl/Cmd+Shift+Z")
        self.name_button = self._button("Name…", self.name_card, "Confirm a filename or edit an Arabic name · N")
        for button in (self.front_button, self.back_button, self.swap_button, self.skip_button,
                       self.undo_button, self.redo_button, self.name_button):
            controls.addWidget(button)
        controls.addStretch()
        layout.addLayout(controls)
        self.name_status = _label("Names are used only after explicit confirmation.")
        layout.addWidget(self.name_status)
        self.editor_layout = QVBoxLayout()
        layout.addLayout(self.editor_layout, 1)
        self.ocr_status = _label("OCR availability is checked in the background. Manual naming always works.")
        layout.addWidget(self.ocr_status)
        save_row = QHBoxLayout()
        self.save_kind = QComboBox()
        self.save_kind.setAccessibleName("Card save variant")
        for title, kind in KINDS:
            self.save_kind.addItem(title, kind)
        save_row.addWidget(self.save_kind, 1)
        self.save_button = self._button("Quick save", self.quick_save, "Review the default output then save · Ctrl/Cmd+S")
        self.save_as_button = self._button("Save As…", self.save_as, "Choose a destination, then review exact output names")
        save_row.addWidget(self.save_button)
        save_row.addWidget(self.save_as_button)
        layout.addLayout(save_row)
        self.status_label = _label("Loading scan…")
        layout.addWidget(self.status_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        layout.addWidget(self.progress_bar)
        self.results = QListWidget()
        self.results.setAccessibleName("Actual saved card outputs and bytes")
        self.results.setMaximumHeight(110)
        self.results.hide()
        layout.addWidget(self.results)
        bottom = QHBoxLayout()
        bottom.addWidget(_label("150 dpi · JPEG q80 · boxes burned in · originals preserved\nN name · S swap · X skip · J JPG pair · O combined PDF · Page Up/Down navigate"), 1)
        self.cancel_button = self._button("Cancel work", self.cancel_work, "Cooperative cancellation; completed files are retained")
        self.close_button = self._button("Close", self.reject, "Close workspace; unsaved edits require confirmation")
        bottom.addWidget(self.cancel_button)
        bottom.addWidget(self.close_button)
        layout.addLayout(bottom)
        self._shortcuts = []
        for key, function in ((QKeySequence.StandardKey.Save, self.quick_save),
                              (QKeySequence.StandardKey.SaveAs, self.save_as),
                              (QKeySequence.StandardKey.Undo, lambda: self.command("undo")),
                              (QKeySequence.StandardKey.Redo, lambda: self.command("redo")),
                              ("PgUp", lambda: self.step(-1)), ("PgDown", lambda: self.step(1))):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(function)
            self._shortcuts.append(shortcut)
        self._set_enabled(False)
        QTimer.singleShot(0, self._load)

    @staticmethod
    def _button(text, function, tooltip):
        button = QPushButton(text)
        button.setAutoDefault(False)
        button.setToolTip(tooltip)
        button.clicked.connect(function)
        return button

    @property
    def busy(self):
        return self._loading or self._saving

    @property
    def dirty(self):
        return self.session is not None and (
            frozenset(self.session.skipped) != self._saved_skipped or
            any(_edit_state(self.session, i) != self._saved_states.get(i) for i in range(len(self.session.cards))))

    def launch(self):
        self.show()
        self.raise_()
        self.activateWindow()
        return self

    def _set_enabled(self, enabled):
        for widget in (self.strip, self.previous_button, self.next_button, self.front_button,
                       self.back_button, self.swap_button, self.skip_button, self.undo_button,
                       self.redo_button, self.name_button, self.save_button, self.save_as_button, self.save_kind):
            widget.setEnabled(enabled)

    def _load(self):
        if self._closed:
            return
        signals, targets, store = self._signals, self.targets, self._store
        def load(cancel):
            try:
                loaded, total = cards.load_cards(targets, labels=store.load().get("card_labels", ()),
                                                  cancel=cancel, progress=signals.progress.emit)
                signals.loaded.emit(loaded, total, "")
            except Exception as error:
                signals.loaded.emit(None, 0, str(error))
        self._worker.start(load)

    @Slot(object, int, str)
    def _loaded(self, session, total, error):
        self._worker.join()
        self._loading = False
        if self._closed:
            return
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        if session is None:
            self.status_label.setText("Could not load cards: " + error)
            self.cancel_button.setEnabled(False)
            return
        self.session = session
        self._saved_states = {i: _edit_state(session, i) for i in range(len(session.cards))}
        self.editor = CardEditor(session, self)
        self.editor.changed.connect(self._changed)
        self.editor_layout.addWidget(self.editor)
        self._set_enabled(True)
        self.status_label.setText(f"Analyzed {min(total, cards.MAX_PAGES)} of {total} pages. Guessed sides require manual review.")
        self.refresh()
        self._start_ocr()

    def refresh(self):
        if self.session is None or self._closed:
            return
        session = self.session
        self.strip.blockSignals(True)
        self.strip.clear()
        for index, card in enumerate(session.cards):
            markers = []
            if index in session.skipped:
                markers.append("skipped")
            if session.names[index]:
                markers.append("named")
            elif session.suggested[index]:
                markers.append("suggestion")
            if any(card.guessed):
                markers.append("guessed side")
            item = QListWidgetItem(f"{index + 1} · " + (" / ".join(markers) or "unnamed"))
            item.setToolTip(card.where + (" · " + filename_text(session.names[index]) if session.names[index] else ""))
            self.strip.addItem(item)
        self.strip.setCurrentRow(session.index)
        self.strip.blockSignals(False)
        self.summary.setText(f"Card {session.index + 1} of {len(session.cards)} · {session.card.where} · "
                             f"{len(session.kept())} kept / {len(session.skipped)} skipped" + (" · unsaved edits" if self.dirty else ""))
        found = session.suggested[session.index]
        name = session.names[session.index]
        self.name_status.setText("Confirmed: " + filename_text(name) if name else
                                 "Suggested (not used in filenames): " + filename_text(found[0]) +
                                 (" · read on back; consider manual Swap" if found[1] else " · read on front") + " · Name… to confirm"
                                 if found else "Unnamed · Name… accepts Arabic and remembered labels")
        self.skip_button.setText("Keep card" if session.index in session.skipped else "Skip card")
        self.previous_button.setEnabled(session.index > 0)
        self.next_button.setEnabled(session.index + 1 < len(session.cards))
        self.undo_button.setEnabled(bool(session._undo))
        self.redo_button.setEnabled(bool(session._redo))
        self.editor.refresh()

    def _changed(self):
        self.refresh()
        self._ocr_timer.start()

    def select_card(self, index):
        if self.session is not None and 0 <= index < len(self.session.cards):
            self.session.step(index - self.session.index)
            self.refresh()

    def step(self, offset):
        if self.session is not None:
            self.session.step(offset)
            self.refresh()

    def focus_side(self, side):
        if self.editor is not None:
            self.editor.focus_side(side)
            self.refresh()

    def command(self, command):
        if self.session is None:
            return
        getattr(self.session, command)()
        if command in {"undo", "redo"}:
            self._persist_labels()
        self._changed()

    def name_card(self):
        if self.session is None:
            return
        index = self.session.index
        try:
            labels = self._store.load().get("card_labels", self.session.labels)
        except (OSError, ValueError) as error:
            self.status_label.setText(f"Could not read label history: {error}")
            labels = self.session.labels
        dialog = NameDialog(self.session, self, labels=labels)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.name_card_value(index, dialog.editor.text())

    def name_card_value(self, index, value):
        if self.session is None or value is None:
            return
        self.session.name(index, value)
        self._persist_labels(cards.card_label(self.session.names[index] or ""))
        self._changed()

    def _persist_labels(self, newest=""):
        try:
            current = self._store.load().get("card_labels", ())
            labels = list(dict.fromkeys(([newest] if newest else []) + list(current) + self.session.labels))[:12]
            self._store.update(card_labels=labels)
        except (OSError, ValueError) as error:
            self.status_label.setText(f"Name is retained in this workspace, but label history could not be saved: {error}")

    def _start_ocr(self):
        if self._closed or self.session is None or self._ocr_running or self._ocr_unavailable:
            return
        requests = tuple(card for card in self.session.ocr_cards() if (card.card_id, card.revision) not in self._ocr_seen)
        if not requests:
            return
        self._ocr_seen.update((card.card_id, card.revision) for card in requests)
        self._ocr_running = True
        self.cancel_button.setEnabled(True)
        self.ocr_status.setText(f"Reading {len(requests)} unnamed card(s) locally · up to 4 workers · suggestions require confirmation")
        signals = self._signals
        def read(cancel):
            error = ""
            try:
                for result in ocr.read_names(requests, cancel=cancel):
                    signals.suggestion.emit(result)
            except JobCancelled:
                error = "OCR cancelled. Manual naming remains available."
            except Exception as exc:
                error = f"OCR failed: {exc}. Manual naming remains available."
            signals.ocr_done.emit(error)
        self._ocr_worker.start(read)

    @Slot(object)
    def _suggestion(self, result):
        if self._closed or self.session is None:
            return
        if not result.available:
            self._ocr_unavailable = True
            self.ocr_status.setText("OCR unavailable: Tesseract was not found. Install/configure Tesseract; manual naming works without it.")
        if self.session.apply_suggestion(result):
            self.refresh()

    @Slot(str)
    def _ocr_finished(self, error):
        self._ocr_worker.join()
        self._ocr_running = False
        if self._closed:
            return
        if error:
            self.ocr_status.setText(error)
        elif not self._ocr_unavailable:
            self.ocr_status.setText("OCR finished · suggestions are unconfirmed; only confirmed names are exported.")
        self.cancel_button.setEnabled(self.busy)
        if not error:
            self._ocr_timer.start()

    def default_destination(self, kind):
        source, session = self.targets[0], self.session
        name = session.names[session.index]
        if kind.endswith("-folder"):
            return source.with_name(source.stem + "_cards")
        if kind == "combined":
            return source.with_name(source.stem + "_cards.pdf")
        numbered = f"_card_{session.index + 1:02d}" if len(session.cards) > 1 else "_card"
        if kind == "pdf":
            return source.with_name((name or source.stem + numbered) + ".pdf")
        base = name or (source.stem + numbered if len(session.cards) > 1 else source.stem)
        side = ("front", "back")[session.side]
        if kind == "pdf-side":
            return source.with_name(base + f"_{side}.pdf")
        if kind == "jpg-side":
            return source.with_name(base + f"_{side}.jpg")
        return source.with_name(base + "_front.jpg")

    def quick_save(self):
        self.save(self.save_kind.currentData())

    def save_as(self):
        self.save(self.save_kind.currentData(), choose=True)

    def review_export(self, plan):
        dialog = QDialog(self)
        dialog.setWindowTitle("Review card outputs")
        dialog.resize(700, 420)
        layout = QVBoxLayout(dialog)
        existing = [path for path in plan.outputs if path.exists() or path.is_symlink()]
        layout.addWidget(_label(f"{len(plan.snapshot.cards)} card(s) · {len(plan.outputs)} output(s) · {len(existing)} existing file(s) will be replaced. Originals cannot be overwritten."))
        listing = QListWidget()
        for path in plan.outputs:
            listing.addItem(filename_text(path) + (" · REPLACE" if path in existing else " · new"))
        layout.addWidget(listing)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        buttons.button(QDialogButtonBox.StandardButton.Save).setAutoDefault(False)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        return not existing or QMessageBox.question(
            self, "Confirm replacement", f"Replace {len(existing)} existing output file(s)?\n" + "\n".join(filename_text(path) for path in existing),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def save(self, kind="pdf", destination=None, *, choose=False):
        if self.session is None or self.busy or self._closed:
            return
        default = self.default_destination(kind)
        if choose:
            if kind.endswith("-folder"):
                chosen = QFileDialog.getExistingDirectory(self, "Save kept cards in folder", str(default.parent))
            else:
                extension = "JPG (*.jpg)" if kind.startswith("jpg") else "PDF (*.pdf)"
                chosen, _ = QFileDialog.getSaveFileName(self, "Save card output", str(default), extension,
                                                       options=QFileDialog.Option.DontConfirmOverwrite)
            if not chosen:
                return
            destination = chosen
        try:
            plan = _export_plan(self.session, kind, destination or default)
            if not self.review_export(plan):
                return
        except (OSError, ValueError) as error:
            self.status_label.setText(f"Cannot save cards: {error}")
            return
        self._saving = True
        self.save_button.setEnabled(False)
        self.save_as_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.results.clear()
        self.results.show()
        self.progress_bar.setRange(0, 0)
        self.status_label.setText(f"Saving immutable snapshot of {len(plan.snapshot.cards)} card(s)… Editing can continue; later edits are not included.")
        signals = self._signals
        def write(cancel):
            result = _execute_export(plan, cancel, signals.progress.emit)
            signals.saved.emit(result, plan)
        self._worker.start(write)

    @Slot(object)
    def _progress(self, event):
        if self._closed:
            return
        self.progress_bar.setRange(0, event.total or 0)
        self.progress_bar.setValue(event.completed)
        if self._loading:
            self.status_label.setText(f"Analyzing page {event.completed} · first 12 pages maximum")

    @Slot(object, object)
    def _saved(self, result, plan):
        self._worker.join()
        self._saving = False
        self.job_result = result
        if result.success:
            for index, state in plan.states:
                if plan.side is None:
                    self._saved_states[index] = state
                else:
                    previous = self._saved_states[index]
                    fields = []
                    for old, new in zip(previous[:4], state[:4]):
                        values = list(old)
                        values[plan.side] = new[plan.side]
                        fields.append(tuple(values))
                    self._saved_states[index] = (*fields, state[4])
            if plan.skipped is not None:
                self._saved_skipped = plan.skipped
        if not self._closed:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1)
            self.save_button.setEnabled(True)
            self.save_as_button.setEnabled(True)
            self.cancel_button.setEnabled(self._ocr_running)
            for path in result.outputs:
                try:
                    size = f"{path.stat().st_size:,} bytes"
                except OSError:
                    size = "no longer available"
                self.results.addItem(filename_text(path) + " · " + size)
            errors = "; ".join(item.error for item in result.outcomes if item.error)
            self.status_label.setText((f"Saved {len(result.outputs)} file(s) · {result.output_bytes:,} bytes" if result.success else
                                       ("Cancelled" if result.cancelled else "Save failed") + f" · {len(result.outputs)} file(s) committed" + (" · " + errors if errors else "")))
            self.refresh()
        self.completed.emit(result)

    def cancel_work(self):
        self._worker.cancel.cancel()
        self._ocr_worker.cancel.cancel()
        self.status_label.setText("Cancelling cooperatively; the current native call must finish. Completed outputs are retained.")

    def _confirm_close(self):
        if self.dirty:
            return QMessageBox.question(self, "Discard unsaved card edits?",
                                         "Unsaved crop, box, name or skip changes will be discarded. Original PDFs are unchanged.",
                                         QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                         QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Discard
        return True

    def shutdown(self):
        self._closed = True
        self._ocr_timer.stop()
        self._worker.shutdown()
        self._ocr_worker.shutdown()

    def reject(self):
        if self._closed or self._confirm_close():
            self.shutdown()
            super().reject()

    def keyPressEvent(self, event):
        focus = QApplication.focusWidget()
        if not isinstance(focus, (QLineEdit, QAbstractSpinBox, QComboBox)) and not event.modifiers():
            actions = {
                "n": self.name_card,
                "s": lambda: self.command("swap"),
                "x": lambda: self.command("skip"),
                "j": lambda: self.save("jpg"),
                "o": lambda: self.save("combined"),
            }
            action = actions.get(event.text().lower())
            if action is not None:
                action()
                event.accept()
                return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        if self._closed or self._confirm_close():
            self.shutdown()
            super().closeEvent(event)
        else:
            event.ignore()
