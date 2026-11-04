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

    def _prefix_length(self):
        return int(QLineEdit.text(self).startswith(self._mark))

    def selectAll(self):
        self.setSelection(0, len(self.text()))

    def setSelection(self, start, length):
        QLineEdit.setSelection(self, start + self._prefix_length(), length)

    def cursorPosition(self):
        return max(0, QLineEdit.cursorPosition(self) - self._prefix_length())

    def setCursorPosition(self, position):
        QLineEdit.setCursorPosition(self, position + self._prefix_length())
