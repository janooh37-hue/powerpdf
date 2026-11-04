"""Presentation-only bidi controls; filesystem strings remain unchanged."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit


def filename_text(value, direction=None):
    direction = QApplication.layoutDirection() if direction is None else direction
    return ('\u2067' if direction == Qt.LayoutDirection.RightToLeft else '\u2066') + str(value) + '\u2069'


class FilenameEdit(QLineEdit):
    """Keep Qt's editable paragraph in the UI direction without changing the name.

    QLineEdit auto-detects paragraph direction even with setLayoutDirection. A
    leading directional mark fixes that native layout; the mark is presentation
    only and is never returned as part of the logical filesystem text.
    """
    def __init__(self, value='', parent=None):
        super().__init__(parent)
        self.setLayoutDirection(QApplication.layoutDirection())
        self._mark = '\u200f' if self.layoutDirection() == Qt.LayoutDirection.RightToLeft else '\u200e'
        self.setText(value)
        self.textChanged.connect(self._restore_mark)

    def _restore_mark(self, value):
        if value and not value.startswith(self._mark):
            cursor = QLineEdit.cursorPosition(self)
            blocked = self.blockSignals(True)
            QLineEdit.setText(self, self._mark + value)
            QLineEdit.setCursorPosition(self, cursor + 1)
            self.blockSignals(blocked)

    def text(self):
        value = QLineEdit.text(self)
        return value[1:] if value.startswith(self._mark) else value

    def setText(self, value):
        QLineEdit.setText(self, self._mark + value if value else "")

    def selectAll(self):
        QLineEdit.setSelection(self, 1, len(self.text()))

    def setSelection(self, start, length):
        QLineEdit.setSelection(self, start + 1, length)

    def cursorPosition(self):
        return max(0, QLineEdit.cursorPosition(self) - 1)

    def setCursorPosition(self, position):
        QLineEdit.setCursorPosition(self, position + 1)
