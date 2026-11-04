"""Native card workspace acceptance using real two-page, eight-card scans.

Running the capture test produces /tmp/gui/cards-workspace.png and
/tmp/gui/cards-save-result.png. Offscreen captures do not qualify OS dialogs.
"""
from pathlib import Path
import threading

from PIL import Image
from pypdf import PdfReader
import pytest
from PySide6.QtCore import QThread, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

from gui_test_fixtures import multi_card_scan
from powerpdf_core import ocr
from powerpdf_core.jobs import CancelToken
from powerpdf_core.settings import SettingsStore
from powerpdf_gui import cards as gui_cards
from powerpdf_gui.cards import CardsWindow, NameDialog, _execute_export, _export_plan


def workspace(qtbot, tmp_path, monkeypatch, *, missing=False, real_ocr=False, name="scan.pdf"):
    source = multi_card_scan(tmp_path / name, missing=missing)
    if not real_ocr:
        monkeypatch.setattr(ocr, "resolve_tesseract", lambda: None)
    dialog = CardsWindow([source], settings_path=tmp_path / "settings.json")
    qtbot.addWidget(dialog, before_close_func=lambda widget: widget.shutdown())
    dialog.show()
    qtbot.waitUntil(lambda: dialog.session is not None or not dialog._loading, timeout=30000)
    assert dialog.session is not None, dialog.status_label.text()
    assert len(dialog.session.cards) >= 8
    qtbot.waitUntil(lambda: not dialog._ocr_running, timeout=90000)
    return dialog


def teardown(dialog):
    dialog.shutdown()
    dialog.close()


def save(qtbot, dialog, monkeypatch, kind, destination):
    reviewed = []
    monkeypatch.setattr(dialog, "review_export", lambda plan: reviewed.append(plan) or True)
    with qtbot.waitSignal(dialog.completed, timeout=60000):
        dialog.save(kind, destination)
    assert dialog.job_result.success, dialog.job_result.errors
    assert reviewed
    assert tuple(reviewed[0].outputs) == dialog.job_result.outputs
    assert dialog._worker.thread is None
    return dialog.job_result, reviewed[0]


def test_real_detection_navigation_markers_swap_skip_undo(qtbot, tmp_path, monkeypatch):
    dialog = workspace(qtbot, tmp_path, monkeypatch, missing=True)
    session = dialog.session
    assert dialog.strip.count() == len(session.cards)
    assert any("guessed" in dialog.strip.item(i).text() for i in range(dialog.strip.count()))
    assert "page 1" in session.card.where
    assert not dialog.dirty
    qtbot.mouseClick(dialog.next_button, Qt.MouseButton.LeftButton)
    assert session.index == 1
    qtbot.mouseClick(dialog.back_button, Qt.MouseButton.LeftButton)
    assert session.side == 1 and not dialog.dirty
    original = session.rects[:]
    dialog.command("swap")
    assert session.rects == original[::-1] and dialog.dirty
    dialog.command("undo")
    assert session.rects == original and not dialog.dirty
    dialog.command("redo")
    assert session.rects == original[::-1]
    dialog.command("skip")
    assert session.index in session.skipped
    assert "skipped" in dialog.strip.item(session.index).text()
    assert dialog.skip_button.text() == "Keep card"
    dialog.command("undo")
    assert not session.skipped
    dialog.step(999)
    assert session.index == len(session.cards) - 1
    dialog.step(-999)
    assert session.index == 0
    assert "OCR unavailable" in dialog.ocr_status.text()
    teardown(dialog)


def test_arabic_name_ime_history_and_two_open_workspaces(qtbot, tmp_path, monkeypatch):
    dialog = workspace(qtbot, tmp_path, monkeypatch)
    second = CardsWindow(dialog.targets, settings_path=tmp_path / "settings.json")
    qtbot.addWidget(second, before_close_func=lambda widget: widget.shutdown())
    second.show()
    qtbot.waitUntil(lambda: second.session is not None, timeout=30000)
    name = NameDialog(dialog.session, dialog)
    qtbot.addWidget(name)
    name.show()
    name.editor.setText("")
    raw = "16/16402 رخصة المركبة"
    event = QInputMethodEvent()
    event.setCommitString(raw)
    QApplication.sendEvent(name.editor, event)
    assert name.editor.text() == raw
    dialog.name_card_value(0, name.editor.text())
    assert dialog.session.names[0] == "16-16402 رخصة المركبة"
    assert dialog.session.labels[0] == "رخصة المركبة"
    assert "named" in dialog.strip.item(0).text()
    name.close()
    second.name_card_value(0, "21/13695 بطاقة هوية")
    labels = SettingsStore(tmp_path / "settings.json").load()["card_labels"]
    assert labels[:2] == ["بطاقة هوية", "رخصة المركبة"]
    for index in range(14):
        dialog.name_card_value(0, f"16-16402 Label{index}")
    labels = SettingsStore(tmp_path / "settings.json").load()["card_labels"]
    assert len(labels) == 12 and labels[0] == "Label13"
    dialog.command("undo")
    assert dialog.session.names[0] == "16-16402 Label12"
    dialog.command("redo")
    assert dialog.session.names[0] == "16-16402 Label13"
    history = NameDialog(dialog.session, labels=["رخصة المركبة"])
    qtbot.addWidget(history)
    history.show()
    history.history.setCurrentRow(0)
    history.history.setFocus()
    qtbot.keyClick(history.history, Qt.Key.Key_Return)
    assert history.editor.text() == "16-16402 رخصة المركبة"
    assert history.isVisible()  # Picking a label does not confirm a filename.
    history.reject()
    teardown(second)
    teardown(dialog)


def test_ocr_background_queue_confirmed_only_and_stale_revision(qtbot, tmp_path, monkeypatch):
    requests_seen, threads = [], []
    def read_names(requests, **kwargs):
        requests_seen.extend(requests)
        threads.append(QThread.currentThread())
        for request in requests:
            yield ocr.OCRResult(request.card_id, request.revision, "16-16402", 1)
    monkeypatch.setattr(ocr, "read_names", read_names)
    dialog = workspace(qtbot, tmp_path, monkeypatch)
    session = dialog.session
    assert len(requests_seen) == len(session.cards)
    assert threads[0] != QApplication.instance().thread()
    assert all(name is None for name in session.names)
    assert all(card.name is None for card in session.snapshot().cards)
    assert "read on back" in dialog.name_status.text()
    assert "manual Swap" in dialog.name_status.text()
    old = requests_seen[0]
    session.change(10, 0)
    dialog._suggestion(ocr.OCRResult(old.card_id, old.revision, "stale-name"))
    assert session.suggested[0] is None and session.names[0] is None
    session.undo()
    dialog._suggestion(ocr.OCRResult(old.card_id, old.revision, "still-stale"))
    assert session.suggested[0] is None
    fresh = session.ocr_cards()[0]
    dialog._suggestion(ocr.OCRResult(fresh.card_id, fresh.revision, "16-16402", 1))
    assert session.names[0] is None
    dialog.name_card_value(0, "16/16402 مركبة")
    dialog._suggestion(ocr.OCRResult(fresh.card_id, fresh.revision, "do-not-replace"))
    assert session.names[0] == "16-16402 مركبة"
    teardown(dialog)


@pytest.mark.parametrize("kind", ["pdf", "jpg", "jpg-side", "pdf-side", "pdf-folder", "jpg-folder", "combined"])
def test_every_real_save_variant_receipts_bookmarks_and_burned_boxes(qtbot, tmp_path, monkeypatch, kind):
    dialog = workspace(qtbot, tmp_path, monkeypatch)
    source_bytes = dialog.targets[0].read_bytes()
    dialog.name_card_value(0, "16/16402 رخصة")
    dialog.session.add_box()
    dialog.session.switch(1)
    dialog.session.add_box()
    dialog.session.step(2)
    dialog.command("skip")
    dialog.session.step(-2)
    dialog.refresh()
    destination = tmp_path / ("out" if kind.endswith("-folder") else "out.jpg" if kind.startswith("jpg") else "out.pdf")
    on_threads = []
    dialog.completed.connect(lambda _result: on_threads.append(QThread.currentThread()))
    result, plan = save(qtbot, dialog, monkeypatch, kind, destination)
    assert on_threads == [QApplication.instance().thread()]
    assert result.output_bytes == sum(path.stat().st_size for path in result.outputs) > 0
    assert all(path.exists() for path in result.outputs)
    assert dialog.results.count() == len(result.outputs)
    assert "Saved" in dialog.status_label.text()
    assert dialog.targets[0].read_bytes() == source_bytes
    if kind == "combined":
        reader = PdfReader(result.outputs[0])
        assert len(reader.pages) == 2 * len(dialog.session.kept())
        assert [(item.title, reader.get_destination_page_number(item)) for item in reader.outline] == [
            (stem, 2 * index) for index, stem in enumerate(dialog.session.file_stems().values())]
        assert reader.outline[0].title == "16-16402 رخصة"
    elif kind.endswith("-folder"):
        multiplier = 2 if kind == "jpg-folder" else 1
        assert len(result.outputs) == multiplier * len(dialog.session.kept())
        assert not any("_card_03" in path.name for path in result.outputs)
        assert any("16-16402 رخصة" in path.name for path in result.outputs)
    elif kind == "jpg":
        assert {path.name for path in result.outputs} == {"out_front.jpg", "out_back.jpg"}
    for path in result.outputs:
        if path.suffix == ".pdf":
            reader = PdfReader(path)
            assert not reader.metadata and "/Metadata" not in reader.trailer["/Root"]
            if kind == "pdf":
                assert len(reader.pages) == 2
            if kind == "pdf-side":
                assert len(reader.pages) == 1
            image = reader.pages[0].images[0].image.convert("RGB")
        else:
            with Image.open(path) as jpg:
                assert jpg.format == "JPEG" and not jpg.getexif()
                image = jpg.convert("RGB")
        if path == result.outputs[0]:
            assert max(image.getpixel((image.width // 2, image.height // 2))) < 8
        image.close()
    if kind.endswith("-side"):
        assert len(plan.snapshot.cards[0].pages) == 1
        assert dialog.dirty  # The opposite side's box has not been saved.
    teardown(dialog)


def test_save_as_native_adapter_and_quick_defaults(qtbot, tmp_path, monkeypatch):
    dialog = workspace(qtbot, tmp_path, monkeypatch)
    assert dialog.default_destination("pdf").name == "scan_card_01.pdf"
    dialog.name_card_value(0, "16/16402 Station")
    assert dialog.default_destination("pdf").name == "16-16402 Station.pdf"
    dialog.save_kind.setCurrentIndex(dialog.save_kind.findData("pdf"))
    chosen = tmp_path / "chosen.pdf"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(chosen), "PDF"))
    monkeypatch.setattr(dialog, "review_export", lambda _plan: True)
    with qtbot.waitSignal(dialog.completed, timeout=60000):
        dialog.save_as()
    assert dialog.job_result.outputs == (chosen,)
    assert len(PdfReader(chosen).pages) == 2
    teardown(dialog)


def test_review_cancel_overwrite_revision_and_source_protection(qtbot, tmp_path, monkeypatch):
    dialog = workspace(qtbot, tmp_path, monkeypatch)
    existing = tmp_path / "existing.pdf"
    existing.write_bytes(b"preserve")
    plan = _export_plan(dialog.session, "pdf", existing)
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No)
    assert not dialog.review_export(plan)
    assert existing.read_bytes() == b"preserve"
    existing.write_bytes(b"changed after consent")
    result = _execute_export(plan, CancelToken(), lambda _event: None)
    assert not result.success and "changed after review" in result.errors[0].error
    assert existing.read_bytes() == b"changed after consent"
    with pytest.raises(ValueError):
        _export_plan(dialog.session, "pdf", dialog.targets[0])
    alias = tmp_path / "alias.pdf"
    alias.hardlink_to(dialog.targets[0])
    with pytest.raises(ValueError):
        _export_plan(dialog.session, "pdf", alias)
    snapshot = _export_plan(dialog.session, "pdf", tmp_path / "stale.pdf")
    dialog.targets[0].write_bytes(dialog.targets[0].read_bytes() + b"\n% changed source")
    result = _execute_export(snapshot, CancelToken(), lambda _event: None)
    assert not result.success and "source PDF changed" in result.errors[0].error
    assert not (tmp_path / "stale.pdf").exists()
    teardown(dialog)


def test_save_immutable_snapshot_worker_and_dirty_discard_lifecycle(qtbot, tmp_path, monkeypatch):
    dialog = workspace(qtbot, tmp_path, monkeypatch)
    dialog.name_card_value(0, "old")
    entered, release = threading.Event(), threading.Event()
    original = gui_cards._execute_export
    def delayed(plan, cancel, progress):
        entered.set()
        assert release.wait(20)
        return original(plan, cancel, progress)
    monkeypatch.setattr(gui_cards, "_execute_export", delayed)
    monkeypatch.setattr(dialog, "review_export", lambda _plan: True)
    dialog.save("pdf", tmp_path / "snapshot.pdf")
    qtbot.waitUntil(entered.is_set)
    dialog.name_card_value(0, "new")
    assert dialog._saving
    with qtbot.waitSignal(dialog.completed, timeout=60000):
        release.set()
    assert dialog.dirty and dialog.session.names[0] == "new"
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Cancel)
    assert not dialog.close()
    assert not dialog._closed and dialog.isVisible()
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Discard)
    with qtbot.waitSignal(dialog.finished):
        assert dialog.close()
    assert dialog._closed and dialog._worker.thread is None and dialog._ocr_worker.thread is None


def test_cancelled_export_preserves_existing_and_no_partial_receipt(qtbot, tmp_path, monkeypatch):
    dialog = workspace(qtbot, tmp_path, monkeypatch)
    destination = tmp_path / "preserve.pdf"
    destination.write_bytes(b"original")
    plan = _export_plan(dialog.session, "pdf", destination)
    token = CancelToken()
    token.cancel()
    result = _execute_export(plan, token, lambda _event: None)
    assert result.cancelled and not result.outputs
    assert destination.read_bytes() == b"original"
    teardown(dialog)


def test_real_bundled_or_system_ocr_queue(qtbot, tmp_path, monkeypatch):
    executable = ocr.resolve_tesseract()
    if not executable:
        pytest.skip("No bundled/system Tesseract available")
    dialog = workspace(qtbot, tmp_path, monkeypatch, real_ocr=True)
    assert not dialog._ocr_unavailable
    assert not dialog._ocr_running
    assert all(name is None for name in dialog.session.names)
    assert all(card.name is None for card in dialog.session.snapshot().cards)
    teardown(dialog)


def test_capture_complete_workspace_and_real_save_result(qtbot, tmp_path, monkeypatch):
    from powerpdf_gui import resources, themes
    resources.load_fonts(QApplication.instance())
    themes.apply_theme(QApplication.instance(), "powerpdf")
    dialog = workspace(qtbot, tmp_path, monkeypatch, missing=True, real_ocr=True, name="رخصة المركبة.pdf")
    dialog.name_card_value(0, "16/16402 رخصة المركبة")
    dialog.session.add_box()
    dialog.session.step(2)
    dialog.command("skip")
    dialog.session.step(-2)
    dialog.refresh()
    folder = Path("/tmp/gui")
    folder.mkdir(parents=True, exist_ok=True)
    qtbot.wait(200)
    assert dialog.grab().save(str(folder / "cards-workspace.png"))
    result, _plan = save(qtbot, dialog, monkeypatch, "combined", tmp_path / "cards.pdf")
    assert len(PdfReader(result.outputs[0]).pages) == 2 * len(dialog.session.kept())
    qtbot.wait(100)
    assert dialog.grab().save(str(folder / "cards-save-result.png"))
    teardown(dialog)


def test_batch_defaults_enter_and_j_choose_kept_card_folders(qtbot, tmp_path, monkeypatch):
    dialog = workspace(qtbot, tmp_path, monkeypatch)
    assert dialog.save_kind.currentData() == "pdf-folder"
    folders = [tmp_path / "pdf-cards", tmp_path / "jpg-cards"]
    for folder in folders:
        folder.mkdir()
    destinations = iter(folders)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args, **kwargs: str(next(destinations)))
    monkeypatch.setattr(dialog, "review_export", lambda _plan: True)
    dialog.editor.front_view.setFocus()
    with qtbot.waitSignal(dialog.completed, timeout=60000):
        qtbot.keyClick(dialog.editor.front_view, Qt.Key.Key_Return)
    assert len(dialog.job_result.outputs) == 8
    assert all(path.parent == folders[0] and len(PdfReader(path).pages) == 2 for path in dialog.job_result.outputs)
    with qtbot.waitSignal(dialog.completed, timeout=60000):
        qtbot.keyClick(dialog.editor.front_view, Qt.Key.Key_J)
    assert len(dialog.job_result.outputs) == 16
    assert all(path.parent == folders[1] and path.suffix == ".jpg" for path in dialog.job_result.outputs)
    teardown(dialog)


def test_single_card_retains_single_pdf_and_jpg_defaults(qtbot, tmp_path, monkeypatch):
    source = multi_card_scan(tmp_path / "batch.pdf")
    single = tmp_path / "single.pdf"
    page = PdfReader(source).pages[0]
    page.cropbox.lower_left = (0, 600)
    page.cropbox.upper_right = (595, 842)
    from pypdf import PdfWriter
    with PdfWriter() as writer:
        writer.add_page(page)
        writer.write(single)
    monkeypatch.setattr(ocr, "resolve_tesseract", lambda: None)
    dialog = CardsWindow([single], settings_path=tmp_path / "settings.json")
    qtbot.addWidget(dialog, before_close_func=lambda widget: widget.shutdown())
    dialog.show()
    qtbot.waitUntil(lambda: dialog.session is not None, timeout=30000)
    assert len(dialog.session.cards) == 1
    assert dialog.save_kind.currentData() == "pdf"
    assert dialog.default_destination("pdf").name == "single_card.pdf"
    assert dialog.default_destination("jpg").name == "single_front.jpg"
    teardown(dialog)
