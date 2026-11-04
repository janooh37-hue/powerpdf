from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit
from powerpdf_gui.text import FilenameEdit, filename_text
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


def test_empty_filename_selection_has_no_phantom_prefix(qtbot, qtlog):
    editor = FilenameEdit()
    qtbot.addWidget(editor)
    editor.selectAll()
    editor.setSelection(0, 0)
    editor.setCursorPosition(0)
    assert editor.text() == editor.selectedText() == ""
    assert editor.cursorPosition() == QLineEdit.cursorPosition(editor) == 0
    assert not any("Invalid start position" in record.message for record in qtlog.records)
    editor.setText("رخصة المركبة")
    editor.selectAll()
    assert editor.selectedText() == editor.text() == "رخصة المركبة"
