"""Native page viewport with one coalescing, cooperatively stopped render lane.

Only RenderSession touches PDFium. The worker owns its session and returns an
owned QImage; QPixmap creation and every widget operation stay on the GUI thread.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QImage, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from powerpdf_core.render import DocumentInfo, RenderSession
from .text import filename_text

# At most 48 MB of four-byte pixels per screen raster, before transfer/display
# copies. This is a screen-preview policy, not the core's export resolution.
SCREEN_PIXEL_BUDGET = 12_000_000
SCREEN_MAX_DIMENSION = 32_768
POINTS_TO_DIP = 96 / 72


@dataclass(frozen=True)
class _Request:
    serial: int
    path: Path | None
    page: int = 0
    scale: float | None = None
    info: DocumentInfo | None = None


class _Results(QObject):
    info = Signal(int, object, object)
    image = Signal(int, object, int, object, float, bool)
    failed = Signal(int, str)


def _screen_scale(width: float, height: float, requested: float) -> tuple[float, bool]:
    if not all(math.isfinite(value) and value > 0 for value in (width, height, requested)):
        raise ValueError("Page dimensions and preview scale must be finite and positive")
    # Leave room for PDFium's ceil-to-pixel rounding on both dimensions.
    scale = min(
        requested,
        (math.sqrt(SCREEN_PIXEL_BUDGET) - 2) / (math.sqrt(width) * math.sqrt(height)),
        (SCREEN_MAX_DIMENSION - 1) / max(width, height),
    )
    return scale, scale < requested


class _RenderWorker:
    """One active request and one replaceable pending request, never an executor.

    The condition protects the mailbox only; it is never held during native work
    or signal delivery. Shutdown does not depend on the Qt event loop, so joining
    from closeEvent cannot deadlock on a queued GUI callback.
    """

    def __init__(self, results: _Results) -> None:
        self._results = results
        self._condition = threading.Condition()
        self._pending: _Request | None = None
        self._latest = 0
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="PowerPDF-preview")
        self._thread.start()

    def invalidate(self, serial: int) -> None:
        with self._condition:
            self._latest = serial
            self._pending = None

    def submit(self, request: _Request) -> None:
        with self._condition:
            if not self._stopping:
                self._latest = request.serial
                self._pending = request
                self._condition.notify()

    def _current(self, serial: int) -> bool:
        with self._condition:
            return not self._stopping and serial == self._latest

    def shutdown(self, *_ignored) -> None:
        with self._condition:
            self._stopping = True
            self._pending = None
            self._condition.notify()
        self._thread.join()

    def _run(self) -> None:
        session: RenderSession | None = None
        source: Path | None = None
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._stopping or self._pending is not None)
                    if self._stopping:
                        return
                    request = self._pending
                    self._pending = None
                assert request is not None
                try:
                    if request.path is None:
                        if session is not None:
                            session.close()
                            session = None
                        source = None
                        continue
                    changed = session is None or source != request.path
                    if not changed:
                        changed = not session.source_unchanged()
                    if changed:
                        if session is not None:
                            session.close()
                            session = None
                        source = request.path
                        if not self._current(request.serial):
                            continue
                        session = RenderSession(source)
                    if not self._current(request.serial):
                        continue
                    # A changed revision may have different dimensions/page count.
                    # Let the GUI calculate a new layout request from fresh info.
                    if changed or request.scale is None or request.info is not session.info:
                        self._results.info.emit(request.serial, request.path, session.info)
                        continue
                    width, height = session.info.page_sizes[request.page]
                    scale, limited = _screen_scale(width, height, request.scale)
                    pixels = session.render(
                        request.page, scale=scale, purpose="preview", max_pixels=SCREEN_PIXEL_BUDGET
                    )
                    try:
                        if not self._current(request.serial):
                            continue
                        # RGB888 is tightly packed; copy() detaches Qt from Python's
                        # temporary bytes and the independently owned PIL image.
                        data = pixels.tobytes()
                        image = QImage(
                            data, pixels.width, pixels.height, pixels.width * 3,
                            QImage.Format.Format_RGB888,
                        ).copy()
                    finally:
                        pixels.close()
                    if self._current(request.serial):
                        self._results.image.emit(
                            request.serial, request.path, request.page, image, scale, limited
                        )
                except Exception as exc:
                    if session is not None:
                        session.close()
                        session = None
                    if self._current(request.serial):
                        self._results.failed.emit(request.serial, f"{type(exc).__name__}: {exc}")
        finally:
            if session is not None:
                session.close()


class PreviewPane(QWidget):
    """A page preview; page_index is zero-based and zoom=1 means 100% at 96 dpi.

    info_ready(Path, DocumentInfo) reports immutable metadata before the first
    image. ready(Path) reports each displayed image; error(str) also appears in
    status_label. Source paths retain their logical spelling, not resolve().
    shutdown() is idempotent and permanently stops this pane's worker; it waits
    for an in-flight native call, which cannot be forcibly interrupted safely.
    set_page() uses zero-based indices. zoom_mode is "page", "width", or None
    for a custom zoom; zoom_mode_changed reports user/programmatic mode changes,
    not the independent scale calculations needed to fit different page sizes.
    normalized_pan is each scrollbar's fraction of its available travel (0.5
    when that axis fits). pan_changed reports scrolling/dragging, not layout or
    programmatic synchronization, so two panes can link without feedback loops.
    """

    info_ready = Signal(object, object)
    ready = Signal(object)
    error = Signal(str)
    page_changed = Signal(int)
    zoom_changed = Signal(float)
    zoom_mode_changed = Signal(object)
    pan_changed = Signal(float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("previewPane")
        self.setAccessibleName("PDF page preview")
        self._path: Path | None = None
        self._info: DocumentInfo | None = None
        self._page = 0
        self._zoom = 1.0
        self._fit: str | None = "page"
        self._serial = 0
        self._closed = False
        self._image = QImage()
        self._center_next = True
        self._last_render_scale = 0.0
        self._pan_blocked = False
        self._pending_pan: tuple[float, float] | None = None

        self.title_label = QLabel("No document selected", self)
        self.title_label.setObjectName("previewTitle")
        self.title_label.setAccessibleName("Preview document name")
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.title_label.setWordWrap(True)
        self.status_label = QLabel("Choose a PDF to preview its pages.", self)
        self.status_label.setObjectName("previewStatus")
        self.status_label.setAccessibleName("Preview status")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)

        self.view = QGraphicsView(self)
        self.view.setObjectName("pageViewport")
        self.view.setAccessibleName("Document page; drag to pan")
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.view.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setMinimumSize(100, 100)
        self._scene = QGraphicsScene(self)
        self.view.setScene(self._scene)
        self._item = QGraphicsPixmapItem()
        self._scene.addItem(self._item)
        self.view.viewport().installEventFilter(self)
        self.view.horizontalScrollBar().valueChanged.connect(self._scroll_changed)
        self.view.verticalScrollBar().valueChanged.connect(self._scroll_changed)

        self.previous_button = self._button("Previous", "Previous page", self.previous_page)
        self.next_button = self._button("Next", "Next page", self.next_page)
        self.page_spin = QSpinBox(self)
        self.page_spin.setObjectName("pageNumber")
        self.page_spin.setAccessibleName("Page number")
        self.page_spin.setKeyboardTracking(False)
        self.page_spin.setRange(0, 0)
        self.page_spin.valueChanged.connect(self._select_page)
        self.page_total = QLabel("/ 0", self)
        self.page_total.setAccessibleName("Total page count")
        self.zoom_out_button = self._button("−", "Zoom out", self.zoom_out)
        self.zoom_in_button = self._button("+", "Zoom in", self.zoom_in)
        self.zoom_label = QLabel("100%", self)
        self.zoom_label.setAccessibleName("Preview zoom percentage")
        self.zoom_label.setMinimumWidth(48)
        self.fit_page_button = self._button("Fit page", "Fit whole page", self.fit_page)
        self.fit_width_button = self._button("Fit width", "Fit page width", self.fit_width)
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)
        for widget in (
            self.previous_button, self.page_spin, self.page_total, self.next_button,
        ):
            controls.addWidget(widget)
        controls.addStretch()
        zoom_controls = QHBoxLayout()
        zoom_controls.setSpacing(6)
        zoom_controls.addStretch()
        for widget in (
            self.zoom_out_button, self.zoom_label, self.zoom_in_button,
            self.fit_page_button, self.fit_width_button,
        ):
            zoom_controls.addWidget(widget)
        zoom_controls.addStretch()
        self._control_widgets = (
            self.previous_button, self.page_spin, self.page_total, self.next_button,
            self.zoom_out_button, self.zoom_label, self.zoom_in_button,
            self.fit_page_button, self.fit_width_button,
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)
        layout.addWidget(self.title_label)
        layout.addWidget(self.view, 1)
        layout.addLayout(controls)
        layout.addLayout(zoom_controls)
        layout.addWidget(self.status_label)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(90)
        self._timer.timeout.connect(self._render)
        self._results = _Results(self)
        self._results.info.connect(self._receive_info, Qt.ConnectionType.QueuedConnection)
        self._results.image.connect(self._receive_image, Qt.ConnectionType.QueuedConnection)
        self._results.failed.connect(self._receive_error, Qt.ConnectionType.QueuedConnection)
        self._worker = _RenderWorker(self._results)
        # A parent's deletion does not necessarily send closeEvent to children.
        self.destroyed.connect(self._worker.shutdown)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)
        self._update_controls()

    def _button(self, text: str, name: str, callback) -> QPushButton:
        button = QPushButton(text, self)
        button.setAccessibleName(name)
        button.setToolTip(name)
        button.clicked.connect(callback)
        return button

    @property
    def source_path(self) -> Path | None:
        return self._path

    @property
    def document_info(self) -> DocumentInfo | None:
        return self._info

    @property
    def page_count(self) -> int:
        return self._info.page_count if self._info is not None else 0

    @property
    def page_index(self) -> int:
        return self._page

    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def zoom_mode(self) -> str | None:
        return self._fit

    @property
    def normalized_pan(self) -> tuple[float, float]:
        values = []
        for bar in (self.view.horizontalScrollBar(), self.view.verticalScrollBar()):
            travel = bar.maximum() - bar.minimum()
            values.append((bar.value() - bar.minimum()) / travel if travel else 0.5)
        return values[0], values[1]

    def set_controls_visible(self, visible: bool) -> None:
        """Let a containing workspace supply one shared navigation toolbar."""
        for widget in self._control_widgets:
            widget.setVisible(visible)

    def set_normalized_pan(self, x: float, y: float) -> None:
        """Set linked scroll travel; retain it through an in-flight raster."""
        if self._closed:
            return
        if not all(math.isfinite(value) for value in (x, y)):
            raise ValueError("Pan coordinates must be finite")
        self._pending_pan = min(1.0, max(0.0, x)), min(1.0, max(0.0, y))
        if not self._image.isNull():
            self._apply_pan(self._pending_pan)

    def _apply_pan(self, pan: tuple[float, float]) -> None:
        blocked = self._pan_blocked
        self._pan_blocked = True
        try:
            for bar, fraction in zip(
                (self.view.horizontalScrollBar(), self.view.verticalScrollBar()), pan
            ):
                bar.setValue(round(bar.minimum() + fraction * (bar.maximum() - bar.minimum())))
        finally:
            self._pan_blocked = blocked

    def _scroll_changed(self, _value: int) -> None:
        if self._closed or self._pan_blocked or self._center_next or self._image.isNull():
            return
        self._pending_pan = self.normalized_pan
        self.pan_changed.emit(*self._pending_pan)

    def _invalidate(self) -> None:
        self._timer.stop()
        self._serial += 1
        self._worker.invalidate(self._serial)

    @Slot(object)
    def open_path(self, path: Path | str) -> None:
        if self._closed:
            return
        self._invalidate()
        self._path = Path(path).absolute()
        self._info = None
        self._page = 0
        self._fit = "page"
        self._center_next = True
        self._clear_image()
        self.title_label.setText(filename_text(self._path.name))
        self.status_label.setText("Opening document…")
        self._update_controls()
        self._worker.submit(_Request(self._serial, self._path))

    @Slot()
    def clear(self) -> None:
        if self._closed:
            return
        self._invalidate()
        self._path = None
        self._info = None
        self._page = 0
        self._clear_image()
        self.title_label.setText("No document selected")
        self.status_label.setText("Choose a PDF to preview its pages.")
        self._update_controls()
        self._worker.submit(_Request(self._serial, None))

    def _clear_image(self) -> None:
        self._pending_pan = None
        self._image = QImage()
        self._item.setPixmap(QPixmap())
        self._scene.setSceneRect(QRectF())
        self._last_render_scale = 0.0

    @Slot(int, object, object)
    def _receive_info(self, serial: int, path: Path, info: DocumentInfo) -> None:
        if self._closed or serial != self._serial or path != self._path:
            return
        self._info = info
        self._page = min(self._page, max(0, info.page_count - 1))
        self._update_controls()
        self.info_ready.emit(path, info)
        if self._closed or serial != self._serial or path != self._path:
            return
        if info.page_count == 0:
            self._clear_image()
            self.status_label.setText("This PDF has no pages.")
            return
        self.status_label.setText("Rendering page…" if self.isVisible() else "Ready to preview.")
        self._schedule_render()

    def _update_controls(self) -> None:
        available = self.page_count > 0 and not self._closed
        self.page_spin.blockSignals(True)
        self.page_spin.setRange(1 if available else 0, self.page_count if available else 0)
        self.page_spin.setValue(self._page + 1 if available else 0)
        self.page_spin.blockSignals(False)
        self.page_spin.setEnabled(available)
        self.page_total.setText(f"/ {self.page_count}")
        self.previous_button.setEnabled(available and self._page > 0)
        self.next_button.setEnabled(available and self._page + 1 < self.page_count)
        for button in (
            self.zoom_in_button, self.zoom_out_button, self.fit_page_button, self.fit_width_button,
        ):
            button.setEnabled(available)
        self.zoom_label.setText(f"{self._zoom * 100:.0f}%")

    @Slot(int)
    def _select_page(self, number: int) -> None:
        self.set_page(number - 1)

    @Slot(int)
    def set_page(self, page: int) -> None:
        if self._closed or not 0 <= page < self.page_count or page == self._page:
            return
        self._page = page
        self._center_next = True
        self._clear_image()
        self._update_controls()
        self.page_changed.emit(page)
        self.status_label.setText(f"Rendering page {page + 1}…")
        self._schedule_render()

    @Slot()
    def previous_page(self) -> None:
        self._select_page(self._page)

    @Slot()
    def next_page(self) -> None:
        self._select_page(self._page + 2)

    @Slot(float)
    def set_zoom(self, zoom: float) -> None:
        if self._closed or not self.page_count:
            return
        if not math.isfinite(zoom):
            raise ValueError("Zoom must be finite")
        zoom = min(16.0, max(0.05, zoom))
        if self._fit is None and zoom == self._zoom:
            return
        changed_mode = self._fit is not None
        self._fit = None
        self._zoom = zoom
        if changed_mode:
            self.zoom_mode_changed.emit(None)
        self._apply_transform()
        self._schedule_render()

    @Slot()
    def zoom_in(self) -> None:
        self.set_zoom(self._zoom * 1.25)

    @Slot()
    def zoom_out(self) -> None:
        self.set_zoom(self._zoom / 1.25)

    @Slot()
    def fit_page(self) -> None:
        self._set_fit("page")

    @Slot()
    def fit_width(self) -> None:
        self._set_fit("width")

    def _set_fit(self, fit: str) -> None:
        if self._closed or not self.page_count:
            return
        self._fit = fit
        self._center_next = True
        self._pending_pan = None
        self.zoom_mode_changed.emit(fit)
        self._schedule_render()

    def _apply_transform(self) -> None:
        pan = self._pending_pan or self.normalized_pan
        self._pan_blocked = True
        try:
            self.view.setTransform(QTransform.fromScale(
                self._zoom * POINTS_TO_DIP, self._zoom * POINTS_TO_DIP
            ))
            if not self._center_next:
                self._apply_pan(pan)
        finally:
            self._pan_blocked = False
        self.zoom_label.setText(f"{self._zoom * 100:.0f}%")
        self.zoom_changed.emit(self._zoom)

    def _schedule_render(self) -> None:
        if self._closed or not self.page_count:
            return
        self._invalidate()
        if self.isVisible():
            self._timer.start()

    @Slot()
    def _render(self) -> None:
        if self._closed or not self.page_count or not self.isVisible():
            return
        viewport = self.view.viewport()
        width, height = viewport.width(), viewport.height()
        if width <= 1 or height <= 1:
            return
        page_width, page_height = self._info.page_sizes[self._page]
        if not all(math.isfinite(value) and value > 0 for value in (page_width, page_height)):
            self._receive_error(self._serial, "Page dimensions must be finite and positive")
            return
        if self._fit:
            scale = max(1, width - 32) / page_width
            if self._fit == "page":
                scale = min(scale, max(1, height - 32) / page_height)
            self._zoom = scale / POINTS_TO_DIP
        self._apply_transform()
        dpr = viewport.devicePixelRatioF()
        self._worker.submit(_Request(
            self._serial, self._path, self._page, self._zoom * POINTS_TO_DIP * dpr, self._info
        ))

    @Slot(int, object, int, object, float, bool)
    def _receive_image(
        self, serial: int, path: Path, page: int, image: QImage, scale: float, limited: bool
    ) -> None:
        if self._closed or serial != self._serial or path != self._path or page != self._page:
            return
        self._image = image
        self._last_render_scale = scale
        self._pan_blocked = True
        try:
            self._item.setPixmap(QPixmap.fromImage(image))
            self._item.setScale(1 / scale)
            width, height = self._info.page_sizes[page]
            self._scene.setSceneRect(QRectF(0, 0, width, height))
            if self._center_next:
                self.view.centerOn(width / 2, height / 2)
                self._center_next = False
            if self._pending_pan is not None:
                self._apply_pan(self._pending_pan)
        finally:
            self._pan_blocked = False
        message = f"Page {page + 1} of {self.page_count} · {self._zoom * 100:.0f}%"
        if limited:
            message += (
                f" · Preview detail limited to {image.width():,} × {image.height():,} pixels "
                "to protect memory; requested zoom is preserved."
            )
        self.status_label.setText(message)
        self.ready.emit(path)

    @Slot(int, str)
    def _receive_error(self, serial: int, message: str) -> None:
        if self._closed or serial != self._serial:
            return
        self._clear_image()
        self.status_label.setText(f"Cannot preview this PDF. {message}")
        self.error.emit(message)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.view.viewport() and event.type() in (
            QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.DevicePixelRatioChange,
        ):
            self._schedule_render()
        return super().eventFilter(watched, event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._schedule_render()

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)

    @Slot()
    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        self._serial += 1
        self._worker.shutdown()
        self._update_controls()
