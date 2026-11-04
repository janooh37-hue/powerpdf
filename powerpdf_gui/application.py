"""Finder/OS document-open events, including opens before the window exists."""
from pathlib import Path
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication


class PowerPDFApplication(QApplication):
    def __init__(self, arguments):
        super().__init__(arguments)
        self._window = None
        self._pending_opens = []

    def attach_window(self, window):
        self._window = window
        if self._pending_opens:
            window.open_paths(self._pending_opens)
            self._pending_opens.clear()

    def event(self, event):
        if event.type() == QEvent.Type.FileOpen:
            name = event.file()
            if not name and event.url().isLocalFile():
                name = event.url().toLocalFile()
            if name:
                path = Path(name)
                if self._window is None:
                    self._pending_opens.append(path)
                else:
                    self._window.open_paths([path])
                    self._window.showNormal()
                    self._window.raise_()
                    self._window.activateWindow()
                return True
        return super().event(event)
