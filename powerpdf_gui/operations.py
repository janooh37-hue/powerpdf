"""Native review → preflight → job → results workflows.

OperationDialog.launch() opens review; run() requests a job, never blocks Qt.
completed(JobResult) is emitted on the GUI thread after the worker has exited.
Closing cancels cooperatively and joins: native calls are never abandoned.
"""
from __future__ import annotations

import os
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QSpinBox,
    QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from powerpdf_core import files
from powerpdf_core.jobs import CancelToken, JobOutcome, JobResult, JobSpec, Progress, run_job
from powerpdf_core.render import RenderSession
from powerpdf_core.outputs import destination_lock
from .text import FilenameEdit, filename_text


TITLES = {"merge": "Merge PDFs", "split": "Split into pages", "compress": "Compress PDFs", "images": "Export page images"}


def _label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _bytes(value):
    for suffix in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or suffix == "GiB":
            return f"{value:,.0f} {suffix}" if suffix == "B" else f"{value:,.1f} {suffix}"
        value /= 1024


@dataclass(frozen=True)
class _Plan:
    specs: tuple[JobSpec, ...]
    outputs: tuple[Path, ...]
    obsolete: tuple[Path, ...]
    signatures: tuple[tuple[Path, object], ...]
    numbered: tuple[tuple[Path, str, str], ...]
    counts: tuple[int, ...]


def _signature(path):
    try:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, str(path.resolve())
    except FileNotFoundError:
        return (None, None, None, None, None, str(path.resolve()), os.readlink(path)) if path.is_symlink() else None


def _plan(action, targets, destination, options, cancel):
    if not targets:
        raise ValueError("Choose at least one PDF first.")
    for source in targets:
        if not files.is_pdf(source):
            raise ValueError(f"Not a PDF file: {source}")
    source_signatures = tuple((source, _signature(source)) for source in targets)
    counts = []
    for source in targets:
        cancel.raise_if_cancelled()
        if action == "images":
            with RenderSession(source) as session:
                count = session.info.page_count
        elif action == "split":
            with source.open("rb") as stream:
                reader = PdfReader(stream)
                if reader.is_encrypted and not reader.decrypt(""):
                    raise ValueError(f"Password-protected PDF: {source}")
                count = len(reader.pages)
        else:
            count = 0
        if action in {"split", "images"} and not count:
            raise ValueError(f"PDF has no pages: {source}")
        counts.append(count)
    for source, signature in source_signatures:
        if _signature(source) != signature:
            raise ValueError(f"Source changed while reading input details: {source}")
    custom = Path(destination).expanduser().absolute() if destination.strip() else None
    specs, outputs, obsolete, numbered, aliases = [], [], [], [], []
    if custom is not None and len(targets) > 1 and action != "merge" and not custom.is_dir():
        raise ValueError("For multiple inputs, choose an existing destination folder.")
    groups = [targets] if action == "merge" else [(path,) for path in targets]
    for index, group in enumerate(groups):
        source = group[0]
        count = counts[index]
        if action == "compress" and source.stem.endswith("_small"):
            specs.append(JobSpec(action, group, options=options))
            continue
        if action == "merge":
            output = custom or source.parent / "merged.pdf"
        elif action == "compress":
            output = (custom if len(targets) == 1 else custom / f"{source.stem}_small.pdf") if custom else source.with_name(f"{source.stem}_small.pdf")
        elif action == "split":
            output = (custom if len(targets) == 1 else custom / f"{source.stem}_pages") if custom else source.with_name(f"{source.stem}_pages")
        else:
            name = f"{source.stem}_image.{options['fmt']}" if count == 1 else f"{source.stem}_images"
            output = (custom if len(targets) == 1 else custom / name) if custom else source.with_name(name)
        output = output.resolve()
        folder_output = action == "split" or action == "images" and count > 1
        if folder_output:
            if output.exists() and not output.is_dir():
                raise ValueError(f"Destination is not a folder: {output}")
            parent = output if output.exists() else output.parent
            width = max(3, len(str(count)))
            suffix = ".pdf" if action == "split" else f".{options['fmt']}"
            paths = [output / f"{source.stem}_page_{page:0{width}d}{suffix}" for page in range(1, count + 1)]
            numbered.append((output, source.stem, suffix))
            old = files.numbered_outputs(output, source.stem, suffix) if output.exists() else []
            obsolete.extend(path for path in old if path not in paths)
        else:
            parent, paths = output.parent, [output]
        if not parent.is_dir():
            raise ValueError(f"Destination parent does not exist: {parent}")
        if not os.access(parent, os.W_OK):
            raise ValueError(f"Destination is not writable: {parent}")
        validated_paths = []
        for path in paths:
            suffix = f".{options['fmt']}" if action == "images" else ".pdf"
            validated = files.validate_output(path, targets, suffix=suffix)
            validated_paths.append(validated)
            if validated != path:
                aliases.append(path)
            if path.exists() and not os.access(path, os.W_OK):
                raise ValueError(f"Output is not writable: {path}")
        specs.append(JobSpec(action, group, output, options))
        outputs.extend(validated_paths)
    affected = outputs + obsolete
    seen_paths, seen_files = set(), set()
    for path in affected:
        files.validate_output(path, targets, suffix=None)
        key = os.path.normcase(str(path.resolve()))
        signature = _signature(path)
        identity = signature[:2] if signature is not None else None
        if key in seen_paths or identity is not None and identity in seen_files:
            raise ValueError(f"Inputs produce the same output: {path}")
        seen_paths.add(key)
        if identity is not None:
            seen_files.add(identity)
    # Record source and output revisions before asking the user to replace anything.
    signatures = tuple((path, _signature(path)) for path in dict.fromkeys((*targets, *affected, *aliases)))
    return _Plan(tuple(specs), tuple(outputs), tuple(obsolete), signatures, tuple(numbered), tuple(counts))


def _check_plan(plan, targets):
    for path, signature in plan.signatures:
        if _signature(path) != signature:
            raise ValueError(f"File changed after review; review the destinations again: {path}")
    expected = set(plan.outputs) | set(plan.obsolete)
    for folder, stem, suffix in plan.numbered:
        if folder.exists():
            for path in files.numbered_outputs(folder, stem, suffix):
                if path not in expected and path.resolve() not in expected:
                    raise ValueError(f"New output appeared after review: {path}")
    for path in expected:
        files.validate_output(path, targets, suffix=None)


class _Signals(QObject):
    prepared = Signal(int, object, str)
    progress = Signal(object)
    outcome = Signal(object)
    done = Signal(object)


class _Worker:
    """The thread touches immutable requests and signals only, never widgets."""
    def __init__(self, signals):
        self.signals = signals
        self.thread = None
        self.cancel = CancelToken()

    def start(self, function, *args):
        self.join()
        self.cancel = CancelToken()
        self.thread = threading.Thread(target=function, args=(*args, self.cancel), name="PowerPDF-operation")
        self.thread.start()

    def join(self):
        if self.thread is not None:
            self.thread.join()
            self.thread = None

    def shutdown(self, *_args):
        self.cancel.cancel()
        self.join()

    def prepare(self, serial, action, targets, destination, options, cancel):
        try:
            plan = _plan(action, targets, destination, options, cancel)
            self.signals.prepared.emit(serial, plan, "")
        except Exception as error:
            self.signals.prepared.emit(serial, None, str(error))

    def execute(self, plan, targets, cancel):
        outcomes = []
        cancelled = False
        try:
            # Use the same reentrant locks as core, before checking the reviewed
            # revisions. Competing native/TUI jobs cannot invalidate consent
            # between this check and the commit.
            lock_paths = {spec.output.resolve() for spec in plan.specs if spec.output is not None}
            lock_paths.update(path.resolve() for path in (*plan.outputs, *plan.obsolete))
            with ExitStack() as locks:
                for path in sorted(lock_paths, key=lambda item: os.path.normcase(str(item))):
                    locks.enter_context(destination_lock(path))
                _check_plan(plan, targets)
                for spec in plan.specs:
                    if cancel.is_cancelled:
                        cancelled = True
                        break
                    result = run_job(spec, cancel=cancel, progress=self.signals.progress.emit,
                                     on_outcome=self.signals.outcome.emit)
                    outcomes.extend(result.outcomes)
                    if result.cancelled:
                        cancelled = True
                        break
        except Exception as error:
            outcome = JobOutcome(targets, error=str(error), status="error")
            outcomes.append(outcome)
            self.signals.outcome.emit(outcome)
        self.signals.done.emit(JobResult(tuple(outcomes), cancelled))


class OperationDialog(QDialog):
    """Ordered target review with truthful preflight, cancellation and output receipts.

    targets must already be the caller's exact target resolution, in desired order.
    An empty destination uses the per-input defaults shown in Output preview. For
    multiple inputs a selected destination is an existing base directory.
    """
    completed = Signal(object)
    started = Signal()

    def __init__(self, action, targets, parent=None):
        if action not in TITLES:
            raise ValueError(f"Unknown operation: {action}")
        super().__init__(parent)
        self.action = action
        self.job_result = None
        self._plan = None
        self._serial = 0
        self._preparing = False
        self._pending_run = False
        self._running = False
        self._closed = False
        self._seen = []
        self.setWindowTitle(TITLES[action])
        self.setObjectName(f"{action}Dialog")
        self.resize(760, 690)
        self.setMinimumSize(620, 520)
        self._signals = _Signals(self)
        self._worker = _Worker(self._signals)
        self._signals.prepared.connect(self._prepared)
        self._signals.progress.connect(self._progress)
        self._signals.outcome.connect(self._outcome)
        self._signals.done.connect(self._done)
        self.destroyed.connect(self._worker.shutdown)
        QApplication.instance().aboutToQuit.connect(self.shutdown)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._prepare)
        layout = QVBoxLayout(self)
        heading = _label(TITLES[action])
        heading.setProperty("role", "heading")
        layout.addWidget(heading)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        review = QWidget()
        form = QVBoxLayout(review)
        form.addWidget(_label("Review the exact input order. Originals are never replaced."))
        self.target_list = QListWidget()
        self.target_list.setAccessibleName("Ordered input PDFs")
        self.target_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        for path in targets:
            path = Path(path).expanduser().absolute()
            item = QListWidgetItem(filename_text(path))
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(str(path))
            self.target_list.addItem(item)
        form.addWidget(self.target_list, 1)
        if action == "merge":
            reorder = QHBoxLayout()
            for text, offset in (("Move up", -1), ("Move down", 1)):
                button = QPushButton(text)
                button.clicked.connect(lambda _checked=False, step=offset: self.move_target(step))
                reorder.addWidget(button)
            reorder.addStretch()
            form.addLayout(reorder)
            if self.target_list.count():
                self.target_list.setCurrentRow(0)
        options = QFormLayout()
        self.quality = QComboBox()
        self.quality.addItem("Ebook · 150 dpi · balanced", "ebook")
        self.quality.addItem("Screen · 72 dpi · smallest", "screen")
        self.quality.addItem("Printer · 300 dpi · print quality", "printer")
        self.crop = QCheckBox("Crop to content (rasterizes pages)")
        self.format = QComboBox()
        self.format.addItem("JPG · compact scans and photos", "jpg")
        self.format.addItem("PNG · lossless text and graphics", "png")
        self.dpi = QSpinBox()
        self.dpi.setRange(50, 600)
        self.dpi.setValue(150)
        self.dpi.setSuffix(" dpi")
        self.dpi.setAccessibleName("Image resolution, 50 to 600 DPI")
        if action == "compress":
            options.addRow("Quality", self.quality)
            options.addRow("", self.crop)
        elif action == "images":
            options.addRow("Format", self.format)
            resolution = QHBoxLayout()
            resolution.addWidget(self.dpi, 1)
            for value in (150, 300):
                preset = QPushButton(f"{value} dpi")
                preset.setObjectName(f"dpiPreset{value}")
                preset.setToolTip(f"Set image export resolution to {value} DPI")
                preset.clicked.connect(lambda _checked=False, dpi=value: self.dpi.setValue(dpi))
                resolution.addWidget(preset)
            options.addRow("Resolution · 50–600", resolution)
        form.addLayout(options)
        # Parent unused controls so their lifetime remains deterministic too.
        for control in (self.quality, self.crop, self.format, self.dpi):
            if control.parent() is None:
                control.setParent(review)
                control.hide()
        self.warning_label = _label()
        form.addWidget(self.warning_label)
        destination_row = QHBoxLayout()
        self.destination = FilenameEdit()
        self.destination.setAccessibleName("Output destination")
        self.destination.setPlaceholderText("Default destinations shown below")
        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self.choose_destination)
        destination_row.addWidget(self.destination, 1)
        destination_row.addWidget(self.browse_button)
        form.addWidget(_label("Destination · leave blank for per-input defaults"))
        form.addLayout(destination_row)
        form.addWidget(_label("Output preview · every replacement and obsolete page removal is confirmed"))
        self.output_list = QListWidget()
        self.output_list.setAccessibleName("Actual output paths")
        form.addWidget(self.output_list, 1)
        self.error_label = _label("Reading input details…")
        form.addWidget(self.error_label)
        self.stack.addWidget(review)
        progress_page = QWidget()
        progress_layout = QVBoxLayout(progress_page)
        self.status_label = _label("Ready")
        self.progress_bar = QProgressBar()
        self.progress_bar.setAccessibleName("Current input progress")
        self.overall_label = _label()
        self.results = QTreeWidget()
        self.results.setHeaderLabels(["Input / output", "Status", "Output bytes"])
        self.results.setColumnWidth(0, 440)
        self.results.setColumnWidth(1, 100)
        self.results.setAccessibleName("Per-input operation results")
        progress_layout.addWidget(self.status_label)
        progress_layout.addWidget(self.progress_bar)
        progress_layout.addWidget(self.overall_label)
        progress_layout.addWidget(self.results, 1)
        self.cancel_note = _label("Cancel is cooperative: PDFium finishes its current native call; Ghostscript is stopped safely. Completed outputs are retained.")
        progress_layout.addWidget(self.cancel_note)
        self.stack.addWidget(progress_page)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.run_button = QPushButton(TITLES[action])
        self.run_button.setDefault(True)
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.run)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        self.destination.textChanged.connect(self._invalidate)
        self.quality.currentIndexChanged.connect(self._invalidate)
        self.crop.toggled.connect(self._invalidate)
        self.format.currentIndexChanged.connect(self._invalidate)
        self.dpi.valueChanged.connect(self._invalidate)
        self._invalidate()

    @property
    def busy(self):
        return self._running or self._preparing

    def targets(self):
        return tuple(self.target_list.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.target_list.count()))

    def launch(self):
        self.show()
        self.raise_()
        self.activateWindow()
        return self

    def move_target(self, offset):
        row = self.target_list.currentRow()
        if 0 <= row < self.target_list.count() and 0 <= row + offset < self.target_list.count():
            item = self.target_list.takeItem(row)
            self.target_list.insertItem(row + offset, item)
            self.target_list.setCurrentRow(row + offset)
            self._invalidate()

    def _options(self):
        if self.action == "compress":
            return {"quality": self.quality.currentData(), "crop": self.crop.isChecked()}
        if self.action == "images":
            return {"fmt": self.format.currentData(), "dpi": self.dpi.value()}
        return {}

    def _invalidate(self, *_args):
        if self._closed or self._running:
            return
        self._serial += 1
        self._plan = None
        self._pending_run = False
        self.run_button.setEnabled(False)
        self.error_label.setText("Checking actual output paths…")
        self.warning_label.setText(
            "Screen quality can make small print and ID cards unreadable. Choose Ebook to preserve legibility. Crop rasterizes pages and can discard searchable text."
            if self.action == "compress" and self.quality.currentData() == "screen" else
            "Compression removes metadata. Crop rasterizes pages and can discard searchable text."
            if self.action == "compress" else
            "Large phone scans are normalized to A4; DPI applies to exported images, not preview."
            if self.action == "images" else "")
        self._timer.start()

    def _prepare(self):
        if self._closed or self._running or self._preparing:
            return
        self._preparing = True
        self._worker.start(self._worker.prepare, self._serial, self.action, self.targets(),
                           self.destination.text(), self._options())

    @Slot(int, object, str)
    def _prepared(self, serial, plan, error):
        self._worker.join()
        self._preparing = False
        if self._closed:
            return
        if serial != self._serial:
            self._prepare()
            return
        self._plan = plan
        self.output_list.clear()
        if plan is not None:
            for path in plan.outputs:
                self.output_list.addItem(filename_text(path) + ("  · replace" if path.exists() else ""))
            for path in plan.obsolete:
                self.output_list.addItem("Remove obsolete page: " + filename_text(path))
            skipped = sum(spec.action == "compress" and spec.inputs[0].stem.endswith("_small") for spec in plan.specs)
            self.error_label.setText(f"{len(self.targets())} input(s) · {len(plan.outputs)} output file(s)" + (f" · {skipped} already-small input(s) will be skipped" if skipped else ""))
        else:
            self.error_label.setText(error)
        self.run_button.setEnabled(plan is not None)
        if self._pending_run and plan is not None:
            self._pending_run = False
            self._begin()

    def choose_destination(self):
        targets = self.targets()
        if not targets:
            return
        folder = len(targets) > 1 and self.action != "merge" or self.action == "split"
        if self.action == "images" and len(targets) == 1:
            if self._plan is None:
                self.error_label.setText("Wait for input details before choosing the destination.")
                return
            folder = self._plan.counts[0] > 1
        current = self.destination.text() or str(self._plan.specs[0].output if self._plan else targets[0].parent)
        if folder:
            chosen = QFileDialog.getExistingDirectory(self, "Choose output folder", current)
        else:
            suffix = self.format.currentData() if self.action == "images" else "pdf"
            chosen, _ = QFileDialog.getSaveFileName(self, "Choose output file", current, f"{suffix.upper()} files (*.{suffix})",
                                                   options=QFileDialog.Option.DontConfirmOverwrite)
        if chosen:
            self.destination.setText(chosen)

    def run(self):
        """Request fresh preflight, confirmation of all side effects, then run."""
        if self._closed or self._running or self.job_result is not None:
            return
        self._timer.stop()
        self._pending_run = True
        self._serial += 1
        self.run_button.setEnabled(False)
        self.error_label.setText("Preflighting destinations before writing…")
        self._prepare()

    def _confirm_overwrite(self, paths, obsolete):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Confirm every output replacement")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(f"Replace {len(paths)} existing output file(s) and remove {len(obsolete)} obsolete page file(s)? Originals are protected.")
        box.setInformativeText("All affected paths are listed in Show Details. This cannot be undone.")
        box.setDetailedText("\n".join([*(f"REPLACE  {path}" for path in paths), *(f"REMOVE  {path}" for path in obsolete)]))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _begin(self):
        plan = self._plan
        existing = tuple(path for path in plan.outputs if path.exists())
        if (existing or plan.obsolete) and not self._confirm_overwrite(existing, plan.obsolete):
            self.run_button.setEnabled(True)
            self.error_label.setText("No files changed. Choose another destination or run again.")
            return
        self._running = True
        self.stack.setCurrentIndex(1)
        self.run_button.hide()
        self.results.clear()
        self._seen.clear()
        self.status_label.setText("Starting local operation…")
        self.overall_label.setText(f"0 / {len(self.targets())} inputs complete")
        self.progress_bar.setRange(0, 0)
        self._worker.start(self._worker.execute, plan, self.targets())
        self.started.emit()

    @Slot(object)
    def _progress(self, event: Progress):
        if self._closed:
            return
        self.progress_bar.setRange(0, max(1, event.total))
        self.progress_bar.setValue(event.completed)
        prefix = f"{filename_text(event.input_path.name)} · " if event.input_path else ""
        self.status_label.setText(prefix + event.message)

    @Slot(object)
    def _outcome(self, outcome: JobOutcome):
        if self._closed:
            return
        self._seen.append(outcome)
        text = " + ".join(filename_text(path.name) for path in outcome.inputs)
        row = QTreeWidgetItem([text, outcome.status.capitalize(), _bytes(outcome.output_bytes)])
        row.setToolTip(0, "\n".join(str(path) for path in outcome.inputs))
        self.results.addTopLevelItem(row)
        for path in outcome.outputs:
            QTreeWidgetItem(row, [filename_text(path), "Written", ""])
        if outcome.error:
            QTreeWidgetItem(row, [outcome.error, outcome.status.capitalize(), ""])
        if outcome.status == "skipped":
            QTreeWidgetItem(row, ["Already has the _small suffix; no output written.", "", ""])
        row.setExpanded(True)
        count = sum(len(item.inputs) for item in self._seen)
        self.overall_label.setText(f"{count} / {len(self.targets())} inputs processed · {_bytes(sum(item.output_bytes for item in self._seen))} written")

    @Slot(object)
    def _done(self, result: JobResult):
        self._worker.join()
        self._running = False
        self.job_result = result
        if self._closed:
            return
        processed = {path for outcome in result.outcomes for path in outcome.inputs}
        for path in self.targets():
            if path not in processed:
                self.results.addTopLevelItem(QTreeWidgetItem([filename_text(path), "Not started", "0 B"]))
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        title = "Cancelled" if result.cancelled else "Completed" if result.success else "Completed with errors"
        self.status_label.setText(f"{title} · {len(result.outputs)} output file(s) · {_bytes(result.output_bytes)} written")
        self.cancel_note.setText("Completed outputs are listed above and remain on disk. Originals are unchanged.")
        self.cancel_button.setText("Close")
        self.cancel_button.setEnabled(True)
        self.completed.emit(result)

    def cancel(self):
        if self._running:
            self._worker.cancel.cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Cancelling safely… waiting for the active native call to finish.")

    def reject(self):
        if self._running:
            self.cancel()
        else:
            self.shutdown()
            super().reject()

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        self._worker.shutdown()
        self._running = False
        self._preparing = False

    def closeEvent(self, event):
        self.shutdown()
        event.accept()
