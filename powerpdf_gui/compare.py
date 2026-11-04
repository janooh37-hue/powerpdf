"""Read-only original/compressed comparison using the shared screen renderer.

The panes fit their own page dimensions in linked fit modes. Custom zoom uses
one physical scale; pan links each axis's normalized available scroll travel.
This is a visual review workspace, not a document-content comparison algorithm.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from powerpdf_core.files import compare_pair
from .preview import PreviewPane
from .text import filename_text


def _label(text: str, parent: QWidget) -> QLabel:
    label = QLabel(text, parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class CompareWindow(QDialog):
    """Compare the highlighted PDF or its _small partner; never inspect a queue.

    Metadata and images arrive asynchronously. ready() reports that both page
    images are displayed. shutdown() is permanent, idempotent and joins both
    render lanes; finished remains QDialog's normal lifecycle signal.
    """

    ready = Signal()

    def __init__(self, path: Path | str, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName("compareWindow")
        self.setWindowTitle("PowerPDF · Compare")
        self.resize(1440, 940)
        self.path = Path(path).absolute()
        self.original_path: Path | None = None
        self.compressed_path: Path | None = None
        self._closed = False
        self._linking = False
        self._page_count = 0
        self._page = 0
        self._displayed: dict[PreviewPane, int] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)
        self.title_label = _label("Compare original and compressed PDF", self)
        self.title_label.setProperty("role", "title")
        layout.addWidget(self.title_label)
        self.summary_label = _label("Finding the original / _small pair…", self)
        self.summary_label.setAccessibleName("File sizes and signed size change")
        layout.addWidget(self.summary_label)
        self.warning_label = _label("", self)
        self.warning_label.setAccessibleName("Comparison warning")
        self.warning_label.hide()
        layout.addWidget(self.warning_label)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.previous_button = self._button("Previous", "Previous linked page · [", self.previous_page)
        self.next_button = self._button("Next", "Next linked page · ]", self.next_page)
        self.page_spin = QSpinBox(self)
        self.page_spin.setAccessibleName("Linked page number")
        self.page_spin.setKeyboardTracking(False)
        self.page_spin.setRange(0, 0)
        self.page_spin.valueChanged.connect(lambda value: self.set_page(value - 1))
        self.page_total = _label("/ 0", self)
        for widget in (self.previous_button, self.page_spin, self.page_total, self.next_button):
            controls.addWidget(widget)
        controls.addStretch()
        self.zoom_out_button = self._button("−", "Zoom out both pages · −", self.zoom_out)
        self.zoom_in_button = self._button("+", "Zoom in both pages · +", self.zoom_in)
        self.zoom_spin = QDoubleSpinBox(self)
        self.zoom_spin.setAccessibleName("Custom linked zoom percentage")
        self.zoom_spin.setPrefix("Custom ")
        self.zoom_spin.setSuffix("%")
        self.zoom_spin.setRange(5, 1600)
        self.zoom_spin.setDecimals(1)
        self.zoom_spin.setValue(100)
        self.zoom_spin.setKeyboardTracking(False)
        self.zoom_spin.valueChanged.connect(lambda value: self.set_zoom(value / 100))
        self.fit_page_button = self._button("Fit page", "Fit both whole pages", self.fit_page)
        self.fit_width_button = self._button("Fit width", "Fit both page widths", self.fit_width)
        for widget in (self.zoom_out_button, self.zoom_in_button, self.zoom_spin,
                       self.fit_page_button, self.fit_width_button):
            controls.addWidget(widget)
        layout.addLayout(controls)
        self.zoom_label = _label("Zoom and pan are linked; each fit uses its own page dimensions.", self)
        self.zoom_label.setAccessibleName("Linked zoom mode and actual scales")
        self.zoom_label.setProperty("role", "muted")
        layout.addWidget(self.zoom_label)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        self.original_pane = PreviewPane(self.splitter)
        self.compressed_pane = PreviewPane(self.splitter)
        self.original_pane.setAccessibleName("Original PDF preview")
        self.compressed_pane.setAccessibleName("Compressed PDF preview")
        self.original_pane.view.setAccessibleName("Original page; drag to pan both pages")
        self.compressed_pane.view.setAccessibleName("Compressed page; drag to pan both pages")
        self.splitter.addWidget(self.original_pane)
        self.splitter.addWidget(self.compressed_pane)
        layout.addWidget(self.splitter, 1)
        self.page_counts_label = _label("Opening both documents…", self)
        self.page_counts_label.setAccessibleName("Original and compressed page counts")
        layout.addWidget(self.page_counts_label)
        footer = QHBoxLayout()
        self.status_label = _label("Drag either page or scroll to pan both views. [ / ] change the linked page.", self)
        footer.addWidget(self.status_label, 1)
        self.close_button = self._button("Close", "Close comparison · Escape", self.reject)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        for pane in self.panes:
            pane.set_controls_visible(False)
            pane.info_ready.connect(lambda path, info, source=pane: self._metadata_ready(source, path))
            pane.ready.connect(lambda path, source=pane: self._image_ready(source, path))
            pane.error.connect(lambda message, source=pane: self._failed(source, message))
            pane.page_changed.connect(lambda page, source=pane: self._page_changed(source, page))
            pane.zoom_mode_changed.connect(lambda mode, source=pane: self._zoom_mode_changed(source, mode))
            pane.zoom_changed.connect(lambda zoom, source=pane: self._zoom_changed(source, zoom))
            pane.pan_changed.connect(lambda x, y, source=pane: self._pan_changed(source, x, y))
        self._update_controls()
        self._open_timer = QTimer(self)
        self._open_timer.setSingleShot(True)
        self._open_timer.timeout.connect(self._open_pair)
        self._open_timer.start(0)

    @property
    def panes(self) -> tuple[PreviewPane, PreviewPane]:
        return self.original_pane, self.compressed_pane

    @property
    def page_count(self) -> int:
        return self._page_count

    @property
    def page_index(self) -> int:
        return self._page

    def _button(self, text, description, callback):
        button = QPushButton(text, self)
        button.setAccessibleName(description)
        button.setToolTip(description)
        button.setAutoDefault(False)
        button.clicked.connect(callback)
        return button

    def _open_pair(self) -> None:
        if self._closed:
            return
        pair = compare_pair(self.path)
        if pair is None:
            self.summary_label.setText("Cannot compare: the original / _small PDF pair is missing.")
            self.status_label.setText(
                "Select an original PDF and use Compress to create its _small.pdf partner in the same folder, "
                "or restore the original beside an existing _small.pdf. Then open Compare again."
            )
            self.page_counts_label.setText(filename_text(self.path))
            self.splitter.hide()
            return
        self.original_path, self.compressed_path = pair
        try:
            before, after = (item.stat().st_size for item in pair)
        except OSError as exc:
            self.summary_label.setText("Cannot read the comparison pair.")
            self.status_label.setText(f"Restore access to both PDFs and open Compare again. {filename_text(exc)}")
            return
        change = f"{(after - before) / before * 100:+.1f}%" if before else "unavailable (original is empty)"
        self.summary_label.setText(
            f"Original {before:,} bytes → Compressed {after:,} bytes · Size change {change}"
        )
        if after > before:
            self.warning_label.setText("Larger output: the compressed PDF uses more bytes than the original.")
            self.warning_label.show()
        self.original_pane.open_path(self.original_path)
        self.compressed_pane.open_path(self.compressed_path)
        self.original_pane.title_label.setText("Original · " + filename_text(self.original_path.name))
        self.compressed_pane.title_label.setText("Compressed · " + filename_text(self.compressed_path.name))

    def _metadata_ready(self, pane: PreviewPane, path: Path) -> None:
        if self._closed or path != pane.source_path:
            return
        self._displayed.pop(pane, None)
        if any(item.document_info is None for item in self.panes):
            return
        self._page_count = min(item.page_count for item in self.panes)
        self._page = min(self._page, max(0, self._page_count - 1))
        for item in self.panes:
            item.set_page(self._page)
        counts = f"Original: {self.original_pane.page_count} pages · Compressed: {self.compressed_pane.page_count} pages"
        if self.original_pane.page_count != self.compressed_pane.page_count:
            counts += f" · Linked navigation stops at the shorter document ({self._page_count} pages)."
        self.page_counts_label.setText(counts)
        if not self._page_count:
            self.status_label.setText("Both PDFs need at least one page to compare. Choose another pair.")
        self._update_controls()

    def _image_ready(self, pane: PreviewPane, path: Path) -> None:
        if self._closed or path != pane.source_path:
            return
        self._displayed[pane] = pane.page_index
        self._update_zoom_label()
        if self._page_count and all(self._displayed.get(item) == self._page for item in self.panes):
            self.ready.emit()

    def _failed(self, pane: PreviewPane, message: str) -> None:
        if self._closed:
            return
        self._displayed.pop(pane, None)
        name = "original" if pane is self.original_pane else "compressed"
        self.status_label.setText(
            f"Cannot preview the {name} PDF. Restore a readable PDF and reopen Compare. {filename_text(message)}"
        )

    def _update_controls(self) -> None:
        available = self._page_count > 0 and not self._closed
        self.page_spin.blockSignals(True)
        self.page_spin.setRange(1 if available else 0, self._page_count if available else 0)
        self.page_spin.setValue(self._page + 1 if available else 0)
        self.page_spin.blockSignals(False)
        self.page_spin.setEnabled(available)
        self.page_total.setText(f"/ {self._page_count}")
        self.previous_button.setEnabled(available and self._page > 0)
        self.next_button.setEnabled(available and self._page + 1 < self._page_count)
        for widget in (self.zoom_out_button, self.zoom_in_button, self.zoom_spin,
                       self.fit_page_button, self.fit_width_button):
            widget.setEnabled(available)

    def set_page(self, page: int) -> None:
        if self._closed or self._linking or not self._page_count:
            return
        page = min(self._page_count - 1, max(0, page))
        if page == self._page and all(pane.page_index == page for pane in self.panes):
            return
        self._page = page
        for pane in self.panes:
            if pane.page_index != page:
                self._displayed.pop(pane, None)
        self._linking = True
        try:
            for pane in self.panes:
                pane.set_page(self._page)
        finally:
            self._linking = False
        self._update_controls()

    def _page_changed(self, source: PreviewPane, page: int) -> None:
        if not self._linking:
            self._displayed.pop(source, None)
            self.set_page(page)

    def previous_page(self) -> None:
        self.set_page(self._page - 1)

    def next_page(self) -> None:
        self.set_page(self._page + 1)

    def set_zoom(self, zoom: float) -> None:
        if self._closed or self._linking or not self._page_count:
            return
        self._linking = True
        try:
            for pane in self.panes:
                pane.set_zoom(zoom)
        finally:
            self._linking = False
        self.zoom_spin.blockSignals(True)
        self.zoom_spin.setValue(self.original_pane.zoom * 100)
        self.zoom_spin.blockSignals(False)
        self._update_zoom_label()

    def zoom_in(self) -> None:
        self.set_zoom(self.original_pane.zoom * 1.25)

    def zoom_out(self) -> None:
        self.set_zoom(self.original_pane.zoom / 1.25)

    def fit_page(self) -> None:
        self._set_fit("page")

    def fit_width(self) -> None:
        self._set_fit("width")

    def _set_fit(self, mode: str) -> None:
        if self._closed or self._linking or not self._page_count:
            return
        self._linking = True
        try:
            for pane in self.panes:
                (pane.fit_page if mode == "page" else pane.fit_width)()
        finally:
            self._linking = False
        self._update_zoom_label()

    def _zoom_mode_changed(self, source: PreviewPane, mode: str | None) -> None:
        if mode is None:
            self.set_zoom(source.zoom)
        else:
            self._set_fit(mode)

    def _zoom_changed(self, source: PreviewPane, zoom: float) -> None:
        if self._closed or self._linking:
            return
        if source.zoom_mode is None:
            self.set_zoom(zoom)
        self._update_zoom_label()

    def _update_zoom_label(self) -> None:
        mode = {"page": "Fit page", "width": "Fit width", None: "Custom zoom"}[self.original_pane.zoom_mode]
        self.zoom_label.setText(
            f"{mode} · Original {self.original_pane.zoom * 100:.1f}% / "
            f"Compressed {self.compressed_pane.zoom * 100:.1f}% · Pan linked"
        )

    def _pan_changed(self, source: PreviewPane, x: float, y: float) -> None:
        if self._closed or self._linking:
            return
        target = self.compressed_pane if source is self.original_pane else self.original_pane
        target.set_normalized_pan(x, y)

    def keyPressEvent(self, event) -> None:
        focus = QApplication.focusWidget()
        if not isinstance(focus, (QLineEdit, QAbstractSpinBox, QComboBox)):
            modifiers = event.modifiers()
            action = None
            if modifiers in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier):
                action = {"[": self.previous_page, "]": self.next_page,
                          "+": self.zoom_in, "=": self.zoom_in, "-": self.zoom_out}.get(event.text())
            elif modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
                if not modifiers & Qt.KeyboardModifier.AltModifier:
                    action = {
                        Qt.Key.Key_0: self.fit_page, Qt.Key.Key_1: self.fit_width,
                        Qt.Key.Key_Plus: self.zoom_in, Qt.Key.Key_Equal: self.zoom_in,
                        Qt.Key.Key_Minus: self.zoom_out,
                    }.get(event.key())
            if action is not None:
                action()
                event.accept()
                return
        super().keyPressEvent(event)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._open_timer.stop()
        for pane in self.panes:
            pane.shutdown()
        self._update_controls()

    def done(self, result: int) -> None:
        self.shutdown()
        super().done(result)

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)
