"""Real PDF jobs through native dialogs; captures are produced by running tests.

No screenshots claim a successful Ghostscript run when the helper is absent.
"""
from pathlib import Path
import threading

from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
import pytest
from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import QApplication, QFileDialog

from pdf_runtime import resolve_tool
from powerpdf_core.jobs import CancelToken
from powerpdf_gui.operations import OperationDialog, _check_plan, _plan


def make_pdf(path, pages=2):
    writer = PdfWriter()
    for number in range(pages):
        writer.add_blank_page(210 + number * 10, 297)
    writer.add_metadata({"/Title": "Operation acceptance sample", "/Author": "PowerPDF"})
    writer.write(path)
    return path


def scan_pdf(path):
    images = []
    for number in range(3):
        image = Image.new("RGB", (700, 990), "#faf8f3")
        draw = ImageDraw.Draw(image)
        draw.rectangle((35, 35, 665, 180), fill="#243b46")
        draw.text((65, 80), f"POWERPDF / LOCAL SAMPLE / PAGE {number + 1}", fill="white")
        for row in range(12):
            draw.text((65, 240 + row * 42), f"Real local PDF job. Row {row + 1}. Originals stay unchanged.", fill="#243b46")
        images.append(image)
    images[0].save(path, "PDF", resolution=100, save_all=True, append_images=images[1:], title="Remove this metadata")
    for image in images:
        image.close()
    return path


def dialog_ready(qtbot, action, targets):
    dialog = OperationDialog(action, targets)
    qtbot.addWidget(dialog)
    dialog.launch()
    qtbot.waitUntil(lambda: dialog._plan is not None, timeout=15000)
    return dialog


def wait_plan(qtbot, dialog):
    qtbot.waitUntil(lambda: dialog._plan is not None and not dialog._preparing, timeout=15000)


def capture(dialog, name):
    folder = Path("/tmp/gui")
    folder.mkdir(parents=True, exist_ok=True)
    assert dialog.grab().save(str(folder / name))


@pytest.mark.parametrize("action", ["merge", "split", "compress", "images"])
def test_real_operation_dialog_progress_result_captures(qtbot, tmp_path, action):
    from powerpdf_gui import resources, themes
    resources.load_fonts(QApplication.instance())
    themes.apply_theme(QApplication.instance(), "powerpdf")
    first = scan_pdf(tmp_path / "رخصة المركبة.pdf")
    second = make_pdf(tmp_path / "Ahmed عقد.pdf", 1)
    targets = [second, first] if action == "merge" else [first]
    originals = {path: path.read_bytes() for path in targets}
    dialog = dialog_ready(qtbot, action, targets)
    assert dialog.targets() == tuple(targets)
    if action == "merge":
        dialog.move_target(1)
        wait_plan(qtbot, dialog)
        assert dialog.targets() == (first, second)
    if action == "compress":
        assert dialog.quality.currentData() == "ebook"
        assert not dialog.crop.isChecked()
    if action == "images":
        dialog.format.setCurrentIndex(1)
        dialog.dpi.setValue(100)
        wait_plan(qtbot, dialog)
    capture(dialog, f"{action}-dialog.png")
    captures = []
    completed_threads = []

    def progress_capture(_event):
        if not captures and _event.completed > 0:
            capture(dialog, f"{action}-progress.png")
            captures.append(True)

    dialog._signals.progress.connect(progress_capture, Qt.ConnectionType.QueuedConnection)
    dialog.completed.connect(lambda _result: completed_threads.append(QThread.currentThread()))
    with qtbot.waitSignal(dialog.completed, timeout=60000):
        dialog.run()
    result = dialog.job_result
    assert captures
    assert completed_threads == [QApplication.instance().thread()]
    assert dialog._worker.thread is None
    assert not dialog.busy
    capture(dialog, f"{action}-result.png")
    if action == "compress" and not resolve_tool("ghostscript"):
        assert not result.success
        assert "Ghostscript not found" in result.errors[0].error
        assert not result.outputs
        assert "errors" in dialog.status_label.text()
    else:
        assert result.success, result.errors
        assert result.output_bytes == sum(path.stat().st_size for path in result.outputs) > 0
        assert all(path.is_file() for path in result.outputs)
        if action == "merge":
            reader = PdfReader(result.outputs[0])
            assert len(reader.pages) == 4
            assert float(reader.pages[-1].mediabox.width) == 210
        elif action == "split":
            assert len(result.outputs) == 3
            assert all(len(PdfReader(path).pages) == 1 for path in result.outputs)
        elif action == "compress":
            reader = PdfReader(result.outputs[0])
            assert len(reader.pages) == 3
            assert reader.metadata is None
        else:
            assert len(result.outputs) == 3
            for path in result.outputs:
                with Image.open(path) as image:
                    assert image.format == "PNG"
                    assert image.width > 0 and image.height > 0
    assert all(path.read_bytes() == snapshot for path, snapshot in originals.items())
    dialog.close()


def test_merge_destination_chooser_and_review_order(qtbot, tmp_path, monkeypatch):
    first, second = make_pdf(tmp_path / "b.pdf"), make_pdf(tmp_path / "a.pdf")
    dialog = dialog_ready(qtbot, "merge", [first, second, first])
    assert dialog.targets() == (first, second, first)
    chosen = tmp_path / "ordered.pdf"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(chosen), "PDF"))
    dialog.choose_destination()
    wait_plan(qtbot, dialog)
    assert dialog._plan.outputs == (chosen,)
    with qtbot.waitSignal(dialog.completed, timeout=15000):
        dialog.run()
    assert len(PdfReader(chosen).pages) == 6
    assert dialog.job_result.success
    dialog.close()


def test_all_replacements_and_obsolete_outputs_confirmed(qtbot, tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "source.pdf", 2)
    folder = tmp_path / "source_pages"
    folder.mkdir()
    old = [make_pdf(folder / f"source_page_{index:03}.pdf", 1) for index in range(1, 4)]
    unrelated = folder / "notes.txt"
    unrelated.write_text("Keep me")
    snapshots = {path: path.read_bytes() for path in old}
    dialog = dialog_ready(qtbot, "split", [source])
    decisions = []

    def deny(paths, obsolete):
        decisions.append((paths, obsolete))
        return False

    monkeypatch.setattr(dialog, "_confirm_overwrite", deny)
    dialog.run()
    qtbot.waitUntil(lambda: bool(decisions))
    assert decisions == [(tuple(old[:2]), (old[2],))]
    assert dialog.job_result is None
    assert all(path.read_bytes() == snapshot for path, snapshot in snapshots.items())
    monkeypatch.setattr(dialog, "_confirm_overwrite", lambda *args: True)
    with qtbot.waitSignal(dialog.completed, timeout=15000):
        dialog.run()
    assert dialog.job_result.success
    assert not old[2].exists()
    assert unrelated.read_text() == "Keep me"
    dialog.close()


def test_batch_destinations_and_per_input_result_bytes(qtbot, tmp_path):
    first, second = make_pdf(tmp_path / "second.pdf", 2), make_pdf(tmp_path / "first.pdf", 1)
    destination = tmp_path / "export"
    destination.mkdir()
    dialog = dialog_ready(qtbot, "images", [first, second])
    dialog.destination.setText(str(destination))
    dialog.dpi.setValue(50)
    wait_plan(qtbot, dialog)
    assert dialog._plan.outputs == (
        destination / "second_images/second_page_001.jpg",
        destination / "second_images/second_page_002.jpg",
        destination / "first_image.jpg",
    )
    with qtbot.waitSignal(dialog.completed, timeout=30000):
        dialog.run()
    assert dialog.job_result.success
    assert [outcome.inputs for outcome in dialog.job_result.outcomes] == [(first,), (second,)]
    assert dialog.results.topLevelItemCount() == 2
    assert all(outcome.output_bytes > 0 for outcome in dialog.job_result.outcomes)
    dialog.close()


def test_single_page_image_defaults_and_dpi_quality_controls(qtbot, tmp_path):
    source = make_pdf(tmp_path / "شهادة 2026.pdf", 1)
    images = dialog_ready(qtbot, "images", [source])
    assert images.dpi.minimum() == 50 and images.dpi.maximum() == 600
    assert images._plan.outputs == (tmp_path / "شهادة 2026_image.jpg",)
    images.format.setCurrentIndex(1)
    wait_plan(qtbot, images)
    assert images._plan.outputs == (tmp_path / "شهادة 2026_image.png",)
    images.close()
    compress = dialog_ready(qtbot, "compress", [source])
    compress.quality.setCurrentIndex(1)
    compress.crop.setChecked(True)
    wait_plan(qtbot, compress)
    assert "unreadable" in compress.warning_label.text()
    assert compress._plan.specs[0].options == {"quality": "screen", "crop": True}
    compress.close()


@pytest.mark.parametrize("action", ["merge", "compress"])
def test_preflight_rejects_sources_and_hardlink_aliases(tmp_path, action):
    source = make_pdf(tmp_path / "source.pdf")
    alias = tmp_path / "alias.pdf"
    alias.hardlink_to(source)
    for output in (source, alias):
        with pytest.raises(ValueError, match="input"):
            _plan(action, (source,), str(output), {"quality": "ebook"} if action == "compress" else {}, CancelToken())
    assert len(PdfReader(source).pages) == 2


def test_batch_cross_input_collision_and_duplicate_stems_rejected(tmp_path):
    first = make_pdf(tmp_path / "source.pdf")
    second = make_pdf(tmp_path / "source_small.pdf")
    with pytest.raises(ValueError, match="input"):
        _plan("compress", (first, second), "", {"quality": "ebook", "crop": False}, CancelToken())
    subfolder = tmp_path / "nested"
    subfolder.mkdir()
    duplicate = make_pdf(subfolder / first.name)
    with pytest.raises(ValueError, match="same output"):
        _plan("split", (first, duplicate), str(tmp_path), {}, CancelToken())


def test_preflight_detects_changed_output_and_new_obsolete_files(tmp_path):
    source = make_pdf(tmp_path / "source.pdf")
    plan = _plan("merge", (source,), "", {}, CancelToken())
    make_pdf(plan.outputs[0])
    with pytest.raises(ValueError, match="changed after review"):
        _check_plan(plan, (source,))
    folder = tmp_path / "source_pages"
    folder.mkdir()
    split = _plan("split", (source,), "", {}, CancelToken())
    make_pdf(folder / "source_page_999.pdf")
    with pytest.raises(ValueError, match="New output"):
        _check_plan(split, (source,))


def test_overwrite_race_is_structured_error_not_unapproved_replacement(qtbot, tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "source.pdf")
    output = make_pdf(tmp_path / "merged.pdf", 1)
    dialog = dialog_ready(qtbot, "merge", [source])
    changed = []

    def replace_during_confirmation(*args):
        make_pdf(output, 3)
        changed.append(output.read_bytes())
        return True

    monkeypatch.setattr(dialog, "_confirm_overwrite", replace_during_confirmation)
    with qtbot.waitSignal(dialog.completed, timeout=15000):
        dialog.run()
    assert not dialog.job_result.success
    assert "changed after review" in dialog.job_result.errors[0].error
    assert output.read_bytes() == changed[0]
    assert not dialog.job_result.outputs
    dialog.close()


def test_cooperative_cancel_keeps_real_completed_outputs(qtbot, tmp_path, monkeypatch):
    from powerpdf_gui import operations
    first, second = make_pdf(tmp_path / "first.pdf"), make_pdf(tmp_path / "second.pdf")
    dialog = dialog_ready(qtbot, "split", [first, second])
    real_run_job = operations.run_job
    waiting, release = threading.Event(), threading.Event()

    def run_real_then_pause(spec, **kwargs):
        result = real_run_job(spec, **kwargs)
        if spec.inputs == (first,):
            waiting.set()
            assert release.wait(10)
        return result

    monkeypatch.setattr(operations, "run_job", run_real_then_pause)
    dialog.run()
    qtbot.waitUntil(waiting.is_set, timeout=15000)
    with qtbot.waitSignal(dialog.completed, timeout=15000):
        dialog.cancel()
        assert not dialog.cancel_button.isEnabled()
        release.set()
    result = dialog.job_result
    assert result.cancelled
    assert len(result.outputs) == 2
    assert all(path.is_file() for path in result.outputs)
    assert not (tmp_path / "second_pages").exists()
    assert dialog.results.topLevelItem(1).text(1) == "Not started"
    assert dialog._worker.thread is None
    dialog.close()


def test_close_joins_preflight_and_empty_targets_report_error(qtbot, tmp_path):
    source = make_pdf(tmp_path / "source.pdf")
    dialog = OperationDialog("images", [source])
    qtbot.addWidget(dialog)
    dialog.show()
    dialog._timer.stop()
    dialog._prepare()
    dialog.close()
    assert dialog._worker.thread is None
    assert not dialog.busy
    empty = OperationDialog("merge", [])
    qtbot.addWidget(empty)
    empty.show()
    qtbot.waitUntil(lambda: "at least one" in empty.error_label.text())
    assert not empty.run_button.isEnabled()
    empty.close()


def test_real_crop_compression_and_batch_error_receipts(qtbot, tmp_path):
    if not resolve_tool("ghostscript"):
        pytest.skip("Ghostscript is not installed; absence is captured by the compression screenshot test")
    source = scan_pdf(tmp_path / "scan.pdf")
    dialog = dialog_ready(qtbot, "compress", [source])
    dialog.crop.setChecked(True)
    wait_plan(qtbot, dialog)
    with qtbot.waitSignal(dialog.completed, timeout=60000):
        dialog.run()
    assert dialog.job_result.success
    assert len(PdfReader(dialog.job_result.outputs[0]).pages) == 3
    assert PdfReader(dialog.job_result.outputs[0]).metadata is None
    dialog.close()
    broken = tmp_path / "invalid.pdf"
    broken.write_bytes(b"This is not a PDF")
    valid = make_pdf(tmp_path / "valid.pdf")
    batch = dialog_ready(qtbot, "compress", [broken, valid])
    with qtbot.waitSignal(batch.completed, timeout=60000):
        batch.run()
    assert not batch.job_result.success
    assert [outcome.status for outcome in batch.job_result.outcomes] == ["error", "success"]
    assert batch.job_result.errors[0].inputs == (broken,)
    assert len(batch.job_result.outputs) == 1
    assert batch.job_result.output_bytes > 0
    assert batch.results.topLevelItemCount() == 2
    batch.close()


def test_image_resolution_presets_use_export_options(qtbot, tmp_path):
    from PySide6.QtWidgets import QPushButton
    source = make_pdf(tmp_path / "source.pdf", 1)
    dialog = dialog_ready(qtbot, "images", [source])
    assert dialog.dpi.value() == 150
    for dpi in (300, 150):
        button = dialog.findChild(QPushButton, f"dpiPreset{dpi}")
        button.click()
        wait_plan(qtbot, dialog)
        assert dialog.dpi.value() == dpi
        assert dialog._plan.specs[0].options["dpi"] == dpi
    dialog.close()


def test_generated_symlink_outputs_show_real_destinations_and_detect_retarget(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", 1)
    folder = tmp_path / "source_pages"
    folder.mkdir()
    actual = make_pdf(tmp_path / "actual.pdf", 1)
    alias = folder / "source_page_001.pdf"
    alias.symlink_to(actual)
    plan = _plan("split", (source,), "", {}, CancelToken())
    assert plan.outputs == (actual,)
    _check_plan(plan, (source,))
    alias.unlink()
    alias.symlink_to(tmp_path / "unapproved.pdf")
    with pytest.raises(ValueError, match="changed after review"):
        _check_plan(plan, (source,))


def test_competing_native_writer_revision_after_confirmation_is_not_overwritten(qtbot, tmp_path, monkeypatch):
    from powerpdf_core.outputs import destination_lock
    source = make_pdf(tmp_path / "source.pdf", 2)
    output = make_pdf(tmp_path / "merged.pdf", 1)
    dialog = dialog_ready(qtbot, "merge", [source])
    locked, confirmed, replace_now = threading.Event(), threading.Event(), threading.Event()
    changed = []

    def competing_writer():
        with destination_lock(output):
            locked.set()
            assert replace_now.wait(10)
            make_pdf(output, 3)
            changed.append(output.read_bytes())

    competitor = threading.Thread(target=competing_writer)
    competitor.start()
    assert locked.wait(5)

    def confirm(*args):
        confirmed.set()
        return True

    monkeypatch.setattr(dialog, "_confirm_overwrite", confirm)
    try:
        with qtbot.waitSignal(dialog.completed, timeout=15000):
            dialog.run()
            qtbot.waitUntil(confirmed.is_set)
            replace_now.set()
        assert not dialog.job_result.success
        assert "changed after review" in dialog.job_result.errors[0].error
        assert output.read_bytes() == changed[0]
        assert not dialog.job_result.outputs
    finally:
        replace_now.set()
        competitor.join()
        dialog.close()
