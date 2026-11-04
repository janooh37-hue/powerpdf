"""Bundled GUI assets and native Qt Unicode font selection."""
from __future__ import annotations

import sys
from importlib.metadata import distribution
from pathlib import Path

from PySide6.QtCore import QThread
from PySide6.QtGui import QFont, QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication


FONTS = (
    ("NotoSans[wdth,wght].ttf", "Noto Sans"),
    ("NotoSansArabic[wdth,wght].ttf", "Noto Sans Arabic"),
)


def asset_path(relative: str | Path) -> Path:
    """Find an asset in a frozen bundle, source checkout, or installed wheel.

    Wheel data files live in ``sys.prefix/share/powerpdf/assets``. A frozen
    bundle is authoritative: missing bundled data must not accidentally resolve
    against an unrelated checkout or host installation.
    """
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Asset names must be relative to the assets directory")
    bundle = getattr(sys, "_MEIPASS", None)
    roots = (
        [Path(bundle) / "assets"]
        if bundle is not None
        else [Path(__file__).resolve().parent.parent / "assets",
              Path(sys.prefix) / "share/powerpdf/assets"]
    )
    for root in roots:
        candidate = root / relative
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Missing PowerPDF asset: {relative}")


def license_path() -> Path:
    """Find the full application license without duplicating it in assets."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle is not None:
        candidate = Path(bundle) / "LICENSE"
        if candidate.is_file():
            return candidate
        raise FileNotFoundError("The frozen bundle is missing LICENSE")
    candidate = Path(__file__).resolve().parent.parent / "LICENSE"
    if candidate.is_file():
        return candidate
    installed = distribution("powerpdf")
    for entry in installed.files or ():
        if entry.name == "LICENSE" and any(part.endswith(".dist-info") for part in entry.parts):
            candidate = Path(installed.locate_file(entry))
            if candidate.is_file():
                return candidate
    raise FileNotFoundError("The installed PowerPDF distribution is missing LICENSE")


def load_fonts(application: QApplication) -> QFont:
    """Load bundled Noto fonts and set the application font on the GUI thread.

    Qt 6's ordered QFont families provide native per-character fallback. The
    PySide6 6.10 binding does not expose QFontDatabase's QChar::Script fallback
    methods; setFamilies is its supported API. Logical text is never reordered
    or reshaped here: Qt handles Arabic shaping and bidirectional layout.
    """
    if QThread.currentThread() != application.thread():
        raise RuntimeError("Application fonts must be loaded on the GUI thread")
    if not application.property("powerpdfFontsLoaded"):
        loaded = []
        try:
            for filename, family in FONTS:
                font_id = QFontDatabase.addApplicationFont(str(asset_path(Path("fonts") / filename)))
                if font_id < 0:
                    raise RuntimeError(f"Could not load bundled font: {filename}")
                loaded.append(font_id)
                if family not in QFontDatabase.applicationFontFamilies(font_id):
                    raise RuntimeError(f"Bundled font does not contain {family}")
        except Exception:
            for font_id in loaded:
                QFontDatabase.removeApplicationFont(font_id)
            raise
        application.setProperty("powerpdfFontsLoaded", True)
    font = QFont(application.font())
    font.setFamilies([family for _, family in FONTS])
    font.setPointSizeF(10.5)
    application.setFont(font)
    return font


def load_app_icon() -> QIcon:
    """Return the authored PNG app icon without depending on QtSvg."""
    icon = QIcon(str(asset_path("powerpdf.png")))
    if icon.isNull():
        raise RuntimeError("Could not load the bundled PowerPDF icon")
    return icon
