from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from gui_test_fixtures import multi_card_scan
from powerpdf_gui.window import MainWindow


def test_card_action_uses_queue_snapshot_and_main_respects_discard(qtbot, tmp_path, monkeypatch):
    source = multi_card_scan(tmp_path / 'رخصة المركبة.pdf')
    window = MainWindow(tmp_path, settings_path=tmp_path / 'settings.json')
    qtbot.addWidget(window)
    window.show()
    window.add_to_queue([source])
    window.browser.tree.setFocus()
    qtbot.keyClick(window.browser.tree, Qt.Key.Key_K)
    assert len(window._edit_workspaces) == 1
    dialog = window._edit_workspaces[0]
    qtbot.waitUntil(lambda: dialog.session is not None, timeout=30000)
    assert len(dialog.session.cards) == 8
    window.clear_queue()
    assert dialog.session.sources == [source.resolve()]
    dialog.session.name(0, 'رخصة المركبة 2026')
    dialog.editor.refresh()
    monkeypatch.setattr(QMessageBox, 'question', lambda *args, **kwargs: QMessageBox.StandardButton.Cancel)
    assert not window.close()
    assert window.isVisible()
    monkeypatch.setattr(QMessageBox, 'question', lambda *args, **kwargs: QMessageBox.StandardButton.Discard)
    assert window.close()
    assert not window.isVisible()
