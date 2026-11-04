from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from powerpdf_gui.text import filename_text
from powerpdf_gui.window import RenameDialog


def test_filename_isolation_follows_application_direction(qapp, tmp_path):
    name = 'رخصة المركبة.pdf'
    original = qapp.layoutDirection()
    path = tmp_path / name
    path.write_bytes(b'fixture')
    try:
        for direction, marker in ((Qt.LayoutDirection.LeftToRight, '\u2066'), (Qt.LayoutDirection.RightToLeft, '\u2067')):
            qapp.setLayoutDirection(direction)
            assert filename_text(name) == marker + name + '\u2069'
            dialog = RenameDialog(path)
            assert dialog.name_input.layoutDirection() == direction
            assert dialog.name_input.text() == name
            assert dialog.name_input.selectedText() == path.stem
            assert dialog.name_input.cursorPosition() == len(path.stem)
            dialog.close()
            dialog.name_input.clear()
            assert dialog.name_input.text() == ""
    finally:
        qapp.setLayoutDirection(original)
