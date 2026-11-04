"""Real Qt PDF-output printing only: native dialogs are adapted, never printers.

The three print screenshots are captured by the end-to-end PDF-output test.
No test submits to an installed printer or the legacy print/export job queue.
"""
import threading
import math
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
from PySide6.QtCore import QMarginsF, QRectF, QSizeF, Qt
from PySide6.QtGui import QPageLayout, QPageRanges, QPageSize
from PySide6.QtPrintSupport import QAbstractPrintDialog, QPrinter
from PySide6.QtWidgets import QDialog

from powerpdf_core.render import RenderSession
from powerpdf_gui import printing


COLORS = ((1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1))


def write_pdf(path, colors=COLORS[:2], size=(144, 216)):
    writer = PdfWriter()
    for color in colors:
        page = writer.add_blank_page(*size)
        stream = DecodedStreamObject()
        stream.set_data(
            f"{' '.join(map(str, color))} rg 0 0 {size[0]} {size[1]} re f\n".encode()
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as output:
        writer.write(output)
    return path


@pytest.fixture
def source(tmp_path):
    return write_pdf(tmp_path / "وثيقة [1].pdf")


@pytest.fixture
def make_dialog(qtbot):
    dialogs = []

    def make(targets):
        dialog = printing.PrintDialog(targets)
        qtbot.addWidget(dialog)
        dialog.show()
        dialogs.append(dialog)
        if targets:
            qtbot.waitUntil(
                lambda: dialog.start_button.isEnabled() or "Cannot print:" in dialog.status_label.text(),
                timeout=10000,
            )
        return dialog

    yield make
    for dialog in dialogs:
        dialog.shutdown()


def pdf_dialog(monkeypatch, output, *, accepted=True, configure=None):
    """Replace only the native UI; QPrinter/QPainter/PDFium remain real."""
    calls = {"exec": 0, "options": {}, "minmax": None}

    class PdfOutputDialog(QDialog):
        def __init__(self, printer, parent):
            super().__init__(parent)
            self.printer = printer
            printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
            printer.setOutputFileName(str(output))
            printer.setPageSize(QPageSize(
                QSizeF(360, 432), QPageSize.Unit.Point, "Print test sheet",
                QPageSize.SizeMatchPolicy.ExactMatch,
            ))
            printer.setPageMargins(QMarginsF(18, 24, 18, 24), QPageLayout.Unit.Point)

        def setMinMax(self, first, last):
            calls["minmax"] = (first, last)

        def setOption(self, option, enabled):
            calls["options"][option] = enabled

        def exec(self):
            calls["exec"] += 1
            if configure:
                configure(self.printer)
            return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

    monkeypatch.setattr(printing, "QPrintDialog", PdfOutputDialog)
    return calls


def submit(qtbot, dialog):
    with qtbot.waitSignal(dialog.completed, timeout=20000) as emitted:
        dialog.start()
    result = emitted.args[0]
    assert result is dialog.last_result
    return result


def output_colors(path):
    with RenderSession(path) as session:
        colors = []
        for page in range(session.info.page_count):
            image = session.render(page, scale=1, purpose="print-output-test")
            try:
                colors.append(image.getpixel((image.width // 2, image.height // 2)))
            finally:
                image.close()
        return colors


def test_explicit_acceptance_is_required(qtbot, make_dialog, source, tmp_path, monkeypatch):
    output = tmp_path / "never-submitted.pdf"
    calls = pdf_dialog(monkeypatch, output, accepted=False)
    dialog = make_dialog([source])
    completed = []
    dialog.completed.connect(completed.append)
    dialog.start()
    assert calls["exec"] == 1
    assert not output.exists()
    assert completed == []
    assert not dialog._running
    assert "No pages submitted" in dialog.status_label.text()
    assert calls["minmax"] == (1, 2)
    assert calls["options"][QAbstractPrintDialog.PrintDialogOption.PrintPageRange]
    assert not calls["options"][QAbstractPrintDialog.PrintDialogOption.PrintSelection]
    assert not calls["options"][QAbstractPrintDialog.PrintDialogOption.PrintCurrentPage]


def test_real_pdf_output_and_review_progress_result_screenshots(
    qtbot, make_dialog, source, tmp_path, monkeypatch,
):
    from PySide6.QtWidgets import QApplication
    from powerpdf_gui import resources, themes
    resources.load_fonts(QApplication.instance())
    themes.apply_theme(QApplication.instance(), "powerpdf")
    output = tmp_path / "explicit-pdf-output.pdf"
    pdf_dialog(monkeypatch, output)
    dialog = make_dialog([source])
    entered, release = threading.Event(), threading.Event()
    real_render = RenderSession.render
    renders = []
    gui_thread = threading.get_ident()

    def gated_render(session, page, *args, **kwargs):
        if kwargs.get("purpose") == "print":
            renders.append((threading.get_ident(), page, kwargs["scale"]))
            if page == 1:
                entered.set()
                if not release.wait(15):
                    raise TimeoutError("PDF-output screenshot gate was not released")
        return real_render(session, page, *args, **kwargs)

    monkeypatch.setattr(RenderSession, "render", gated_render)
    shots = Path("/tmp/gui")
    shots.mkdir(parents=True, exist_ok=True)
    assert dialog.grab().save(str(shots / "print-dialog.png"))
    try:
        with qtbot.waitSignal(dialog.completed, timeout=20000) as emitted:
            dialog.start()
            qtbot.waitUntil(lambda: entered.is_set() and dialog.progress.maximum() == 2, timeout=10000)
            assert dialog.progress.value() == 1
            assert not output.exists()  # The first page is staged; the destination is still uncommitted.
            assert not dialog.start_button.isEnabled()
            assert dialog.grab().save(str(shots / "print-progress.png"))
            release.set()
        result = emitted.args[0]
    finally:
        release.set()
    assert result.success
    assert result.targets == (source,)
    assert result.output_path == output
    assert result.submitted_pages == result.total_pages == 2
    assert all(thread != gui_thread and scale == 300 / 72 for thread, _, scale in renders)
    assert [page for _, page, _ in renders] == [0, 1]
    assert len(PdfReader(output).pages) == 2
    assert output_colors(output) == [(255, 0, 0), (0, 255, 0)]
    assert "No physical printing was requested" in dialog.status_label.text()
    qtbot.waitUntil(lambda: dialog.status_label.height() >= dialog.status_label.heightForWidth(dialog.status_label.width()))
    assert dialog.grab().save(str(shots / "print-result.png"))


@pytest.mark.parametrize("collate, expected", [
    (True, [(255, 0, 0), (0, 255, 0)] * 2),
    (False, [(255, 0, 0)] * 2 + [(0, 255, 0)] * 2),
])
def test_real_copies_are_not_duplicated_by_driver(
    qtbot, make_dialog, source, tmp_path, monkeypatch, collate, expected,
):
    output = tmp_path / "copies.pdf"
    pdf_dialog(monkeypatch, output)
    dialog = make_dialog([source])
    dialog.copies_spin.setValue(2)
    dialog.collate_check.setChecked(collate)
    result = submit(qtbot, dialog)
    assert result.success and result.total_pages == 4
    assert output_colors(output) == expected


def test_ranges_use_combined_exact_target_order(qtbot, make_dialog, tmp_path, monkeypatch):
    first = write_pdf(tmp_path / "first.pdf", COLORS[:2])
    second = write_pdf(tmp_path / "second.pdf", COLORS[2:])
    unselected = write_pdf(tmp_path / "not-selected.pdf", ((0, 0, 0),))
    output = tmp_path / "range.pdf"

    def configure(printer):
        printer.setPrintRange(QPrinter.PrintRange.PageRange)
        ranges = QPageRanges()
        ranges.addRange(2, 3)
        printer.setPageRanges(ranges)
        printer.setPageOrder(QPrinter.PageOrder.LastPageFirst)

    pdf_dialog(monkeypatch, output, configure=configure)
    targets = [second, first]
    dialog = make_dialog(targets)
    targets.append(unselected)  # A later queue mutation cannot expand this review.
    result = submit(qtbot, dialog)
    assert result.success and result.total_pages == 2
    assert result.targets == (second, first)
    assert output_colors(output) == [(255, 0, 0), (255, 0, 255)]
    assert dialog.target_list.count() == 2
    assert dialog.target_list.item(0).data(Qt.ItemDataRole.UserRole) == second


def test_duplicate_targets_stay_in_the_queue(qtbot, make_dialog, tmp_path, monkeypatch):
    source = write_pdf(tmp_path / "one.pdf", COLORS[:1])
    output = tmp_path / "duplicates.pdf"
    pdf_dialog(monkeypatch, output)
    dialog = make_dialog([source, source])
    result = submit(qtbot, dialog)
    assert result.success and result.total_pages == 2
    assert result.targets == (source, source)
    assert len(PdfReader(output).pages) == 2


@pytest.mark.parametrize("fit", [True, False])
def test_physical_points_and_margins_in_generated_pdf(
    qtbot, make_dialog, tmp_path, monkeypatch, fit,
):
    source = write_pdf(tmp_path / "dimensions.pdf", COLORS[:1])
    output = tmp_path / "geometry.pdf"
    pdf_dialog(monkeypatch, output)
    dialog = make_dialog([source])
    dialog.scale_combo.setCurrentIndex(0 if fit else 1)
    assert submit(qtbot, dialog).success
    page = PdfReader(output).pages[0]
    assert tuple(map(float, (page.mediabox.width, page.mediabox.height))) == (360, 432)
    # The image embedded by the actual Qt print engine is a full 300-dpi render,
    # independent of fitting, device resolution, or screen dimensions.
    assert page.images[0].image.size == (math.ceil(144 * (300 / 72)), math.ceil(216 * (300 / 72)))
    with RenderSession(output) as session:
        image = session.render(0, scale=2, purpose="physical-print-test")
        try:
            assert image.getpixel((360, 432)) == (255, 0, 0)
            assert image.getpixel((10, 432)) == (255, 255, 255)
            # Fit: height 384pt (24pt margins), width 256pt, left=52pt.
            # Actual: 144x216pt, centered, left=108pt / top=108pt.
            assert image.getpixel((140, 432)) == ((255, 0, 0) if fit else (255, 255, 255))
            assert image.getpixel((360, 80)) == ((255, 0, 0) if fit else (255, 255, 255))
            assert image.getpixel((360, 20)) == (255, 255, 255)
        finally:
            image.close()


def test_actual_size_clips_instead_of_shrinking(qtbot, make_dialog, tmp_path, monkeypatch):
    source = write_pdf(tmp_path / "oversized.pdf", COLORS[:1], size=(720, 864))
    output = tmp_path / "clipped.pdf"
    pdf_dialog(monkeypatch, output)
    dialog = make_dialog([source])
    dialog.scale_combo.setCurrentIndex(1)
    assert submit(qtbot, dialog).success
    bounds = QRectF(18, 24, 324, 384)
    destination = printing._destination((720, 864), bounds, False)
    assert destination.width() == 720 and destination.height() == 864
    with RenderSession(output) as session:
        image = session.render(0, scale=1, purpose="clip-test")
        try:
            assert image.getpixel((180, 216)) == (255, 0, 0)
            assert image.getpixel((5, 216)) == (255, 255, 255)
            assert image.getpixel((180, 5)) == (255, 255, 255)
        finally:
            image.close()


def test_cancel_during_raster_never_submits_that_page(
    qtbot, make_dialog, source, tmp_path, monkeypatch,
):
    output = tmp_path / "cancel.pdf"
    pdf_dialog(monkeypatch, output)
    dialog = make_dialog([source])
    entered, release = threading.Event(), threading.Event()
    real_render = RenderSession.render

    def gated(session, page, *args, **kwargs):
        image = real_render(session, page, *args, **kwargs)
        if kwargs.get("purpose") == "print":
            entered.set()
            if not release.wait(15):
                image.close()
                raise TimeoutError("Cancellation gate not released")
        return image

    monkeypatch.setattr(RenderSession, "render", gated)
    try:
        with qtbot.waitSignal(dialog.completed, timeout=20000) as emitted:
            dialog.start()
            qtbot.waitUntil(entered.is_set, timeout=10000)
            dialog.close()  # Requests cancellation, does not destroy active worker.
            assert dialog.isVisible()
            assert "Cancellation requested" in dialog.status_label.text()
            release.set()
        result = emitted.args[0]
    finally:
        release.set()
    assert result.cancelled and not result.success and not result.error
    assert result.submitted_pages == 0
    assert not output.exists()
    assert "No PDF output was committed" in dialog.status_label.text()


def test_error_after_one_page_reports_partial_submission(
    qtbot, make_dialog, source, tmp_path, monkeypatch,
):
    output = tmp_path / "partial.pdf"
    pdf_dialog(monkeypatch, output)
    dialog = make_dialog([source])
    real_render = RenderSession.render

    def broken_second(session, page, *args, **kwargs):
        if kwargs.get("purpose") == "print" and page == 1:
            raise OSError("Raster failed on page two")
        return real_render(session, page, *args, **kwargs)

    monkeypatch.setattr(RenderSession, "render", broken_second)
    result = submit(qtbot, dialog)
    assert not result.success and not result.cancelled
    assert result.submitted_pages == 1 and result.total_pages == 2
    assert "Raster failed on page two" in result.error
    assert "No PDF output was committed" in dialog.status_label.text()
    assert "did not replace the destination" in dialog.status_label.text()
    assert result.output_path is None
    assert not output.exists()
    assert not list(tmp_path.glob(".powerpdf_print_*"))


def test_source_changed_after_review_is_not_printed(qtbot, make_dialog, source, tmp_path, monkeypatch):
    output = tmp_path / "changed.pdf"
    pdf_dialog(monkeypatch, output)
    dialog = make_dialog([source])
    write_pdf(source, COLORS[2:])
    result = submit(qtbot, dialog)
    assert "Source changed since print review" in result.error
    assert result.submitted_pages == 0
    assert not output.exists()


def test_output_cannot_overwrite_source(qtbot, make_dialog, source, monkeypatch):
    original = source.read_bytes()
    pdf_dialog(monkeypatch, source)
    result = submit(qtbot, make_dialog([source]))
    assert "must not overwrite a source PDF" in result.error
    assert result.submitted_pages == 0
    assert source.read_bytes() == original


def test_empty_and_unreadable_targets_never_enable_printing(make_dialog, tmp_path):
    empty = make_dialog([])
    assert not empty.start_button.isEnabled()
    assert "No documents selected" in empty.status_label.text()
    missing = make_dialog([tmp_path / "missing.pdf"])
    assert not missing.start_button.isEnabled()
    assert "Cannot print:" in missing.status_label.text()


def test_invalid_native_range_is_an_error_before_output(qtbot, make_dialog, source, tmp_path, monkeypatch):
    output = tmp_path / "invalid-range.pdf"

    def configure(printer):
        printer.setPrintRange(QPrinter.PrintRange.PageRange)
        ranges = QPageRanges()
        ranges.addRange(2, 9)
        printer.setPageRanges(ranges)

    pdf_dialog(monkeypatch, output, configure=configure)
    result = submit(qtbot, make_dialog([source]))
    assert "Page range must be within" in result.error
    assert result.submitted_pages == 0
    assert not output.exists()


def test_oversized_pdf_is_rejected_without_downsampling(qtbot, make_dialog, tmp_path, monkeypatch):
    source = write_pdf(tmp_path / "oversized-canvas.pdf", COLORS[:1], size=(4000, 4000))
    output = tmp_path / "too-large.pdf"
    pdf_dialog(monkeypatch, output)
    dialog = make_dialog([source])
    dialog.scale_combo.setCurrentIndex(1)
    result = submit(qtbot, dialog)
    assert not result.success
    assert "RenderLimitError" in result.error
    assert "40,000,000" in result.error
    assert result.submitted_pages == 0 and result.output_path is None
    assert not output.exists()
    assert not list(tmp_path.glob(".powerpdf_print_*"))


@pytest.mark.parametrize("accept", [True, False])
def test_existing_pdf_requires_separate_explicit_confirmation(
    qtbot, make_dialog, source, tmp_path, monkeypatch, accept,
):
    output = write_pdf(tmp_path / "existing.pdf", COLORS[2:3])
    original = output.read_bytes()
    pdf_dialog(monkeypatch, output)
    confirmations = []

    def answer(message):
        confirmations.append((message.text(), message.defaultButton().text()))
        return printing.QMessageBox.StandardButton.Yes if accept else printing.QMessageBox.StandardButton.No

    monkeypatch.setattr(printing.QMessageBox, "exec", answer)
    dialog = make_dialog([source])
    if accept:
        result = submit(qtbot, dialog)
        assert result.success and result.output_path == output
        assert len(PdfReader(output).pages) == 2
    else:
        dialog.start()
        assert dialog.last_result is None
        assert not dialog._running
        assert "overwrite declined" in dialog.status_label.text()
        assert output.read_bytes() == original
    assert len(confirmations) == 1
    assert str(output) in confirmations[0][0]
    assert "No" in confirmations[0][1]
    assert not list(tmp_path.glob(".powerpdf_print_*"))


@pytest.mark.parametrize("cancel", [True, False])
def test_existing_output_survives_cancel_or_error_before_commit(
    qtbot, make_dialog, source, tmp_path, monkeypatch, cancel,
):
    output = write_pdf(tmp_path / "preserved.pdf", COLORS[2:3])
    original = output.read_bytes()
    pdf_dialog(monkeypatch, output)
    dialog = make_dialog([source])
    monkeypatch.setattr(dialog, "_confirm_overwrite", lambda path: True)
    real_commit = printing.commit_files

    def stop_before_commit(pairs, **kwargs):
        # Real QPainter has finished a real, valid staged PDF at this point.
        assert len(PdfReader(pairs[0][0]).pages) == 2
        assert pairs[0][0].parent.parent == output.parent
        if cancel:
            dialog._worker.cancel()
            return real_commit(pairs, **kwargs)
        raise OSError("Destination became unavailable")

    monkeypatch.setattr(printing, "commit_files", stop_before_commit)
    result = submit(qtbot, dialog)
    assert not result.success and result.output_path is None
    assert result.cancelled == cancel
    assert bool(result.error) != cancel
    assert output.read_bytes() == original
    assert not list(tmp_path.glob(".powerpdf_print_*"))


def test_destination_appearing_after_review_is_not_overwritten(
    qtbot, make_dialog, source, tmp_path, monkeypatch,
):
    output = tmp_path / "late-arrival.pdf"
    pdf_dialog(monkeypatch, output)
    real_commit = printing.commit_files
    replacement = b"Another application's output"

    def race(pairs, **kwargs):
        output.write_bytes(replacement)
        return real_commit(pairs, **kwargs)

    monkeypatch.setattr(printing, "commit_files", race)
    result = submit(qtbot, make_dialog([source]))
    assert not result.success and result.output_path is None
    assert "changed after overwrite review" in result.error
    assert output.read_bytes() == replacement
    assert not list(tmp_path.glob(".powerpdf_print_*"))


def test_native_page_plan_collates_inside_each_exact_source_job(qapp, source, tmp_path):
    second = write_pdf(tmp_path / "other.pdf", COLORS[2:])
    documents = (printing._Document(source, 2, b""), printing._Document(second, 2, b""))
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    # Explicit PDF backend: this planning test never opens a paint engine.
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(tmp_path / "not-submitted.pdf"))
    printer.setCopyCount(2)
    printer.setCollateCopies(True)
    printer.setPageOrder(QPrinter.PageOrder.LastPageFirst)
    order, total = printing._page_order(documents, printer, combine_documents=False)
    assert total == 8
    assert list(order) == [(0, 1), (0, 0)] * 2 + [(1, 1), (1, 0)] * 2
    assert printer.copyCount() == 1
    assert not printer.collateCopies()
    assert not (tmp_path / "not-submitted.pdf").exists()


def test_review_follows_application_direction(qapp, make_dialog, source):
    previous = qapp.layoutDirection()
    try:
        qapp.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        dialog = make_dialog([source])
        assert dialog.target_list.layoutDirection() == Qt.LayoutDirection.RightToLeft
        assert "\u2067" in dialog.target_list.item(0).text()
        assert dialog.target_list.item(0).data(Qt.ItemDataRole.UserRole) == source
    finally:
        qapp.setLayoutDirection(previous)


def test_native_accepted_orientation_resolution_and_copies_are_preserved(
    qtbot, make_dialog, source, tmp_path, monkeypatch,
):
    output = tmp_path / "accepted-settings.pdf"

    def configure(printer):
        printer.setResolution(600)
        printer.setPageOrientation(QPageLayout.Orientation.Landscape)
        printer.setCopyCount(2)
        printer.setCollateCopies(False)
        printer.setPrintRange(QPrinter.PrintRange.PageRange)
        ranges = QPageRanges()
        ranges.addPage(1)
        printer.setPageRanges(ranges)

    pdf_dialog(monkeypatch, output, configure=configure)
    result = submit(qtbot, make_dialog([source]))
    assert result.success and result.total_pages == 2
    pages = PdfReader(output).pages
    assert len(pages) == 2
    assert (float(pages[0].mediabox.width), float(pages[0].mediabox.height)) == (432, 360)
    assert pages[0].images[0].image.size == (math.ceil(144 * (300 / 72)), math.ceil(216 * (300 / 72)))  # Native raster ceil at 300 dpi, not device 600 dpi.
    assert output_colors(output) == [(255, 0, 0)] * 2


@pytest.mark.parametrize("failure", ["begin", "end"])
def test_print_engine_failures_are_errors_not_success(
    qtbot, make_dialog, source, tmp_path, monkeypatch, failure,
):
    output = tmp_path / "engine-failed.pdf"
    pdf_dialog(monkeypatch, output)
    real_painter = printing.QPainter

    class FailingPainter(real_painter):
        def begin(self, device):
            return False if failure == "begin" else super().begin(device)

        def end(self):
            result = super().end()
            return False if failure == "end" else result

    monkeypatch.setattr(printing, "QPainter", FailingPainter)
    result = submit(qtbot, make_dialog([source]))
    assert not result.success and result.error
    assert ("could not begin" if failure == "begin" else "could not finish") in result.error
    assert result.submitted_pages == (0 if failure == "begin" else 2)
    assert result.output_path is None
    assert not output.exists()
    assert not list(tmp_path.glob(".powerpdf_print_*"))
