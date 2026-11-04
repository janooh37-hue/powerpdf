"""PowerPDF's native desktop entry point (no TUI imports)."""
from __future__ import annotations

VERSION = "0.8.0"

def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    from pathlib import Path
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox
    from .application import PowerPDFApplication
    from .window import MainWindow

    parser = argparse.ArgumentParser(prog="powerpdf-gui", description="PowerPDF native PDF workspace")
    parser.add_argument("paths", nargs="*", help="PDF files to queue, or folders to browse")
    args = parser.parse_args(argv)
    app = QApplication.instance() or PowerPDFApplication([sys.argv[0]])
    app.setApplicationName("PowerPDF")
    app.setOrganizationName("PowerPDF")
    app.setApplicationVersion(VERSION)
    paths = [Path(value).expanduser().resolve() for value in args.paths]
    folders = [path for path in paths if path.is_dir()]
    try:
        window = MainWindow(folders[-1] if folders else None)
    except (OSError, ValueError, RuntimeError) as error:
        QMessageBox.critical(None, "PowerPDF could not start", str(error))
        return 1
    if isinstance(app, PowerPDFApplication):
        app.attach_window(window)
    window.show()
    if paths:
        QTimer.singleShot(0, lambda: window.open_paths(paths))
    return app.exec()
