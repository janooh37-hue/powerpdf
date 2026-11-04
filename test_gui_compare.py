"""Real independent PDFs through the linked native comparison workspace."""
import threading
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from powerpdf_gui import preview
from powerpdf_gui.compare import CompareWindow
from powerpdf_gui.text import FilenameEdit, filename_text


def vector_pdf(path, sizes, *, color="1 0 0", rotations=()):
    writer = PdfWriter()
    for index, (width, height) in enumerate(sizes):
        page = writer.add_blank_page(width, height)
        stream = DecodedStreamObject()
        stream.set_data(
            f"{color} rg 0 0 {width / 2} {height} re f\n"
            f"0 0 1 rg {width / 2} 0 {width / 2} {height} re f\n".encode()
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
        if index < len(rotations):
            page.rotate(rotations[index])
    writer.write(path)
    writer.close()
    return path


@pytest.fixture
def pair(tmp_path):
    original = vector_pdf(tmp_path / "رخصة المركبة [1].pdf",
                          ((300, 500), (500, 300), (300, 500)), rotations=(0, 90))
    small = vector_pdf(tmp_path / "رخصة المركبة [1]_small.pdf",
                       ((440, 640), (300, 480)), color="0 1 0", rotations=(0, 90))
    return original, small


def show_compare(qtbot, path):
    window = CompareWindow(path)
    qtbot.addWidget(window)
    with qtbot.waitSignal(window.ready, timeout=15000):
        window.show()
    return window


def image(pane):
    return pane.view.scene().items()[0].pixmap().toImage()


def render_both(qtbot, window, action):
    with qtbot.waitSignals([pane.ready for pane in window.panes], timeout=15000):
        action()


@pytest.mark.parametrize("selected", [0, 1], ids=["original-input", "compressed-input"])
def test_pair_roles_independent_pixels_sizes_and_signed_change(qtbot, pair, selected):
    window = show_compare(qtbot, pair[selected])
    try:
        assert isinstance(window, QDialog)
        assert window.original_path == pair[0]
        assert window.compressed_path == pair[1]
        assert window.original_pane.source_path == pair[0]
        assert window.compressed_pane.source_path == pair[1]
        assert window.original_pane.title_label.text() == "Original · " + filename_text(pair[0].name)
        assert window.compressed_pane.title_label.text() == "Compressed · " + filename_text(pair[1].name)
        assert window.original_pane.view is not window.compressed_pane.view
        for pane, expected in zip(window.panes, ((255, 0, 0), (0, 255, 0))):
            pixels = image(pane)
            assert not pixels.isNull()
            assert pixels.pixelColor(pixels.width() // 4, pixels.height() // 2).getRgb()[:3] == expected
            assert pixels.pixelColor(pixels.width() * 3 // 4, pixels.height() // 2).getRgb()[:3] == (0, 0, 255)
        before, after = (path.stat().st_size for path in pair)
        assert f"Original {before:,} bytes" in window.summary_label.text()
        assert f"Compressed {after:,} bytes" in window.summary_label.text()
        assert f"{(after - before) / before * 100:+.1f}%" in window.summary_label.text()
        assert "3 pages" in window.page_counts_label.text()
        assert "2 pages" in window.page_counts_label.text()
        assert "shorter document" in window.page_counts_label.text()
    finally:
        window.close()


def test_linked_navigation_uses_shorter_count_and_rotated_dimensions(qtbot, pair):
    window = show_compare(qtbot, pair[0])
    try:
        assert window.page_count == 2
        assert window.page_spin.maximum() == 2
        assert not window.previous_button.isEnabled()
        render_both(qtbot, window, lambda: qtbot.mouseClick(window.next_button, Qt.MouseButton.LeftButton))
        assert window.page_index == 1
        assert all(pane.page_index == 1 for pane in window.panes)
        assert not window.next_button.isEnabled()
        assert window.original_pane.document_info.page_sizes[1] == (300, 500)
        assert window.compressed_pane.document_info.page_sizes[1] == (480, 300)
        for pane, ratio in zip(window.panes, (300 / 500, 480 / 300)):
            pixels = image(pane)
            assert pixels.width() / pixels.height() == pytest.approx(ratio, abs=.01)
        window.next_page()
        assert all(pane.page_index == 1 for pane in window.panes)
        # Even public pane navigation cannot escape the common page range.
        window.original_pane.set_page(2)
        assert all(pane.page_index == 1 for pane in window.panes)
        render_both(qtbot, window, lambda: window.page_spin.setValue(1))
        assert all(pane.page_index == 0 for pane in window.panes)
        render_both(qtbot, window, window.compressed_pane.next_page)
        assert window.page_spin.value() == 2
    finally:
        window.close()


def assert_zoom_display(window, mode):
    prefix = {"page": "Fit page ", "width": "Fit width ", None: "Custom "}[mode]
    assert window.zoom_spin.prefix() == prefix
    assert window.zoom_spin.value() == pytest.approx(window.original_pane.zoom * 100, abs=.05)
    assert f"Original {window.zoom_spin.value():.1f}%" in window.zoom_label.text()
    assert f"Compressed {window.compressed_pane.zoom * 100:.1f}%" in window.zoom_label.text()
    assert prefix.strip() in window.zoom_spin.accessibleName()
    assert "original" in window.zoom_spin.accessibleName().lower()
    assert "custom" in window.zoom_spin.toolTip().lower()


def test_fit_percentage_control_matches_active_original_scale_and_edit_commits_custom(qtbot, pair):
    window = show_compare(qtbot, pair[0])
    try:
        assert_zoom_display(window, "page")
        # A fit may differ per document; the editable control names its baseline.
        render_both(qtbot, window, lambda: window.resize(1000, 700))
        assert_zoom_display(window, "page")
        assert window.original_pane.zoom != window.compressed_pane.zoom
        # Repaint/fit notifications must never feed the percentage back as custom.
        changes = []
        for pane in window.panes:
            pane.zoom_mode_changed.connect(changes.append)
        window.zoom_spin.setFocus()
        window.original_pane.view.setFocus()
        assert all(pane.zoom_mode == "page" for pane in window.panes)
        render_both(qtbot, window, lambda: window.resize(1100, 750))
        assert_zoom_display(window, "page")
        assert changes == []
        # Committing even the currently displayed percentage explicitly leaves fit.
        field = window.zoom_spin.lineEdit()
        field.setFocus()
        field.selectAll()
        QTest.keyClicks(field, f"{window.zoom_spin.value():.1f}")
        render_both(qtbot, window, lambda: qtbot.keyClick(field, Qt.Key.Key_Return))
        assert all(pane.zoom_mode is None for pane in window.panes)
        assert window.original_pane.zoom == window.compressed_pane.zoom
        assert_zoom_display(window, None)
        render_both(qtbot, window, window.compressed_pane.fit_width)
        assert_zoom_display(window, "width")
    finally:
        window.close()


def test_fit_custom_zoom_bidirectional_hooks_and_resize(qtbot, pair):
    window = show_compare(qtbot, pair[0])
    try:
        render_both(qtbot, window, lambda: qtbot.mouseClick(window.fit_width_button, Qt.MouseButton.LeftButton))
        assert all(pane.zoom_mode == "width" for pane in window.panes)
        assert_zoom_display(window, "width")
        for pane in window.panes:
            expected = (pane.view.viewport().width() - 32) / pane.document_info.page_sizes[0][0]
            assert pane.zoom * preview.POINTS_TO_DIP == pytest.approx(expected, abs=.01)
        assert window.original_pane.zoom != window.compressed_pane.zoom
        render_both(qtbot, window, lambda: window.zoom_spin.setValue(225))
        assert all(pane.zoom_mode is None and pane.zoom == pytest.approx(2.25) for pane in window.panes)
        assert_zoom_display(window, None)
        for pane in window.panes:
            assert pane.view.transform().m11() == pytest.approx(2.25 * preview.POINTS_TO_DIP)
        render_both(qtbot, window, lambda: window.compressed_pane.set_zoom(3))
        assert all(pane.zoom == pytest.approx(3) for pane in window.panes)
        assert window.zoom_spin.value() == 300
        render_both(qtbot, window, window.compressed_pane.fit_page)
        assert all(pane.zoom_mode == "page" for pane in window.panes)
        assert_zoom_display(window, "page")
        old = tuple(pane.zoom for pane in window.panes)
        render_both(qtbot, window, lambda: window.resize(1200, 800))
        assert all(pane.zoom_mode == "page" for pane in window.panes)
        assert tuple(pane.zoom for pane in window.panes) != old
        assert_zoom_display(window, "page")
        render_both(qtbot, window, lambda: qtbot.mouseClick(window.zoom_in_button, Qt.MouseButton.LeftButton))
        assert window.original_pane.zoom_mode is None
        assert window.compressed_pane.zoom == window.original_pane.zoom
        render_both(qtbot, window, lambda: qtbot.mouseClick(window.zoom_out_button, Qt.MouseButton.LeftButton))
    finally:
        window.close()


@pytest.mark.parametrize("source_index", [0, 1], ids=["drag-original", "drag-compressed"])
@pytest.mark.parametrize("page", [0, 1], ids=["different-sizes", "rotated-sizes"])
def test_mouse_drag_and_wheel_pan_bidirectionally_normalized(qtbot, pair, source_index, page):
    window = show_compare(qtbot, pair[0])
    try:
        if page:
            render_both(qtbot, window, lambda: window.set_page(page))
        render_both(qtbot, window, lambda: window.set_zoom(4))
        source, target = window.panes[source_index], window.panes[1 - source_index]
        for pane in window.panes:
            pane.set_normalized_pan(.5, .5)
            assert pane.view.horizontalScrollBar().maximum() > 0
            assert pane.view.verticalScrollBar().maximum() > 0
        changes = []
        source.pan_changed.connect(lambda x, y: changes.append((x, y)))
        viewport = source.view.viewport()
        start = viewport.rect().center()
        before = source.normalized_pan
        qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(viewport, start + QPoint(90, 75), delay=20)
        qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=start + QPoint(90, 75))
        assert changes
        assert source.normalized_pan != before
        assert target.normalized_pan == pytest.approx(source.normalized_pan, abs=.005)
        before = source.normalized_pan
        wheel = QWheelEvent(QPointF(start), QPointF(viewport.mapToGlobal(start)),
                            QPoint(), QPoint(0, -120), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
        QApplication.sendEvent(viewport, wheel)
        assert source.normalized_pan[1] > before[1]
        assert target.normalized_pan == pytest.approx(source.normalized_pan, abs=.005)
        # Actual scrollbars, rather than only synchronization setters, link too.
        bar = target.view.horizontalScrollBar()
        bar.setValue(bar.minimum() + (bar.maximum() - bar.minimum()) * 3 // 4)
        assert source.normalized_pan == pytest.approx(target.normalized_pan, abs=.005)
        render_both(qtbot, window, lambda: window.set_zoom(3.5))
        assert source.normalized_pan == pytest.approx(target.normalized_pan, abs=.005)
    finally:
        window.close()


def test_keyboard_navigation_zoom_and_native_text_not_trapped(qtbot, pair):
    window = show_compare(qtbot, pair[0])
    try:
        viewport = window.original_pane.view.viewport()
        viewport.setFocus()
        render_both(qtbot, window, lambda: QTest.keyClick(viewport, Qt.Key.Key_BracketRight))
        assert window.page_index == 1
        render_both(qtbot, window, lambda: QTest.keyClick(viewport, Qt.Key.Key_BracketLeft))
        assert window.page_index == 0
        render_both(qtbot, window, lambda: QTest.keyClick(viewport, Qt.Key.Key_Plus))
        assert window.original_pane.zoom_mode is None
        render_both(qtbot, window, lambda: QTest.keyClick(viewport, Qt.Key.Key_0, Qt.KeyboardModifier.ControlModifier))
        assert all(pane.zoom_mode == "page" for pane in window.panes)
        render_both(qtbot, window, lambda: QTest.keyClick(viewport, Qt.Key.Key_1, Qt.KeyboardModifier.ControlModifier))
        assert all(pane.zoom_mode == "width" for pane in window.panes)
        editor = FilenameEdit("", window)
        window.layout().addWidget(editor)
        editor.show()
        editor.setFocus()
        before = (window.page_index, window.original_pane.zoom)
        QTest.keyClicks(editor, "d[]+-")
        assert editor.text() == "d[]+-"
        assert (window.page_index, window.original_pane.zoom) == before
        with qtbot.waitSignal(window.finished, timeout=10000):
            QTest.keyClick(editor, Qt.Key.Key_Escape)
        assert all(not pane._worker._thread.is_alive() for pane in window.panes)
    finally:
        window.shutdown()


@pytest.mark.parametrize("selected_small", [False, True])
def test_missing_partner_is_actionable_and_never_starts_pdf_session(qtbot, tmp_path, selected_small, monkeypatch):
    path = tmp_path / ("مفقود_small.pdf" if selected_small else "مفقود.pdf")
    vector_pdf(path, ((300, 500),))
    calls = []
    original = preview.RenderSession

    class RecordedSession(original):
        def __init__(self, path):
            calls.append(path)
            super().__init__(path)

    monkeypatch.setattr(preview, "RenderSession", RecordedSession)
    window = CompareWindow(path)
    qtbot.addWidget(window)
    window.show()
    try:
        qtbot.waitUntil(lambda: "missing" in window.summary_label.text())
        assert "Compress" in window.status_label.text()
        assert "same folder" in window.status_label.text()
        assert not window.next_button.isEnabled()
        assert not window.zoom_spin.isEnabled()
        assert not calls
    finally:
        window.close()
    assert all(not pane._worker._thread.is_alive() for pane in window.panes)


def test_larger_partner_warning_uses_actual_bytes(qtbot, pair):
    original, small = pair
    with small.open("ab") as output:
        output.write(b"\n% Larger output fixture\n" + b" " * (original.stat().st_size + 1000))
    window = show_compare(qtbot, small)
    try:
        assert small.stat().st_size > original.stat().st_size
        assert window.warning_label.isVisible()
        assert "Larger output" in window.warning_label.text()
        assert "Size change +" in window.summary_label.text()
    finally:
        window.close()


def test_metadata_does_not_block_gui_and_close_joins_both_sessions(qtbot, pair, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    sessions = []
    original = preview.RenderSession

    class DelayedSession(original):
        def __init__(self, path):
            if path == pair[1]:
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("Real metadata open was not released")
            super().__init__(path)
            self.closed_for_test = False
            sessions.append(self)

        def close(self):
            self.closed_for_test = True
            super().close()

    monkeypatch.setattr(preview, "RenderSession", DelayedSession)
    window = CompareWindow(pair[0])
    qtbot.addWidget(window)
    window.show()
    try:
        qtbot.waitUntil(entered.is_set, timeout=10000)
        qtbot.waitUntil(lambda: window.original_pane.document_info is not None, timeout=10000)
        assert window.page_count == 0
        assert not window.page_spin.isEnabled()
        assert window.compressed_pane.document_info is None
        # Closing must not rely on delivering queued GUI callbacks to unblock.
        timer = threading.Timer(.05, release.set)
        timer.start()
        try:
            with qtbot.waitSignal(window.finished, timeout=10000):
                window.close()
        finally:
            timer.join()
        assert len(sessions) == 2
        assert all(session.closed_for_test for session in sessions)
        assert all(not pane._worker._thread.is_alive() for pane in window.panes)
        window.shutdown()
    finally:
        release.set()
        window.shutdown()


def test_close_during_real_render_ignores_stale_completions(qtbot, pair, monkeypatch):
    window = show_compare(qtbot, pair[0])
    entered, release = threading.Event(), threading.Event()
    original = preview.RenderSession.render

    def delayed(self, page, **kwargs):
        if self.source_path == pair[0] and page == 1:
            entered.set()
            if not release.wait(10):
                raise TimeoutError("Real render was not released")
        return original(self, page, **kwargs)

    monkeypatch.setattr(preview.RenderSession, "render", delayed)
    completions = []
    window.ready.connect(lambda: completions.append(True))
    for pane in window.panes:
        pane.ready.connect(lambda path: completions.append(path))
    try:
        window.next_page()
        qtbot.waitUntil(entered.is_set, timeout=10000)
        completions.clear()  # A partner may legitimately finish before close begins.
        timer = threading.Timer(.05, release.set)
        timer.start()
        try:
            window.close()
        finally:
            timer.join()
        qtbot.wait(50)  # Deliver already queued metadata/image events after close.
        assert not completions
        assert all(not pane._worker._thread.is_alive() for pane in window.panes)
    finally:
        release.set()
        window.shutdown()


def test_real_vector_original_and_ghostscript_output_capture(qtbot, tmp_path):
    from gui_test_fixtures import multi_card_scan
    from pdf_runtime import resolve_tool
    from powerpdf_core.compress import compress
    from powerpdf_gui import resources, themes

    gs = resolve_tool("ghostscript")
    if not gs:
        pytest.skip("Ghostscript is required for an actual compressed comparison capture")
    original = multi_card_scan(tmp_path / "مقارنة رخص المركبات.pdf")
    small = compress(gs, original, quality="ebook")
    assert small.is_file()
    assert small != original
    app = QApplication.instance()
    previous_palette, previous_style = app.palette(), app.styleSheet()
    resources.load_fonts(app)
    themes.apply_theme(app, "powerpdf")
    window = show_compare(qtbot, small)
    try:
        assert all(not image(pane).isNull() for pane in window.panes)
        assert_zoom_display(window, "page")
        folder = Path("/tmp/gui")
        folder.mkdir(parents=True, exist_ok=True)
        assert window.grab().save(str(folder / "compare-view.png"))
    finally:
        window.close()
        app.setPalette(previous_palette)
        app.setStyleSheet(previous_style)


@pytest.mark.parametrize("direction", [Qt.LayoutDirection.LeftToRight, Qt.LayoutDirection.RightToLeft])
def test_arabic_pair_titles_follow_application_direction(qtbot, pair, direction):
    previous = QApplication.layoutDirection()
    QApplication.setLayoutDirection(direction)
    window = None
    try:
        window = show_compare(qtbot, pair[1])
        for pane, role, path in zip(window.panes, ("Original", "Compressed"), pair):
            assert pane.title_label.text() == role + " · " + filename_text(path.name, direction)
            assert pane.source_path.name == path.name
    finally:
        if window is not None:
            window.close()
        QApplication.setLayoutDirection(previous)


def test_compare_reuses_preview_physical_scale_and_memory_budget(qtbot, pair, monkeypatch):
    window = show_compare(qtbot, pair[0])
    try:
        monkeypatch.setattr(preview, "SCREEN_PIXEL_BUDGET", 60_000)
        for pane in window.panes:
            monkeypatch.setattr(pane.view.viewport(), "devicePixelRatioF", lambda: 2.0)
        render_both(qtbot, window, lambda: window.set_zoom(3))
        for pane in window.panes:
            pixels = image(pane)
            assert pixels.width() * pixels.height() <= 60_000
            assert pane.zoom == 3
            assert pane.view.transform().m11() == pytest.approx(3 * preview.POINTS_TO_DIP)
            assert "Preview detail limited" in pane.status_label.text()
            assert "requested zoom is preserved" in pane.status_label.text()
    finally:
        window.close()
