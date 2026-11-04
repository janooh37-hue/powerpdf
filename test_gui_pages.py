"""Real vector PDF page-grid acceptance; capture writes /tmp/gui/pages-grid.png.

No owner documents are used. Native chooser adapters do not qualify OS dialogs.
"""
import json
import threading
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from PySide6.QtCore import QItemSelectionModel, QMimeData, QModelIndex, QThread, Qt, QUrl
from PySide6.QtWidgets import QApplication, QFileDialog, QListView, QMessageBox

from powerpdf_core.jobs import CancelToken
from powerpdf_core.pages import PageEditSession
from powerpdf_core.render import RenderSession, THUMBNAIL_CACHE_BYTES
from powerpdf_gui import pages as gui_pages
from powerpdf_gui.pages import MIME, PagesWindow, _execute_export, _export_plan


def vector_pdf(path, count=8):
    """Small vector source: unique media widths, labels, colors and intrinsic /Rotate."""
    with PdfWriter() as writer:
        font = writer._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }))
        for index in range(count):
            page = writer.add_blank_page(width=300 + index, height=420)
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
            })
            stream = DecodedStreamObject()
            stream.set_data((f"q 0.12 {0.3 + (index % 5) * .08:.2f} 0.55 rg 20 250 260 140 re f Q\n"
                             f"BT /F1 25 Tf 25 210 Td (SYNTHETIC PAGE {index + 1}) Tj ET\n"
                             "BT /F1 13 Tf 25 170 Td (PowerPDF local vector fixture) Tj ET\n").encode())
            page[NameObject("/Contents")] = writer._add_object(stream)
            if index == 0:
                page.rotate(90)
        writer.write(path)
    return path


def workspace(qtbot, tmp_path, count=8, name="رخصة المركبة.pdf"):
    dialog = PagesWindow(vector_pdf(tmp_path / name, count))
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: not dialog._loading, timeout=30000)
    assert dialog.session is not None, dialog.status_label.text()
    return dialog


def teardown(dialog):
    dialog.shutdown()
    dialog.close()


def select(dialog, pages, cursor=None):
    selection = dialog.grid.selectionModel()
    selection.clearSelection()
    for page in pages:
        row = dialog.session.order.index(page)
        selection.select(dialog.model.index(row), QItemSelectionModel.SelectionFlag.Select)
    if cursor is not None:
        row = dialog.session.order.index(cursor)
        selection.setCurrentIndex(dialog.model.index(row), QItemSelectionModel.SelectionFlag.NoUpdate)


def saved(qtbot, dialog, monkeypatch, destination=None, **kwargs):
    reviewed = []
    monkeypatch.setattr(dialog, "review_export", lambda plan: reviewed.append(plan) or True)
    with qtbot.waitSignal(dialog.completed, timeout=30000):
        dialog.save(destination, **kwargs)
    assert dialog.job_result.success, dialog.job_result.errors
    assert reviewed and dialog.job_result.outputs == reviewed[0].outputs
    return dialog.job_result


def test_virtualized_long_pdf_visible_subset_and_bounded_images(qtbot, tmp_path, monkeypatch):
    rendered = []
    threads = []
    original = RenderSession.render
    def record(self, page, *args, **kwargs):
        rendered.append((page, kwargs.get("rotation", 0), kwargs.get("purpose")))
        threads.append(threading.current_thread())
        return original(self, page, *args, **kwargs)
    monkeypatch.setattr(RenderSession, "render", record)
    dialog = workspace(qtbot, tmp_path, count=128)
    assert isinstance(dialog.grid, QListView)
    assert dialog.model.rowCount() == 128
    qtbot.waitUntil(lambda: bool(dialog.model.images), timeout=30000)
    dialog.request_thumbnails()
    expected = set(dialog._wanted)
    qtbot.waitUntil(lambda: expected.issubset(dialog.model.images), timeout=30000)
    assert 0 < len({page for page, _, _ in rendered}) <= len(dialog.grid.visible_rows()) + 8 < 128
    assert all(thread is not threading.main_thread() for thread in threads)
    assert all(purpose == "pages-thumbnail" for _, _, purpose in rendered)
    assert dialog.model.cache_budget == THUMBNAIL_CACHE_BYTES == 32 * 1024 * 1024
    assert dialog.model.cache_bytes <= dialog.model.cache_budget
    image = next(iter(dialog.model.images.values()))
    assert not image.isNull()
    assert len({image.pixelColor(x, y).rgb()
                for x in range(0, image.width(), max(1, image.width() // 8))
                for y in range(0, image.height(), max(1, image.height() // 8))}) > 1
    # Force real LRU eviction; lower-detail visible rasters must still fit together.
    old_keys = set(dialog.model.images)
    dialog.model.cache_budget = image.sizeInBytes() * 3
    dialog.grid.scrollTo(dialog.model.index(110), QListView.ScrollHint.PositionAtCenter)
    dialog.request_thumbnails()
    qtbot.waitUntil(lambda: any(page > 90 for page, _, _ in rendered), timeout=30000)
    expected = set(dialog._wanted)
    qtbot.waitUntil(lambda: expected.issubset(dialog.model.images), timeout=30000)
    assert dialog.model.cache_bytes <= dialog.model.cache_budget
    assert old_keys - set(dialog.model.images)
    assert all(dialog.model.images[key].sizeInBytes() < image.sizeInBytes() for key in expected)
    teardown(dialog)
    assert not dialog._worker.thread.is_alive()


def test_selection_cursor_commands_shift_reorder_history_and_empty(qtbot, tmp_path):
    dialog = workspace(qtbot, tmp_path)
    assert not dialog.dirty
    select(dialog, [], cursor=2)
    assert dialog.session.targets() == [2]
    dialog.command("rotate")
    assert dialog.session.turns == {2: 1}
    dialog.command("undo")
    assert not dialog.dirty
    select(dialog, [1, 3], cursor=1)
    dialog.command("rotate")
    assert dialog.session.turns == {1: 1, 3: 1}
    dialog.grid.setFocus()
    qtbot.keyClick(dialog.grid, Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier)
    assert dialog.session.order[:4] == [1, 0, 3, 2]
    assert dialog.session.order[dialog.session.cursor] == 1
    assert dialog.session.picked == {1, 3}
    dialog.command("delete")
    assert dialog.session.order == [0, 2, 4, 5, 6, 7]
    dialog.command("undo")
    assert dialog.session.picked == {1, 3}
    dialog.command("undo")
    assert dialog.session.order == list(range(8))
    dialog.command("redo")
    assert dialog.session.order[:4] == [1, 0, 3, 2]
    select(dialog, list(dialog.session.order))
    dialog.command("delete")
    assert not dialog.session.order and not dialog.save_button.isEnabled()
    assert not dialog.extract_button.isEnabled() and not dialog.save_as_button.isEnabled()
    dialog.command("undo")
    assert len(dialog.session.order) == 8 and dialog.save_button.isEnabled()
    teardown(dialog)


def test_internal_drag_ids_duplicate_external_rejection_and_single_undo(qtbot, tmp_path):
    dialog = workspace(qtbot, tmp_path)
    select(dialog, [1, 3], cursor=3)
    data = dialog.model.mimeData([dialog.model.index(1), dialog.model.index(3)])
    assert dialog.model.dropMimeData(data, Qt.DropAction.MoveAction, 7, 0, QModelIndex())
    assert dialog.session.order == [0, 2, 4, 5, 6, 1, 3, 7]
    assert dialog.session.order[dialog.session.cursor] == 3
    assert dialog.session.picked == {1, 3}
    dialog.command("undo")
    assert dialog.session.order == list(range(8)) and not dialog.session.can_undo
    for payload in ({"session": dialog.model.identity, "pages": [1, 1]},
                    {"session": "another-workspace", "pages": [1]},
                    {"session": dialog.model.identity, "pages": [999]},
                    {"session": dialog.model.identity, "pages": [True]}):
        invalid = QMimeData()
        invalid.setData(MIME, json.dumps(payload).encode())
        assert not dialog.model.dropMimeData(invalid, Qt.DropAction.MoveAction, 0, 0, QModelIndex())
    external = QMimeData()
    external.setUrls([QUrl.fromLocalFile(str(dialog.path))])
    assert not dialog.model.canDropMimeData(external, Qt.DropAction.MoveAction, 0, 0, QModelIndex())
    assert not dialog.model.canDropMimeData(data, Qt.DropAction.CopyAction, 0, 0, QModelIndex())
    teardown(dialog)


def test_grid_displays_native_added_and_total_rotation_without_reloading(qtbot, tmp_path, monkeypatch):
    import pypdf

    reads = []
    reader = pypdf.PdfReader

    def record(*args, **kwargs):
        reads.append(threading.current_thread())
        return reader(*args, **kwargs)

    monkeypatch.setattr(pypdf, "PdfReader", record)
    dialog = workspace(qtbot, tmp_path)
    try:
        index = dialog.model.index(0)
        assert "+0° (90° total)" in index.data()
        for role in (Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.AccessibleTextRole,
                     Qt.ItemDataRole.AccessibleDescriptionRole):
            description = index.data(role)
            assert "source rotation 90°" in description
            assert "additional rotation 0°" in description
            assert "total rotation 90°" in description
        select(dialog, [0], cursor=0)
        dialog.command("rotate")
        assert "+90° (180° total)" in dialog.model.index(0).data()
        description = dialog.model.index(0).data(Qt.ItemDataRole.ToolTipRole)
        assert "source rotation 90°" in description
        assert "additional rotation 90°" in description
        assert "total rotation 180°" in description
        dialog.request_thumbnails()
        key = dialog.model.key(0)
        qtbot.waitUntil(lambda: key in dialog.model.images, timeout=30000)
        image = dialog.model.images[key]
        assert image.width() / image.height() == pytest.approx(300 / 420, abs=.01)
        # Metadata is immutable and keyed by original ID, not by working order.
        dialog.command("move_before", 3, [0])
        row = dialog.session.order.index(0)
        for _ in range(3):
            dialog.refresh()
            assert "+90° (180° total)" in dialog.model.index(row).data()
            assert "original page 1" in dialog.model.index(row).data(Qt.ItemDataRole.AccessibleTextRole)
        dialog.command("undo")
        dialog.command("undo")
        assert "+0° (90° total)" in dialog.model.index(0).data()
        assert len(reads) == 1
        assert reads[0] is not threading.main_thread()
    finally:
        teardown(dialog)


def test_real_exports_intrinsic_additional_rotation_order_extract_and_dirty(qtbot, tmp_path, monkeypatch):
    dialog = workspace(qtbot, tmp_path)
    original = dialog.path.read_bytes()
    select(dialog, [0, 2])
    dialog.command("rotate")
    dialog.command("move_before", 0, [2])
    result = saved(qtbot, dialog, monkeypatch, extract=True)
    assert result.outputs == (dialog.default_destination(extract=True),)
    reader = PdfReader(result.outputs[0])
    assert [float(page.mediabox.width) for page in reader.pages] == [302, 300]
    assert [page.rotation for page in reader.pages] == [90, 180]
    assert dialog.dirty  # Extract does not save the whole working document.
    result = saved(qtbot, dialog, monkeypatch)
    assert result.outputs == (dialog.default_destination(),)
    reader = PdfReader(result.outputs[0])
    assert len(reader.pages) == 8 and float(reader.pages[0].mediabox.width) == 302
    assert reader.pages[1].rotation == 180
    assert result.output_bytes == result.outputs[0].stat().st_size > 0
    assert not dialog.dirty and dialog.path.read_bytes() == original
    dialog.command("undo")
    assert dialog.dirty
    dialog.command("redo")
    assert not dialog.dirty
    assert dialog._save_thread is None
    teardown(dialog)


def test_snapshot_save_edits_during_write_remain_dirty(qtbot, tmp_path, monkeypatch):
    dialog = workspace(qtbot, tmp_path)
    dialog.command("rotate")
    started, release = threading.Event(), threading.Event()
    execute = gui_pages._execute_export
    def blocked(plan, cancel, progress):
        started.set()
        release.wait(10)
        return execute(plan, cancel, progress)
    monkeypatch.setattr(gui_pages, "_execute_export", blocked)
    monkeypatch.setattr(dialog, "review_export", lambda plan: True)
    threads = []
    dialog.completed.connect(lambda result: threads.append(QThread.currentThread()))
    dialog.save()
    qtbot.waitUntil(started.is_set, timeout=5000)
    dialog.command("delete")
    try:
        with qtbot.waitSignal(dialog.completed, timeout=30000):
            release.set()
        assert dialog.job_result.success and dialog.dirty
        assert len(PdfReader(dialog.job_result.outputs[0]).pages) == 8
        assert len(dialog.session.order) == 7
        assert threads == [QApplication.instance().thread()]
        dialog.command("undo")
        assert not dialog.dirty
    finally:
        release.set()
        teardown(dialog)


def test_native_save_as_default_and_declined_review(qtbot, tmp_path, monkeypatch):
    dialog = workspace(qtbot, tmp_path)
    destination = tmp_path / "نسخة 16 رخصة.pdf"
    calls = []
    def chooser(*args, **kwargs):
        calls.append((args, kwargs))
        return str(destination), "PDF (*.pdf)"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", chooser)
    result = saved(qtbot, dialog, monkeypatch, choose=True)
    assert result.outputs == (destination,)
    assert calls[0][0][2] == str(dialog.default_destination())
    assert calls[0][1]["options"] == QFileDialog.Option.DontConfirmOverwrite
    monkeypatch.setattr(dialog, "review_export", lambda plan: False)
    dialog.save(tmp_path / "declined.pdf")
    assert not (tmp_path / "declined.pdf").exists() and not dialog.busy
    teardown(dialog)


@pytest.mark.parametrize("kind", ["same", "hardlink", "symlink"])
def test_original_and_aliases_never_overwritten(tmp_path, kind):
    source = vector_pdf(tmp_path / "source.pdf")
    session = PageEditSession(source)
    output = source if kind == "same" else tmp_path / "alias.pdf"
    if kind != "same":
        try:
            output.hardlink_to(source) if kind == "hardlink" else output.symlink_to(source)
        except OSError:
            pytest.skip("Filesystem does not support requested alias")
    with pytest.raises(ValueError):
        _export_plan(session, output)
    with pytest.raises(ValueError, match="original"):
        session.save(output)
    assert len(PdfReader(source).pages) == 8


@pytest.mark.parametrize("change", ["destination-before", "destination-during", "source-before", "source-during", "cancel"])
def test_review_source_revision_and_cancel_preserve_existing_output(tmp_path, change):
    source = vector_pdf(tmp_path / "source.pdf")
    session = PageEditSession(source)
    output = tmp_path / "output.pdf"
    output.write_bytes(b"previous output")
    plan = _export_plan(session, output)
    cancel = CancelToken()
    if change == "destination-before":
        output.write_bytes(b"external output")
    elif change == "source-before":
        source.write_bytes(b"external source")
    elif change == "cancel":
        cancel.cancel()
    def progress(event):
        if event.completed == event.total:
            if change == "destination-during":
                output.write_bytes(b"external output")
            elif change == "source-during":
                source.write_bytes(b"external source")
    result = _execute_export(plan, cancel, progress)
    assert not result.success and result.outputs == ()
    assert result.cancelled == (change == "cancel")
    assert output.read_bytes() == (b"external output" if change.startswith("destination") else b"previous output")
    assert not any(path.name.startswith(".powerpdf_") for path in tmp_path.iterdir())


def test_dirty_close_decline_returns_false_then_discard_joins(qtbot, tmp_path, monkeypatch):
    dialog = workspace(qtbot, tmp_path)
    dialog.command("rotate")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Cancel)
    assert dialog.close() is False
    assert dialog.isVisible() and dialog._worker.thread.is_alive()
    dialog.reject()
    assert dialog.isVisible() and not dialog._closed
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Discard)
    assert dialog.close() is True
    assert dialog._closed and not dialog._worker.thread.is_alive()


def test_capture_pages_grid_selection_and_rotation(qtbot, tmp_path):
    from powerpdf_gui import resources, themes
    resources.load_fonts(QApplication.instance())
    themes.apply_theme(QApplication.instance(), "powerpdf")
    dialog = workspace(qtbot, tmp_path, count=128, name="مستندات السيارات 16.pdf")
    select(dialog, [0, 1, 4], cursor=1)
    dialog.command("rotate")
    dialog.request_thumbnails()
    expected = set(dialog._wanted)
    qtbot.waitUntil(lambda: expected.issubset(dialog.model.images), timeout=30000)
    folder = Path("/tmp/gui")
    folder.mkdir(parents=True, exist_ok=True)
    assert dialog.grab().save(str(folder / "pages-grid.png"))
    teardown(dialog)


def test_high_dpr_cache_keeps_all_visible_and_prefetched_thumbnails(qtbot, tmp_path, monkeypatch):
    dialog = workspace(qtbot, tmp_path, count=128)
    monkeypatch.setattr(dialog.grid, "devicePixelRatioF", lambda: 4.0)
    dialog.model.cache_budget = 1024 * 1024
    dialog.request_thumbnails()
    expected = set(dialog._wanted)
    assert dialog.model.dpr < 4.0
    qtbot.waitUntil(lambda: expected.issubset(dialog.model.images), timeout=30000)
    assert dialog.model.cache_bytes <= dialog.model.cache_budget
    for row in dialog.grid.visible_rows():
        assert dialog.model.image(dialog.session.order[row]) is not None
    teardown(dialog)
