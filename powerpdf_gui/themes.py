"""Flat Qt palettes with PowerPDF's existing dark and Game Boy identities."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette

THEMES = {
    "powerpdf": "PowerPDF dark",
    "light": "Light",
    "system": "System",
    "powerpdf-gameboy": "Game Boy",
    "high-contrast": "High contrast",
}

_COLORS = {
    "powerpdf": ("#0e0b16", "#17121f", "#241b33", "#f2e9e4", "#bfb6ca", "#ff4f81"),
    "light": ("#f4f3f6", "#ffffff", "#e7e3ec", "#211b2a", "#655b70", "#a71949"),
    "powerpdf-gameboy": ("#0f380f", "#173f17", "#306230", "#e0f8d0", "#c4e39a", "#9bbc0f"),
    "high-contrast": ("#000000", "#000000", "#292929", "#ffffff", "#ffffff", "#ffff00"),
}


def apply_theme(app, name: str) -> None:
    if not hasattr(app, "_powerpdf_system_palette"):
        app._powerpdf_system_palette = QPalette(app.palette())
    name = name if name in THEMES else "powerpdf"
    if name == "system":
        if app.styleHints().colorScheme() == Qt.ColorScheme.Dark:
            colors = ("#111111", "#181818", "#303030", "#ffffff", "#bfbfbf", "#d4d4d4")
        else:
            app.setPalette(app._powerpdf_system_palette)
            colors = None
    else:
        colors = _COLORS[name]
    if colors:
        ground, surface, raised, text, muted, accent = colors
        palette = QPalette()
        for role, color in {
            QPalette.ColorRole.Window: ground,
            QPalette.ColorRole.WindowText: text,
            QPalette.ColorRole.Base: surface,
            QPalette.ColorRole.AlternateBase: ground,
            QPalette.ColorRole.Text: text,
            QPalette.ColorRole.Button: raised,
            QPalette.ColorRole.ButtonText: text,
            QPalette.ColorRole.Highlight: raised if name != "high-contrast" else accent,
            QPalette.ColorRole.HighlightedText: text if name != "high-contrast" else ground,
            QPalette.ColorRole.ToolTipBase: raised,
            QPalette.ColorRole.ToolTipText: text,
            QPalette.ColorRole.PlaceholderText: muted,
            QPalette.ColorRole.Link: accent,
            QPalette.ColorRole.BrightText: accent,
            QPalette.ColorRole.Accent: accent,
        }.items():
            palette.setColor(role, QColor(color))
        palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(muted))
        palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(muted))
        app.setPalette(palette)
    # Heights and spacing, not a second set of widget colours. Native file dialogs
    # retain their operating-system presentation rather than being painted here.
    app.setStyleSheet("""
        QMainWindow::separator { width: 5px; height: 5px; }
        QToolBar { border: none; spacing: 6px; padding: 8px; }
        QToolButton, QPushButton { padding: 6px 10px; }
        QDockWidget::title { padding: 9px 10px; font-weight: 600; }
        QTreeView, QListView, QGraphicsView, QPlainTextEdit { border: none; }
        QTreeView::item { min-height: 32px; }
        QListView::item { padding: 8px 6px; }
        QHeaderView::section { padding: 7px; border: none; }
        QLineEdit, QSpinBox { padding: 6px; }
        QTabBar::tab { padding: 9px 16px; }
        QStatusBar { padding: 4px 8px; }
        QLabel[role="title"] { font-size: 18px; font-weight: 600; }
        QLabel[role="brand"] { font-size: 18px; font-weight: 700; }
        QLabel[role="muted"] { color: palette(placeholder-text); }
    """)
