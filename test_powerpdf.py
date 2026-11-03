"""End-to-end tests: drive the real app with Textual's pilot against real PDFs on disk."""

import asyncio
import json
import math
import os
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pypdfium2 as pdfium
import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from textual import events
from textual.widgets import Input, Label, RichLog
from textual_image.renderable import SixelImage, TGPImage

import pdf_card
import powerpdf
from powerpdf import FileTree, PowerPDF, PromptDialog, Splash


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(powerpdf, "SETTINGS", tmp_path_factory.mktemp("settings") / "settings.json")


@pytest.fixture(autouse=True)
def no_ocr(monkeypatch):
    """The card screen reads names with Tesseract in the background; tests script it instead (deterministic, fast)."""
    monkeypatch.setattr(pdf_card, "suggest_name", lambda sides: None)


def make_pdf(path: Path, pages: int = 1, size: int = 100) -> Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(size, size)
    writer.write(path)
    return path


def run(root: Path, scenario, size=(140, 40)):
    """Start the app in root, skip the splash, run scenario(app, pilot)."""
    async def go():
        app = PowerPDF(root)
        async with app.run_test(size=size) as pilot:
            await pilot.press("z")  # any key skips the splash
            await settle(app, pilot)
            app.query_one(FileTree).focus()
            await scenario(app, pilot)
    asyncio.run(go())


async def settle(app, pilot):
    # DirectoryTree's "_loader" worker never finishes; wait for everything else.
    # Cancelled previews raise on wait(); a real worker failure still crashes the app and fails run_test.
    for _ in range(3):  # a finished worker can queue the next (tree load -> highlight -> preview)
        pending = [w for w in app.workers if w.group != "_loader"]
        await asyncio.gather(*(w.wait() for w in pending), return_exceptions=True)
        await pilot.pause()


async def highlight(app, pilot, name: str):
    tree = app.query_one(FileTree)
    tree.focus()
    await settle(app, pilot)
    tree.move_cursor(next(n for n in tree.root.children if n.data.path.name == name))
    await settle(app, pilot)


async def submit(app, pilot, value: str | None = None):
    if value is not None:
        app.screen.query_one(Input).value = value
    await pilot.press("enter")
    await settle(app, pilot)


def log_lines(app) -> list[str]:
    return [line.text for line in app.query_one(RichLog).lines]


def test_splash_closes_by_itself(tmp_path):
    async def go():
        app = PowerPDF(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause(2)  # past the 1.5 s timer, no key pressed
            assert not isinstance(app.screen, Splash)
    asyncio.run(go())


def test_merge_follows_queue_order_and_guards_overwrite(tmp_path):
    make_pdf(tmp_path / "a.pdf", pages=1, size=100)
    make_pdf(tmp_path / "b.pdf", pages=2, size=200)
    merged = tmp_path / "merged.pdf"

    async def scenario(app, pilot):
        await highlight(app, pilot, "b.pdf")
        await pilot.press("space")
        await highlight(app, pilot, "a.pdf")
        await pilot.press("space")

        await pilot.press("m")
        await submit(app, pilot)
        sizes = [int(page.mediabox.width) for page in PdfReader(merged).pages]
        assert sizes == [200, 200, 100]  # queue order, not name order

        await pilot.press("m")
        await submit(app, pilot)
        assert isinstance(app.screen, PromptDialog)  # existing file: first Enter only warns
        assert "overwrite" in str(app.screen.query_one("#hint", Label).render())
        await submit(app, pilot)
        assert not isinstance(app.screen, PromptDialog)
        assert sum(line.startswith("✓") for line in log_lines(app)) == 2
        assert app.recent[0] == merged

    run(tmp_path, scenario)


def test_merge_refuses_a_queued_file_as_output(tmp_path):
    make_pdf(tmp_path / "a.pdf")
    make_pdf(tmp_path / "b.pdf")

    async def scenario(app, pilot):
        await pilot.press("a")  # queue the folder
        await pilot.press("m")
        await submit(app, pilot, str(tmp_path / "a.pdf"))
        assert "input" in str(app.screen.query_one("#hint", Label).render())
        await submit(app, pilot, str(tmp_path / "out.txt"))
        assert ".pdf" in str(app.screen.query_one("#hint", Label).render())

    run(tmp_path, scenario)


def test_split_writes_pages_and_survives_a_broken_pdf(tmp_path):
    make_pdf(tmp_path / "a.pdf", pages=3)
    (tmp_path / "broken.pdf").write_text("not a pdf")

    async def scenario(app, pilot):
        await pilot.press("a", "s")
        await settle(app, pilot)
        assert sorted(p.name for p in (tmp_path / "a_pages").iterdir()) == [
            "a_page_001.pdf", "a_page_002.pdf", "a_page_003.pdf",
        ]
        assert any(line.startswith("✗ broken.pdf") for line in log_lines(app))
        assert app.recent == [tmp_path / "a.pdf"]  # only the successful split counts

    run(tmp_path, scenario)


def test_images_one_per_page_capped_at_a4_and_rerun_drops_stale_pages(tmp_path):
    make_pdf(tmp_path / "a.pdf", pages=3, size=595)
    make_pdf(tmp_path / "phone.pdf", size=2647)  # phone scans claim ~37 inch pages
    (tmp_path / "broken.pdf").write_text("not a pdf")

    async def scenario(app, pilot):
        await pilot.press("a", "i", "enter")  # queue all, JPG 150 dpi
        await settle(app, pilot)
        assert sorted(p.name for p in (tmp_path / "a_images").iterdir()) == [
            "a_page_001.jpg", "a_page_002.jpg", "a_page_003.jpg",
        ]
        with Image.open(tmp_path / "a_images" / "a_page_001.jpg") as image:
            assert image.size == (1240, 1240)  # 595 pt at 150 dpi
        with Image.open(tmp_path / "phone_image.jpg") as image:
            assert max(image.size) <= 1755  # rendered as A4 at 150 dpi, not 5514 px
        assert any(line.startswith("✗ broken.pdf") for line in log_lines(app))

        make_pdf(tmp_path / "a.pdf", pages=2, size=595)
        await pilot.press("i", "down", "enter")  # PNG next to the JPGs
        await settle(app, pilot)
        await pilot.press("i", "enter")
        await settle(app, pilot)
        assert sorted(p.name for p in (tmp_path / "a_images").iterdir()) == [
            "a_page_001.jpg", "a_page_001.png", "a_page_002.jpg", "a_page_002.png",
        ]

    run(tmp_path, scenario)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")
@pytest.mark.parametrize("convert", ["split", "image", "images"])
def test_outputs_get_the_folders_permissions(tmp_path, convert):
    """Staging in a tempfile.mkdtemp folder gave outputs an owner-only ACL (SYSTEM, Administrators, OWNER RIGHTS):
    from an elevated shell the owner is Administrators, so Photos and Explorer couldn't open the JPGs."""
    from pdf_image import to_images
    from pdf_split import split_pdf
    folder = tmp_path / "plain"
    folder.mkdir()
    subprocess.run(["icacls", str(folder), "/grant", "*S-1-1-0:(OI)(CI)R"], check=True, capture_output=True)  # Everyone may read
    pdf = make_pdf(folder / "a.pdf", pages=1 if convert == "image" else 2)
    if convert == "split":
        outputs = list(split_pdf(pdf)[1].iterdir())
    else:
        outputs = to_images(pdf, "jpg")
    assert outputs
    for output in outputs:
        acl = subprocess.run(["icacls", str(output)], capture_output=True, text=True, check=True).stdout
        assert "Everyone:(I)(R)" in acl, acl


def test_queue_reorder_and_remove(tmp_path):
    for name in "abc":
        make_pdf(tmp_path / f"{name}.pdf")

    async def scenario(app, pilot):
        await pilot.press("a")
        app.query_one("#queue").focus()
        await pilot.press("end", "shift+up")
        assert [p.name for p in app.queue] == ["a.pdf", "c.pdf", "b.pdf"]
        await pilot.press("delete")
        assert [p.name for p in app.queue] == ["a.pdf", "b.pdf"]

    run(tmp_path, scenario)


def test_explore_folders_and_filter(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    make_pdf(sub / "x.pdf")
    (tmp_path / "notes.txt").write_text("hi")

    async def scenario(app, pilot):
        tree = app.query_one(FileTree)
        names = lambda: [n.data.path.name for n in tree.root.children]
        assert names() == ["sub"]  # PDFs only by default

        await pilot.press("f")
        await settle(app, pilot)
        assert names() == ["sub", "notes.txt"]
        await highlight(app, pilot, "notes.txt")
        assert "TXT" in str(app.query_one("#info").render())

        await highlight(app, pilot, "sub")
        await pilot.press("o")
        await settle(app, pilot)
        assert app.root == sub and names() == ["x.pdf"]

        await pilot.press("backspace")
        await settle(app, pilot)
        assert app.root == tmp_path

        await pilot.press("g")
        await submit(app, pilot, str(tmp_path / "missing"))
        assert "Not a folder" in str(app.screen.query_one("#hint", Label).render())
        await submit(app, pilot, str(sub))
        assert app.root == sub

    run(tmp_path, scenario)


def test_dropped_paths_are_queued_or_navigated_to(tmp_path):
    arabic = make_pdf(tmp_path / "رخصة.pdf")
    spaced = make_pdf(tmp_path / "a b.pdf")
    sub = tmp_path / "sub"
    sub.mkdir()
    toasts = lambda app: [n.message for n in app._notifications]

    async def scenario(app, pilot):
        # a real terminal paste is posted to the app, which forwards it to the focused widget
        app.post_message(events.Paste(f'"{spaced}" "{arabic}"\n{tmp_path / "missing.pdf"} not a path'))
        await settle(app, pilot)
        assert app.queue == [spaced, arabic]
        assert toasts(app) == ["Queued 2 PDFs"]

        await pilot.press("g")  # an Input keeps its own paste: nothing queued, no navigation
        app.post_message(events.Paste(f'"{sub}"'))
        await settle(app, pilot)
        assert f'"{sub}"' in app.screen.query_one(Input).value
        assert app.queue == [spaced, arabic] and app.root == tmp_path
        await pilot.press("escape")

        app.post_message(events.Paste(f'"{sub}"'))
        await settle(app, pilot)
        assert app.root == sub

    run(tmp_path, scenario)


def test_preview_pages(tmp_path):
    make_pdf(tmp_path / "two.pdf", pages=2)

    async def scenario(app, pilot):
        await highlight(app, pilot, "two.pdf")
        assert app.current == tmp_path / "two.pdf" and app.page_count == 2
        for key, page in [("right_square_bracket", 1), ("right_square_bracket", 1), ("left_square_bracket", 0)]:
            await pilot.press(key)
            await settle(app, pilot)
            assert app.page == page
        await pilot.press("v")
        assert type(app.query_one("#page")).__name__ == "HalfcellImage"

    run(tmp_path, scenario)


def test_sharp_preview_is_skipped_only_for_kitty_graphics_under_herdr():
    herdr = {"HERDR_ENV": "1"}
    assert not powerpdf.sharp_preview_works(herdr, TGPImage)
    assert powerpdf.sharp_preview_works(herdr, SixelImage)
    assert powerpdf.sharp_preview_works({}, TGPImage)
    assert powerpdf.sharp_preview_works({"HERDR_ENV": "0"}, TGPImage)


def test_preview_render_ignores_panel_size(tmp_path):
    make_pdf(tmp_path / "a_normal.pdf", size=600)
    make_pdf(tmp_path / "b_huge.pdf", size=2000)

    async def scenario(app, pilot):
        assert 0 < app.query_one("#preview").content_size.width < 40  # tiny but laid-out panel: the old panel-sized render was far smaller
        for name, side in [("a_normal.pdf", 900), ("b_huge.pdf", 1600)]:  # 600 pt x 1.5; 2000 pt capped to a 1600 px long side
            await highlight(app, pilot, name)
            assert app.query_one("#page").image.size == (side, side)

    run(tmp_path, scenario, size=(90, 30))


def test_compare_original_and_small(tmp_path):
    make_pdf(tmp_path / "a.pdf", pages=3)
    make_pdf(tmp_path / "a_small.pdf", pages=2)
    make_pdf(tmp_path / "lonely.pdf")
    original, small = [(tmp_path / name).stat().st_size for name in ("a.pdf", "a_small.pdf")]

    async def scenario(app, pilot):
        await highlight(app, pilot, "lonely.pdf")
        await pilot.press("d")
        assert not isinstance(app.screen, powerpdf.CompareScreen)  # no counterpart: toast only
        for name in ("a.pdf", "a_small.pdf"):  # either file opens the same comparison
            await highlight(app, pilot, name)
            await pilot.press("d")
            await settle(app, pilot)
            screen = app.screen
            assert isinstance(screen, powerpdf.CompareScreen) and screen.paths == (tmp_path / "a.pdf", tmp_path / "a_small.pdf")
            saved = str(screen.query_one("#saved").render())
            assert f"{powerpdf.human_size(original)} → {powerpdf.human_size(small)}" in saved and f"−{round((1 - small / original) * 100)}%" in saved
            assert screen.query_one("#shot0").image is not None and screen.query_one("#shot1").image is not None
            assert [screen.query_one(f"#side{i}").border_subtitle for i in range(2)] == [
                f"{powerpdf.human_size(size)} · page 1/{pages}" for size, pages in ((original, 3), (small, 2))]
            for key, page in [("right_square_bracket", 1), ("right_square_bracket", 1), ("left_square_bracket", 0)]:  # 2nd ] stops at the shorter doc's last page
                await pilot.press(key)
                await settle(app, pilot)
                assert screen.page == page
            await pilot.press("escape")
            assert app.screen is not screen and not isinstance(app.screen, powerpdf.CompareScreen)

    run(tmp_path, scenario)


def test_pages_grid_deletes_rotates_reorders_saves_and_extracts(tmp_path):
    writer = PdfWriter()
    for size in (100, 200, 300):  # the size tells the pages apart
        writer.add_blank_page(size, size)
    writer.write(tmp_path / "a.pdf")
    (tmp_path / "sub").mkdir()
    original = (tmp_path / "a.pdf").read_bytes()

    def pages(name: str) -> list[tuple[int, int]]:
        return [(int(p.mediabox.width), p.rotation) for p in PdfReader(tmp_path / name).pages]

    async def scenario(app, pilot):
        await highlight(app, pilot, "sub")
        await pilot.press("t")
        assert not isinstance(app.screen, powerpdf.PagesScreen)  # not a PDF: toast only

        await highlight(app, pilot, "a.pdf")
        await pilot.press("t")
        await settle(app, pilot)
        screen = app.screen
        assert isinstance(screen, powerpdf.PagesScreen) and screen.order == [0, 1, 2] and len(screen.thumbs) == 3
        assert screen.cols == 140 // powerpdf.CELL_W
        await pilot.press("right", "delete", "left", "r")  # drop page 2, turn page 1
        assert screen.order == [0, 2] and screen.turns == {0: 1} and screen.cursor == 0
        assert screen.cells[0].query_one(".thumb").image.size == screen.thumbs[0].rotate(-90, expand=True).size
        assert "↻90°" in screen.caption(0) and "↻" not in screen.caption(2)
        await pilot.press("enter")
        await submit(app, pilot)
        assert not isinstance(app.screen, powerpdf.PagesScreen) and app.recent[0] == tmp_path / "a_edited.pdf"
        assert pages("a_edited.pdf") == [(100, 90), (300, 0)]

        await highlight(app, pilot, "a.pdf")
        await pilot.press("t")  # picks beat the cursor; shift+arrows move them; x extracts them
        await settle(app, pilot)
        screen = app.screen
        await pilot.press("space", "right", "right", "space", "x")
        await submit(app, pilot)
        assert isinstance(app.screen, powerpdf.PagesScreen)  # extracting keeps the grid open
        assert pages("a_extract.pdf") == [(100, 0), (300, 0)]
        await pilot.press("shift+right", "shift+right")  # page 1 passes page 2 once; page 3 is at the edge, so the block stays put
        assert screen.order == [1, 0, 2]

        await pilot.press("escape")  # unsaved changes: ask first
        assert isinstance(app.screen, powerpdf.ConfirmDialog)
        await pilot.press("n")
        assert app.screen is screen
        await pilot.press("escape", "y")
        assert not isinstance(app.screen, powerpdf.PagesScreen)
        assert (tmp_path / "a.pdf").read_bytes() == original

    run(tmp_path, scenario)


def test_rename_and_delete_keep_queue_and_recent_in_step(tmp_path, monkeypatch):
    make_pdf(tmp_path / "a.pdf")
    make_pdf(tmp_path / "b.pdf")
    monkeypatch.setattr(powerpdf, "send2trash", lambda path: os.remove(path))
    monkeypatch.setattr(powerpdf.os, "startfile", lambda path: None, raising=False)

    async def scenario(app, pilot):
        await highlight(app, pilot, "a.pdf")
        await pilot.press("space", "o")  # queue it and open it (-> recent)

        await pilot.press("f2")
        await submit(app, pilot, "b.pdf")
        assert "taken" in str(app.screen.query_one("#hint", Label).render())
        await submit(app, pilot, "renamed.pdf")
        renamed = tmp_path / "renamed.pdf"
        assert renamed.exists() and not (tmp_path / "a.pdf").exists()
        assert app.queue == [renamed] and app.recent == [renamed]

        await highlight(app, pilot, "renamed.pdf")
        await pilot.press("delete", "n")
        assert renamed.exists()  # "n" cancels
        await pilot.press("delete", "y")
        await settle(app, pilot)
        assert not renamed.exists()
        assert app.queue == [] and app.recent == []

    run(tmp_path, scenario)


def test_recent_and_theme_survive_restart(tmp_path, monkeypatch):
    a, b = make_pdf(tmp_path / "a.pdf"), make_pdf(tmp_path / "b.pdf")
    monkeypatch.setattr(powerpdf.os, "startfile", lambda path: None, raising=False)

    async def first(app, pilot):
        for name in ("a.pdf", "b.pdf"):
            await highlight(app, pilot, name)
            await pilot.press("o")
        app.theme = "powerpdf-gameboy"
        assert app.recent == [b, a]  # newest first

    async def second(app, pilot):
        assert app.recent == [b, a] and app.theme == "powerpdf-gameboy"
        await pilot.press("r")  # Recent tab
        assert app.focused is app.query_one("#recent")
        await pilot.press("delete")  # forget the top entry
        assert app.recent == [a]
        await settle(app, pilot)  # the Recent cursor moved to a.pdf: let its preview finish reading it (Windows can't delete an open file)
        a.unlink()

    async def third(app, pilot):
        assert app.recent == []  # missing files are dropped on load

    run(tmp_path, first)
    run(tmp_path, second)
    run(tmp_path, third)


FRONT, BACK = (200, 220, 240), (240, 200, 210)


def make_scan(path: Path) -> Path:
    """Two A4 pages at 300 dpi, each with a tilted ID-1 card (85.6 x 54 mm = 1011 x 638 px) on a white lid."""
    pages = []
    for color, angle in ((FRONT, 5), (BACK, -3)):
        img = np.full((3508, 2480, 3), 250, np.uint8)
        box = cv2.boxPoints(((1100, 1500), (1011, 638), angle)).astype(np.int32)
        cv2.fillPoly(img, [box], color)
        pages.append(Image.fromarray(img))
    pages[0].save(path, "PDF", resolution=300, save_all=True, append_images=pages[1:])
    return path


def test_card_front_and_back_as_card_sized_pages(tmp_path):
    make_scan(tmp_path / "scan.pdf")

    async def scenario(app, pilot):
        await highlight(app, pilot, "scan.pdf")
        await pilot.press("k")
        await settle(app, pilot)
        screen = app.screen
        assert isinstance(screen, powerpdf.CardScreen)
        for (_, (w, h), angle), expected in zip(screen.rects, (5, -3)):
            assert abs(w - 1011) < 8 and abs(h - 638) < 8 and abs(angle - expected) < 0.5
        corner = np.asarray(pdf_card.cut(screen.pages[0], screen.rects[0]))[5:45, 5:45].astype(int)
        assert abs(corner - FRONT).max() < 12  # straightened: the cut's corner is card, not lid

        before = [angle for _, _, angle in screen.rects]
        await pilot.press("right_square_bracket", "tab", "r")  # tilt the front, then flip the back
        assert [angle for _, _, angle in screen.rects] == pytest.approx([before[0] - 0.5, before[1] + 180])

        await pilot.press("enter")
        await submit(app, pilot)
        output = tmp_path / "scan_card.pdf"
        assert app.recent[0] == output and not isinstance(app.screen, powerpdf.CardScreen)
        pdf = pdfium.PdfDocument(output)
        assert len(pdf) == 2  # card-sized pages at 150 dpi (1011 x 638 px at 300 dpi -> 506 x 319 px = 243 x 153 pt), front first
        assert all(abs(w - 243) < 2 and abs(h - 153) < 2 for w, h in (pdf[i].get_size() for i in range(2)))
        mids = [np.asarray(pdf[i].render(scale=150 / 72).to_pil().convert("RGB"))[160, 253].astype(int) for i in range(2)]
        pdf.close()
        assert abs(mids[0] - FRONT).max() < 12 and abs(mids[1] - BACK).max() < 12  # the card fills the page
        reader = PdfReader(output)
        assert set(reader.metadata or {}) <= {"/Producer"} and all(len(page.images) == 1 for page in reader.pages)

    run(tmp_path, scenario)


def test_card_saves_front_and_back_jpgs(tmp_path):
    make_scan(tmp_path / "scan.pdf")
    front, back = tmp_path / "scan_front.jpg", tmp_path / "scan_back.jpg"
    front.write_bytes(b"old")

    async def scenario(app, pilot):
        await highlight(app, pilot, "scan.pdf")
        await pilot.press("k")
        await settle(app, pilot)
        await pilot.press("j")  # front exists: asks first
        assert isinstance(app.screen, powerpdf.ConfirmDialog)
        await pilot.press("n")
        assert isinstance(app.screen, powerpdf.CardScreen) and front.read_bytes() == b"old" and not back.exists()
        await pilot.press("j", "y")
        assert not isinstance(app.screen, powerpdf.CardScreen)
        for path, color in ((front, FRONT), (back, BACK)):
            with Image.open(path) as img:  # 1011 x 638 px at 300 dpi -> 506 x 319 px at 150 dpi
                assert img.format == "JPEG" and abs(img.width - 506) < 3 and abs(img.height - 319) < 3
                assert round(img.info["dpi"][0]) == 150 and not img.getexif() and "comment" not in img.info
                assert abs(np.asarray(img.convert("RGB"))[160, 253].astype(int) - color).max() < 12

    run(tmp_path, scenario)


@pytest.mark.parametrize("key", ["enter", "j"])
def test_card_black_boxes_are_pure_black_in_the_saved_images(tmp_path, key):
    make_scan(tmp_path / "scan.pdf")

    async def scenario(app, pilot):
        await highlight(app, pilot, "scan.pdf")
        await pilot.press("k")
        await settle(app, pilot)
        screen = app.screen
        rect = screen.rects[0]
        await pilot.press("b")  # a box on the front, selected
        (x, y, w, h), = screen.boxes[0]
        assert screen.active == 0 and abs(x - rect[1][0] / 2) < 1 and abs(y - rect[1][1] / 2) < 1
        await pilot.press("right", "right", "down")  # the box moves; the crop stays
        assert screen.boxes[0] == [(x + 20, y + 10, w, h)] and screen.rects[0] == rect
        await pilot.press("backspace")
        assert screen.boxes[0] == [] and screen.active is None
        await pilot.press("b", "shift+right")  # a new box, wider
        (x, y, w2, h2), = screen.boxes[0]
        assert (w2, h2) == (w + 10, h)
        await pilot.press("space", "right")  # none selected: the crop moves, the box stays over the same spot of the card
        assert screen.active is None and screen.boxes[0][0][0] == pytest.approx(x - 10, abs=1)
        await pilot.press("tab", "b", "left", "tab", "tab", "b")  # the back: two boxes; the second still selected at save
        boxes = [list(side) for side in screen.boxes]
        assert [len(side) for side in boxes] == [1, 2] and screen.active == 1

        await pilot.press(key)
        if key == "enter":
            await submit(app, pilot)
            reader = PdfReader(tmp_path / "scan_card.pdf")
            assert all(len(page.images) == 1 for page in reader.pages)
            images = [page.images[0].image for page in reader.pages]
        else:
            images = [Image.open(tmp_path / f"scan_{side}.jpg") for side in ("front", "back")]
        for img, side, color in zip(images, boxes, (FRONT, BACK)):
            pixels = np.asarray(img.convert("RGB"))
            for cx, cy, w, h in side:  # the box at 150 dpi, shrunk to whole pixels
                x0, x1, y0, y1 = math.ceil((cx - w / 2) / 2), int((cx + w / 2) / 2), math.ceil((cy - h / 2) / 2), int((cy + h / 2) / 2)
                assert x1 - x0 > 100 and (pixels[y0:y1, x0:x1] == 0).all()
            assert abs(pixels[160, 5].astype(int) - color).max() < 12  # the rest of the card is untouched

    run(tmp_path, scenario)


def card_page(cards: list[tuple[tuple[int, int], float, tuple[int, int, int]]]) -> np.ndarray:
    """An A4 page at 300 dpi with ID-1 cards: (center, angle, color) each."""
    img = np.full((3508, 2480, 3), 250, np.uint8)
    for center, angle, color in cards:
        cv2.fillPoly(img, [cv2.boxPoints((center, (1011, 638), angle)).astype(np.int32)], color)
    return img


def side_color(card: pdf_card.Card, side: int) -> np.ndarray:
    crop = np.asarray(pdf_card.cut(card.pages[side], card.rects[side]))
    return crop[crop.shape[0] // 2, crop.shape[1] // 2].astype(int)


def test_stacked_cards_pair_top_front_bottom_back_per_page():
    """Two pages, each one vehicle stacked front over back: two cards, not one card made of two backs."""
    colors = [(200, 60, 60), (60, 200, 60), (60, 60, 200), (200, 200, 60)]
    pages = [card_page([((1240, 1000), 2, colors[0]), ((1240, 2400), -1, colors[1])]),
             card_page([((1200, 1100), 0, colors[2]), ((1280, 2500), 3, colors[3])])]
    cards = pdf_card.pair_cards(pages)
    assert [card.where for card in cards] == ["page 1", "page 2"]
    for card, (front, back) in zip(cards, (colors[:2], colors[2:])):
        assert abs(side_color(card, 0) - front).max() < 12 and abs(side_color(card, 1) - back).max() < 12
        assert card.guessed == [False, False]


GRID = [  # side by side, left = back, right = front; row centers differ by up to 45 px, as on real scans
    ((650, 600), 1, (180, 60, 60)), ((1830, 645), -2, (60, 180, 60)),
    ((650, 1700), 0, (60, 60, 180)), ((1830, 1660), 2, (180, 180, 60)),
    ((650, 2800), -1, (180, 60, 180)),  # its front is missing
]


def test_card_batch_pairs_rows_and_saves_a_pdf_per_kept_card(tmp_path):
    Image.fromarray(card_page(GRID)).save(tmp_path / "scan.pdf", "PDF", resolution=300)
    (back1, front1, back2, front2, back3) = (color for _, _, color in GRID)

    async def scenario(app, pilot):
        await highlight(app, pilot, "scan.pdf")
        await pilot.press("k")
        await settle(app, pilot)
        screen = app.screen
        assert isinstance(screen, powerpdf.CardScreen)
        cards = screen.cards
        assert [card.where for card in cards] == ["page 1, row 1", "page 1, row 2", "page 1, row 3"]
        for card, front, back in zip(cards, (front1, front2), (back1, back2)):
            assert abs(side_color(card, 0) - front).max() < 12 and abs(side_color(card, 1) - back).max() < 12
        assert cards[2].guessed == [True, False] and abs(side_color(cards[2], 1) - back3).max() < 12
        assert abs(cards[2].rects[0][0][0] - (2480 - 650)) < 2  # the missing front: mirrored across the page

        await pilot.press("pagedown", "s")  # card 2: swap its sides, then back again
        assert screen.index == 1 and abs(side_color(cards[1], 0) - back2).max() < 12
        await pilot.press("s", "pagedown", "pagedown", "x")  # card 3: past the last stays on it; skip it
        assert screen.index == 2 and screen.skipped == {2}
        await pilot.press("enter")
        assert isinstance(app.screen, PromptDialog) and app.screen.value == str(tmp_path / "scan_cards")
        await submit(app, pilot)
        assert not isinstance(app.screen, powerpdf.CardScreen)
        outputs = sorted((tmp_path / "scan_cards").iterdir())
        assert [path.name for path in outputs] == ["scan_card_01.pdf", "scan_card_02.pdf"]  # no staging folder left over
        for path, colors in zip(outputs, ((front1, back1), (front2, back2))):
            pdf = pdfium.PdfDocument(path)
            mids = [np.asarray(pdf[i].render(scale=150 / 72).to_pil().convert("RGB"))[160, 253].astype(int) for i in range(len(pdf))]
            pdf.close()
            assert len(mids) == 2 and all(abs(mid - color).max() < 12 for mid, color in zip(mids, colors))
        assert any(line.startswith("✓ 2 cards → ") for line in log_lines(app))

    run(tmp_path, scenario)


@pytest.mark.parametrize("text, name", [
    ("Traffic Plate No. | 16 / 16402 aa5ltl", "16-16402"),
    ("| Traffic Plate No. __ 21|13695 Aastha", "21-13695"),
    ("Exp. Date 18-10-2027 Reg. Date 01-10-2024 T. C. No. 1180021637", None),  # dates and other numbers aren't names
    ("ID Number 784-1990-1234567-1", "784-1990-1234567-1"),
    ("ID Number 784199012345671", "784-1990-1234567-1"),
])
def test_card_name_from_ocr_text(text, name):
    assert pdf_card.name_from_text(text) == name


def test_card_name_needs_two_agreeing_readings_and_falls_back_to_the_back(monkeypatch):
    """OCR drops digits on real scans (21/13695 read as 21/3695), so one reading or a contested one suggests nothing."""
    monkeypatch.undo()  # the real suggest_name, not the autouse stub
    monkeypatch.setattr(pdf_card, "TESSERACT", __file__)  # "installed"
    front, back = Image.new("RGB", (100, 60)), Image.new("RGB", (100, 60), (1, 1, 1))
    passes = len(pdf_card.READINGS)

    def scripted(front_texts, back_texts):  # the sides tell themselves apart by their first pixel: 0 front, 1 back
        texts = {0: iter(front_texts), 1: iter(back_texts)}
        monkeypatch.setattr(pdf_card, "read_text", lambda band, scale, ink: next(texts[band.getpixel((0, 0))[0]]))
        return pdf_card.suggest_name([front, back])

    assert scripted(["16/16402", ""] + ["16/16402"] * (passes - 2), []) == ("16-16402", 0)
    assert scripted(["21/13695", "21/3695"] + [""] * (passes - 2), ["99/99999"] * passes) is None  # contested: back not tried
    assert scripted(["21/13695"] + [""] * (passes - 1), ["99/99999"] * passes) is None  # one reading isn't enough
    assert scripted([""] * passes, ["", "18/94778", "18/94778"] + [""] * (passes - 3)) == ("18-94778", 1)


def test_one_pdf_of_cards_has_a_bookmark_per_card(tmp_path):
    sides = [Image.new("RGB", (1011, 638), color) for color in ((200, 0, 0), (0, 200, 0), (0, 0, 200), (200, 200, 0))]
    pdf_card.save_cards(sides, tmp_path / "all.pdf", [[]] * 4, ["16-16402 Station", "scan_card_02"])
    reader = PdfReader(tmp_path / "all.pdf")
    assert [(item.title, reader.get_destination_page_number(item)) for item in reader.outline] == [("16-16402 Station", 0), ("scan_card_02", 2)]
    assert set(reader.metadata or {}) <= {"/Producer"}


def test_card_names_are_suggested_confirmed_and_used_for_the_files(tmp_path, monkeypatch):
    Image.fromarray(card_page(GRID)).save(tmp_path / "scan.pdf", "PDF", resolution=300)
    (back1, front1, back2, front2, back3) = (color for _, _, color in GRID)

    def suggest(sides):  # card 1: plate read on its front; card 3: on its back (its front wasn't found)
        colors = [np.asarray(side)[side.height // 2, side.width // 2].astype(int) for side in sides]
        if abs(colors[0] - front1).max() < 12:
            return "21-13695", 0
        if abs(colors[1] - back3).max() < 12:
            return "18-94778", 1
        return None

    monkeypatch.setattr(pdf_card, "suggest_name", suggest)

    async def name(app, pilot, value=None, picks=()):
        await pilot.press("n")
        dialog = app.screen
        assert isinstance(dialog, powerpdf.NameDialog)
        for pick in picks:  # choose OptionList entries by index
            options = dialog.query_one(powerpdf.OptionList)
            options.focus()
            options.highlighted = pick
            await pilot.press("enter")
        if value is not None:
            dialog.query_one(Input).value = value
        dialog.query_one(Input).focus()
        await pilot.press("enter")
        await pilot.pause()
        return dialog

    async def scenario(app, pilot):
        await highlight(app, pilot, "scan.pdf")
        await pilot.press("k")
        await settle(app, pilot)
        screen = app.screen
        assert screen.suggested == [("21-13695", 0), None, ("18-94778", 1)] and screen.names == [None] * 3

        dialog = await name(app, pilot)  # card 1: the suggestion is pre-filled, Enter confirms it
        assert dialog.value == "21-13695" and screen.names[0] == "21-13695"
        await pilot.press("pagedown")
        await name(app, pilot, "16/16402  Station ")  # card 2: typed; / becomes -, spaces tidied, the label remembered
        assert screen.names[1] == "16-16402 Station" and app.settings["card_labels"] == ["Station"]
        await pilot.press("pagedown")
        await name(app, pilot, picks=(0, 1))  # card 3: the OCR pick, then the remembered label after it
        assert screen.names[2] == "18-94778 Station"
        await pilot.press("pageup")
        await name(app, pilot, "21-13695")  # card 2 renamed to card 1's name: its files get " (2)"

        await pilot.press("enter")
        await submit(app, pilot)
        folder = tmp_path / "scan_cards"
        assert sorted(path.name for path in folder.iterdir()) == ["18-94778 Station.pdf", "21-13695 (2).pdf", "21-13695.pdf"]
        for file, front in (("21-13695.pdf", front1), ("21-13695 (2).pdf", front2)):
            pdf = pdfium.PdfDocument(folder / file)
            mid = np.asarray(pdf[0].render(scale=150 / 72).to_pil().convert("RGB"))[160, 253].astype(int)
            pdf.close()
            assert abs(mid - front).max() < 12

    run(tmp_path, scenario)


@pytest.mark.skipif(not shutil.which("gswin64c"), reason="needs Ghostscript")
def test_compress_plain_and_cut_to_content(tmp_path):
    make_scan(tmp_path / "scan.pdf")
    output = tmp_path / "scan_small.pdf"

    async def scenario(app, pilot):
        await highlight(app, pilot, "scan.pdf")
        for keys, widths in [(("down", "enter"), (250, 270)), (("enter",), (595, 596))]:  # ebook+cut, then ebook
            await pilot.press("c")
            await pilot.press(*keys)
            await settle(app, pilot)
            pdf = pdfium.PdfDocument(output)
            sizes = [round(page.get_size()[0]) for page in pdf]
            pdf.close()
            # cut: each page shrinks to the card (1011 px at 300 dpi = 243 pt, plus padding); plain keeps A4
            assert len(sizes) == 2 and all(widths[0] <= w <= widths[1] for w in sizes), sizes
        assert app.recent[0] == output
        assert sum(line.startswith("✓ scan.pdf → scan_small.pdf") for line in log_lines(app)) == 2

    run(tmp_path, scenario)


def test_arabic_display_never_changes_logical_paths_or_rename_input(tmp_path, monkeypatch):
    from pdf_display import display_name
    from powerpdf import QueueList, RecentList

    monkeypatch.setenv("POWERPDF_BIDI", "on")
    original = make_pdf(tmp_path / "رخصة المركبة.pdf")
    renamed = tmp_path / "شهادة 2026.pdf"

    async def scenario(app, pilot):
        await highlight(app, pilot, original.name)
        await pilot.press("space")
        app.remember(original)
        tree = app.query_one(FileTree)
        node = tree.cursor_node
        assert node.data.path == original
        assert node.label.plain == original.name
        rendered = tree.render_label(node, tree.rich_style, tree.rich_style)
        assert display_name(original.name) in rendered.plain
        assert app.query_one(QueueList).get_option_at_index(0).id == str(original)
        assert display_name(original.name) in str(app.query_one(QueueList).get_option_at_index(0).prompt)
        assert app.query_one(RecentList).get_option_at_index(0).id == str(original)
        assert display_name(original.name) in str(app.query_one("#info").render())
        await pilot.press("f2")
        assert app.screen.query_one(Input).value == original.name
        await submit(app, pilot, renamed.name)
        assert renamed.is_file() and not original.exists()
        assert app.queue == [renamed]
        assert app.recent == [renamed]
        assert json.loads(powerpdf.SETTINGS.read_text(encoding="utf-8"))["recent"] == [str(renamed)]

    run(tmp_path, scenario)
