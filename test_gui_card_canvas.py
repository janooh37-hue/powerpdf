"""Native card gestures over a generated, detected two-page scan batch."""
from dataclasses import replace

import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest

from gui_test_fixtures import multi_card_scan
from powerpdf_core import cards
from powerpdf_gui.card_canvas import CardEditor


@pytest.fixture(scope="module")
def scan(tmp_path_factory):
    source = multi_card_scan(tmp_path_factory.mktemp("card-canvas") / "بطاقات [scan].pdf", missing=True)
    session, total = cards.load_cards([source])
    assert total == 2
    assert len(session.cards) == 8
    return source, session.cards


@pytest.fixture
def editor(qtbot, scan):
    source, detected = scan
    session = cards.CardEditSession([source], detected)
    widget = CardEditor(session)
    widget.resize(1100, 850)
    qtbot.addWidget(widget)
    widget.show()
    QTest.qWait(1)
    return widget


def drag(qtbot, view, start, end):
    start, end = start.toPoint(), end.toPoint()
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=start)
    for part in (0.25, 0.5, 0.75, 1):
        point = start + (end - start) * part
        qtbot.mouseMove(view, pos=point, delay=1)
    qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=end)


def test_real_front_back_source_pixels_and_refresh_contract(editor):
    session = editor.session
    assert not editor.front_view.image.isNull()
    assert not editor.back_view.image.isNull()
    assert editor.source_view.native_size == (session.pages[0].shape[1], session.pages[0].shape[0])
    assert len(editor.source_view.markers) == 8
    changes = []
    editor.changed.connect(lambda: changes.append(True))
    before = session.revisions[:]
    session.index = 4
    session.switch(1)
    editor.refresh()
    assert editor.fields["cx"].value() == pytest.approx(session.rects[1][0][0], abs=0.01)
    assert editor.source_view.image.width() == session.pages[1].shape[1]
    assert not changes
    assert session.revisions == before
    assert "mm" in editor.side_labels[1].text()


def test_source_crop_move_is_one_undoable_drag(qtbot, editor):
    session, view = editor.session, editor.source_view
    original = session.rects[0]
    start = view.point_to_widget(QPointF(*original[0]))
    end = start + QPointF(9, 7)
    expected_delta = view.point_from_widget(end) - view.point_from_widget(start)
    changes = []
    editor.changed.connect(lambda: changes.append(True))
    drag(qtbot, view, start, end)
    actual = session.rects[0]
    assert actual[0][0] == pytest.approx(original[0][0] + expected_delta.x(), abs=5)
    assert actual[0][1] == pytest.approx(original[0][1] + expected_delta.y(), abs=5)
    assert changes
    assert session.undo()
    assert session.rects[0] == original
    assert not session.undo()
    assert session.redo()
    assert session.rects[0] == actual


def test_source_resize_holds_opposite_corner(qtbot, editor):
    session, view = editor.session, editor.source_view
    original = session.rects[0]
    start = view.handle_points()["se"]
    drag(qtbot, view, start, start + QPointF(8, 6))
    (cx, cy), (w, h), _ = session.rects[0]
    assert w > original[1][0]
    assert h > original[1][1]
    # Fixture cards have horizontal edges; the northwest corner stays fixed.
    assert cx - w / 2 == pytest.approx(original[0][0] - original[1][0] / 2, abs=1)
    assert cy - h / 2 == pytest.approx(original[0][1] - original[1][1] / 2, abs=1)
    assert session.undo()
    assert session.rects[0] == original
    assert not session.undo()


def test_rotated_resize_uses_crop_local_axes(qtbot, editor):
    session, view = editor.session, editor.source_view
    session.rotate_crop(22)
    editor.refresh()
    before = session.rects[0]
    start = view.handle_points()["e"]
    # Move in the crop's width direction, not the widget's horizontal direction.
    radians = np.deg2rad(before[2])
    drag(qtbot, view, start, start + QPointF(float(np.cos(radians) * 12), float(np.sin(radians) * 12)))
    assert session.rects[0][1][0] > before[1][0]
    assert session.rects[0][1][1] == before[1][1]
    assert session.rects[0][2] == before[2]
    assert session.undo()
    assert session.rects[0] == before


def test_mouse_rotation_and_reset(qtbot, editor):
    session, view = editor.session, editor.source_view
    original = session.rects[0]
    start = view.handle_points()["rotate"]
    drag(qtbot, view, start, start + QPointF(12, 0))
    assert abs(session.rects[0][2] - original[2]) > 1
    assert session.undo()
    assert session.rects[0] == original
    session.rotate_crop(90)
    editor.refresh()
    qtbot.mouseClick(editor.reset_button, Qt.MouseButton.LeftButton)
    assert session.rects[0] == original
    assert session.undo()
    assert session.rects[0][2] == original[2] + 90


def test_numeric_crop_and_mask_fields_call_core(qtbot, editor):
    session = editor.session
    original = session.rects[0]
    editor.fields["cx"].setValue(original[0][0] + 23)
    assert session.rects[0][0][0] == pytest.approx(original[0][0] + 23, abs=0.01)
    assert session.undo()
    assert session.rects[0] == original
    editor.refresh()
    qtbot.mouseClick(editor.add_box_button, Qt.MouseButton.LeftButton)
    assert session.active == 0
    assert not editor.fields["angle"].isEnabled()
    original_box = session.boxes[0][0]
    editor.fields["w"].setValue(original_box[2] + 18)
    assert session.boxes[0][0][2] == pytest.approx(original_box[2] + 18, abs=0.01)
    assert session.undo()
    assert session.boxes[0][0] == original_box
    assert session.rects[0] == original
    assert editor.fields["w"].minimum() == 8


def test_mask_preview_uses_core_burned_pixels(qtbot, editor):
    session = editor.session
    qtbot.mouseClick(editor.add_box_button, Qt.MouseButton.LeftButton)
    cx, cy, _, _ = session.boxes[0][0]
    image = editor.front_view.image
    x = round(cx / session.rects[0][1][0] * image.width())
    y = round(cy / session.rects[0][1][1] * image.height())
    assert image.pixelColor(x, y).getRgb()[:3] == (0, 0, 0)
    with cards.cut(session.pages[0], session.rects[0]) as crop:
        with cards.downscale(crop, session.boxes[0]) as expected:
            assert image.size().width() == expected.width
            assert image.size().height() == expected.height
            assert image.pixelColor(x, y).getRgb()[:3] == expected.getpixel((x, y))
    qtbot.mouseClick(editor.delete_box_button, Qt.MouseButton.LeftButton)
    assert session.boxes[0] == []
    assert session.undo()
    editor.refresh()
    assert editor.front_view.image.pixelColor(x, y).getRgb()[:3] == (0, 0, 0)


def test_box_drag_resize_and_delete_are_individually_undoable(qtbot, editor):
    session, view = editor.session, editor.front_view
    qtbot.mouseClick(editor.add_box_button, Qt.MouseButton.LeftButton)
    original = session.boxes[0][0]
    start = view.point_to_widget(QPointF(original[0], original[1]))
    drag(qtbot, view, start, start + QPointF(20, 12))
    moved = session.boxes[0][0]
    assert moved[0] > original[0]
    assert moved[1] > original[1]
    assert session.undo()
    assert session.boxes[0][0] == original
    editor.refresh()
    start = view.handle_points()["e"]
    drag(qtbot, view, start, start + QPointF(15, 0))
    resized = session.boxes[0][0]
    assert resized[2] > original[2]
    assert resized[3] == original[3]
    assert session.undo()
    assert session.boxes[0][0] == original
    editor.refresh()
    qtbot.keyClick(view, Qt.Key.Key_Delete)
    assert not session.boxes[0]
    assert session.undo()
    assert session.boxes[0] == [original]


def test_draw_box_gesture_adds_exactly_one_history_entry(qtbot, editor):
    session, view = editor.session, editor.front_view
    qtbot.mouseClick(editor.draw_box_button, Qt.MouseButton.LeftButton)
    start = view.point_to_widget(QPointF(100, 100))
    end = view.point_to_widget(QPointF(260, 180))
    drag(qtbot, view, start, end)
    assert len(session.boxes[0]) == 1
    assert session.boxes[0][0] == pytest.approx((180, 140, 160, 80), abs=4)
    assert not editor.draw_box_button.isChecked()
    assert session.undo()
    assert not session.boxes[0]
    assert not session.undo()
    assert session.redo()
    assert len(session.boxes[0]) == 1


def test_key_nudges_resize_tilt_rotate_and_undo(qtbot, editor):
    session, view = editor.session, editor.front_view
    original = session.rects[0]
    qtbot.keyClick(view, Qt.Key.Key_Right)
    assert session.rects[0][0][0] == original[0][0] + 1
    qtbot.keyClick(view, Qt.Key.Key_Down, modifier=Qt.KeyboardModifier.ControlModifier)
    assert session.rects[0][0][1] == original[0][1] + 10
    qtbot.keyClick(view, Qt.Key.Key_Right, modifier=Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier)
    assert session.rects[0][1][0] == original[1][0] + 10
    qtbot.keyClick(view, Qt.Key.Key_BracketRight)
    assert session.rects[0][2] == original[2] + 0.5
    qtbot.keyClick(view, Qt.Key.Key_R)
    assert session.rects[0][1] == (original[1][1], original[1][0] + 10)
    assert session.rects[0][2] == original[2] + 90.5
    QTest.keySequence(view, QKeySequence(QKeySequence.StandardKey.Undo))
    assert session.rects[0][2] == original[2] + 0.5
    QTest.keySequence(view, QKeySequence(QKeySequence.StandardKey.Redo))
    assert session.rects[0][2] == original[2] + 90.5
    qtbot.keyClick(view, Qt.Key.Key_B)
    assert session.active == 0
    qtbot.keyClick(view, Qt.Key.Key_Space)
    assert session.active is None
    qtbot.keyClick(view, Qt.Key.Key_F)
    assert session.side == 1


def test_text_entry_does_not_trigger_canvas_shortcuts(qtbot, editor):
    session = editor.session
    field = editor.fields["cx"]
    field.setFocus()
    before = session.rects[0]
    qtbot.keyClick(field, Qt.Key.Key_Right)
    qtbot.keyClick(field, Qt.Key.Key_B)
    assert session.rects[0] == before
    assert not session.boxes[0]


def test_map_guessed_skipped_and_current_markers(qtbot, editor):
    session = editor.session
    session.index = 7
    session.switch(1)
    session.skip()
    editor.refresh()
    markers = editor.source_view.markers
    assert len(markers) == 8
    assert sum(marker.current for marker in markers) == 2
    assert sum(marker.skipped for marker in markers) == 2
    assert any(marker.guessed and marker.current for marker in markers)
    assert "guessed" in editor.side_labels[1].text()
    assert session.card.guessed[1]
    original = session.rects[1]
    editor.fields["cx"].setValue(original[0][0] + 4)
    assert not session.card.guessed[1]
    assert not any(marker.guessed and marker.current and marker.side == 1 for marker in editor.source_view.markers)
    assert session.undo()
    editor.refresh()
    assert session.card.guessed[1]
    target = next(marker for marker in editor.source_view.markers if marker.index == 4 and marker.side == 0)
    qtbot.mouseClick(editor.source_view, Qt.MouseButton.LeftButton,
                     pos=editor.source_view.point_to_widget(QPointF(*target.rect[0])).toPoint())
    assert session.index == 4
    assert session.side == 0


def test_crop_carries_masks_and_reset_respects_swap(editor):
    session = editor.session
    original_front, original_back = session.rects[:]
    session.add_box()
    box = session.boxes[0][:]
    editor._rotate(0.5)
    expected = cards.carry(box, original_front, session.rects[0])
    assert np.allclose(session.boxes[0], expected)
    session.swap()
    session.switch(0)
    session.change(20, 10)
    session.reset_crop()
    assert session.rects[0] == original_back
    assert session.undo()
    assert session.rects[0] != original_back


def test_source_background_cache_survives_crop_nudges(editor):
    view = editor.source_view
    view.grab()
    image_key = view.image.cacheKey()
    background_key = view._background.cacheKey()
    editor.session.change(1, 0)
    editor.refresh()
    view.grab()
    assert view.image.cacheKey() == image_key
    assert view._background.cacheKey() == background_key


def test_core_gesture_revision_and_tui_command_history(scan):
    source, detected = scan
    session = cards.CardEditSession([source], detected)
    original = session.rects[0]
    session.begin_edit()
    session.change(2, 0)
    session.change(3, 0)
    session.end_edit()
    assert session.revisions[0] == 2
    assert session.undo()
    assert session.rects[0] == original
    assert session.revisions[0] == 3
    assert not session.undo()
    session.change(1, 0)
    session.change(1, 0)
    assert session.undo()
    assert session.rects[0][0][0] == original[0][0] + 1
    assert session.undo()
    assert session.rects[0] == original
    session.begin_edit()
    session.end_edit()
    assert not session.undo()


def test_selected_side_jpg_exports_only_that_crop(scan, tmp_path, monkeypatch):
    source, detected = scan
    session = cards.CardEditSession([source], detected)
    session.add_box()
    full = session.snapshot()
    card = full.cards[0]
    single = replace(card, pages=card.pages[:1], rects=card.rects[:1], guessed=card.guessed[:1], boxes=card.boxes[:1])
    snapshot = replace(full, cards=(single,))
    calls = []
    original_cut = cards.cut

    def record_cut(page, rect):
        calls.append(rect)
        return original_cut(page, rect)

    monkeypatch.setattr(cards, "cut", record_cut)
    destination = tmp_path / "front.jpg"
    assert snapshot.export([[destination]]) == [destination]
    assert calls == [single.rects[0]]
    with Image.open(destination) as image:
        assert image.format == "JPEG"
        assert not image.getexif()
        assert image.getpixel((image.width // 2, image.height // 2)) == (0, 0, 0)
    with pytest.raises(ValueError, match="exactly one"):
        full.export([[tmp_path / "not-one-side.jpg"]])


def test_oversized_crop_is_rejected_before_allocation_or_undo(editor, monkeypatch):
    session = editor.session
    before, history = session.rects[0], len(session._undo)
    oversized = (before[0], (1_000_000, 1_000_000), before[2])
    with pytest.raises(ValueError, match="card limit"):
        session.set_crop(oversized)
    assert session.rects[0] == before
    assert len(session._undo) == history
    assert not editor._set_crop(oversized)
    assert "card limit" in editor.selection_label.text()
    assert editor.fields["w"].maximum() * round(before[1][1]) <= cards.MAX_PAGE_PIXELS

    def forbidden_allocation(*args, **kwargs):
        pytest.fail("Oversized crop reached OpenCV allocation")

    monkeypatch.setattr(cards.cv2, "warpAffine", forbidden_allocation)
    with pytest.raises(ValueError, match="card limit"):
        cards.cut(session.pages[0], oversized)
