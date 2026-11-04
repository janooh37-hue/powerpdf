"""Virtualized page editing: stable core IDs, viewport-only rasters and snapshot saves.

The grid owns a 32 MiB QImage LRU. Its worker renders uncached through the shared
RenderSession so a second full thumbnail cache does not double that budget.
Labels distinguish added clockwise rotation from total source-plus-edit rotation.
Native rotations are cached by the core source model, never read during painting.
"""
from __future__ import annotations

import json
import math
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel, QEvent, QItemSelectionModel, QMimeData, QModelIndex,
    QObject, QRect, QSize, Qt, QTimer, Signal, Slot,
)
from PySide6.QtGui import QDrag, QImage, QKeySequence, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QLabel, QListView, QMessageBox, QProgressBar, QPushButton, QStyle,
    QStyledItemDelegate, QVBoxLayout,
)

from powerpdf_core import files
from powerpdf_core.jobs import CancelToken, JobCancelled, JobOutcome, JobResult
from powerpdf_core.outputs import OutputCommitError, destination_lock
from powerpdf_core.pages import PageEditSession, PageExportSnapshot
from powerpdf_core.render import RenderSession, THUMBNAIL_CACHE_BYTES
from .operations import _signature
from .text import filename_text

CELL = QSize(188, 256)
THUMB = QSize(160, 190)
MIME = "application/x-powerpdf-page-ids"


def _label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class _Signals(QObject):
    loaded = Signal(object, object, str)
    image = Signal(object, object)
    failed = Signal(str)
    progress = Signal(object)
    saved = Signal(object, object)


class _ThumbnailWorker:
    """One active raster and a replaceable viewport mailbox; owns PDFium lifetime."""

    def __init__(self, path, signals):
        self.signals = signals
        self.path = path
        self.condition = threading.Condition()
        self.pending = ()
        self.wanted = frozenset()
        self.stopping = False
        self.thread = threading.Thread(target=self._run, name="PowerPDF-page-thumbnails")
        self.thread.start()

    def submit(self, keys):
        with self.condition:
            self.wanted = frozenset(keys)
            self.pending = tuple(keys)
            self.condition.notify()

    def shutdown(self):
        with self.condition:
            self.stopping = True
            self.pending = ()
            self.condition.notify()
        self.thread.join()

    def _current(self, key):
        with self.condition:
            return not self.stopping and key in self.wanted

    def _run(self):
        try:
            edit = PageEditSession(self.path)
            with RenderSession(edit.source_bytes) as renderer:
                with self.condition:
                    if self.stopping:
                        return
                self.signals.loaded.emit(edit, renderer.info, "")
                while True:
                    with self.condition:
                        self.condition.wait_for(lambda: self.stopping or self.pending)
                        if self.stopping:
                            return
                        key, *rest = self.pending
                        self.pending = tuple(rest)
                    if not self._current(key):
                        continue
                    try:
                        page, turns, dpr = key
                        width, height = renderer.info.page_sizes[page]
                        if turns % 2:
                            width, height = height, width
                        scale = min(THUMB.width() * dpr / width, THUMB.height() * dpr / height)
                        pixels = renderer.render(page, scale=scale, rotation=turns * 90,
                                                 purpose="pages-thumbnail", max_pixels=1_000_000)
                        try:
                            data = pixels.tobytes()
                            image = QImage(data, pixels.width, pixels.height, pixels.width * 3,
                                           QImage.Format.Format_RGB888).copy()
                            image.setDevicePixelRatio(dpr)
                        finally:
                            pixels.close()
                        if self._current(key):
                            self.signals.image.emit(key, image)
                    except Exception as error:
                        if self._current(key):
                            self.signals.failed.emit(f"Thumbnail failed: {error}")
        except Exception as error:
            self.signals.loaded.emit(None, None, str(error))


class PageGridModel(QAbstractListModel):
    """No per-page widgets or icons: the delegate borrows bounded owned images."""

    moved = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = None
        self.identity = uuid.uuid4().hex
        self.images = OrderedDict()
        self.cache_bytes = 0
        self.cache_budget = THUMBNAIL_CACHE_BYTES
        self.dpr = 1.0

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() or self.session is None else len(self.session.order)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < self.rowCount():
            return None
        page = self.session.order[index.row()]
        added = self.session.turns.get(page, 0) * 90
        native = self.session.source_rotations[page]
        total = (native + added) % 360
        if role == Qt.ItemDataRole.DisplayRole:
            return f"{index.row() + 1} · source {page + 1}\n+{added}° ({total}° total)"
        if role in (Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.AccessibleTextRole,
                    Qt.ItemDataRole.AccessibleDescriptionRole):
            return (f"Working page {index.row() + 1}; original page {page + 1}; "
                    f"source rotation {native}°; additional rotation {added}°; "
                    f"total rotation {total}° clockwise")
        if role == Qt.ItemDataRole.UserRole:
            return page
        if role == Qt.ItemDataRole.SizeHintRole:
            return CELL
        return None

    def key(self, page):
        return page, self.session.turns.get(page, 0) % 4, self.dpr

    def image(self, page):
        key = self.key(page)
        image = self.images.get(key)
        if image is not None:
            self.images.move_to_end(key)
        return image

    def put_image(self, key, image):
        cost = image.sizeInBytes()
        if cost > self.cache_budget:
            return
        previous = self.images.pop(key, None)
        if previous is not None:
            self.cache_bytes -= previous.sizeInBytes()
        while self.images and self.cache_bytes + cost > self.cache_budget:
            _, old = self.images.popitem(last=False)
            self.cache_bytes -= old.sizeInBytes()
        self.images[key] = image
        self.cache_bytes += cost
        page = key[0]
        if self.session is not None and page in self.session.order:
            index = self.index(self.session.order.index(page))
            self.dataChanged.emit(index, index)

    def refresh(self, session):
        self.beginResetModel()
        self.session = session
        self.endResetModel()

    def flags(self, index):
        flags = Qt.ItemFlag.ItemIsDropEnabled
        if index.isValid():
            flags |= Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
        return flags

    def mimeTypes(self):
        return [MIME]

    def supportedDropActions(self):
        return Qt.DropAction.MoveAction

    def mimeData(self, indexes):
        mime = QMimeData()
        pages = [self.session.order[row] for row in sorted({index.row() for index in indexes if index.isValid()})]
        mime.setData(MIME, json.dumps({"session": self.identity, "pages": pages}).encode())
        return mime

    def _drop_pages(self, data):
        if self.session is None or not data.hasFormat(MIME):
            return None
        try:
            payload = json.loads(bytes(data.data(MIME)))
            pages = payload["pages"]
            if payload["session"] != self.identity or not isinstance(pages, list) or not pages:
                return None
            if any(type(page) is not int or page not in self.session.order for page in pages):
                return None
            return pages if len(pages) == len(set(pages)) else None
        except (ValueError, TypeError, KeyError):
            return None

    def canDropMimeData(self, data, action, row, column, parent):
        return (action == Qt.DropAction.MoveAction and column in (-1, 0)
                and -1 <= row <= self.rowCount() and self._drop_pages(data) is not None)

    def dropMimeData(self, data, action, row, column, parent):
        if not self.canDropMimeData(data, action, row, column, parent):
            return False
        if row < 0:
            row = parent.row() if parent.isValid() else self.rowCount()
        before = self.session.order[row] if row < self.rowCount() else None
        self.session.move_before(before, self._drop_pages(data))
        self.moved.emit()
        return True


class _PageDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return CELL

    def paint(self, painter, option, index):
        model = index.model()
        page = model.session.order[index.row()]
        painter.save()
        rect = option.rect.adjusted(4, 4, -4, -4)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.fillRect(rect, option.palette.highlight() if selected else option.palette.base())
        image = model.image(page)
        area = QRect(rect.left() + 10, rect.top() + 9, rect.width() - 20, THUMB.height())
        if image is not None:
            size = image.deviceIndependentSize().toSize()
            target = QRect(0, 0, size.width(), size.height())
            target.moveCenter(area.center())
            painter.drawImage(target, image)
        else:
            painter.setPen(option.palette.highlightedText().color() if selected else option.palette.text().color())
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "Loading page…")
        painter.setPen(option.palette.highlightedText().color() if selected else option.palette.text().color())
        label = QRect(rect.left() + 4, rect.bottom() - 48, rect.width() - 8, 46)
        painter.drawText(label, Qt.AlignmentFlag.AlignCenter, index.data())
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setPen(QPen(option.palette.highlightedText().color() if selected else option.palette.highlight().color(), 2))
            painter.drawRect(rect.adjusted(1, 1, -1, -1))
        painter.restore()


class PageGrid(QListView):
    viewport_changed = Signal()
    reorder_requested = Signal(int)
    delete_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setWrapping(True)
        self.setGridSize(CELL)
        self.setUniformItemSizes(True)
        self.setLayoutMode(QListView.LayoutMode.Batched)
        self.setBatchSize(100)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setItemDelegate(_PageDelegate(self))
        self.setAccessibleName("PDF pages; select pages to rotate, delete, extract or reorder")
        self.verticalScrollBar().valueChanged.connect(self.viewport_changed)
        self.horizontalScrollBar().valueChanged.connect(self.viewport_changed)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.viewport_changed.emit()

    def showEvent(self, event):
        super().showEvent(event)
        self.viewport_changed.emit()

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.Type.DevicePixelRatioChange:
            self.viewport_changed.emit()
        return result

    def keyPressEvent(self, event):
        if event.modifiers() == Qt.KeyboardModifier.ShiftModifier and event.key() in (
            Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
        ):
            self.reorder_requested.emit(-1 if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up) else 1)
            event.accept()
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and not event.modifiers():
            self.delete_requested.emit()
            event.accept()
        else:
            super().keyPressEvent(event)

    def startDrag(self, supported_actions):
        # Own the drag lifecycle: QListView's default Move cleanup must not
        # remove rows a second time after our stable-ID core command.
        indexes = self.selectedIndexes()
        if not indexes:
            return
        drag = QDrag(self)
        drag.setMimeData(self.model().mimeData(indexes))
        drag.exec(Qt.DropAction.MoveAction)

    def dropEvent(self, event):
        model = self.model()
        if event.source() is not self or model._drop_pages(event.mimeData()) is None:
            event.ignore()
            return
        position = event.position().toPoint()
        index = self.indexAt(position)
        row = index.row() if index.isValid() else model.rowCount()
        if index.isValid() and position.x() > self.visualRect(index).center().x():
            row += 1
        if model.dropMimeData(event.mimeData(), Qt.DropAction.MoveAction, row, 0, QModelIndex()):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def visible_rows(self):
        """Arithmetic fixed-cell bounds: no scan over a 10,000-page model."""
        if self.model() is None or not self.model().rowCount():
            return ()
        columns = max(1, self.viewport().width() // CELL.width())
        top = max(0, self.verticalScrollBar().value() // CELL.height())
        bottom = (self.verticalScrollBar().value() + self.viewport().height()) // CELL.height()
        return tuple(range(top * columns, min(self.model().rowCount(), (bottom + 1) * columns)))


@dataclass(frozen=True)
class _ExportPlan:
    snapshot: PageExportSnapshot
    output: Path
    signatures: tuple
    extract: bool

    @property
    def outputs(self):
        return (self.output,)


def _export_plan(session, destination, *, extract=False):
    requested = Path(destination).expanduser().absolute()
    output = files.validate_output(requested, (session.path,) if session.path is not None else ())
    if not output.parent.is_dir():
        raise ValueError("Choose an existing destination folder")
    snapshot = session.snapshot(session.targets() if extract else None)
    if not snapshot.pages:
        raise ValueError("No pages left to save")
    signatures = tuple((path, _signature(path)) for path in dict.fromkeys((requested, output)))
    return _ExportPlan(snapshot, output, signatures, extract)


def _execute_export(plan, cancel, progress):
    outputs, error, status = (), None, "success"

    def check_destinations():
        for path, signature in plan.signatures:
            if _signature(path) != signature:
                raise ValueError("Destination changed after review; review again")

    try:
        with destination_lock(plan.output):
            cancel.raise_if_cancelled()
            check_destinations()
            outputs = (plan.snapshot.write(plan.output, cancel=cancel, progress=progress,
                                           before_commit=check_destinations),)
    except Exception as exc:
        outputs = tuple(exc.committed) if isinstance(exc, OutputCommitError) else ()
        error = str(exc)
        cause = exc.cause if isinstance(exc, OutputCommitError) else exc
        status = "cancelled" if isinstance(cause, JobCancelled) else "error"
    size = 0
    for path in outputs:
        try:
            size += path.stat().st_size
        except OSError:
            pass  # Preserve actual commit receipts even if an output was moved externally.
    inputs = (plan.snapshot.path,) if plan.snapshot.path is not None else ()
    return JobResult((JobOutcome(inputs, outputs, pages=len(plan.snapshot.pages), output_bytes=size,
                                 error=error, status=status),), status == "cancelled")


class PagesWindow(QDialog):
    """One highlighted PDF. completed(JobResult) contains actual committed outputs."""

    completed = Signal(object)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = Path(path).expanduser().absolute()
        self.session = None
        self.info = None
        self.job_result = None
        self._saved_state = ()
        self._closed = False
        self._loading = True
        self._saving = False
        self._syncing = False
        self._save_thread = None
        self._cancel = CancelToken()
        self._wanted = frozenset()
        self._signals = _Signals(self)
        self._signals.loaded.connect(self._loaded)
        self._signals.image.connect(self._image)
        self._signals.failed.connect(self._failed)
        self._signals.progress.connect(self._progress)
        self._signals.saved.connect(self._saved)
        self.setWindowTitle("Pages · " + filename_text(self.path.name))
        self.resize(1040, 780)
        layout = QVBoxLayout(self)
        layout.addWidget(_label("PowerPDF · Pages · " + filename_text(self.path.name)))
        controls = QHBoxLayout()
        self.rotate_button = QPushButton("Rotate 90°")
        self.delete_button = QPushButton("Delete pages")
        self.undo_button = QPushButton("Undo")
        self.redo_button = QPushButton("Redo")
        for button, action in ((self.rotate_button, lambda: self.command("rotate")),
                               (self.delete_button, lambda: self.command("delete")),
                               (self.undo_button, lambda: self.command("undo")),
                               (self.redo_button, lambda: self.command("redo"))):
            button.clicked.connect(action)
            button.setAutoDefault(False)
            controls.addWidget(button)
        controls.addStretch()
        self.extract_button = QPushButton("Extract…")
        self.save_button = QPushButton("Save edited")
        self.save_as_button = QPushButton("Save As…")
        self.extract_button.clicked.connect(lambda: self.save(extract=True))
        self.save_button.clicked.connect(lambda: self.save())
        self.save_as_button.clicked.connect(lambda: self.save(choose=True))
        for button in (self.extract_button, self.save_button, self.save_as_button):
            button.setAutoDefault(False)
            controls.addWidget(button)
        layout.addLayout(controls)
        self.summary = _label("Loading immutable source bytes…")
        layout.addWidget(self.summary)
        self.model = PageGridModel(self)
        self.grid = PageGrid(self)
        self.grid.setModel(self.model)
        layout.addWidget(self.grid, 1)
        self.grid.selectionModel().selectionChanged.connect(self._selection)
        self.grid.selectionModel().currentChanged.connect(self._selection)
        self.grid.reorder_requested.connect(lambda step: self.command("reorder", step))
        self.grid.delete_requested.connect(lambda: self.command("delete"))
        self.model.moved.connect(self.refresh)
        self._viewport_timer = QTimer(self)
        self._viewport_timer.setSingleShot(True)
        self._viewport_timer.setInterval(25)
        self._viewport_timer.timeout.connect(self.request_thumbnails)
        self.grid.viewport_changed.connect(self._viewport_timer.start)
        layout.addWidget(_label("Drag to reorder · Shift+arrows move selected pages · Ctrl/Cmd+click selects multiple · Delete removes from this copy only"))
        footer = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.cancel_button = QPushButton("Cancel save")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_work)
        footer.addWidget(self.progress_bar, 1)
        footer.addWidget(self.cancel_button)
        layout.addLayout(footer)
        self.status_label = _label("Loading PDF… Original will never be overwritten.")
        layout.addWidget(self.status_label)
        for key, action in ((QKeySequence.StandardKey.Undo, lambda: self.command("undo")),
                            (QKeySequence.StandardKey.Redo, lambda: self.command("redo")),
                            (QKeySequence.StandardKey.Save, lambda: self.save()),
                            (QKeySequence.StandardKey.SaveAs, lambda: self.save(choose=True))):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(action)
        self._update_controls()
        self._worker = _ThumbnailWorker(self.path, self._signals)

    @property
    def dirty(self):
        return self.session is not None and self.session.state != self._saved_state

    @property
    def busy(self):
        return self._loading or self._saving

    @Slot(object, object, str)
    def _loaded(self, session, info, error):
        if self._closed:
            return
        self._loading = False
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        if session is None:
            self.status_label.setText("Cannot open pages: " + error)
            self.summary.setText("PDF could not be loaded")
            self._update_controls()
            return
        self.session, self.info = session, info
        self._saved_state = session.state
        self.status_label.setText("Original preserved · select pages or use the current page")
        self.refresh()

    def _update_controls(self):
        has_pages = self.session is not None and bool(self.session.order) and not self._closed
        for button in (self.rotate_button, self.delete_button):
            button.setEnabled(has_pages)
        for button in (self.extract_button, self.save_button, self.save_as_button):
            button.setEnabled(has_pages and not self._saving)
        self.undo_button.setEnabled(self.session is not None and self.session.can_undo and not self._closed)
        self.redo_button.setEnabled(self.session is not None and self.session.can_redo and not self._closed)

    def _selection(self, *_args):
        if self._syncing or self.session is None:
            return
        self.session.picked = {self.session.order[index.row()] for index in self.grid.selectionModel().selectedIndexes()}
        current = self.grid.currentIndex()
        if current.isValid():
            self.session.cursor = current.row()
        self._summary()

    def _summary(self):
        if self.session is None:
            return
        count = len(self.session.order)
        current = self.session.cursor + 1 if count else 0
        self.summary.setText(f"{count:,} of {self.session.page_count:,} pages · current {current} · "
                             f"{len(self.session.picked)} selected · " + ("Unsaved edits" if self.dirty else "Saved state"))
        self.setWindowTitle(("* " if self.dirty else "") + "Pages · " + filename_text(self.path.name))

    def refresh(self):
        if self.session is None or self._closed:
            return
        self._syncing = True
        self.model.refresh(self.session)
        selection = self.grid.selectionModel()
        for row, page in enumerate(self.session.order):
            if page in self.session.picked:
                selection.select(self.model.index(row), QItemSelectionModel.SelectionFlag.Select)
        if self.session.order:
            selection.setCurrentIndex(self.model.index(self.session.cursor), QItemSelectionModel.SelectionFlag.NoUpdate)
        self._syncing = False
        self._summary()
        self._update_controls()
        self._viewport_timer.start()

    def command(self, name, *args):
        if self.session is None or self._closed:
            return
        getattr(self.session, name)(*args)
        self.refresh()

    def request_thumbnails(self):
        if self.session is None or self._closed:
            return
        requested_dpr = min(4.0, max(1.0, round(self.grid.devicePixelRatioF(), 3)))
        visible = self.grid.visible_rows()
        # Four stable neighbors on either side, not an entire hidden page row.
        neighbors = (() if not visible else tuple(range(max(0, visible[0] - 4), visible[0]))
                     + tuple(range(visible[-1] + 1, min(len(self.session.order), visible[-1] + 5))))
        # Every wanted raster must fit together, including at high desktop DPR.
        # Four bytes/pixel plus rounding room conservatively covers owned RGB
        # storage. Otherwise newly prefetched neighbors can evict visible pages.
        count = max(1, len(visible) + len(neighbors))
        budget_dpr = math.sqrt(self.model.cache_budget / (count * (THUMB.width() + 2) * (THUMB.height() + 2) * 4))
        self.model.dpr = max(0.001, math.floor(min(requested_dpr, budget_dpr) * 1000) / 1000)
        keys = tuple(self.model.key(self.session.order[row]) for row in (*visible, *neighbors))
        self._wanted = frozenset(keys)
        for key in keys:
            if key in self.model.images:
                self.model.images.move_to_end(key)
        self._worker.submit(tuple(key for key in keys if key not in self.model.images))

    @Slot(object, object)
    def _image(self, key, image):
        if not self._closed and key in self._wanted:
            self.model.put_image(key, image)

    @Slot(str)
    def _failed(self, error):
        if not self._closed:
            self.status_label.setText(error)

    def default_destination(self, *, extract=False):
        return self.path.with_name(self.path.stem + ("_extract.pdf" if extract else "_edited.pdf"))

    def review_export(self, plan):
        dialog = QDialog(self)
        dialog.setWindowTitle("Review page output")
        layout = QVBoxLayout(dialog)
        layout.addWidget(_label(f"{'Extract' if plan.extract else 'Save'} {len(plan.snapshot.pages)} page(s), in working order. Original remains unchanged."))
        layout.addWidget(_label(filename_text(plan.output)))
        layout.addWidget(_label("Existing output will be replaced." if plan.output.exists() else "New PDF output."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        buttons.button(QDialogButtonBox.StandardButton.Save).setAutoDefault(False)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def save(self, destination=None, *, extract=False, choose=False):
        if self.session is None or self.busy or self._closed or not self.session.order:
            return
        default = self.default_destination(extract=extract)
        if choose:
            chosen, _ = QFileDialog.getSaveFileName(self, "Extract pages" if extract else "Save edited PDF",
                                                   str(default), "PDF (*.pdf)",
                                                   options=QFileDialog.Option.DontConfirmOverwrite)
            if not chosen:
                return
            destination = chosen
        try:
            plan = _export_plan(self.session, destination or default, extract=extract)
            if not self.review_export(plan):
                return
        except (OSError, ValueError) as error:
            self.status_label.setText("Cannot save pages: " + str(error))
            return
        self._saving = True
        self._cancel = CancelToken()
        self.progress_bar.setRange(0, len(plan.snapshot.pages))
        self.progress_bar.setValue(0)
        self.cancel_button.setEnabled(True)
        self._update_controls()
        self.status_label.setText("Saving immutable snapshot… Editing can continue; later edits are not included.")
        signals, cancel = self._signals, self._cancel
        def write():
            result = _execute_export(plan, cancel, signals.progress.emit)
            signals.saved.emit(result, plan)
        self._save_thread = threading.Thread(target=write, name="PowerPDF-pages-save")
        self._save_thread.start()

    @Slot(object)
    def _progress(self, event):
        if not self._closed:
            self.progress_bar.setRange(0, event.total)
            self.progress_bar.setValue(event.completed)

    @Slot(object, object)
    def _saved(self, result, plan):
        self._save_thread.join()
        self._save_thread = None
        self._saving = False
        self.job_result = result
        if result.success and not plan.extract:
            self._saved_state = plan.snapshot.state
        if not self._closed:
            self.cancel_button.setEnabled(False)
            errors = "; ".join(item.error for item in result.outcomes if item.error)
            self.status_label.setText((f"Saved {len(result.outputs)} PDF · {result.output_bytes:,} bytes · "
                                       + filename_text(result.outputs[0]) if result.success else
                                       ("Cancelled" if result.cancelled else "Save failed") + (" · " + errors if errors else "")))
            self._summary()
            self._update_controls()
        self.completed.emit(result)

    def cancel_work(self):
        self._cancel.cancel()
        self.status_label.setText("Cancelling between page operations; unfinished output will not replace an existing PDF.")

    def _confirm_close(self):
        if not self.dirty:
            return True
        return QMessageBox.question(self, "Discard unsaved page edits?",
                                    "Discard unsaved order, rotation and deletion changes? Original PDF is unchanged.",
                                    QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                    QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Discard

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._viewport_timer.stop()
        self._cancel.cancel()
        self._worker.shutdown()
        if self._save_thread is not None:
            self._save_thread.join()
        self.model.images.clear()
        self.model.cache_bytes = 0

    def reject(self):
        if self._closed or self._confirm_close():
            self.shutdown()
            super().reject()

    def closeEvent(self, event):
        if self._closed or self._confirm_close():
            self.shutdown()
            super().closeEvent(event)
        else:
            event.ignore()
