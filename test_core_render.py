"""Real-PDF behavior at the shared rendering seam; no UI or escaped handles."""

import gc
import io
import math
import os
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from powerpdf_core import render
from powerpdf_core.render import RenderLimitError, RenderSession, SourceChangedError, preview_scale


def pdf_bytes(sizes=((100, 200), (200, 100)), color="1 0 0"):
    writer = PdfWriter()
    writer.add_metadata({"/Title": "Snapshot title", "/Author": "PowerPDF test"})
    for width, height in sizes:
        page = writer.add_blank_page(width, height)
        stream = DecodedStreamObject()
        stream.set_data(
            f"{color} rg 0 0 {width / 2} {height} re f\n"
            f"0 0 1 rg {width / 2} 0 {width / 2} {height} re f\n".encode()
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.fixture(autouse=True)
def isolated_caches():
    render.clear_render_cache()
    yield
    render.clear_render_cache()


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "بطاقة [1].pdf"
    path.write_bytes(pdf_bytes())
    return path


@pytest.fixture
def render_calls(monkeypatch):
    calls = []
    original = render._pdfium.PdfPage.render

    def counted(page, *args, **kwargs):
        calls.append((kwargs["scale"], kwargs["rotation"]))
        return original(page, *args, **kwargs)

    monkeypatch.setattr(render._pdfium.PdfPage, "render", counted)
    return calls


def test_info_and_owned_pixels_survive_close(source):
    with RenderSession(source) as session:
        assert session.source_path == source.resolve()
        assert session.source_bytes == source.read_bytes()
        assert session.info.page_count == 2
        assert session.info.page_sizes == ((100.0, 200.0), (200.0, 100.0))
        assert session.info.metadata["Title"] == "Snapshot title"
        assert session.info.metadata["Author"] == "PowerPDF test"
        with pytest.raises(TypeError):
            session.info.metadata["Title"] = "mutated"
        image = session.render(0, scale=1, purpose="image-export")
        assert image.mode == "RGB"
        assert image.size == (100, 200)
        assert image.getpixel((10, 10)) == (255, 0, 0)
        assert image.getpixel((90, 10)) == (0, 0, 255)
    render.clear_render_cache()
    assert image.getpixel((10, 10)) == (255, 0, 0)
    image.putpixel((10, 10), (0, 255, 0))
    assert image.getpixel((10, 10)) == (0, 255, 0)
    image.close()


def test_byte_inputs_and_deterministic_idempotent_close():
    data = pdf_bytes()
    session = RenderSession(data)
    with session:
        assert session.source_path is None
        assert session.source_bytes is data
        assert session.source_unchanged()
        session.check_source_unchanged()
    session.close()
    session.close()
    assert session.info.page_count == 2
    assert session.source_unchanged()
    with pytest.raises(ValueError, match="closed"):
        session.render(0)
    with pytest.raises(ValueError, match="closed"):
        with session:
            pass


def test_context_closes_after_exception(source):
    session = RenderSession(source)
    with pytest.raises(RuntimeError, match="caller failure"):
        with session:
            raise RuntimeError("caller failure")
    with pytest.raises(ValueError, match="closed"):
        session.render(0)


def test_open_snapshot_does_not_block_rename_or_replace(source):
    with RenderSession(source) as session:
        moved = source.with_name("renamed.pdf")
        source.rename(moved)
        assert not session.source_unchanged()
        source.write_bytes(pdf_bytes(color="0 1 0"))
        assert not session.source_unchanged()
        with pytest.raises(SourceChangedError, match="changed"):
            session.check_source_unchanged()
        image = session.render(0, scale=1, purpose="image-export")
        assert image.getpixel((10, 10)) == (255, 0, 0)
        image.close()
        moved.unlink()
        source.unlink()
        assert not session.source_unchanged()


def test_source_unchanged_and_content_revision_in_cache(source, render_calls):
    old_stat = source.stat()
    with RenderSession(source) as first:
        assert first.source_unchanged()
        first.check_source_unchanged()
        image = first.render(0, scale=1)
        image.close()
        # Same-sized content and restored mtime must not alias an older render.
        replacement = pdf_bytes(color="0 1 0")
        assert len(replacement) == old_stat.st_size
        source.write_bytes(replacement)
        os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        assert not first.source_unchanged()
        with RenderSession(source) as second:
            image = second.render(0, scale=1)
            assert image.getpixel((10, 10)) == (0, 255, 0)
            image.close()
    assert len(render_calls) == 2


def test_source_content_check_not_only_stat(source, monkeypatch):
    with RenderSession(source) as session:
        source.write_bytes(pdf_bytes(color="0 1 0"))
        # Exercise content checking even on filesystems whose stat signatures
        # cannot distinguish a same-length overwrite.
        old_revision = session._revision.stat
        monkeypatch.setattr(render, "_stat_revision", lambda stat: old_revision)
        assert not session.source_unchanged()
        with pytest.raises(SourceChangedError):
            session.check_source_unchanged()


@pytest.mark.parametrize("size", [(100, 200), (595, 842), (2000, 3000), (4000, 100)])
def test_preview_scale_is_layout_independent_and_capped(size):
    scale = preview_scale(*size)
    assert scale <= 1.5
    assert max(size) * scale <= 1600


def test_default_preview_and_explicit_actual_size_are_different():
    with RenderSession(pdf_bytes(sizes=((2000, 100),))) as session:
        preview = session.render(0)
        assert preview.size == (1600, 80)
        preview.close()
        # Physical-size policy belongs to callers, not a universal A4 cap.
        actual = session.render(0, scale=1, purpose="card-analysis", max_pixels=200_000)
        assert actual.size == (2000, 100)
        actual.close()


@pytest.mark.parametrize("rotation,size", [(0, (100, 200)), (90, (200, 100)),
                                           (180, (100, 200)), (270, (200, 100))])
def test_additional_rotation(rotation, size):
    with RenderSession(pdf_bytes(sizes=((100, 200),))) as session:
        image = session.render(0, scale=1, rotation=rotation)
        assert image.size == size
        image.close()


def test_cache_results_are_independently_owned(source, render_calls):
    with RenderSession(source) as session:
        first = session.render(0, scale=1)
        first.putpixel((10, 10), (0, 255, 0))
        first.close()
        second = session.render(0, scale=1)
        assert second.getpixel((10, 10)) == (255, 0, 0)
        render.clear_render_cache()
        assert second.getpixel((90, 10)) == (0, 0, 255)
        second.close()
    assert len(render_calls) == 1


def test_cache_shared_across_sessions_of_same_source(source, render_calls):
    for _ in range(2):
        with RenderSession(source) as session:
            image = session.render(0, scale=1)
            image.close()
    assert len(render_calls) == 1


def test_cache_keys_include_page_scale_rotation_and_purpose(source, render_calls):
    requests = [(0, 1, 0, "preview"), (1, 1, 0, "preview"),
                (0, 0.5, 0, "preview"), (0, 1, 90, "preview"),
                (0, 1, 0, "thumbnail")]
    with RenderSession(source) as session:
        for _ in range(2):
            for page, scale, rotation, purpose in requests:
                image = session.render(page, scale=scale, rotation=rotation, purpose=purpose)
                image.close()
    assert len(render_calls) == len(requests)


def test_preview_and_thumbnail_budgets_are_separate(source, render_calls, monkeypatch):
    # One full preview, one thumbnail. A long grid must not evict the preview.
    preview_cache = render._ImageCache(100 * 200 * 4)
    thumbnail_cache = render._ImageCache(50 * 100 * 4)
    monkeypatch.setattr(render, "_preview_cache", preview_cache)
    monkeypatch.setattr(render, "_thumbnail_cache", thumbnail_cache)
    with RenderSession(source) as session:
        for page, scale, purpose in [(0, 1, "preview"), (0, 0.5, "thumbnail"),
                                     (1, 0.5, "thumbnail"), (0, 1, "preview")]:
            image = session.render(page, scale=scale, purpose=purpose)
            image.close()
    assert len(render_calls) == 3
    assert preview_cache.bytes == 80_000
    assert thumbnail_cache.bytes == 20_000
    assert render.PREVIEW_CACHE_BYTES == 128 * 1024 * 1024
    assert render.THUMBNAIL_CACHE_BYTES == 32 * 1024 * 1024


def test_cache_eviction_is_lru_and_byte_bounded(render_calls, monkeypatch):
    cache = render._ImageCache(2 * 100 * 100 * 4)
    monkeypatch.setattr(render, "_preview_cache", cache)
    with RenderSession(pdf_bytes(sizes=((100, 100),) * 3)) as session:
        for page in (0, 1, 0, 2, 0, 1):
            image = session.render(page, scale=1)
            image.close()
    assert len(render_calls) == 4
    assert cache.bytes == cache.budget
    assert len(cache.images) == 2


def test_larger_than_cache_budget_and_exports_not_retained(source, render_calls, monkeypatch):
    cache = render._ImageCache(100)
    monkeypatch.setattr(render, "_preview_cache", cache)
    with RenderSession(source) as session:
        for purpose in ("preview", "preview", "card-analysis", "card-analysis",
                        "image-export", "image-export", "print", "print"):
            image = session.render(0, scale=1, purpose=purpose)
            image.close()
    assert len(render_calls) == 8
    assert cache.bytes == 0
    assert not cache.images


def test_pixel_budget_rejects_before_render_and_before_cache_hit(source, render_calls):
    with RenderSession(source) as session:
        with pytest.raises(RenderLimitError, match="limit is 19,999"):
            session.render(0, scale=1, purpose="card-analysis", max_pixels=19_999)
        assert not render_calls
        image = session.render(0, scale=1, max_pixels=20_000)
        image.close()
        with pytest.raises(RenderLimitError):
            session.render(0, scale=1, max_pixels=19_999)
        assert len(render_calls) == 1


def test_pixel_budget_matches_ceil_sizing(render_calls):
    with RenderSession(pdf_bytes(sizes=((10.1, 10.1),))) as session:
        with pytest.raises(RenderLimitError):
            session.render(0, scale=1, max_pixels=120)
        image = session.render(0, scale=1, max_pixels=121)
        assert image.size == (11, 11)
        image.close()
    assert len(render_calls) == 1


@pytest.mark.parametrize("kwargs,error", [
    ({"page_index": -1}, IndexError), ({"page_index": 2}, IndexError),
    ({"page_index": 0.5}, IndexError), ({"scale": 0}, ValueError),
    ({"scale": -1}, ValueError), ({"scale": math.nan}, ValueError),
    ({"scale": math.inf}, ValueError), ({"rotation": 45}, ValueError),
    ({"purpose": ""}, ValueError), ({"max_pixels": 0}, ValueError),
    ({"max_pixels": 1.5}, ValueError),
])
def test_invalid_requests_do_not_call_native_render(source, render_calls, kwargs, error):
    request = {"page_index": 0, **kwargs}
    with RenderSession(source) as session:
        with pytest.raises(error):
            session.render(**request)
    assert not render_calls


def test_all_native_operations_and_abandoned_session_cleanup_hold_lock(source, monkeypatch):
    observed = []
    for cls, names in [
        (render._pdfium.PdfDocument, ("__init__", "__len__", "__getitem__", "get_metadata_dict", "close")),
        (render._pdfium.PdfPage, ("get_size", "render", "close")),
        (render._pdfium.PdfBitmap, ("to_pil", "close")),
    ]:
        for name in names:
            original = getattr(cls, name)

            def guarded(self, *args, _original=original, _name=name, **kwargs):
                assert render.PDFIUM_LOCK._is_owned(), _name
                if _name == "__init__":
                    assert isinstance(args[0], bytes)
                observed.append(_name)
                return _original(self, *args, **kwargs)

            monkeypatch.setattr(cls, name, guarded)
    session = RenderSession(source)
    image = session.render(0, scale=1)
    image.close()
    reference = weakref.ref(session)
    del session
    gc.collect()
    assert reference() is None
    assert "to_pil" in observed
    assert observed.count("close") >= 4  # info pages, bitmap, render page, document


def test_render_failure_closes_native_bitmap_and_page(source, monkeypatch):
    original_render = render._pdfium.PdfPage.render
    retained = []

    def failing(page, *args, **kwargs):
        bitmap = original_render(page, *args, **kwargs)
        retained.extend((page, bitmap))
        raise RuntimeError("failed after bitmap allocation")

    monkeypatch.setattr(render._pdfium.PdfPage, "render", failing)
    with RenderSession(source) as session:
        with pytest.raises(RuntimeError, match="bitmap allocation"):
            session.render(0)
        assert all(obj.raw is None for obj in retained)
        monkeypatch.setattr(render._pdfium.PdfPage, "render", original_render)
        image = session.render(0)
        assert image.getpixel((10, 10)) == (255, 0, 0)
        image.close()


def test_pil_conversion_failure_closes_native_resources(source, monkeypatch):
    retained = []

    def failing(bitmap):
        retained.append(bitmap)
        raise RuntimeError("failed PIL conversion")

    monkeypatch.setattr(render._pdfium.PdfBitmap, "to_pil", failing)
    with RenderSession(source) as session:
        with pytest.raises(RuntimeError, match="PIL conversion"):
            session.render(0)
        assert retained[0].raw is None


def test_info_failure_closes_document_under_lock(source, monkeypatch):
    closed = []
    original_close = render._pdfium.PdfDocument.close

    def failing(document, *args, **kwargs):
        raise RuntimeError("failed metadata")

    def close(document, *args, **kwargs):
        assert render.PDFIUM_LOCK._is_owned()
        closed.append(document)
        return original_close(document, *args, **kwargs)

    monkeypatch.setattr(render._pdfium.PdfDocument, "get_metadata_dict", failing)
    monkeypatch.setattr(render._pdfium.PdfDocument, "close", close)
    with pytest.raises(RuntimeError, match="metadata"):
        RenderSession(source)
    assert len(closed) == 1
    assert closed[0].raw is None


def test_concurrent_sessions_and_close_share_one_lane(source, monkeypatch):
    original_render = render._pdfium.PdfPage.render
    inside = 0
    maximum = 0

    def guarded(page, *args, **kwargs):
        nonlocal inside, maximum
        assert render.PDFIUM_LOCK._is_owned()
        inside += 1
        maximum = max(maximum, inside)
        try:
            return original_render(page, *args, **kwargs)
        finally:
            inside -= 1

    monkeypatch.setattr(render._pdfium.PdfPage, "render", guarded)
    start = threading.Barrier(4)

    def worker(index):
        with RenderSession(source) as session:
            start.wait(timeout=10)
            image = session.render(index % 2, scale=1, purpose="image-export")
            size = image.size
            image.close()
            return size

    with ThreadPoolExecutor(max_workers=4) as pool:
        sizes = list(pool.map(worker, range(4)))
    assert sizes == [(100, 200), (200, 100), (100, 200), (200, 100)]
    assert maximum == 1


def test_corrupt_and_missing_sources_fail_without_live_session(tmp_path):
    with pytest.raises(render.RenderError):
        RenderSession(b"not a PDF")
    with pytest.raises(FileNotFoundError):
        RenderSession(tmp_path / "missing.pdf")


def test_close_waits_for_active_render_and_preserves_result(source, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    close_started = threading.Event()
    close_finished = threading.Event()
    original = render._pdfium.PdfPage.render

    def blocked(page, *args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return original(page, *args, **kwargs)

    monkeypatch.setattr(render._pdfium.PdfPage, "render", blocked)
    session = RenderSession(source)

    def close():
        close_started.set()
        session.close()
        close_finished.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(session.render, 0, 1)
            try:
                assert entered.wait(timeout=10)
                closing = pool.submit(close)
                assert close_started.wait(timeout=10)
                assert not close_finished.is_set()
            finally:
                release.set()
            image = pending.result(timeout=10)
            closing.result(timeout=10)
        assert close_finished.is_set()
        assert image.getpixel((10, 10)) == (255, 0, 0)
        image.close()
        with pytest.raises(ValueError, match="closed"):
            session.render(0)
    finally:
        session.close()


def test_encrypted_document_without_supported_password_fails():
    writer = PdfWriter()
    writer.add_blank_page(100, 200)
    writer.encrypt("secret")
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(render.RenderError):
        RenderSession(output.getvalue())


def test_empty_pdf_is_rejected_without_returning_a_live_session():
    writer = PdfWriter()
    output = io.BytesIO()
    writer.write(output)
    # PDFium rejects page-less documents during open, before render is possible.
    with pytest.raises(render.RenderError):
        RenderSession(output.getvalue())


def _changed_stat(stat, **changes):
    from types import SimpleNamespace
    fields = {name: getattr(stat, name) for name in dir(stat) if name.startswith("st_")}
    return SimpleNamespace(**(fields | changes))


def test_fresh_merge_exports_images_with_stale_path_metadata(source, monkeypatch):
    from pathlib import Path
    from PIL import Image
    from powerpdf_core.merge import merge_pdfs
    from powerpdf_core.images import to_images

    fresh = source.with_name("مدمج.pdf")
    assert merge_pdfs([source, source], fresh) == 4
    original_stat = Path.stat
    actual = original_stat(fresh)

    def stale_stat(path, *args, **kwargs):
        stat = original_stat(path, *args, **kwargs)
        if path == fresh:
            return _changed_stat(stat, st_size=0, st_mtime_ns=stat.st_mtime_ns - 1_000_000,
                                 st_ctime_ns=stat.st_ctime_ns - 2_000_000)
        return stat

    monkeypatch.setattr(Path, "stat", stale_stat)
    assert fresh.stat().st_size != actual.st_size
    with RenderSession(fresh) as session:
        assert session.info.page_count == 4
        assert session.source_bytes == fresh.read_bytes()
        session.check_source_unchanged()
        outputs = to_images(fresh, "png", 100)
        assert len(outputs) == 4
        with Image.open(outputs[0]) as image:
            assert image.format == "PNG"
            assert image.getpixel((10, 10)) == (255, 0, 0)
        fresh.write_bytes(fresh.read_bytes() + b"\n% external change")
        assert not session.source_unchanged()


def test_handle_revision_change_during_snapshot_read_is_rejected(source, monkeypatch):
    original = os.fstat
    calls = 0

    def changed_after_read(fd):
        nonlocal calls
        calls += 1
        stat = original(fd)
        return _changed_stat(stat, st_mtime_ns=stat.st_mtime_ns + 1) if calls == 2 else stat

    monkeypatch.setattr(os, "fstat", changed_after_read)
    with pytest.raises(SourceChangedError, match="changed while opening") as caught:
        RenderSession(source)
    assert calls == 2
    assert "handle before/after read" in str(caught.value)
    assert "st_mtime_ns=" in str(caught.value) and "->" in str(caught.value)


def test_path_replacement_after_snapshot_read_is_rejected(source, monkeypatch):
    from pathlib import Path
    replacement = source.with_name("replacement.pdf")
    replacement.write_bytes(pdf_bytes(color="0 1 0"))
    original = Path.open
    calls = 0

    def replace_before_reopen(path, *args, **kwargs):
        nonlocal calls
        if path == source:
            calls += 1
            if calls == 2:
                replacement.replace(source)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", replace_before_reopen)
    with pytest.raises(SourceChangedError, match="changed while opening") as caught:
        RenderSession(source)
    assert "snapshot handle/path reopen" in str(caught.value)
    assert any(field + "=" in str(caught.value) for field in ("st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"))


def test_path_replacement_after_hash_is_rejected(source, monkeypatch):
    from pathlib import Path
    replacement = source.with_name("replacement.pdf")
    replacement.write_bytes(source.read_bytes())
    original = Path.open
    calls = 0
    with RenderSession(source) as session:
        def replace_before_reopen(path, *args, **kwargs):
            nonlocal calls
            if path == source:
                calls += 1
                if calls == 2:
                    replacement.replace(source)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", replace_before_reopen)
        assert not session.source_unchanged()
        assert calls == 2


def test_handle_modification_during_hash_is_rejected(source, monkeypatch):
    original = render.hashlib.file_digest
    with RenderSession(source) as session:
        def modify_after_hash(stream, algorithm):
            digest = original(stream, algorithm)
            with source.open("ab") as target:
                target.write(b"\n% concurrent modification")
            return digest

        monkeypatch.setattr(render.hashlib, "file_digest", modify_after_hash)
        assert not session.source_unchanged()


def test_export_source_change_diagnostic_names_comparison_and_fields(source):
    with RenderSession(source) as session:
        source.write_bytes(source.read_bytes() + b"\n% external change")
        with pytest.raises(SourceChangedError) as caught:
            session.check_source_unchanged()
        assert "opened handle/snapshot" in str(caught.value)
        assert "st_size=" in str(caught.value)
