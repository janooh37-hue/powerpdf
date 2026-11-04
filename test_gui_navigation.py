"""Real Qt navigation interactions; filesystem operations remain with the owner."""
import pytest
from pypdf import PdfWriter
from PySide6.QtCore import QMimeData, QModelIndex, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QAbstractItemView

from powerpdf_core.workspace import Workspace
from powerpdf_gui.navigation import FileBrowser, QueueList, RecentList


def make_pdf(path):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=150)
    writer.write(path)
    writer.close()
    return path


def visible_names(browser):
    root = browser.tree.rootIndex()
    return [browser.proxy.index(row, 0, root).data() for row in range(browser.proxy.rowCount(root))]


def open_browser(qtbot, folder):
    browser = FileBrowser()
    qtbot.addWidget(browser)
    browser.resize(900, 500)
    browser.set_folder(folder)
    browser.show()
    return browser


def send_urls(view, paths, remote=False):
    mime = QMimeData()
    urls = [QUrl.fromLocalFile(str(path)) for path in paths]
    if remote:
        urls.append(QUrl("https://example.invalid/not-local.pdf"))
    mime.setUrls(urls)
    actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
    buttons = Qt.MouseButton.LeftButton
    modifiers = Qt.KeyboardModifier.ShiftModifier
    position = QPoint(10, 10)
    enter = QDragEnterEvent(position, actions, mime, buttons, modifiers)
    QApplication.sendEvent(view.viewport(), enter)
    move = QDragMoveEvent(position, actions, mime, buttons, modifiers)
    QApplication.sendEvent(view.viewport(), move)
    drop = QDropEvent(QPointF(position), actions, mime, buttons, modifiers)
    QApplication.sendEvent(view.viewport(), drop)
    return enter, move, drop


def test_browser_filters_suffix_case_and_preserves_logical_arabic(qtbot, tmp_path):
    pdf = make_pdf(tmp_path / "رخصة المركبة [12].PDF")
    folder = tmp_path / "مستندات جديدة"
    folder.mkdir()
    (tmp_path / "notes.txt").write_text("notes")
    (tmp_path / "notpdf").write_text("notes")
    browser = open_browser(qtbot, tmp_path)
    qtbot.waitUntil(lambda: set(visible_names(browser)) == {pdf.name, folder.name})
    browser.tree.setCurrentIndex(QModelIndex())  # Offscreen may auto-select its first row on focus.
    spy = QSignalSpy(browser.selected)
    browser.select_path(pdf)
    qtbot.waitUntil(lambda: browser.selected_path() == pdf)
    assert spy.count() == 1
    assert spy.at(0) == [pdf]
    assert browser.tree.currentIndex().data() == pdf.name
    assert browser.model.isReadOnly()
    assert browser.tree.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert not browser.tree.dragEnabled()
    qtbot.mouseClick(browser.show_all, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: set(visible_names(browser)) == {pdf.name, folder.name, "notes.txt", "notpdf"})
    browser.set_show_all(False)
    qtbot.waitUntil(lambda: set(visible_names(browser)) == {pdf.name, folder.name})


def test_browser_folder_activation_and_path_controls(qtbot, tmp_path):
    folder = tmp_path / "مجلد عربي"
    folder.mkdir()
    pdf = make_pdf(folder / "هوية.pdf")
    browser = open_browser(qtbot, tmp_path)
    qtbot.waitUntil(lambda: folder.name in visible_names(browser))
    browser.tree.setCurrentIndex(QModelIndex())
    selected = QSignalSpy(browser.selected)
    activated = QSignalSpy(browser.activated_path)
    changed = QSignalSpy(browser.folder_changed)
    browser.activated_path.connect(lambda path: browser.set_folder(path) if path.is_dir() else None)
    browser.select_path(folder)
    qtbot.waitUntil(lambda: browser.selected_path() == folder)
    browser.tree.setFocus()
    qtbot.keyClick(browser.tree, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: browser.folder == folder)
    assert selected.count() >= 1
    assert activated.count() == 1
    assert changed.count() == 1
    assert selected.at(0) == [folder]
    assert activated.at(0) == [folder]
    assert changed.at(0) == [folder]
    qtbot.waitUntil(lambda: visible_names(browser) == [pdf.name])
    qtbot.mouseClick(browser.up_button, Qt.MouseButton.LeftButton)
    assert browser.folder == tmp_path
    browser.path_edit.setText(folder.name)
    qtbot.keyClick(browser.path_edit, Qt.Key.Key_Return)
    assert browser.folder == folder


def test_browser_select_path_loads_another_folder(qtbot, tmp_path):
    folder = tmp_path / "other"
    folder.mkdir()
    pdf = make_pdf(folder / "اسم طويل.pdf")
    browser = open_browser(qtbot, tmp_path)
    browser.select_path(pdf)
    qtbot.waitUntil(lambda: browser.selected_path() == pdf)
    assert browser.folder == folder
    assert browser.tree.currentIndex().data() == pdf.name


def test_browser_refresh_reloads_and_preserves_selection(qtbot, tmp_path):
    pdf = make_pdf(tmp_path / "قبل.pdf")
    browser = open_browser(qtbot, tmp_path)
    browser.select_path(pdf)
    qtbot.waitUntil(lambda: browser.selected_path() == pdf)
    second = make_pdf(tmp_path / "بعد.pdf")
    browser.refresh()
    qtbot.waitUntil(lambda: set(visible_names(browser)) == {pdf.name, second.name})
    qtbot.waitUntil(lambda: browser.selected_path() == pdf)


def test_browser_unavailable_folder_keeps_current_location(qtbot, tmp_path):
    browser = open_browser(qtbot, tmp_path)
    with pytest.raises(NotADirectoryError):
        browser.set_folder(tmp_path / "missing")
    assert browser.folder == tmp_path


@pytest.mark.parametrize("view_kind", [FileBrowser, QueueList])
def test_external_url_drop_is_ordered_copy_only(qtbot, tmp_path, view_kind):
    pdf = make_pdf(tmp_path / "ملف عربي.pdf")
    folder = tmp_path / "مجلد"
    folder.mkdir()
    text = tmp_path / "notes.txt"
    text.write_text("unchanged")
    widget = view_kind()
    qtbot.addWidget(widget)
    widget.resize(900, 400)
    widget.show()
    view = widget.tree if isinstance(widget, FileBrowser) else widget
    if isinstance(widget, FileBrowser):
        widget.set_folder(tmp_path)
    before = pdf.read_bytes()
    dropped = QSignalSpy(widget.paths_dropped)
    payload = [pdf, folder, text, pdf]
    enter, move, drop = send_urls(view, payload, remote=True)
    assert enter.isAccepted()
    assert move.isAccepted()
    assert drop.isAccepted()
    assert drop.dropAction() == Qt.DropAction.CopyAction
    assert dropped.count() == 1
    assert dropped.at(0) == [payload]
    assert pdf.read_bytes() == before
    assert folder.is_dir()
    assert text.read_text() == "unchanged"
    if isinstance(widget, QueueList):
        assert widget.paths() == []
    else:
        assert widget.folder == tmp_path


def test_remote_url_drop_is_rejected(qtbot):
    queue = QueueList()
    qtbot.addWidget(queue)
    queue.show()
    dropped = QSignalSpy(queue.paths_dropped)
    enter, _move, drop = send_urls(queue, [], remote=True)
    assert not enter.isAccepted()
    assert not drop.isAccepted()
    assert dropped.count() == 0


def test_queue_keyboard_mutations_are_workspace_requests(qtbot, tmp_path):
    first = make_pdf(tmp_path / "الأول.pdf")
    second = make_pdf(tmp_path / "الثاني.pdf")
    third = make_pdf(tmp_path / "الثالث.pdf")
    workspace = Workspace(queue=[first, second, third])
    queue = QueueList()
    qtbot.addWidget(queue)
    queue.resize(500, 250)
    queue.set_paths(workspace.queue)
    queue.show()
    moves = QSignalSpy(queue.move_requested)
    removes = QSignalSpy(queue.remove_requested)
    orders = QSignalSpy(queue.order_changed)

    def move(index, step):
        workspace.move(index, step)
        queue.set_paths(workspace.queue)

    def remove(path):
        workspace.drop(workspace.queue.index(path))
        queue.set_paths(workspace.queue)

    queue.move_requested.connect(move)
    queue.remove_requested.connect(remove)
    queue.setCurrentRow(1)
    qtbot.keyClick(queue, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)
    assert moves.count() == 1
    assert moves.at(0) == [1, -1]
    assert workspace.queue == queue.paths() == [second, first, third]
    assert queue.current_path() == second
    qtbot.keyClick(queue, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    assert workspace.queue == queue.paths() == [first, second, third]
    assert queue.current_path() == second
    qtbot.keyClick(queue, Qt.Key.Key_Delete)
    assert removes.count() == 1
    assert removes.at(0) == [second]
    assert workspace.queue == queue.paths() == [first, third]
    assert orders.count() == 0
    assert all(path.is_file() for path in (first, second, third))


def test_queue_internal_model_reorder_emits_one_complete_order(qtbot, tmp_path):
    paths = [make_pdf(tmp_path / name) for name in ("ج.pdf", "أ.pdf", "ب.pdf")]
    queue = QueueList()
    qtbot.addWidget(queue)
    queue.set_paths(paths)
    queue.show()
    queue.setCurrentRow(0)
    orders = QSignalSpy(queue.order_changed)
    assert queue.model().moveRows(QModelIndex(), 0, 1, QModelIndex(), 3)
    assert queue.paths() == paths[1:] + paths[:1]
    assert queue.current_path() == paths[0]
    assert orders.count() == 1
    assert orders.at(0) == [queue.paths()]
    queue.set_paths(paths)
    assert orders.count() == 1


@pytest.mark.parametrize("view_kind", [QueueList, RecentList])
def test_lists_native_names_selection_and_enter(qtbot, tmp_path, view_kind):
    first = make_pdf(tmp_path / "هوية [1] version 2.pdf")
    second = make_pdf(tmp_path / "رخصة.pdf")
    view = view_kind()
    qtbot.addWidget(view)
    view.resize(500, 250)
    view.set_paths([first, second])
    view.show()
    selected = QSignalSpy(view.selected)
    activated = QSignalSpy(view.activated_path)
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualItemRect(view.item(1)).center())
    assert view.current_path() == second
    assert selected.count() >= 1
    assert selected.at(selected.count() - 1) == [second]
    assert view.item(0).text() == first.name
    assert view.item(0).toolTip() == str(first)
    qtbot.keyClick(view, Qt.Key.Key_Return)
    assert activated.count() == 1
    assert activated.at(0) == [second]
    count = selected.count()
    view.set_paths([second, first])
    assert view.current_path() == second
    assert selected.count() == count


def test_views_do_not_apply_workspace_validation_or_dedup(qtbot, tmp_path):
    missing = tmp_path / "missing.pdf"
    text = tmp_path / "notpdf.txt"
    paths = [missing, text, missing]
    for kind in (QueueList, RecentList):
        view = kind()
        qtbot.addWidget(view)
        view.set_paths(paths)
        assert view.paths() == paths
        if isinstance(view, QueueList):
            view.remove_selected()
        assert view.paths() == paths
