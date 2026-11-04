from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox
from pypdf import PdfWriter

from gui_test_fixtures import multi_card_scan
from powerpdf_core.files import compare_pair
from powerpdf_gui.preview import PreviewPane
from powerpdf_gui.window import MainWindow


def test_pages_and_compare_use_highlight_not_queue(qtbot, tmp_path):
    source = multi_card_scan(tmp_path / 'رخصة المركبة.pdf')
    small = source.with_name(source.stem + '_small.pdf')
    small.write_bytes(source.read_bytes())
    queued = tmp_path / 'queued.pdf'
    with PdfWriter() as writer:
        writer.add_blank_page(300, 400)
        writer.write(queued)
    window = MainWindow(tmp_path, settings_path=tmp_path / 'settings.json')
    qtbot.addWidget(window)
    window.show()
    window.add_to_queue([queued])
    window.browser.select_path(source)
    qtbot.waitUntil(lambda: window.browser.selected_path() == source)
    window.browser.tree.setFocus()
    qtbot.keyClick(window.browser.tree, Qt.Key.Key_T)
    pages = window._edit_workspaces[-1]
    qtbot.waitUntil(lambda: pages.session is not None, timeout=30000)
    assert pages.session.path == source
    assert pages.session.page_count == 2
    assert pages.close()
    assert not window._edit_workspaces
    window.launch_compare()
    compare = window._edit_workspaces[-1]
    panes = compare.findChildren(PreviewPane)
    assert len(panes) == 2
    qtbot.waitUntil(lambda: all(pane.document_info is not None for pane in panes), timeout=30000)
    assert {pane.source_path for pane in panes} == {source, small}
    assert window.workspace.queue == [queued]
    assert window.close()


def test_missing_partner_is_actionable_and_pair_policy_shared(qtbot, tmp_path, monkeypatch):
    source = multi_card_scan(tmp_path / 'شهادة 2026.pdf')
    assert compare_pair(source) is None
    window = MainWindow(tmp_path, settings_path=tmp_path / 'settings.json')
    qtbot.addWidget(window)
    window.show()
    window.browser.select_path(source)
    qtbot.waitUntil(lambda: window.browser.selected_path() == source)
    messages = []
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: messages.append(args[2]))
    window.launch_compare()
    assert not window._edit_workspaces
    assert 'Compress' in messages[-1] and '_small.pdf' in messages[-1]
    small = source.with_name(source.stem + '_small.pdf')
    small.write_bytes(source.read_bytes())
    assert compare_pair(source) == compare_pair(small) == (source, small)
    window.close()
