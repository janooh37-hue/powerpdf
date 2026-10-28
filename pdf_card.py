"""Cut ID / vehicle-license cards out of scanned PDFs, straighten them, and save front and back as a card-sized PDF or JPGs.
One scan may hold many cards: side by side (left = back, right = front) or stacked (front above back)."""

from __future__ import annotations

import contextlib
import io
import math
import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pypdfium2 as pdfium
from PIL import Image

DPI = 300  # render scans at true size, so cuts keep their real size
OUT_DPI = 150  # saved cards: small files, text still readable
JPEG = {"quality": 80, "optimize": True}
MAX_PAGES = 12  # an A4 page at DPI is 26 MB in memory; 12 pages ≈ 310 MB

Rect = tuple[tuple[float, float], tuple[float, float], float]  # OpenCV RotatedRect: center, size, angle
Box = tuple[float, float, float, float]  # black box on a crop: center x, y, width, height in crop pixels at DPI

# Tesseract is optional: without it cards get no name suggestions. Not on PATH by default on Windows.
TESSERACT = shutil.which("tesseract") or str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe")
PLATE = re.compile(r"(?<!\d)(\d{1,2}) ?[/|] ?(\d{3,6})(?!\d)")  # UAE traffic plate, code/number: 16/16402
EMIRATES_ID = re.compile(r"(?<!\d)784 ?-? ?(\d{4}) ?-? ?(\d{7}) ?-? ?(\d)(?!\d)")
# OCR passes per side: (top, bottom) band of the card, scale, ink threshold. The 22-45% band is where a vehicle
# license's plate row is; reading it alone cuts the clutter around it. Measured on real scans: single passes drop digits.
READINGS = ((0.0, 1.0, 1.0, 140), (0.0, 1.0, 1.0, 110), (0.0, 1.0, 1.5, 110), (0.22, 0.45, 1.5, 110), (0.22, 0.45, 2.0, 120))


@dataclass
class Card:
    """Both sides of one card, front first, each a rect on its page. A guessed side wasn't found on the scan:
    it sits where the partner should be, for the user to move onto the card."""

    pages: list[np.ndarray]
    rects: list[Rect]
    guessed: list[bool]
    where: str  # e.g. "page 1, row 2"

    def swap(self) -> None:
        for sides in (self.pages, self.rects, self.guessed):
            sides.reverse()


def render_pages(paths: list[Path], limit: int = MAX_PAGES, lock=contextlib.nullcontext()) -> tuple[list[np.ndarray], int]:
    """The first `limit` pages across the PDFs as RGB arrays at DPI, and how many pages the PDFs have.
    lock guards pdfium (not thread-safe), one page at a time, so the app's preview keeps going between pages.
    Opens from bytes: a path-opened pdfium document keeps a Windows file handle on the PDF."""
    pages, total = [], 0
    for path in paths:
        data = path.read_bytes()
        with lock:
            pdf = pdfium.PdfDocument(data)
            count = len(pdf)
        try:
            total += count
            for index in range(min(count, limit - len(pages))):
                with lock:
                    page = pdf[index]
                    try:
                        image = page.render(scale=DPI / 72).to_pil()
                    finally:
                        page.close()
                pages.append(np.asarray(image.convert("RGB")))
        finally:
            with lock:
                pdf.close()
    return pages, total


def landscape(rect: Rect) -> Rect:
    """Long side as width, angle in [-90, 90)."""
    center, (w, h), angle = rect
    if w < h:
        w, h, angle = h, w, angle - 90
    return center, (w, h), (angle + 90) % 180 - 90


def detect_cards(img: np.ndarray) -> list[Rect]:
    """Every card on the page. Cards in one scan share a size, so card-shaped blobs under half the largest one's area are dropped."""
    gray = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_RGB2GRAY), (5, 5), 0)
    # card outline (edges) plus printed content (anything darker than paper white), merged into blobs
    mask = cv2.Canny(gray, 30, 100) | (gray < 225).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    page_area = img.shape[0] * img.shape[1]
    rects = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < 0.016 * page_area:  # can't pass the filter below; most blobs are specks, skip them cheaply
            continue
        rect = landscape(cv2.minAreaRect(contour))
        (w, h) = rect[1]
        # ponytail: shape filter tuned for ID-1 cards (85.6 x 54 mm, ratio 1.59); widen for other cards.
        # Filled: a card is a solid rectangle, text lines and stamps are not.
        if 0.02 < w * h / page_area < 0.6 and 1.3 < w / h < 1.9 and area > 0.8 * w * h:
            rects.append(rect)
    largest = max((w * h for _, (w, h), _ in rects), default=0)
    return [rect for rect in rects if rect[1][0] * rect[1][1] >= largest / 2]


def rows(rects: list[Rect]) -> list[list[Rect]]:
    """Rects in rows, top to bottom, each left to right. A rect joins a row when its center is within half a card height
    of the row's first: scans tilt, so centers in one row can differ by tens of pixels."""
    grouped: list[list[Rect]] = []
    for rect in sorted(rects, key=lambda r: r[0][1]):
        if grouped and abs(rect[0][1] - grouped[-1][0][0][1]) < grouped[-1][0][1][1] / 2:
            grouped[-1].append(rect)
        else:
            grouped.append([rect])
    return [sorted(row, key=lambda r: r[0][0]) for row in grouped]


def partner(rect: Rect, page: np.ndarray, across: bool) -> Rect:
    """Where a side that wasn't found should be: the found side mirrored across the page's middle, left-right (across) or top-bottom."""
    (cx, cy), size, _ = rect
    height, width = page.shape[:2]
    return ((width - cx, cy) if across else (cx, height - cy)), size, 0.0


def band(page: np.ndarray, index: int, count: int) -> Rect:
    """A horizontal band of the page: what stands in for a card when none is found."""
    height, width = page.shape[:2]
    return landscape(((width / 2, height * (2 * index + 1) / (2 * count)), (width, height / count), 0))


def cut_matrix(rect: Rect) -> np.ndarray:
    """The 2x3 affine that maps page pixels to the straightened crop of rect."""
    (cx, cy), (w, h), angle = rect
    m = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)
    m[:, 2] += (w / 2 - cx, h / 2 - cy)
    return m


def cut(img: np.ndarray, rect: Rect) -> Image.Image:
    """The rect's contents, rotated upright."""
    (_, _), (w, h), _ = rect
    size = (max(1, round(w)), max(1, round(h)))
    return Image.fromarray(cv2.warpAffine(img, cut_matrix(rect), size, flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255)))


def carry(boxes: list[Box], old: Rect, new: Rect) -> list[Box]:
    """The boxes over the same spot of the page after the crop changes from old to new."""
    if not boxes:
        return boxes
    centers = np.array([[(cx, cy) for cx, cy, _, _ in boxes]], np.float32)
    page = cv2.transform(centers, cv2.invertAffineTransform(cut_matrix(old)))
    return [(float(x), float(y), w, h) for (x, y), (_, _, w, h) in zip(cv2.transform(page, cut_matrix(new))[0], boxes)]


def pair_cards(pages: list[np.ndarray]) -> list[Card]:
    """Every card on the pages, in page order. Side by side (a row holds two or more): left = back, right = front.
    Otherwise one per row, paired in reading order, front first: front above back, or page 1 then page 2.
    A side without a partner gets a guess at the mirrored spot. Pages without cards are skipped (blank duplex backs);
    with none found anywhere, the first two pages (or one page's halves) stand in, marked guessed."""
    found: list[tuple[tuple[int, int], Card]] = []  # (page, row) sort key, card
    single: list[tuple[int, np.ndarray, Rect]] = []  # (page number, page, rect): one card per row
    for number, page in enumerate(pages, 1):
        page_rows = rows(detect_cards(page))
        if all(len(row) == 1 for row in page_rows):
            single += [(number, page, row[0]) for row in page_rows]
            continue
        for row_number, row in enumerate(page_rows, 1):
            for i in range(0, len(row), 2):
                pair = row[i:i + 2]
                guess = partner(pair[0], page, across=True) if len(pair) == 1 else None
                if guess:
                    pair = sorted([pair[0], guess], key=lambda rect: rect[0][0])
                back, front = pair
                where = f"page {number}, row {row_number}"
                found.append(((number, row_number), Card([page, page], [front, back], [front is guess, back is guess], where)))
    for i in range(0, len(single), 2):
        (number, page, front), *rest = single[i:i + 2]
        if rest:
            (other, back_page, back), = rest
            where = f"page {number}" if other == number else f"pages {number}–{other}"
            card = Card([page, back_page], [front, back], [False, False], where)
        else:
            card = Card([page, page], [front, partner(front, page, across=False)], [False, True], f"page {number}")
        found.append(((number, 0), card))
    if found:
        return [card for _, card in sorted(found, key=lambda item: item[0])]
    if len(pages) >= 2:
        return [Card(pages[:2], [band(page, 0, 1) for page in pages[:2]], [True, True], "pages 1–2")]
    return [Card([pages[0]] * 2, [band(pages[0], i, 2) for i in range(2)], [True, True], "page 1")]


def downscale(card: Image.Image, boxes: list[Box]) -> Image.Image:
    """The card at OUT_DPI: small files, text still readable, the boxes painted pure black.
    Painted after the downscale and grown outward to 8-px JPEG blocks: a block that is all black decodes to exactly (0, 0, 0) with no ringing."""
    out = card.resize((max(1, round(card.width * OUT_DPI / DPI)), max(1, round(card.height * OUT_DPI / DPI))), Image.LANCZOS)
    k = OUT_DPI / DPI / 8
    for cx, cy, w, h in boxes:
        x0, x1 = (min(max(n * 8, 0), out.width) for n in (math.floor((cx - w / 2) * k), math.ceil((cx + w / 2) * k)))
        y0, y1 = (min(max(n * 8, 0), out.height) for n in (math.floor((cy - h / 2) * k), math.ceil((cy + h / 2) * k)))
        if x0 < x1 and y0 < y1:
            out.paste((0, 0, 0), (x0, y0, x1, y1))
    return out


def jpeg_options(boxes: list[list[Box]]) -> dict:
    """JPEG, but with boxes full chroma resolution: 4:2:0 upsampling bleeds the neighbours' color into the box edge (not pure black)."""
    return JPEG | {"subsampling": 0} if any(boxes) else JPEG


def save_cards(cards: list[Image.Image], output: Path, boxes: list[list[Box]], titles: list[str] | None = None) -> None:
    """One page per card, page = card, at OUT_DPI (small, still readable); no metadata beyond the Producer.
    titles: a bookmark per front and back pair (pages 1, 3, 5, …), so a PDF of many cards can be searched by name."""
    from pdf_compress import strip_metadata
    from pypdf import PdfWriter

    pages = [downscale(card, card_boxes) for card, card_boxes in zip(cards, boxes)]
    pages[0].save(output, "PDF", save_all=True, append_images=pages[1:], resolution=OUT_DPI, **jpeg_options(boxes))
    if titles:
        writer = PdfWriter(clone_from=io.BytesIO(output.read_bytes()))
        for index, title in enumerate(titles):
            writer.add_outline_item(title, index * 2)
        writer.write(output)
    strip_metadata(output)


def save_jpgs(cards: list[Image.Image], outputs: list[Path], boxes: list[list[Box]]) -> None:
    """One JPEG per card at OUT_DPI; no EXIF (Pillow writes none unless asked)."""
    for card, output, card_boxes in zip(cards, outputs, boxes):
        downscale(card, card_boxes).save(output, "JPEG", dpi=(OUT_DPI, OUT_DPI), **jpeg_options([card_boxes]))


def read_text(card: Image.Image, scale: float, ink: int) -> str:
    """Tesseract's English reading of the card, after dropping the background: a pixel is ink only when its brightest
    channel is below `ink`. Black print stays; yellow, blue or white card backgrounds and their security patterns go."""
    brightest = np.asarray(card.convert("RGB")).max(axis=2)
    image = Image.fromarray(np.where(brightest < ink, 0, 255).astype(np.uint8))
    if scale != 1:
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    png = io.BytesIO()
    image.save(png, "PNG")
    result = subprocess.run(
        [TESSERACT, "stdin", "stdout", "-l", "eng", "--psm", "6"], input=png.getvalue(), capture_output=True, timeout=60,
        env=os.environ | {"OMP_THREAD_LIMIT": "1"},  # callers run several at once; one thread each is faster overall
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout.decode("utf-8", "replace")


def name_from_text(text: str) -> str | None:
    """A file name from what a card says: a UAE traffic plate (16/16402 → 16-16402) or an Emirates ID number."""
    if plate := PLATE.search(text):
        return f"{plate[1]}-{plate[2]}"
    if eid := EMIRATES_ID.search(text):
        return f"784-{eid[1]}-{eid[2]}-{eid[3]}"
    return None


def suggest_name(sides: list[Image.Image]) -> tuple[str, int] | None:
    """A name read off the card and the side it was on (0 front, 1 back; the back is read only when the front gives
    nothing). None without Tesseract or a trustworthy reading. On real scans OCR finds about half the plates and some
    passes drop a digit, so a name counts only when two passes agree on it and no pass reads a different one.
    The user still confirms it."""
    if not Path(TESSERACT).is_file():
        return None
    for side, card in enumerate(sides):
        names = []
        for top, bottom, scale, ink in READINGS:
            band = card.crop((0, round(card.height * top), card.width, round(card.height * bottom)))
            if name := name_from_text(read_text(band, scale, ink)):
                names.append(name)
        if len(set(names)) == 1 and len(names) >= 2:
            return names[0], side
        if names:  # a name is on this side but unconfirmed or contested: the other side won't settle it
            return None
    return None


def save_batch(jobs: Iterable[tuple[list[Image.Image], list[list[Box]], list[Path]]], folder: Path) -> list[Path]:
    """Many cards into folder: per job, its sides to one path (a 2-page PDF) or two (front and back JPGs).
    Every file is encoded in a staging folder first, so a failure mid-batch leaves the folder untouched; then they replace same-named files.
    jobs is consumed lazily, so only one card's cuts are in memory at a time."""
    from pdf_split import staging_dir

    folder.mkdir(parents=True, exist_ok=True)
    staging = staging_dir(folder, ".cards_")
    try:
        outputs = []
        for sides, boxes, paths in jobs:
            staged = [staging / path.name for path in paths]
            save_cards(sides, staged[0], boxes) if len(staged) == 1 else save_jpgs(sides, staged, boxes)
            outputs += paths
        for path in outputs:
            os.replace(staging / path.name, path)
        return outputs
    finally:
        shutil.rmtree(staging, ignore_errors=True)
