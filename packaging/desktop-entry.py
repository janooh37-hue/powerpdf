"""Frozen desktop entry; normal launches delegate directly to powerpdf_gui.main.

The private --packaging-smoke-output DIRECTORY switch lets external native CI
capture the real window after its real PDF preview has rendered, then shut down.
It is not an alternate application or a shipped test harness.
"""
from __future__ import annotations

import sys


def main() -> int:
    arguments = sys.argv[1:]

    from powerpdf_gui import main as gui_main
    if "--packaging-smoke-output" not in arguments:
        return gui_main(arguments)

    import argparse
    import json
    from pathlib import Path
    import traceback

    from PySide6.QtCore import QLibraryInfo, QTimer
    from PySide6.QtPrintSupport import QPrinter, QPrinterInfo
    from PySide6.QtWidgets import QGraphicsPixmapItem

    import pdf_runtime
    from powerpdf_gui.application import PowerPDFApplication
    from powerpdf_gui.window import MainWindow

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--packaging-smoke-output", type=Path, required=True)
    options, paths = parser.parse_known_args(arguments)
    output = options.packaging_smoke_output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    app = PowerPDFApplication(sys.argv[:1])
    timer = QTimer(app)
    timer.setInterval(100)
    attempts = 0
    completed = False

    def capture() -> None:
        nonlocal attempts, completed
        attempts += 1
        window = next((item for item in app.topLevelWidgets()
                       if isinstance(item, MainWindow)), None)
        try:
            if attempts >= 300:
                raise RuntimeError("The real GUI did not render its fixture PDF within 30 seconds")
            if window is None or not window.isVisible() or not window.preview.page_count:
                return
            preview = window.preview
            if not any(isinstance(item, QGraphicsPixmapItem) and not item.pixmap().isNull()
                       for item in preview.view.scene().items()):
                return
            model = window.browser.tree.model()
            root = window.browser.tree.rootIndex()
            names = [model.index(row, 0, root).data() for row in range(model.rowCount(root))]
            expected = sorted(path.name for path in window.root.glob("*.pdf"))
            if not set(expected).issubset(names):
                return
            if not getattr(sys, "frozen", False):
                raise RuntimeError("Packaging smoke must launch a frozen PowerPDF artifact")
            bundle = Path(sys._MEIPASS)
            for relative in ("assets/powerpdf.png", "LICENSE", "NOTICE"):
                if not (bundle / relative).is_file():
                    raise RuntimeError(f"Missing frozen application data: {relative}")
            if not app.property("powerpdfFontsLoaded"):
                raise RuntimeError("The real GUI did not load its bundled fonts")
            # Instantiate the real print-support binding and native printer plugin.
            printer = QPrinter()
            evidence = {
                "frozen": True,
                "executable": sys.executable,
                "bundle": str(bundle),
                "window_class": type(window).__name__,
                "window_title": window.windowTitle(),
                "folder": str(window.root),
                "filenames": names,
                "preview_source": str(preview.source_path),
                "preview_pages": preview.page_count,
                "fonts": app.font().families(),
                "qt_plugins": QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath),
                "printer_valid": printer.isValid(),
                "native_printers": len(QPrinterInfo.availablePrinters()),
                "helpers": {name: pdf_runtime.resolve_tool(name)
                            for name in ("ghostscript", "tesseract", "fzf")},
            }
            if not window.grab().save(str(output / "powerpdf-gui.png"), "PNG"):
                raise RuntimeError("Could not save the real PowerPDF window screenshot")
            (output / "runtime.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            timer.stop()
            completed = True
            window.close()  # Join the real preview worker and save preferences normally.
            app.exit(0)
        except Exception:
            timer.stop()
            (output / "gui-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            if window is not None:
                window.close()
            app.exit(1)

    timer.timeout.connect(capture)
    timer.start()
    result = gui_main(paths)
    return result if completed or result else 1


if __name__ == "__main__":
    raise SystemExit(main())
