"""Reviewed Qt printing, independent of export/SHARP jobs.

PrintDialog freezes its caller's exact ordered paths. Page numbers in the native
print dialog address the concatenation of those documents. Only an explicitly
accepted QPrintDialog transfers its QPrinter to the worker. Thereafter the GUI
never reads, mutates, aborts, or destroys that printer. PDFium access, including
metadata and cleanup, is exclusively through RenderSession's serialized lane.

One uncached 300-dpi page (at most 40 million pixels) is rendered, copied into
an owned QImage, painted, and released before another is rendered. Oversized
pages fail explicitly rather than silently changing their physical scale.
Counts mean pages handed to Qt, NOT confirmed physical output. Native printers
receive one job per input; explicit PDF output combines the reviewed queue.
"""
from __future__ import annotations

import hashlib
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, Qt, Signal, Slot
from shiboken6 import delete
from PySide6.QtGui import QImage, QPageLayout, QPainter
from PySide6.QtPrintSupport import QAbstractPrintDialog, QPrintDialog, QPrinter
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QSpinBox,
    QVBoxLayout,
)

from powerpdf_core.render import RenderSession, SourceChangedError
from powerpdf_core.outputs import OutputCommitError, commit_files, staging_dir
from .text import filename_text

PRINT_DPI = 300
PRINT_PIXEL_BUDGET = 40_000_000


def _revision(path: Path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@dataclass(frozen=True)
class PrintResult:
    targets: tuple[Path, ...]
    submitted_pages: int
    total_pages: int
    cancelled: bool = False
    error: str = ""
    output_path: Path | None = None
    pdf_output: bool = False

    @property
    def success(self) -> bool:
        return not self.cancelled and not self.error and self.submitted_pages == self.total_pages > 0


@dataclass(frozen=True)
class _Document:
    path: Path
    pages: int
    digest: bytes


class _Signals(QObject):
    prepared = Signal(object, str)
    progress = Signal(int, int, str)
    finished = Signal(object)


class _Cancelled(Exception):
    pass


def _destination(size: tuple[float, float], bounds: QRectF, fit: bool) -> QRectF:
    """Physical PDF points, never image pixel dimensions or screen DPI."""
    width, height = size
    scale = min(bounds.width() / width, bounds.height() / height) if fit else 1.0
    width, height = width * scale, height * scale
    return QRectF(
        bounds.center().x() - width / 2, bounds.center().y() - height / 2,
        width, height,
    )


def _page_order(documents: tuple[_Document, ...], printer: QPrinter, *, combine_documents: bool):
    """Apply selection once, collating within each native source job."""
    count = sum(document.pages for document in documents)
    mode = printer.printRange()
    if mode == QPrinter.PrintRange.PageRange:
        ranges = printer.pageRanges()
        if ranges.isEmpty():
            first, last = printer.fromPage(), printer.toPage()
            if not 1 <= first <= last <= count:
                raise ValueError(f"Page range must be within 1–{count}")
            selected = list(range(first - 1, last))
        else:
            if ranges.firstPage() < 1 or ranges.lastPage() > count:
                raise ValueError(f"Page range must be within 1–{count}")
            selected = [index for index in range(count) if ranges.contains(index + 1)]
    elif mode == QPrinter.PrintRange.AllPages:
        selected = list(range(count))
    else:
        raise ValueError("Use All pages or Page range; current-page and selection printing are unavailable")
    if not selected:
        raise ValueError("No pages selected for printing")
    if printer.pageOrder() == QPrinter.PageOrder.LastPageFirst:
        selected.reverse()
    copies = printer.copyCount()
    if copies < 1:
        raise ValueError("Copies must be positive")
    collate = printer.collateCopies()
    # Prevent a driver from multiplying the explicitly sequenced copies again.
    printer.setCopyCount(1)
    printer.setCollateCopies(False)
    printer.setPrintRange(QPrinter.PrintRange.AllPages)
    printer.setFromTo(0, 0)
    from PySide6.QtGui import QPageRanges
    printer.setPageRanges(QPageRanges())
    lookup = []
    for document_index, document in enumerate(documents):
        lookup.extend((document_index, page) for page in range(document.pages))
    if combine_documents:
        groups = ([lookup[index] for index in selected],)
    else:
        # LastPageFirst reverses pages within each independent source job, not
        # the caller's exact ordered queue of input documents.
        groups = (
            [lookup[index] for index in selected if lookup[index][0] == document_index]
            for document_index in range(len(documents))
        )

    def sequence():
        for group in groups:
            if collate:
                yield from (page for _ in range(copies) for page in group)
            else:
                yield from (page for page in group for _ in range(copies))

    return sequence(), len(selected) * copies


class _PrintWorker:
    def __init__(self, signals: _Signals) -> None:
        self.signals = signals
        self.cancelled = threading.Event()
        self.direction = QApplication.layoutDirection()
        self.thread: threading.Thread | None = None

    def prepare(self, paths: tuple[Path, ...]) -> None:
        self.thread = threading.Thread(target=self._prepare, args=(paths,), name="PowerPDF-print-review")
        self.thread.start()

    def _prepare(self, paths: tuple[Path, ...]) -> None:
        documents = []
        try:
            for path in paths:
                self._checkpoint()
                with RenderSession(path) as session:
                    if not session.info.page_count:
                        raise ValueError(f"Document has no pages: {path}")
                    documents.append(_Document(
                        path, session.info.page_count, hashlib.sha256(session.source_bytes).digest(),
                    ))
            self._checkpoint()
            self.signals.prepared.emit(tuple(documents), "")
        except _Cancelled:
            return
        except Exception as exc:
            self.signals.prepared.emit((), f"{type(exc).__name__}: {exc}")

    def start(self, printer: QPrinter, documents: tuple[_Document, ...], fit: bool, output_revision) -> None:
        if self.thread is not None:
            self.thread.join()
        self.cancelled.clear()
        self.thread = threading.Thread(
            target=self._print, args=(printer, documents, fit, output_revision), name="PowerPDF-print",
        )
        self.thread.start()

    def _checkpoint(self) -> None:
        if self.cancelled.is_set():
            raise _Cancelled

    def cancel(self) -> None:
        self.cancelled.set()

    def shutdown(self, *_ignored) -> None:
        self.cancel()
        if self.thread is not None:
            self.thread.join()

    def _print(self, printer: QPrinter, documents: tuple[_Document, ...], fit: bool, output_revision) -> None:
        submitted, total = 0, 0
        error, cancelled = "", False
        output_path = None
        requested_output = None
        staging = None
        painter = QPainter()
        session = None
        current_document = None
        try:
            if printer.outputFormat() == QPrinter.OutputFormat.PdfFormat:
                requested_output = Path(printer.outputFileName()).absolute()
                if not printer.outputFileName():
                    raise ValueError("PDF output requires an explicit output filename")
                if any(requested_output.resolve() == document.path.resolve() for document in documents):
                    raise ValueError("Print output must not overwrite a source PDF")
            order, total = _page_order(documents, printer, combine_documents=requested_output is not None)
            if requested_output is not None:
                staging = staging_dir(requested_output.parent, ".powerpdf_print_")
                printer.setOutputFileName(str(staging / "print.pdf"))
            self.signals.progress.emit(0, total, "Preparing print engine…")
            # Capture the selected physical margins BEFORE enabling full-paper
            # coordinates; FullPageMode would otherwise erase the paint margins.
            layout = printer.pageLayout()
            layout.setMode(QPageLayout.Mode.StandardMode)
            bounds = layout.paintRect(QPageLayout.Unit.Point).intersected(
                layout.fullRect(QPageLayout.Unit.Point)
            )
            if bounds.isEmpty():
                raise ValueError("The selected printer has no printable area")
            printer.setFullPage(True)
            for document_index, page in order:
                self._checkpoint()
                document = documents[document_index]
                if current_document != document_index:
                    if session is not None:
                        if requested_output is None and painter.isActive():
                            self._finish_job(painter, printer)
                        session.check_source_unchanged()
                        session.close()
                        session = None
                    session = RenderSession(document.path)
                    current_document = document_index
                    if hashlib.sha256(session.source_bytes).digest() != document.digest:
                        raise SourceChangedError(f"Source changed since print review: {document.path}")
                    if requested_output is None:
                        printer.setDocName(filename_text(document.path.name, self.direction))
                session.check_source_unchanged()
                self.signals.progress.emit(
                    submitted, total,
                    f"Rendering {filename_text(document.path.name, self.direction)} · page {page + 1} at {PRINT_DPI} dpi",
                )
                pixels = session.render(
                    page, scale=PRINT_DPI / 72, purpose="print", max_pixels=PRINT_PIXEL_BUDGET,
                )
                try:
                    self._checkpoint()
                    data = pixels.tobytes()
                    image = QImage(
                        data, pixels.width, pixels.height, pixels.width * 3,
                        QImage.Format.Format_RGB888,
                    ).copy()
                    del data
                finally:
                    pixels.close()
                try:
                    self._checkpoint()
                    if not painter.isActive():
                        if not painter.begin(printer):
                            raise OSError("The Qt print engine could not begin the job")
                    elif not printer.newPage():
                        raise OSError("The Qt print engine rejected the next page")
                    painter.save()
                    try:
                        painter.scale(printer.logicalDpiX() / 72, printer.logicalDpiY() / 72)
                        painter.setClipRect(bounds)
                        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                        # The PDF paint engine otherwise JPEG-encodes an already
                        # rasterized page, adding avoidable artifacts to fine print.
                        painter.setRenderHint(QPainter.RenderHint.LosslessImageRendering)
                        painter.drawImage(_destination(session.info.page_sizes[page], bounds, fit), image)
                    finally:
                        painter.restore()
                    if printer.printerState() in (QPrinter.PrinterState.Error, QPrinter.PrinterState.Aborted):
                        raise OSError("The Qt print engine reported an error or aborted the job")
                    submitted += 1
                    self.signals.progress.emit(submitted, total, "Pages handed to the Qt print engine")
                finally:
                    del image
            self._checkpoint()
            if session is not None:
                session.check_source_unchanged()
            self._finish_job(painter, printer)
            if requested_output is not None:
                def before_commit():
                    self._checkpoint()
                    if _revision(requested_output) != output_revision:
                        raise OSError("PDF output changed after overwrite review; nothing was replaced")
                    for document in documents:
                        with document.path.open("rb") as source:
                            if hashlib.file_digest(source, "sha256").digest() != document.digest:
                                raise SourceChangedError(f"Source changed before print output commit: {document.path}")
                    self._checkpoint()

                commit_files([(staging / "print.pdf", requested_output)], before_commit=before_commit)
                output_path = requested_output
        except _Cancelled:
            cancelled = True
        except Exception as exc:
            if isinstance(exc, OutputCommitError) and isinstance(exc.cause, _Cancelled):
                cancelled = True
            else:
                error = f"{type(exc).__name__}: {exc}"
        finally:
            if painter.isActive():
                # Only the owning worker may abort. Neither this nor the count
                # promises that already-spooled pages can be recalled.
                try:
                    printer.abort()
                    painter.end()
                except Exception as exc:
                    error = error or f"{type(exc).__name__}: {exc}"
            if session is not None:
                try:
                    session.close()
                except Exception as exc:
                    error = error or f"{type(exc).__name__}: {exc}"
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            # The thread's argument reference is also released on return; the
            # GUI joins that thread before exposing the completed result.
            del printer
        self.signals.finished.emit(PrintResult(
            tuple(document.path for document in documents), submitted, total,
            cancelled, error, output_path, requested_output is not None,
        ))

    @staticmethod
    def _finish_job(painter: QPainter, printer: QPrinter) -> None:
        if not painter.end():
            raise OSError("The Qt print engine could not finish submitting the job")
        if printer.printerState() in (QPrinter.PrinterState.Error, QPrinter.PrinterState.Aborted):
            raise OSError("The Qt print engine failed while finishing the job")


class PrintDialog(QDialog):
    """Review exact paths, then explicitly accept the native print dialog.

    completed(PrintResult) reports submission, cancellation, or failure, never
    physical delivery. Closing during work requests cooperative cancellation and
    keeps this widget alive until its worker has released the print device.
    """
    completed = Signal(object)

    def __init__(self, targets, parent=None):
        super().__init__(parent)
        self.targets = tuple(Path(path).absolute() for path in targets)
        self._documents: tuple[_Document, ...] = ()
        self._running = False
        self.last_result: PrintResult | None = None
        self.setWindowTitle("Print PDFs · PowerPDF")
        self.setObjectName("printDialog")
        self.resize(660, 590)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(14)
        title = QLabel("Print PDFs")
        title.setProperty("role", "title")
        layout.addWidget(title)
        self.summary_label = QLabel(f"{len(self.targets)} document(s) · exact queue order")
        layout.addWidget(self.summary_label)
        self.target_list = QListWidget()
        self.target_list.setAccessibleName("Exact documents to print, in order")
        self.target_list.setLayoutDirection(QApplication.layoutDirection())
        for index, path in enumerate(self.targets, 1):
            item = QListWidgetItem(f"{index:02d}  {filename_text(path.name)}")
            item.setToolTip(filename_text(path))
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.target_list.addItem(item)
        layout.addWidget(self.target_list, 1)
        self.controls = QFormLayout()
        self.scale_combo = QComboBox()
        self.scale_combo.addItem("Fit to printable area", True)
        self.scale_combo.addItem("Actual size · 100% physical points (may clip)", False)
        self.scale_combo.setAccessibleName("Print scaling")
        self.controls.addRow("Page scaling", self.scale_combo)
        self.copies_spin = QSpinBox()
        self.copies_spin.setRange(1, 999)
        self.copies_spin.setAccessibleName("Print copies")
        self.controls.addRow("Copies", self.copies_spin)
        self.collate_check = QCheckBox("Collate complete copies")
        self.collate_check.setChecked(True)
        self.collate_check.setAccessibleName("Collate complete copies")
        self.controls.addRow("Order", self.collate_check)
        layout.addLayout(self.controls)
        note = QLabel(
            "300 dpi raster printing (40 million pixels/page maximum; larger pages are rejected). "
            "Choose paper, margins, printer and page ranges in the system dialog. "
            "Page numbers run through the listed documents. Native printers receive one job per input; "
            "explicit PDF output combines the queue into one file."
        )
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        layout.addWidget(note)
        self.progress = QProgressBar()
        self.progress.setAccessibleName("Pages handed to print engine")
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status_label = QLabel("Reading document page counts…")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName("Print status")
        layout.addWidget(self.status_label)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        self.cancel_button = QPushButton("Cancel submission")
        self.cancel_button.setAccessibleName("Cancel print submission")
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.hide()
        self.start_button = QPushButton("Choose printer…")
        self.start_button.setAccessibleName("Choose printer and review native print settings")
        self.start_button.setDefault(True)
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.start)
        buttons.addWidget(self.close_button)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.start_button)
        layout.addLayout(buttons)
        self._signals = _Signals(self)
        self._signals.prepared.connect(self._prepared)
        self._signals.progress.connect(self._progress)
        self._signals.finished.connect(self._finished)
        self._worker = _PrintWorker(self._signals)
        self.destroyed.connect(self._worker.shutdown)
        QApplication.instance().aboutToQuit.connect(self._worker.shutdown)
        if self.targets:
            self._worker.prepare(self.targets)
        else:
            self.status_label.setText("No documents selected. Nothing will be printed.")

    @Slot(object, str)
    def _prepared(self, documents, error):
        self._documents = documents
        if error:
            self.status_label.setText(f"Cannot print: {error}")
        else:
            count = sum(document.pages for document in documents)
            self.summary_label.setText(f"{len(documents)} document(s) · {count} pages · exact queue order")
            self.status_label.setText("Ready for review. Nothing is submitted until you accept the system print dialog.")
            self.start_button.setEnabled(True)

    def _create_printer(self) -> QPrinter:
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setResolution(PRINT_DPI)
        printer.setDocName("PowerPDF · " + filename_text(self.targets[0].name))
        printer.setCopyCount(self.copies_spin.value())
        printer.setCollateCopies(self.collate_check.isChecked())
        printer.setFullPage(False)
        return printer

    def _choose_printer(self, printer: QPrinter) -> bool:
        dialog = QPrintDialog(printer, self)
        dialog.setWindowTitle("Choose printer · PowerPDF")
        dialog.setMinMax(1, sum(document.pages for document in self._documents))
        dialog.setOption(QAbstractPrintDialog.PrintDialogOption.PrintPageRange, True)
        dialog.setOption(QAbstractPrintDialog.PrintDialogOption.PrintCollateCopies, True)
        dialog.setOption(QAbstractPrintDialog.PrintDialogOption.PrintSelection, False)
        dialog.setOption(QAbstractPrintDialog.PrintDialogOption.PrintCurrentPage, False)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        # Destroy the native dialog on the GUI thread, not on the worker, and
        # release its printer reference before ownership is transferred.
        delete(dialog)
        return accepted

    def _confirm_overwrite(self, path: Path) -> bool:
        message = QMessageBox(self)
        message.setWindowTitle("Replace PDF print output?")
        message.setIcon(QMessageBox.Icon.Warning)
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setText(f"Replace the existing PDF?\n{filename_text(path)}")
        message.setInformativeText(
            "The existing file is replaced only after the complete PDF print job succeeds."
        )
        message.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        message.setDefaultButton(QMessageBox.StandardButton.No)
        return message.exec() == QMessageBox.StandardButton.Yes

    def _approve_pdf_output(self, printer: QPrinter):
        if printer.outputFormat() != QPrinter.OutputFormat.PdfFormat or not printer.outputFileName():
            return True, None
        path = Path(printer.outputFileName()).absolute()
        revision = _revision(path)
        # A source destination is rejected by the worker, never offered as a
        # replaceable output, even if the native file dialog allowed it.
        if any(path.resolve() == source.resolve() for source in self.targets):
            return True, revision
        if revision is not None and not self._confirm_overwrite(path):
            return False, revision
        return True, revision

    @Slot()
    def start(self):
        if self._running or not self._documents:
            return
        printer = self._create_printer()
        if not self._choose_printer(printer):
            self.status_label.setText("System print dialog cancelled. No pages submitted.")
            return
        approved, output_revision = self._approve_pdf_output(printer)
        if not approved:
            self.status_label.setText("PDF overwrite declined. No pages submitted; existing output is unchanged.")
            return
        self._running = True
        self.last_result = None
        self.start_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.scale_combo.setEnabled(False)
        self.copies_spin.setEnabled(False)
        self.collate_check.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.cancel_button.show()
        self.progress.setRange(0, 0)
        self.progress.show()
        self.status_label.setText("Preparing submission…")
        self._worker.start(printer, self._documents, self.scale_combo.currentData(), output_revision)

    @Slot()
    def cancel(self):
        if self._running:
            self._worker.cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText(
                "Cancellation requested. Waiting for the current native operation; already-spooled pages cannot be recalled."
            )

    @Slot(int, int, str)
    def _progress(self, submitted, total, message):
        self.progress.setRange(0, total)
        self.progress.setValue(submitted)
        if not self._worker.cancelled.is_set():
            self.status_label.setText(f"{submitted} / {total} pages handed to the print engine. {message}")

    @Slot(object)
    def _finished(self, result):
        self._worker.thread.join()
        self._running = False
        self.last_result = result
        self.progress.setRange(0, max(1, result.total_pages))
        self.progress.setValue(result.submitted_pages)
        self.cancel_button.hide()
        self.close_button.setEnabled(True)
        self.start_button.setEnabled(True)
        self.scale_combo.setEnabled(True)
        self.copies_spin.setEnabled(True)
        self.collate_check.setEnabled(True)
        prefix = f"{result.submitted_pages} / {result.total_pages} pages handed to the Qt print engine. "
        if result.error:
            if result.output_path:
                detail = f"PDF output was committed to {filename_text(result.output_path)}; a subsequent cleanup failed."
            else:
                detail = ("No PDF output was committed; this job did not replace the destination." if result.pdf_output
                          else "Delivery is unverified; partial output may exist.")
            message = prefix + f"Submission failed: {result.error}. {detail}"
        elif result.cancelled:
            detail = ("No PDF output was committed; this job did not replace the destination." if result.pdf_output
                      else "Already-spooled pages cannot be recalled; partial output may exist.")
            message = prefix + "Submission cancelled. " + detail
        elif result.output_path:
            message = prefix + f"PDF output saved: {filename_text(result.output_path)}. No physical printing was requested."
        else:
            message = prefix + "Job submission finished. This is not confirmation of physical printing."
        self.status_label.setText(message)
        self.completed.emit(result)

    def reject(self):
        if self._running:
            self.cancel()
            return
        self._worker.shutdown()
        super().reject()

    def closeEvent(self, event):
        if self._running:
            self.cancel()
            event.ignore()
        else:
            self._worker.shutdown()
            super().closeEvent(event)

    def shutdown(self):
        self._worker.shutdown()
