"""Direct-manipulation Qt adapter for the shared card edit model.

All coordinates are native scan pixels (map/crop) or cropped-side pixels (masks).
Core cut/downscale supply the preview, including the export's black-mask padding;
Qt only fits those owned pixels and draws interactive selection overlays.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPen, QPolygonF, QTransform
from PySide6.QtWidgets import (
    QApplication, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QSplitter, QVBoxLayout, QWidget,
)

from powerpdf_core.cards import CardEditSession, DPI, MAX_PAGE_PIXELS, cut, downscale


def _image(image) -> QImage:
    rgb = image.convert("RGB")
    try:
        return QImage(rgb.tobytes(), rgb.width, rgb.height, rgb.width * 3,
                      QImage.Format.Format_RGB888).copy()
    finally:
        rgb.close()


def _rect_transform(rect) -> QTransform:
    (cx, cy), _, angle = rect
    transform = QTransform()
    transform.translate(cx, cy)
    transform.rotate(angle)
    return transform


def _polygon(rect) -> QPolygonF:
    _, (w, h), _ = rect
    return _rect_transform(rect).map(QPolygonF([
        QPointF(-w / 2, -h / 2), QPointF(w / 2, -h / 2),
        QPointF(w / 2, h / 2), QPointF(-w / 2, h / 2),
    ]))


@dataclass(frozen=True)
class _Marker:
    index: int
    side: int
    rect: tuple
    guessed: bool
    skipped: bool
    current: bool


class _Canvas(QWidget):
    """A fitted raster viewport; drag geometry remains in model coordinates."""

    def __init__(self, editor: CardEditor, side: int | None) -> None:
        super().__init__(editor)
        self.editor, self.side = editor, side
        self.image = QImage()
        self.native_size = (1.0, 1.0)
        self.markers: list[_Marker] = []
        self.zoom = 1.0
        self.offset = QPointF()
        self._drag = None
        self._pan = None
        self._background_key = None
        self._background = QImage()
        self.setMinimumSize(180, 160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Source scan map" if side is None else ("Front card canvas" if side == 0 else "Back card canvas"))
        self.setToolTip("Drag to move; square handles resize; round handle rotates the crop. "
                        "Wheel zooms, middle-drag pans, double-click fits. Arrows move; Shift+arrows resize; "
                        "Ctrl+arrows use a 10-pixel step. B adds a mask; Delete removes it; Space cycles selection; F switches side.")

    def view_transform(self) -> QTransform:
        w, h = self.native_size
        scale = max(0.001, min(max(1, self.width() - 40) / w,
                               max(1, self.height() - 40) / h)) * self.zoom
        return QTransform(scale, 0, 0, scale,
                          (self.width() - w * scale) / 2 + self.offset.x(),
                          (self.height() - h * scale) / 2 + self.offset.y())

    def point_to_widget(self, point: QPointF) -> QPointF:
        return self.view_transform().map(point)

    def point_from_widget(self, point: QPointF) -> QPointF:
        return self.view_transform().inverted()[0].map(point)

    def fit(self) -> None:
        self.zoom = 1.0
        self.offset = QPointF()
        self.update()

    def _selection_rect(self):
        session = self.editor.session
        if self.side is None:
            return session.rects[session.side]
        if self.side != session.side or session.active is None:
            return None
        cx, cy, w, h = session.boxes[self.side][session.active]
        return ((cx, cy), (w, h), 0)

    def handle_points(self) -> dict[str, QPointF]:
        rect = self._selection_rect()
        if rect is None:
            return {}
        _, (w, h), _ = rect
        transform = _rect_transform(rect)
        local = {"nw": (-w / 2, -h / 2), "n": (0, -h / 2), "ne": (w / 2, -h / 2),
                 "e": (w / 2, 0), "se": (w / 2, h / 2), "s": (0, h / 2),
                 "sw": (-w / 2, h / 2), "w": (-w / 2, 0)}
        points = {key: self.point_to_widget(transform.map(QPointF(*value))) for key, value in local.items()}
        if self.side is None:
            scale = self.view_transform().m11()
            points["rotate"] = self.point_to_widget(transform.map(QPointF(0, -h / 2 - 24 / scale)))
        return points

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        transform = self.view_transform()
        if self.side is None:
            # The source page is immutable and much larger than its viewport.
            # Crop/selection nudges repaint overlays, not a 26 MB page resize.
            dpr = self.devicePixelRatioF()
            key = (self.image.cacheKey(), self.width(), self.height(), dpr,
                   self.native_size, self.zoom, self.offset.x(), self.offset.y())
            if key != self._background_key:
                self._background = QImage(max(1, math.ceil(self.width() * dpr)),
                                          max(1, math.ceil(self.height() * dpr)),
                                          QImage.Format.Format_ARGB32_Premultiplied)
                self._background.setDevicePixelRatio(dpr)
                self._background.fill(Qt.GlobalColor.transparent)
                background = QPainter(self._background)
                background.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                background.setTransform(transform)
                background.drawImage(QRectF(0, 0, *self.native_size), self.image)
                background.end()
                self._background_key = key
            painter.drawImage(QPointF(), self._background)
        else:
            painter.setTransform(transform)
            painter.drawImage(QRectF(0, 0, *self.native_size), self.image)
            painter.resetTransform()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        session = self.editor.session
        accent = self.palette().highlight().color()
        if self.side is None:
            for marker in self.markers:
                polygon = transform.map(_polygon(marker.rect))
                color = QColor(accent if marker.current else self.palette().text().color())
                if marker.guessed:
                    color = QColor("#c58b25")
                pen = QPen(color, 2.5 if marker.current else 1.2)
                if marker.guessed or marker.skipped:
                    pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                fill = QColor(color)
                fill.setAlpha(65 if marker.current else 15)
                if marker.skipped:
                    fill = QColor(0, 0, 0, 140)
                painter.setBrush(fill)
                painter.drawPolygon(polygon)
                label = f"{marker.index + 1}{'F' if marker.side == 0 else 'B'}"
                if marker.guessed:
                    label += " ?"
                if marker.skipped:
                    label += " skipped"
                painter.setPen(QPen(self.palette().text().color()))
                bounds = polygon.boundingRect()
                painter.fillRect(QRectF(bounds.left(), bounds.top(), min(bounds.width(), 116), 20), self.palette().base())
                painter.drawText(QRectF(bounds.left() + 3, bounds.top(), bounds.width(), 20), label)
        elif self.side == session.side:
            for index, (cx, cy, w, h) in enumerate(session.boxes[self.side]):
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(accent, 2 if index == session.active else 1))
                painter.drawPolygon(transform.map(_polygon(((cx, cy), (w, h), 0))))
        selection = self._selection_rect()
        if selection is not None:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(accent, 2))
            painter.drawPolygon(transform.map(_polygon(selection)))
            handles = self.handle_points()
            if "rotate" in handles:
                painter.drawLine(handles["n"], handles["rotate"])
            painter.setBrush(self.palette().base())
            for name, point in handles.items():
                bounds = QRectF(point.x() - 4, point.y() - 4, 8, 8)
                if name == "rotate":
                    painter.drawEllipse(bounds)
                else:
                    painter.drawRect(bounds)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(accent, 2))
            painter.drawRect(self.rect().adjusted(1, 1, -2, -2))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan = (event.position(), QPointF(self.offset))
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.setFocus()
        session = self.editor.session
        self.editor._finish_gesture()
        if self.side is not None and self.side != session.side:
            self.editor.focus_side(self.side)
        handle = next((name for name, point in self.handle_points().items()
                       if (point - event.position()).manhattanLength() <= 11), None)
        point = self.point_from_widget(event.position())
        if self.side is None:
            session.select_box(None)
            # Active crop handles win over adjacent map rectangles.
            if handle is None:
                marker = next((marker for marker in reversed(self.markers)
                               if _polygon(marker.rect).containsPoint(point, Qt.FillRule.OddEvenFill)), None)
                if marker is None:
                    self.editor._selection_changed()
                    return
                if marker.index != session.index or marker.side != session.side:
                    session.index = marker.index
                    session.switch(marker.side)
                    self.editor.refresh()
                    point = self.point_from_widget(event.position())
        else:
            if handle is None:
                found = next((index for index in reversed(range(len(session.boxes[self.side])))
                              if QRectF(session.boxes[self.side][index][0] - session.boxes[self.side][index][2] / 2,
                                        session.boxes[self.side][index][1] - session.boxes[self.side][index][3] / 2,
                                        session.boxes[self.side][index][2], session.boxes[self.side][index][3]).contains(point)), None)
                session.select_box(found)
            if session.active is None and not self.editor.draw_box_button.isChecked():
                self.editor._selection_changed()
                return
        self.editor._selection_changed()
        creating = self.side is not None and self.editor.draw_box_button.isChecked() and handle is None
        if creating:
            session.select_box(None)
        rect = self._selection_rect()
        session.begin_edit()
        self._drag = {"start": point, "rect": rect, "handle": handle or "move", "creating": creating,
                      "context": (session.index, session.side), "box": session.active}
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._pan is not None:
            start, offset = self._pan
            self.offset = offset + event.position() - start
            self.update()
            return
        if self._drag is None:
            return super().mouseMoveEvent(event)
        session, drag = self.editor.session, self._drag
        if drag["context"] != (session.index, session.side):
            self.finish_gesture()
            return
        point = self.point_from_widget(event.position())
        start = drag["start"]
        delta = point - start
        if delta.manhattanLength() < 0.01:
            return
        if drag["creating"]:
            if drag["box"] is None:
                session.add_box()
                drag["box"] = session.active
            session.set_box(((point.x() + start.x()) / 2, (point.y() + start.y()) / 2,
                             max(8, abs(delta.x())), max(8, abs(delta.y()))))
        else:
            (cx, cy), (w, h), angle = rect = drag["rect"]
            handle = drag["handle"]
            if handle == "move":
                rect = ((cx + delta.x(), cy + delta.y()), (w, h), angle)
            elif handle == "rotate":
                initial = math.atan2(start.y() - cy, start.x() - cx)
                current = math.atan2(point.y() - cy, point.x() - cx)
                rect = ((cx, cy), (w, h), angle + math.degrees(current - initial))
            else:
                inverse = _rect_transform(drag["rect"]).inverted()[0]
                local_delta = inverse.map(point) - inverse.map(start)
                sx = -1 if "w" in handle else (1 if "e" in handle else 0)
                sy = -1 if "n" in handle else (1 if "s" in handle else 0)
                minimum = 20 if self.side is None else 8
                dw = max(minimum, w + sx * local_delta.x()) - w if sx else 0
                dh = max(minimum, h + sy * local_delta.y()) - h if sy else 0
                center = _rect_transform(drag["rect"]).map(QPointF(sx * dw / 2, sy * dh / 2))
                rect = ((center.x(), center.y()), (w + dw, h + dh), angle)
            if self.side is None:
                if not self.editor._set_crop(rect):
                    return
            else:
                (cx, cy), (w, h), _ = rect
                session.set_box((cx, cy, w, h))
        self.editor._model_changed()

    def finish_gesture(self) -> None:
        if self._drag is not None:
            self._drag = None
            self.editor.session.end_edit()
        self._pan = None

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag is not None:
            self.mouseMoveEvent(event)
            self.finish_gesture()
            if self.editor.draw_box_button.isChecked():
                self.editor.draw_box_button.setChecked(False)
            event.accept()
        elif event.button() == Qt.MouseButton.MiddleButton:
            self._pan = None
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self.finish_gesture()
        self.fit()
        event.accept()

    def wheelEvent(self, event) -> None:
        if self._drag is not None:
            event.accept()
            return
        point = self.point_from_widget(event.position())
        self.zoom = min(12, max(1, self.zoom * (1.25 if event.angleDelta().y() > 0 else 0.8)))
        self.offset += event.position() - self.point_to_widget(point)
        self.update()
        event.accept()

    def keyPressEvent(self, event) -> None:
        if self.editor._key(event):
            event.accept()
        else:
            super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:
        self.finish_gesture()
        self.update()
        super().focusOutEvent(event)

    def focusInEvent(self, event) -> None:
        if self.side is not None and self.side != self.editor.session.side:
            self.editor.focus_side(self.side)
        self.update()
        super().focusInEvent(event)


class CardEditor(QWidget):
    """Shared-session editor; refresh reads external navigation without signals.

    changed signals selection as well as edits. Source arrays remain model-owned;
    neither refresh nor selection creates an undo entry or invalidates OCR.
    """

    changed = Signal()

    def __init__(self, session: CardEditSession, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self._source_key = None
        self._preview_keys = [None, None]
        self._context = None
        self.setAccessibleName("Card crop and black-box editor")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.source_view = _Canvas(self, None)
        self.front_view = _Canvas(self, 0)
        self.back_view = _Canvas(self, 1)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        scan = QWidget()
        scan_layout = QVBoxLayout(scan)
        self.source_label = QLabel("Source scan map")
        scan_layout.addWidget(self.source_label)
        scan_layout.addWidget(self.source_view, 1)
        legend = QLabel("F front · B back · ? guessed · dashed/dimmed skipped")
        legend.setWordWrap(True)
        scan_layout.addWidget(legend)
        splitter.addWidget(scan)
        previews = QWidget()
        previews_layout = QHBoxLayout(previews)
        previews_layout.setContentsMargins(0, 0, 0, 0)
        self.side_labels = []
        for title, view in (("Front", self.front_view), ("Back", self.back_view)):
            panel = QWidget()
            panel_layout = QVBoxLayout(panel)
            panel_layout.setContentsMargins(0, 0, 0, 0)
            label = QLabel(title)
            label.setWordWrap(True)
            self.side_labels.append(label)
            panel_layout.addWidget(label)
            panel_layout.addWidget(view, 1)
            previews_layout.addWidget(panel, 1)
        splitter.addWidget(previews)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([340, 1040])
        layout.addWidget(splitter, 1)
        self.selection_label = QLabel()
        layout.addWidget(self.selection_label)
        numeric = QGridLayout()
        self.fields = {}
        for column, (key, title) in enumerate((("cx", "Center X"), ("cy", "Center Y"), ("w", "Width"), ("h", "Height"), ("angle", "Angle"))):
            spin = QDoubleSpinBox()
            spin.setDecimals(2)
            spin.setRange(-1_000_000, 1_000_000)
            spin.setSingleStep(0.5 if key == "angle" else 1)
            spin.setKeyboardTracking(False)
            spin.setSuffix("°" if key == "angle" else " px")
            spin.setAccessibleName(f"Selected {title.lower()}")
            label = QLabel(title)
            label.setBuddy(spin)
            numeric.addWidget(label, 0, column)
            numeric.addWidget(spin, 1, column)
            spin.valueChanged.connect(lambda value, field=key: self._numeric(field, value))
            self.fields[key] = spin
        layout.addLayout(numeric)
        commands = QGridLayout()
        controls = (
            ("add_box_button", "Add black box", self._add_box),
            ("draw_box_button", "Draw black box", None),
            ("delete_box_button", "Delete box", self._delete_box),
            ("select_crop_button", "Select crop", self._select_crop),
            ("switch_side_button", "Switch side", lambda: self.focus_side(1 - session.side)),
            ("tilt_left_button", "Tilt −0.5°", lambda: self._rotate(-0.5)),
            ("tilt_right_button", "Tilt +0.5°", lambda: self._rotate(0.5)),
            ("rotate90_button", "Rotate 90°", lambda: self._rotate(90)),
            ("rotate180_button", "Rotate 180°", lambda: self._rotate(180)),
            ("reset_button", "Reset crop", self._reset),
            ("undo_button", "Undo", lambda: self._travel(False)),
            ("redo_button", "Redo", lambda: self._travel(True)),
        )
        for index, (name, text, callback) in enumerate(controls):
            button = QPushButton(text)
            button.setAccessibleName(text)
            button.setToolTip(text)
            setattr(self, name, button)
            commands.addWidget(button, index // 6, index % 6)
            if callback is not None:
                button.clicked.connect(callback)
        self.draw_box_button.setCheckable(True)
        self.draw_box_button.setToolTip("Drag a rectangle on the front or back preview to create a black box")
        layout.addLayout(commands)
        help_label = QLabel("Arrows: move · Shift+arrows: resize · Ctrl: 10 px · [ / ]: tilt · R: rotate 90° · "
                            "Space: select next box · F: other side · Ctrl/Cmd+Z: undo · wheel: zoom · middle-drag: pan")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        self.refresh()

    def _finish_gesture(self) -> None:
        for view in (self.source_view, self.front_view, self.back_view):
            view.finish_gesture()

    def focus_side(self, side: int) -> None:
        self._finish_gesture()
        self.session.switch(side)
        self._selection_changed()
        (self.front_view if side == 0 else self.back_view).setFocus(Qt.FocusReason.OtherFocusReason)

    def _selection_changed(self) -> None:
        self.refresh()
        self.changed.emit()

    def _model_changed(self) -> None:
        self.refresh()
        self.changed.emit()

    def refresh(self) -> None:
        session = self.session
        context = (session.index, session.side)
        if context != self._context:
            self._finish_gesture()
            self.source_view.fit()
            self.front_view.fit()
            self.back_view.fit()
            self._context = context
        source = session.pages[session.side]
        if self._source_key != id(source):
            self.source_view.image = QImage(source.data, source.shape[1], source.shape[0], source.strides[0], QImage.Format.Format_RGB888).copy()
            self.source_view.native_size = (source.shape[1], source.shape[0])
            self._source_key = id(source)
        self.source_view.markers = [
            _Marker(index, side, card.rects[side], card.guessed[side], index in session.skipped,
                    index == session.index)
            for index, card in enumerate(session.cards) for side in (0, 1)
            if card.pages[side] is source
        ]
        self.source_label.setText(f"Source scan map · {session.card.where}")
        for side, view in enumerate((self.front_view, self.back_view)):
            rect = session.rects[side]
            key = (id(session.pages[side]), rect, tuple(session.boxes[side]))
            if key != self._preview_keys[side]:
                with cut(session.pages[side], rect) as cropped:
                    with downscale(cropped, session.boxes[side]) as preview:
                        view.image = _image(preview)
                view.native_size = rect[1]
                self._preview_keys[side] = key
            w, h = rect[1]
            title = "Front" if side == 0 else "Back"
            warning = " · guessed crop — adjust to confirm" if session.card.guessed[side] else ""
            self.side_labels[side].setText(f"{title}{' · active' if side == session.side else ''} · "
                                            f"{w / DPI * 25.4:.1f} × {h / DPI * 25.4:.1f} mm{warning}")
            view.update()
        if session.active is None:
            (cx, cy), (w, h), angle = session.rects[session.side]
            selected = "Crop · source scan pixels"
        else:
            cx, cy, w, h = session.boxes[session.side][session.active]
            angle = 0
            selected = f"Black box {session.active + 1} · cropped-side pixels"
        self.selection_label.setText(selected)
        for key, value in zip(("cx", "cy", "w", "h", "angle"), (cx, cy, w, h, angle)):
            field = self.fields[key]
            field.blockSignals(True)
            field.setMinimum((20 if session.active is None else 8) if key in ("w", "h") else -1_000_000)
            if key in ("w", "h") and session.active is None:
                other = h if key == "w" else w
                field.setMaximum(min(32766, MAX_PAGE_PIXELS // max(1, round(other))))
                field.setToolTip("Crop limit: 32 million pixels, 32766 pixels per side.")
            else:
                field.setMaximum(1_000_000)
            field.setValue(value)
            field.blockSignals(False)
        self.fields["angle"].setEnabled(session.active is None)
        self.delete_box_button.setEnabled(session.active is not None)
        self.source_view.update()

    def _numeric(self, key: str, value: float) -> None:
        self._finish_gesture()
        session = self.session
        if session.active is None:
            (cx, cy), (w, h), angle = session.rects[session.side]
            values = dict(cx=cx, cy=cy, w=w, h=h, angle=angle)
            values[key] = value
            if not self._set_crop(((values["cx"], values["cy"]), (values["w"], values["h"]), values["angle"])):
                return
        else:
            values = dict(zip(("cx", "cy", "w", "h"), session.boxes[session.side][session.active]))
            values[key] = value
            session.set_box(tuple(values[field] for field in ("cx", "cy", "w", "h")))
        self._model_changed()

    def _set_crop(self, rect) -> bool:
        try:
            self.session.set_crop(rect)
        except ValueError as error:
            self.refresh()
            self.selection_label.setText(str(error))
            return False
        return True

    def _add_box(self) -> None:
        self._finish_gesture()
        self.session.add_box()
        self._model_changed()

    def _delete_box(self) -> None:
        self._finish_gesture()
        self.session.remove_box()
        self._model_changed()

    def _select_crop(self) -> None:
        self._finish_gesture()
        self.session.select_box(None)
        self._selection_changed()

    def _rotate(self, angle: float) -> None:
        self._finish_gesture()
        self.session.rotate_crop(angle)
        self._model_changed()

    def _reset(self) -> None:
        self._finish_gesture()
        self.session.reset_crop()
        self._model_changed()

    def _travel(self, redo: bool) -> None:
        self._finish_gesture()
        if self.session.redo() if redo else self.session.undo():
            self._model_changed()

    def _key(self, event) -> bool:
        if event.matches(QKeySequence.StandardKey.Undo):
            self._travel(False)
            return True
        if event.matches(QKeySequence.StandardKey.Redo):
            self._travel(True)
            return True
        modifiers = event.modifiers()
        key = event.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            step = 10 if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier) else 1
            dx = step * ((key == Qt.Key.Key_Right) - (key == Qt.Key.Key_Left))
            dy = step * ((key == Qt.Key.Key_Down) - (key == Qt.Key.Key_Up))
            self._finish_gesture()
            try:
                if modifiers & Qt.KeyboardModifier.ShiftModifier:
                    self.session.change(0, 0, dx, dy)
                else:
                    self.session.change(dx, dy)
            except ValueError as error:
                self.refresh()
                self.selection_label.setText(str(error))
                return True
            self._model_changed()
            return True
        if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.AltModifier):
            return False
        actions = {Qt.Key.Key_B: self._add_box, Qt.Key.Key_Delete: self._delete_box,
                   Qt.Key.Key_Backspace: self._delete_box, Qt.Key.Key_F: lambda: self.focus_side(1 - self.session.side),
                   Qt.Key.Key_R: lambda: self._rotate(90), Qt.Key.Key_BracketLeft: lambda: self._rotate(-0.5),
                   Qt.Key.Key_BracketRight: lambda: self._rotate(0.5)}
        if key == Qt.Key.Key_Space:
            self._finish_gesture()
            self.session.cycle()
            self._selection_changed()
            return True
        if key in actions:
            actions[key]()
            return True
        if key == Qt.Key.Key_Escape and self.draw_box_button.isChecked():
            self.draw_box_button.setChecked(False)
            return True
        return False

    def keyPressEvent(self, event) -> None:
        focus = QApplication.focusWidget()
        if focus is not None and any(focus is field or field.isAncestorOf(focus) for field in self.fields.values()):
            return super().keyPressEvent(event)
        if self._key(event):
            event.accept()
        else:
            super().keyPressEvent(event)
