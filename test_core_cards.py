"""Shared card edits, OCR evidence and real staged export contracts."""
from threading import Barrier, Lock

import numpy as np
import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from powerpdf_core import cards, ocr
from powerpdf_core.jobs import CancelToken, JobCancelled


def session(tmp_path, count=1):
    source = tmp_path / "scan.pdf"
    with PdfWriter() as writer:
        writer.add_blank_page(72, 72)
        writer.write(source)
    page = np.full((160, 240, 3), (210, 170, 100), dtype=np.uint8)
    page.setflags(write=False)
    rect = ((120.0, 80.0), (200.0, 120.0), 0.0)
    found = [cards.Card([page, page], [rect, rect], [True, False], f"row {i}") for i in range(count)]
    return cards.CardEditSession([source], found)


def test_snapshot_shares_pages_but_freezes_edits(tmp_path):
    edit = session(tmp_path)
    edit.add_box()
    edit.name(0, "16/16402 Station")
    before = edit.snapshot()
    assert before.cards[0].pages[0] is edit.pages[0]
    old_rect = before.cards[0].rects[0]
    old_box = before.cards[0].boxes[0][0]
    edit.select_box(None)
    edit.change(10, 5, 20, 10, 0.5)
    edit.name(0, "21/12345 New")
    assert before.cards[0].rects[0] == old_rect
    assert before.cards[0].boxes[0][0] == old_box
    assert before.cards[0].name == "16-16402 Station"
    with pytest.raises(TypeError):
        before.cards[0].boxes[0][0] = old_box


def test_history_restores_box_geometry_and_selection(tmp_path):
    edit = session(tmp_path)
    original = edit.rects[:]
    edit.add_box()
    box = edit.boxes[0][0]
    edit.change(12, 5, -1000, -1000)
    assert edit.boxes[0][0] == (box[0] + 12, box[1] + 5, 8, 8)
    edit.remove_box()
    assert edit.boxes[0] == []
    assert edit.undo() and edit.active == 0
    assert edit.boxes[0][0][2:] == (8, 8)
    assert edit.undo() and edit.boxes[0][0] == box
    assert edit.redo() and edit.boxes[0][0][2:] == (8, 8)
    edit.select_box(None)
    edit.change(0, 0, 0, 0, 180)
    assert edit.rects[0][2] == 180
    assert not edit.card.guessed[0]
    assert edit.undo() and edit.rects == original
    assert edit.card.guessed[0]
    edit.change(10, 0)
    assert not edit.redo()


def test_carry_keeps_box_page_center_when_crop_moves(tmp_path):
    edit = session(tmp_path)
    edit.add_box()
    x, y, w, h = edit.boxes[0][0]
    edit.select_box(None)
    edit.change(10, -20)
    assert edit.boxes[0][0] == pytest.approx((x - 10, y + 20, w, h))


def test_swap_skip_and_navigation_are_session_commands(tmp_path):
    edit = session(tmp_path, 2)
    edit.add_box()
    edit.swap()
    assert not edit.boxes[0] and edit.boxes[1]
    assert edit.card.guessed == [False, True]
    assert edit.undo() and edit.boxes[0]
    edit.skip()
    assert edit.kept() == [1]
    assert edit.undo() and edit.kept() == [0, 1]
    edit.step(999)
    edit.switch(1)
    assert (edit.index, edit.side, edit.active) == (1, 1, None)


def test_names_are_confirmed_only_and_labels_keep_twelve(tmp_path):
    edit = session(tmp_path)
    request, = edit.ocr_cards()
    result = ocr.OCRResult(request.card_id, request.revision, "16-16402", 1)
    assert edit.apply_suggestion(result)
    assert edit.names == [None]
    assert edit.snapshot().cards[0].name is None
    edit.name(0, None)
    assert edit.suggested[0] == ("16-16402", 1)
    for number in range(14):
        edit.name(0, f"16/16402 Label{number}")
    assert edit.names == ["16-16402 Label13"]
    assert edit.labels == [f"Label{i}" for i in range(13, 1, -1)]
    edit.name(0, "16-16402 Label10")
    assert edit.labels[0] == "Label10" and len(edit.labels) == 12
    edit.name(0, "")
    assert edit.names == [None]
    assert not edit.apply_suggestion(result)


def test_stems_are_globally_unique_with_literal_suffixes_and_case(tmp_path):
    edit = session(tmp_path, 5)
    for index, name in enumerate(("A", "A", "A (2)", "a", "a (2)")):
        edit.name(index, name)
    stems = list(edit.file_stems().values())
    assert len({stem.casefold() for stem in stems}) == 5
    paths = edit.batch_paths(tmp_path / "out", "pdf")
    assert len({path[0] for path in paths}) == 5
    edit.skip()
    assert 0 not in edit.file_stems()


def test_numbered_stems_leave_skipped_strip_gaps(tmp_path):
    edit = session(tmp_path, 3)
    edit.step(1)
    edit.skip()
    assert list(edit.file_stems().values()) == ["scan_card_01", "scan_card_03"]


def test_source_collision_is_rejected_before_overwrite(tmp_path):
    edit = session(tmp_path)
    edit.name(0, "scan")
    original = edit.sources[0].read_bytes()
    with pytest.raises(ValueError):
        edit.batch_paths(tmp_path, "pdf")
    with pytest.raises(ValueError):
        edit.snapshot().export([[edit.sources[0]]])
    assert edit.sources[0].read_bytes() == original
    alias = tmp_path / "alias.pdf"
    alias.symlink_to(edit.sources[0])
    with pytest.raises(ValueError):
        edit.snapshot().export([[alias]])


def test_changed_source_rejects_snapshot_export(tmp_path):
    edit = session(tmp_path)
    captured = edit.snapshot()
    edit.sources[0].write_bytes(edit.sources[0].read_bytes() + b"\n% changed")
    output = tmp_path / "out.pdf"
    with pytest.raises(ValueError, match="source PDF changed"):
        captured.export([[output]])
    assert not output.exists()


def test_export_real_pdf_bookmarks_and_burned_jpeg_masks(tmp_path):
    edit = session(tmp_path, 2)
    edit.add_box()
    edit.name(0, "16/16402 Station")
    output = tmp_path / "all.pdf"
    captured = edit.snapshot()
    paths = captured.export([[output]], titles=list(edit.file_stems().values()))
    assert paths == [output]
    reader = PdfReader(output)
    assert len(reader.pages) == 4
    assert [(item.title, reader.get_destination_page_number(item)) for item in reader.outline] == [("16-16402 Station", 0), ("scan_card_02", 2)]
    assert not reader.metadata
    assert "/Metadata" not in reader.trailer["/Root"]
    assert reader.pages[0].mediabox.width == pytest.approx(48, abs=1)
    image = reader.pages[0].images[0].image.convert("RGB")
    assert image.getpixel((image.width // 2, image.height // 2)) == (0, 0, 0)


def test_batch_encode_failure_leaves_existing_outputs_untouched(tmp_path, monkeypatch):
    edit = session(tmp_path, 2)
    folder = tmp_path / "out"
    folder.mkdir()
    paths = edit.batch_paths(folder, "pdf")
    for group in paths:
        group[0].write_bytes(b"previous result")
    encode = cards._encode_cards
    count = 0

    def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("disk full")
        return encode(*args, **kwargs)

    monkeypatch.setattr(cards, "_encode_cards", fail_second)
    with pytest.raises(OSError, match="disk full"):
        edit.snapshot().export(paths, folder)
    assert all(group[0].read_bytes() == b"previous result" for group in paths)
    assert set(folder.iterdir()) == {group[0] for group in paths}


def test_cancelled_export_does_not_replace_output(tmp_path):
    edit = session(tmp_path)
    output = tmp_path / "out.pdf"
    output.write_bytes(b"keep")
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(JobCancelled):
        edit.snapshot().export([[output]], cancel=cancel)
    assert output.read_bytes() == b"keep"


def test_ocr_revision_rejects_edits_undo_and_confirmed_names(tmp_path):
    edit = session(tmp_path)
    request, = edit.ocr_cards()
    result = ocr.OCRResult(request.card_id, request.revision, "16-16402", 0)
    edit.change(10, 0)
    assert not edit.apply_suggestion(result)
    edit.undo()
    assert not edit.apply_suggestion(result)
    current, = edit.ocr_cards()
    fresh = ocr.OCRResult(current.card_id, current.revision, "16-16402", 1)
    assert edit.apply_suggestion(fresh)
    edit.swap()
    assert edit.suggested[0] == ("16-16402", 0)
    assert not edit.apply_suggestion(fresh)
    edit.name(0, "manual")
    assert not edit.apply_suggestion(fresh)


@pytest.mark.parametrize(("text", "name"), [("Plate 16/16402", "16-16402"), ("784-1980-1234567-1", "784-1980-1234567-1"), ("nothing", None)])
def test_ocr_name_patterns(text, name):
    assert ocr.name_from_text(text) == name


def test_ocr_requires_unanimous_two_pass_evidence(monkeypatch):
    front, back = Image.new("RGB", (100, 60)), Image.new("RGB", (100, 60), (1, 1, 1))

    def scripted(front_text, back_text):
        texts = {0: iter(front_text), 1: iter(back_text)}
        monkeypatch.setattr(ocr, "read_text", lambda band, scale, ink, **kwargs: next(texts[band.getpixel((0, 0))[0]]))
        return ocr.suggest_name([front, back], executable="explicit-tesseract")

    assert scripted(["16/16402"] * 5, []) == ("16-16402", 0)
    assert scripted(["21/13695", "21/3695", "", "", ""], []) is None
    assert scripted(["16/16402", "", "", "", ""], []) is None
    assert scripted([""] * 5, ["21/13695"] * 5) == ("21-13695", 1)


def test_ocr_unavailable_is_explicit_and_does_not_cut(tmp_path, monkeypatch):
    edit = session(tmp_path)
    monkeypatch.setattr(ocr, "resolve_tesseract", lambda: None)
    monkeypatch.setattr(ocr, "cut", lambda *args: pytest.fail("No OCR must not cut pages"))
    result, = ocr.read_names(edit.ocr_cards())
    assert not result.available and result.name is None


def test_ocr_limits_card_concurrency_to_four(tmp_path, monkeypatch):
    edit = session(tmp_path, 8)
    barrier, lock = Barrier(4), Lock()
    active = peak = 0

    def suggest(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=10)
        with lock:
            active -= 1
        return "16-16402", 1

    monkeypatch.setattr(ocr, "suggest_name", suggest)
    results = list(ocr.read_names(edit.ocr_cards(), executable="explicit-tesseract"))
    assert len(results) == 8 and peak == 4
    assert {result.card_id for result in results} == set(edit.card_ids)
    assert all(result.side == 1 and result.name == "16-16402" for result in results)


def test_pairing_empty_pages_has_clear_error():
    with pytest.raises(ValueError, match="No pages"):
        cards.pair_cards([])


def test_card_file_name_sanitizes_devices_and_path_components():
    assert cards.card_file_name(" ../16/16402: Station? ") == "-16-16402- Station"
    assert cards.card_file_name("CON") == "_CON"


def test_load_cards_uses_owned_renderer_and_shares_arrays(tmp_path):
    edit = session(tmp_path)
    loaded, total = cards.load_cards(edit.sources)
    assert total == 1 and len(loaded.cards) == 1
    assert loaded.pages[0] is loaded.pages[1]
    assert not loaded.pages[0].flags.writeable
    assert loaded.snapshot().cards[0].pages[0] is loaded.pages[0]


def test_load_cards_rejects_change_between_render_and_session(tmp_path, monkeypatch):
    edit = session(tmp_path)
    page = edit.pages[0]

    def changed(sources, *args, **kwargs):
        sources[0].write_bytes(sources[0].read_bytes() + b"\n% changed during detection")
        return [page], 1

    monkeypatch.setattr(cards, "render_pages", changed)
    with pytest.raises(ValueError, match="source PDF changed during card loading"):
        cards.load_cards(edit.sources)


def test_card_analysis_refuses_oversized_page_before_rendering(tmp_path):
    source = tmp_path / "huge.pdf"
    with PdfWriter() as writer:
        writer.add_blank_page(12000, 12000)
        writer.write(source)
    with pytest.raises(ValueError, match="pixel/memory limit"):
        cards.render_pages([source])


def test_ocr_child_uses_explicit_executable_data_and_one_thread(tmp_path, monkeypatch):
    captured = {}

    class Process:
        returncode = 0

        def __init__(self, args, **kwargs):
            captured.update(args=args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def communicate(self, input=None, timeout=None):
            captured["input"] = input
            return b"16/16402", b""

        def poll(self):
            return 0

    monkeypatch.setattr(ocr.subprocess, "Popen", Process)
    monkeypatch.setattr(ocr.pdf_runtime, "helper_env", lambda executable: {"HELPER": executable}, raising=False)
    image = Image.new("RGB", (20, 10))
    assert ocr.read_text(image, 1, 110, executable="chosen-tesseract", data_dir=tmp_path) == "16/16402"
    assert captured["args"][0] == "chosen-tesseract"
    assert captured["args"][-2:] == ["--tessdata-dir", str(tmp_path)]
    assert captured["env"] == {"HELPER": "chosen-tesseract", "OMP_THREAD_LIMIT": "1"}
    assert captured["input"].startswith(b"\x89PNG")
