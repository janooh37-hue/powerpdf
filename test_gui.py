"""Offscreen native-shell acceptance with real files, PDFium previews and Arabic input."""
from pathlib import Path
import shutil

import pytest
from PIL import Image
from pypdf import PdfWriter
from PySide6.QtCore import QEvent, QMimeData, QPointF, QRect, Qt, QUrl
from PySide6.QtGui import QColor, QDropEvent, QFileOpenEvent, QFont, QImage, QInputMethodEvent, QKeySequence, QPainter, QPalette
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFileDialog, QLabel, QMessageBox

from powerpdf_gui import resources
from powerpdf_gui.window import CommandPalette, MainWindow, RenameDialog


NAMES = ["رخصة المركبة.pdf", "شهادة 2026.pdf", "Ahmed عقد.pdf", "تقرير الترخيص 2026 (2).pdf"]


def make_pdf(path, pages=2):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(595, 842)
    writer.add_metadata({"/Title": "Local document sample", "/Author": "PowerPDF"})
    writer.write(path)
    return path


@pytest.fixture
def documents(tmp_path):
    for name in NAMES:
        make_pdf(tmp_path / name)
    (tmp_path / "مستندات السيارات").mkdir()
    (tmp_path / "notes.txt").write_text("Not a PDF", encoding="utf-8")
    return tmp_path


@pytest.fixture
def window(qtbot, documents, tmp_path):
    item = MainWindow(documents, settings_path=tmp_path / "config/settings.json")
    qtbot.addWidget(item)
    item.show()
    qtbot.waitExposed(item)
    yield item
    item.close()


def preview(qtbot, window, path):
    window.browser.select_path(path)
    window.show_path(path)
    qtbot.waitUntil(lambda: window.preview.source_path == path and window.preview.page_count == 2)
    qtbot.waitUntil(lambda: not window.preview.view.scene().items()[0].pixmap().isNull())


def test_real_preview_info_recent_and_queue_keyboard(qtbot, window, documents):
    path = documents / NAMES[0]
    preview(qtbot, window, path)
    assert window.info_name.text() == "\u2066" + path.name + "\u2069"
    assert window.findChild(QLabel, "infoPages").text() == "2"
    assert window.findChild(QLabel, "infoTitle").text() == "Local document sample"
    assert window.workspace.recent[0] == path
    window.browser.tree.setFocus()
    qtbot.keyClick(window.browser.tree, Qt.Key.Key_Space)
    assert window.workspace.queue == [path]
    window.add_to_queue([documents / NAMES[1], path])
    assert window.queue_list.paths() == [path, documents / NAMES[1]]
    window.queue_list.setFocus()
    window.queue_list.setCurrentRow(1)
    qtbot.keyClick(window.queue_list, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)
    assert window.workspace.queue == [documents / NAMES[1], path]
    qtbot.keyClick(window.queue_list, Qt.Key.Key_Delete)
    assert window.workspace.queue == [path]
    assert path.exists()
    qtbot.waitUntil(lambda: window.preview.page_count == 2)
    window.browser.tree.setFocus()
    qtbot.keyClick(window.browser.tree, Qt.Key.Key_BracketRight)
    qtbot.waitUntil(lambda: window.preview.page_index == 1)


def test_native_dialog_adapters_and_ordered_mixed_drop(qtbot, window, documents, monkeypatch):
    pdfs = [documents / NAMES[2], documents / NAMES[0]]
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *args, **kwargs: ([str(path) for path in pdfs], "PDF"))
    window.actions["open_files"].trigger()
    assert window.workspace.queue == pdfs
    folder = documents / "مستندات السيارات"
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args, **kwargs: str(folder))
    window.actions["open_folder"].trigger()
    assert window.root == folder
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(documents / NAMES[1])), QUrl.fromLocalFile(str(documents))])
    event = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    window.dropEvent(event)
    assert event.dropAction() == Qt.DropAction.CopyAction
    assert window.workspace.queue == pdfs + [documents / NAMES[1]]
    assert window.root == documents
    assert all(path.exists() for path in pdfs)


def test_arabic_rename_input_and_path_remapping(qtbot, window, documents):
    path = documents / NAMES[0]
    preview(qtbot, window, path)
    window.add_to_queue([path])
    dialog = RenameDialog(path, window)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.name_input.setFocus()
    dialog.name_input.selectAll()
    event = QInputMethodEvent()
    typed = "شهادة الملكية 2026.pdf"
    event.setCommitString(typed)
    QApplication.sendEvent(dialog.name_input, event)
    assert dialog.name_input.text() == typed
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    dialog.accept()
    assert window.rename_path(path, dialog.name_input.text())
    destination = documents / typed
    assert not path.exists() and destination.is_file()
    assert window.workspace.queue == [destination]
    assert window.workspace.recent[0] == destination
    qtbot.waitUntil(lambda: window.preview.source_path == destination)


def test_folder_rename_and_trash_remap_descendants_without_permanent_fallback(qtbot, window, documents, monkeypatch):
    folder = documents / "مستندات السيارات"
    nested = make_pdf(folder / "عقد.pdf")
    window.add_to_queue([nested])
    window.workspace.remember(nested)
    window.show_path(folder)
    assert window.rename_path(folder, "الأرشيف")
    moved = documents / "الأرشيف"
    assert window.workspace.queue == [moved / "عقد.pdf"]
    assert window.workspace.recent == [moved / "عقد.pdf"]
    errors = []
    monkeypatch.setattr(window, "report_error", lambda title, error: errors.append(str(error)))
    def deny(path):
        raise PermissionError("Trash denied")
    monkeypatch.setattr("powerpdf_gui.window.send2trash", deny)
    assert not window.trash_path(moved)
    assert moved.exists() and window.workspace.queue
    assert errors == ["Trash denied"]
    monkeypatch.setattr("powerpdf_gui.window.send2trash", lambda path: shutil.rmtree(path))
    assert window.trash_path(moved)
    assert not moved.exists() and not window.workspace.queue and not window.workspace.recent


def test_text_entry_does_not_trigger_workspace_keys(qtbot, window, documents):
    path = documents / NAMES[0]
    window.add_to_queue([path])
    field = window.browser.path_edit
    field.setFocus()
    field.selectAll()
    qtbot.keyClicks(field, "qrxaf")
    assert field.text() == "qrxaf"
    assert window.isVisible() and window.workspace.queue == [path]
    assert window.navigation.currentIndex() == 0


def test_theme_and_window_preferences_preserve_shared_settings(qtbot, window):
    window.settings_store.update(extra={"preserved": True})
    dark = QApplication.palette().color(QPalette.ColorRole.Window)
    window.set_theme("light")
    assert QApplication.palette().color(QPalette.ColorRole.Window) != dark
    window.set_theme("system")
    window.set_theme("powerpdf-gameboy")
    window.set_theme("high-contrast")
    window.set_theme("powerpdf")
    window.actions["all_files"].trigger()
    window.close()
    saved = window.settings_store.load()
    assert saved["extra"] == {"preserved": True}
    assert saved["gui"]["theme"] == "powerpdf"
    assert saved["gui"]["show_all"] is True
    assert saved["gui"]["geometry"] and saved["gui"]["dock_state"]


def test_recent_open_queue_forget_clear_and_safe_trash_confirmation(qtbot, window, documents, monkeypatch):
    path = documents / NAMES[0]
    preview(qtbot, window, path)
    window.recent_list.queue_requested.emit(path)
    assert window.workspace.queue == [path]
    window.recent_list.forget_requested.emit(path)
    assert window.workspace.recent == []
    window.workspace.remember(path)
    window.refresh_lists()
    window.recent_list.clear_requested.emit()
    assert window.workspace.recent == []
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Cancel)
    window.trash_selected()
    assert path.is_file()


def test_desktop_actions_report_reveal_degradation(qtbot, window, documents, monkeypatch):
    path = documents / NAMES[0]
    window.show_path(path)
    opened, revealed = [], []
    monkeypatch.setattr("powerpdf_gui.desktop.open_external", lambda value: opened.append(value))
    monkeypatch.setattr("powerpdf_gui.desktop.reveal", lambda value: revealed.append(value) or False)
    window.open_externally()
    window.reveal_selected()
    assert opened == revealed == [path]
    assert "containing folder" in window.activity.toPlainText()


def test_native_file_open_events_before_and_after_window(qapp, qtbot, window, documents):
    qapp._window = None
    first, second = documents / NAMES[0], documents / NAMES[1]
    assert QApplication.sendEvent(qapp, QFileOpenEvent(str(first)))
    qapp.attach_window(window)
    assert first in window.workspace.queue
    assert QApplication.sendEvent(qapp, QFileOpenEvent(str(second)))
    assert window.workspace.queue == [first, second]
    qapp._window = None


def test_command_palette_filters_the_real_menu_actions(qtbot, window):
    palette = CommandPalette(window.actions.values(), window)
    qtbot.addWidget(palette)
    palette.show()
    palette.search.setFocus()
    qtbot.keyClicks(palette.search, "rename")
    assert palette.commands.count() == 1
    assert palette.commands.item(0).text().startswith("Rename")
    qtbot.keyClick(palette.search, Qt.Key.Key_Return)
    assert palette.chosen is window.actions["rename"]


def test_keyboard_actions_have_menu_equivalents_without_fake_future_actions(window):
    assert set(window.actions) >= {"open", "rename", "trash", "reveal", "external", "go", "all_files", "recent", "cycle_theme"}
    assert set(window.actions) >= {"merge", "split", "compress", "print", "images", "cards", "pages", "compare"}
    assert window.actions["open_files"].shortcut().matches(QKeySequence(QKeySequence.StandardKey.Open))

@pytest.mark.parametrize("action", ["merge", "split", "compress", "images", "print"])
def test_shell_opens_real_operation_with_exact_queue_snapshot(qtbot, window, documents, action):
    targets = [documents / NAMES[2], documents / NAMES[0]]
    window.add_to_queue(targets)
    window.launch_operation(action)
    dialog = window._operation_dialogs[-1]
    assert dialog.isVisible()
    assert list(dialog.targets if action == "print" else dialog.targets()) == targets
    window.clear_queue()
    assert list(dialog.targets if action == "print" else dialog.targets()) == targets
    dialog.shutdown()
    dialog.close()



def _sample_scan(path):
    """Synthetic, explicitly labelled document, drawn by Qt with native Arabic shaping."""
    image = QImage(1190, 1684, QImage.Format.Format_RGB32)
    image.fill(QColor("#faf8f3"))
    painter = QPainter(image)
    painter.fillRect(QRect(0, 0, 1190, 210), QColor("#243b46"))
    painter.setPen(QColor("#ffffff"))
    painter.setFont(QFont("Noto Sans Arabic", 35, QFont.Weight.Bold))
    painter.drawText(QRect(70, 50, 1040, 100), Qt.AlignmentFlag.AlignRight, "رخصة المركبة")
    painter.setFont(QFont("Noto Sans", 21))
    painter.drawText(80, 160, "VEHICLE DOCUMENT  /  LOCAL SAMPLE")
    painter.setPen(QColor("#243b46"))
    painter.setFont(QFont("Noto Sans Arabic", 25))
    for index, text in enumerate(("مستند تجريبي للعرض", "نوع المستند: رخصة مركبة", "السنة: 2026", "حالة المستند: نسخة محلية")):
        painter.drawText(QRect(90, 290 + index * 125, 1000, 100), Qt.AlignmentFlag.AlignRight, text)
    painter.fillRect(QRect(90, 880, 1010, 300), QColor("#e6e3da"))
    painter.setFont(QFont("Noto Sans", 22))
    painter.drawText(125, 950, "Preview your documents clearly.")
    painter.setFont(QFont("Noto Sans Arabic", 23))
    painter.drawText(QRect(125, 990, 940, 100), Qt.AlignmentFlag.AlignRight, "أسماء عربية، كما كتبتها")
    painter.setFont(QFont("Noto Sans", 15))
    painter.drawText(95, 1520, "SYNTHETIC SAMPLE • No personal information • PowerPDF")
    painter.end()
    owned = Image.frombytes("RGB", (image.width(), image.height()), bytes(image.bits()), "raw", "BGRX", image.bytesPerLine())
    owned.save(path, "PDF", resolution=144, title="Vehicle document — local sample", author="PowerPDF")
    owned.close()


def test_capture_arabic_main_and_rename_dialog(qtbot, tmp_path):
    folder = tmp_path / "PowerPDF Arabic demo"
    folder.mkdir()
    (folder / "مستندات السيارات").mkdir()
    _sample_scan(folder / NAMES[0])
    for name in NAMES[1:]:
        make_pdf(folder / name)
    window = MainWindow(folder, settings_path=tmp_path / "demo-settings.json")
    qtbot.addWidget(window)
    window.show()
    window.add_to_queue([folder / NAMES[0], folder / NAMES[2], folder / NAMES[3]])
    window.browser.select_path(folder / NAMES[0])
    window.show_path(folder / NAMES[0])
    qtbot.waitUntil(lambda: window.preview.page_count == 1 and not window.preview.view.scene().items()[0].pixmap().isNull())
    qtbot.waitUntil(lambda: window.browser.model.rowCount(window.browser.model.index(str(folder))) == 5)
    qtbot.wait(250)
    output = Path("/tmp/gui")
    output.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(output / "main-window-arabic.png"))
    dialog = RenameDialog(folder / NAMES[0], window)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.name_input.setFocus()
    dialog.name_input.selectAll()
    event = QInputMethodEvent()
    event.setCommitString("رخصة المركبة الجديدة 2026.pdf")
    QApplication.sendEvent(dialog.name_input, event)
    assert dialog.name_input.text() == "رخصة المركبة الجديدة 2026.pdf"
    qtbot.wait(100)
    assert dialog.grab().save(str(output / "rename-dialog-arabic.png"))
    dialog.reject()
    window.close()
