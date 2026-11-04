"""Qt behavior through real PDFs; run with QT_QPA_PLATFORM=offscreen headlessly."""

import threading
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from powerpdf_core.render import clear_render_cache
from powerpdf_gui import preview


def write_pdf(path, sizes=((240, 360), (360, 240), (300, 400)), color="1 0 0"):
    writer = PdfWriter()
    writer.add_metadata({"/Title": "عنوان المستند", "/Author": "Preview test"})
    for width, height in sizes:
        page = writer.add_blank_page(width, height)
        stream = DecodedStreamObject()
        stream.set_data(
            f"{color} rg 0 0 {width / 2} {height} re f\n"
            f"0 0 1 rg {width / 2} 0 {width / 2} {height} re f\n".encode()
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
    with Path(path).open("wb") as output:
        writer.write(output)
    return Path(path)


@pytest.fixture
def source(tmp_path):
    return write_pdf(tmp_path / "وثيقة [1].pdf")


@pytest.fixture
def pane(qtbot):
    widget = preview.PreviewPane()
    widget.resize(1000, 800)
    qtbot.addWidget(widget)
    widget.show()
    yield widget
    widget.shutdown()


def open_ready(qtbot, pane, source):
    with qtbot.waitSignal(pane.ready, timeout=10000) as result:
        pane.open_path(source)
    assert result.args == [source.absolute()]


def page_image(pane):
    return pane.view.scene().items()[0].pixmap().toImage()


def test_real_unicode_document_info_and_owned_image(qtbot, pane, source):
    infos = []
    pane.info_ready.connect(lambda path, info: infos.append((path, info)))
    open_ready(qtbot, pane, source)
    assert pane.source_path == source
    assert pane.title_label.text() == "\u2066" + source.name + "\u2069"
    assert pane.page_count == 3
    assert pane.page_index == 0
    assert infos[0][0] == source
    assert infos[0][1].page_sizes == ((240, 360), (360, 240), (300, 400))
    assert infos[0][1].metadata["Title"] == "عنوان المستند"
    image = page_image(pane)
    assert image.pixelColor(image.width() // 4, image.height() // 2).getRgb()[:3] == (255, 0, 0)
    pane.shutdown()
    clear_render_cache()
    # Neither the session, its PIL result nor its cached image owns Qt's pixels.
    assert image.pixelColor(image.width() * 3 // 4, image.height() // 2).getRgb()[:3] == (0, 0, 255)


def test_page_controls_zoom_fit_and_accessibility(qtbot, pane, source):
    open_ready(qtbot, pane, source)
    assert not pane.previous_button.isEnabled()
    with qtbot.waitSignal(pane.ready, timeout=10000):
        qtbot.mouseClick(pane.next_button, Qt.MouseButton.LeftButton)
    assert pane.page_index == 1
    assert pane.page_spin.value() == 2
    with qtbot.waitSignal(pane.ready, timeout=10000):
        pane.page_spin.setValue(3)
    assert pane.page_index == 2
    assert not pane.next_button.isEnabled()
    with qtbot.waitSignal(pane.ready, timeout=10000):
        pane.previous_page()
    before = pane.zoom
    with qtbot.waitSignal(pane.ready, timeout=10000):
        pane.zoom_in()
    assert pane.zoom == pytest.approx(before * 1.25)
    with qtbot.waitSignal(pane.ready, timeout=10000):
        pane.zoom_out()
    assert pane.zoom == pytest.approx(before)
    with qtbot.waitSignal(pane.ready, timeout=10000):
        pane.fit_width()
    width_zoom = pane.zoom
    assert pane.view.transform().m11() == pytest.approx(pane.zoom * preview.POINTS_TO_DIP)
    with qtbot.waitSignal(pane.ready, timeout=10000):
        pane.fit_page()
    assert pane.zoom <= width_zoom
    for control in (
        pane.view, pane.page_spin, pane.previous_button, pane.next_button,
        pane.zoom_in_button, pane.zoom_out_button, pane.fit_page_button, pane.fit_width_button,
    ):
        assert control.accessibleName()


def test_first_render_waits_for_visible_layout(qtbot, source, monkeypatch):
    calls = []
    original = preview.RenderSession.render

    def record(self, *args, **kwargs):
        calls.append(kwargs["scale"])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(preview.RenderSession, "render", record)
    pane = preview.PreviewPane()
    qtbot.addWidget(pane)
    try:
        with qtbot.waitSignal(pane.info_ready, timeout=10000):
            pane.open_path(source)
        assert not calls
        pane.resize(1000, 800)
        with qtbot.waitSignal(pane.ready, timeout=10000):
            pane.show()
        assert calls[0] > 1
        assert page_image(pane).height() > 500
    finally:
        pane.shutdown()


def test_open_before_show_does_not_cancel_pending_open(qtbot, source):
    pane = preview.PreviewPane()
    qtbot.addWidget(pane)
    try:
        pane.resize(1000, 800)
        with qtbot.waitSignal(pane.ready, timeout=10000):
            pane.open_path(source)
            pane.show()
        assert pane.page_count == 3
        assert not page_image(pane).isNull()
    finally:
        pane.shutdown()


def test_hidpi_and_resize_rerender_at_physical_pixel_scale(qtbot, pane, source, monkeypatch):
    monkeypatch.setattr(pane.view.viewport(), "devicePixelRatioF", lambda: 2.0)
    open_ready(qtbot, pane, source)
    image = page_image(pane)
    expected = 360 * pane.zoom * preview.POINTS_TO_DIP * 2
    assert image.height() == pytest.approx(expected, abs=1)
    assert pane.view.transform().m11() == pytest.approx(pane.zoom * preview.POINTS_TO_DIP)
    old_height = image.height()
    with qtbot.waitSignal(pane.ready, timeout=10000):
        pane.resize(1400, 1300)
    assert page_image(pane).height() > old_height
    # Desktop resolution is not the terminal's fixed 1600-pixel ceiling.
    assert page_image(pane).height() > 1600


def test_pixel_budget_keeps_requested_zoom_and_explains_detail_limit(
    qtbot, pane, source, monkeypatch
):
    monkeypatch.setattr(preview, "SCREEN_PIXEL_BUDGET", 40_000)
    open_ready(qtbot, pane, source)
    before = pane.zoom
    with qtbot.waitSignal(pane.ready, timeout=10000):
        pane.zoom_in()
    image = page_image(pane)
    assert image.width() * image.height() <= 40_000
    assert pane.zoom == pytest.approx(before * 1.25)
    assert pane.view.transform().m11() == pytest.approx(pane.zoom * preview.POINTS_TO_DIP)
    assert "limited" in pane.status_label.text()
    assert "requested zoom is preserved" in pane.status_label.text()


def test_zoomed_page_can_be_dragged_to_pan(qtbot, pane, source):
    open_ready(qtbot, pane, source)
    with qtbot.waitSignal(pane.ready, timeout=10000):
        for _ in range(6):
            pane.zoom_in()
    viewport = pane.view.viewport()
    start = viewport.rect().center()
    pane.view.centerOn(120, 180)
    before = (pane.view.horizontalScrollBar().value(), pane.view.verticalScrollBar().value())
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(viewport, start + QPoint(80, 70), delay=20)
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=start + QPoint(80, 70))
    after = (pane.view.horizontalScrollBar().value(), pane.view.verticalScrollBar().value())
    assert after != before


def test_native_session_operations_stay_on_one_worker_and_close(
    qtbot, pane, source, tmp_path, monkeypatch
):
    operations = []
    sessions = []
    original = preview.RenderSession

    class RecordedSession(original):
        def __init__(self, path):
            operations.append(("open", threading.get_ident()))
            super().__init__(path)
            self.closed_for_test = False
            sessions.append(self)

        def source_unchanged(self):
            operations.append(("check", threading.get_ident()))
            return super().source_unchanged()

        def render(self, *args, **kwargs):
            operations.append(("render", threading.get_ident()))
            return super().render(*args, **kwargs)

        def close(self):
            operations.append(("close", threading.get_ident()))
            self.closed_for_test = True
            super().close()

    monkeypatch.setattr(preview, "RenderSession", RecordedSession)
    open_ready(qtbot, pane, source)
    with qtbot.waitSignal(pane.ready, timeout=10000):
        pane.next_page()
    open_ready(qtbot, pane, source)
    assert len(sessions) == 1
    other = write_pdf(tmp_path / "other.pdf")
    open_ready(qtbot, pane, other)
    assert sessions[0].closed_for_test
    pane.clear()
    qtbot.waitUntil(lambda: sessions[-1].closed_for_test, timeout=10000)
    pane.shutdown()
    pane.shutdown()
    assert not pane._worker._thread.is_alive()
    assert {thread for _, thread in operations} == {pane._worker._thread.ident}
    assert threading.get_ident() not in {thread for _, thread in operations}
    assert {kind for kind, _ in operations} == {"open", "check", "render", "close"}


def test_source_revision_is_reopened_before_reuse(qtbot, pane, source):
    open_ready(qtbot, pane, source)
    write_pdf(source, sizes=((240, 360),), color="0 1 0")
    with qtbot.waitSignal(pane.ready, timeout=10000):
        pane.zoom_in()
    assert pane.page_count == 1
    image = page_image(pane)
    assert image.pixelColor(image.width() // 4, image.height() // 2).getRgb()[:3] == (0, 255, 0)


def test_stale_render_and_queued_requests_cannot_replace_latest_source(
    qtbot, pane, source, tmp_path, monkeypatch
):
    open_ready(qtbot, pane, source)
    entered = threading.Event()
    release = threading.Event()
    original = preview.RenderSession.render
    calls = []

    def delayed(self, page, *args, **kwargs):
        calls.append((self.source_path, page))
        if self.source_path == source.resolve() and page == 1:
            entered.set()
            if not release.wait(10):
                raise TimeoutError("Test did not release the real render")
        return original(self, page, *args, **kwargs)

    monkeypatch.setattr(preview.RenderSession, "render", delayed)
    ready_paths = []
    pane.ready.connect(ready_paths.append)
    other = write_pdf(tmp_path / "أحدث.pdf", sizes=((200, 250),), color="0 1 0")
    try:
        pane.next_page()
        qtbot.waitUntil(entered.is_set, timeout=10000)
        pane.next_page()
        pane.previous_page()
        pane.open_path(other)
        with qtbot.waitSignal(pane.ready, timeout=10000):
            release.set()
        assert ready_paths == [other]
        assert pane.source_path == other
        assert pane.page_count == 1
        assert len(calls) == 2  # Active old render plus final source, no queued middle page.
        image = page_image(pane)
        assert image.pixelColor(image.width() // 4, image.height() // 2).getRgb()[:3] == (0, 255, 0)
    finally:
        release.set()


def test_stale_open_metadata_cannot_replace_latest_source(
    qtbot, pane, source, tmp_path, monkeypatch
):
    entered = threading.Event()
    release = threading.Event()
    original = preview.RenderSession

    class DelayedSession(original):
        def __init__(self, path):
            if path == source:
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("Test did not release the real open")
            super().__init__(path)

    monkeypatch.setattr(preview, "RenderSession", DelayedSession)
    other = write_pdf(tmp_path / "latest.pdf", sizes=((200, 250),))
    infos = []
    pane.info_ready.connect(lambda path, info: infos.append(path))
    try:
        pane.open_path(source)
        qtbot.waitUntil(entered.is_set, timeout=10000)
        pane.open_path(other)
        with qtbot.waitSignal(pane.ready, timeout=10000):
            release.set()
        assert infos == [other]
        assert pane.page_count == 1
    finally:
        release.set()


def test_corrupt_pdf_and_render_errors_are_visible(qtbot, pane, tmp_path, source, monkeypatch):
    bad = tmp_path / "تالف.pdf"
    bad.write_bytes(b"This is not a PDF")
    with qtbot.waitSignal(pane.error, timeout=10000) as failure:
        pane.open_path(bad)
    assert failure.args[0] in pane.status_label.text()
    assert "Cannot preview" in pane.status_label.text()
    assert page_image(pane).isNull()

    def fail_render(self, *args, **kwargs):
        raise RuntimeError("Visible render failure")

    monkeypatch.setattr(preview.RenderSession, "render", fail_render)
    with qtbot.waitSignal(pane.error, timeout=10000):
        pane.open_path(source)
    assert "Visible render failure" in pane.status_label.text()
    assert page_image(pane).isNull()


def test_clear_suppresses_inflight_render(qtbot, pane, source, monkeypatch):
    open_ready(qtbot, pane, source)
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    original_render = preview.RenderSession.render
    original_close = preview.RenderSession.close

    def delayed(self, *args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("Test did not release the real render")
        return original_render(self, *args, **kwargs)

    def record_close(self):
        original_close(self)
        closed.set()

    monkeypatch.setattr(preview.RenderSession, "render", delayed)
    monkeypatch.setattr(preview.RenderSession, "close", record_close)
    ready_paths = []
    pane.ready.connect(ready_paths.append)
    try:
        pane.next_page()
        qtbot.waitUntil(entered.is_set, timeout=10000)
        pane.clear()
        release.set()
        qtbot.waitUntil(closed.is_set, timeout=10000)
        assert ready_paths == []
        assert pane.page_count == 0
        assert pane.source_path is None
        assert page_image(pane).isNull()
        assert pane.status_label.text() == "Choose a PDF to preview its pages."
    finally:
        release.set()


def test_shutdown_joins_without_gui_signal_delivery(qtbot, pane, source, monkeypatch):
    open_ready(qtbot, pane, source)
    entered = threading.Event()
    release = threading.Event()
    original = preview.RenderSession.render

    def delayed(self, *args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("Test did not release the real render")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(preview.RenderSession, "render", delayed)
    pane.next_page()
    qtbot.waitUntil(entered.is_set, timeout=10000)
    # The release comes from another thread, not a GUI timer: shutdown must not
    # require processing a queued completion signal to let the worker exit.
    timer = threading.Timer(0.05, release.set)
    timer.start()
    try:
        pane.shutdown()
        assert not pane._worker._thread.is_alive()
    finally:
        release.set()
        timer.join()


def test_stale_page_result_cannot_replace_new_page(qtbot, pane, source, monkeypatch):
    open_ready(qtbot, pane, source)
    entered = threading.Event()
    release = threading.Event()
    original = preview.RenderSession.render

    def delayed(self, page, *args, **kwargs):
        if page == 1:
            entered.set()
            if not release.wait(10):
                raise TimeoutError("Test did not release the real render")
        return original(self, page, *args, **kwargs)

    monkeypatch.setattr(preview.RenderSession, "render", delayed)
    pages = []
    pane.ready.connect(lambda path: pages.append(pane.page_index))
    try:
        pane.next_page()
        qtbot.waitUntil(entered.is_set, timeout=10000)
        pane.next_page()
        with qtbot.waitSignal(pane.ready, timeout=10000):
            release.set()
        assert pages == [2]
        assert pane.page_index == 2
        assert "Page 3 of 3" in pane.status_label.text()
        image = page_image(pane)
        assert image.width() / image.height() == pytest.approx(300 / 400, abs=0.01)
    finally:
        release.set()


def test_close_event_stops_worker(qtbot, pane, source):
    open_ready(qtbot, pane, source)
    pane.close()
    assert not pane._worker._thread.is_alive()


def test_parent_deletion_stops_child_worker(qtbot):
    from PySide6.QtWidgets import QWidget

    parent = QWidget()
    qtbot.addWidget(parent)
    child = preview.PreviewPane(parent)
    worker = child._worker
    parent.deleteLater()
    qtbot.waitUntil(lambda: not worker._thread.is_alive(), timeout=10000)


def test_public_page_zoom_mode_and_pan_linkage_state(qtbot, pane, source):
    open_ready(qtbot, pane, source)
    pages, modes, pans = [], [], []
    pane.page_changed.connect(pages.append)
    pane.zoom_mode_changed.connect(modes.append)
    pane.pan_changed.connect(lambda x, y: pans.append((x, y)))
    assert pane.zoom_mode == "page"
    with qtbot.waitSignal(pane.ready, timeout=10000):
        pane.set_page(1)
    assert pages == [1]
    with qtbot.waitSignal(pane.ready, timeout=10000):
        pane.set_zoom(4)
    assert pane.zoom_mode is None
    assert modes == [None]
    pans.clear()
    pane.set_normalized_pan(.2, .7)
    assert not pans  # Programmatic synchronization must never feed back.
    assert pane.normalized_pan == pytest.approx((.2, .7), abs=.005)
    with qtbot.waitSignal(pane.ready, timeout=10000):
        pane.set_zoom(3)
        pane.set_normalized_pan(.3, .6)
    assert modes == [None]
    assert not pans
    assert pane.normalized_pan == pytest.approx((.3, .6), abs=.005)
    bar = pane.view.horizontalScrollBar()
    bar.setValue(bar.minimum())
    assert pans[-1][0] == 0
    with qtbot.waitSignal(pane.ready, timeout=10000):
        pane.fit_width()
    assert pane.zoom_mode == "width"
    assert modes == [None, "width"]
    with qtbot.waitSignal(pane.ready, timeout=10000):
        pane.fit_page()
    assert modes == [None, "width", "page"]
    pane.set_controls_visible(False)
    assert pane.view.isVisible()
    assert not pane.next_button.isVisible()
    assert not pane.fit_page_button.isVisible()
    pane.set_controls_visible(True)
    assert pane.next_button.isVisible()


def test_pan_requested_before_first_image_is_applied_after_layout(qtbot, source):
    pane = preview.PreviewPane()
    qtbot.addWidget(pane)
    try:
        with qtbot.waitSignal(pane.info_ready, timeout=10000):
            pane.open_path(source)
        pane.set_zoom(4)
        pane.set_normalized_pan(.25, .75)
        pane.resize(700, 600)
        with qtbot.waitSignal(pane.ready, timeout=10000):
            pane.show()
        assert pane.normalized_pan == pytest.approx((.25, .75), abs=.005)
    finally:
        pane.shutdown()
