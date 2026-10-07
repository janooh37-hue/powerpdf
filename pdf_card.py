"""Cut ID / vehicle-license cards out of scanned PDFs, straighten them, and save front and back as a card-sized PDF or JPGs."""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
import pypdfium2 as pdfium
from PIL import Image

DPI = 300  # render scans at true size, so cuts keep their real size
OUT_DPI = 150  # saved cards: small files, text still readable
JPEG = {"quality": 80, "optimize": True}

Rect = tuple[tuple[float, float], tuple[float, float], float]  # OpenCV RotatedRect: center, size, angle
Box = tuple[float, float, float, float]  # black box on a crop: center x, y, width, height in crop pixels at DPI


def render_pages(paths: list[Path], limit: int = 2) -> list[np.ndarray]:
    """The first `limit` pages across the PDFs, as RGB arrays at DPI. Not thread-safe (pdfium)."""
    pages = []
    for path in paths:
        pdf = pdfium.PdfDocument(path)
        try:
            for page in pdf:
                if len(pages) == limit:
                    return pages
                pages.append(np.asarray(page.render(scale=DPI / 72).to_pil().convert("RGB")))
        finally:
            pdf.close()
    return pages


def landscape(rect: Rect) -> Rect:
    """Long side as width, angle in [-90, 90)."""
    center, (w, h), angle = rect
    if w < h:
        w, h, angle = h, w, angle - 90
    return center, (w, h), (angle + 90) % 180 - 90


def find_cards(img: np.ndarray, n: int) -> list[Rect]:
    """The n largest card-shaped regions, top to bottom; page bands when detection comes up short."""
    gray = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_RGB2GRAY), (5, 5), 0)
    # card outline (edges) plus printed content (anything darker than paper white), merged into blobs
    mask = cv2.Canny(gray, 30, 100) | (gray < 225).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    page_area = img.shape[0] * img.shape[1]
    rects = []
    for contour in sorted(contours, key=cv2.contourArea, reverse=True):
        rect = landscape(cv2.minAreaRect(contour))
        (w, h) = rect[1]
        # ponytail: shape filter tuned for ID-1 cards (85.6 x 54 mm, ratio 1.59); widen for other cards
        if 0.02 < w * h / page_area < 0.6 and 1.3 < w / h < 1.9:
            rects.append(rect)
        if len(rects) == n:
            break
    if len(rects) < n:
        H, W = img.shape[:2]
        rects = [landscape(((W / 2, H * (2 * i + 1) / (2 * n)), (W, H / n), 0)) for i in range(n)]
    return sorted(rects, key=lambda r: (round(r[0][1] / 50), r[0][0]))


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


def pair_cards(pages: list[np.ndarray]) -> list[tuple[np.ndarray, Rect]]:
    """Front and back: one card from each of two pages, or two cards from a single page."""
    if len(pages) >= 2:
        return [(page, find_cards(page, 1)[0]) for page in pages[:2]]
    return [(pages[0], rect) for rect in find_cards(pages[0], 2)]


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


def save_cards(cards: list[Image.Image], output: Path, boxes: list[list[Box]]) -> None:
    """One page per card, page = card, at OUT_DPI (small, still readable); no metadata beyond the Producer."""
    from pdf_compress import strip_metadata

    pages = [downscale(card, card_boxes) for card, card_boxes in zip(cards, boxes)]
    pages[0].save(output, "PDF", save_all=True, append_images=pages[1:], resolution=OUT_DPI, **jpeg_options(boxes))
    strip_metadata(output)


def save_jpgs(cards: list[Image.Image], outputs: list[Path], boxes: list[list[Box]]) -> None:
    """One JPEG per card at OUT_DPI; no EXIF (Pillow writes none unless asked)."""
    for card, output, card_boxes in zip(cards, outputs, boxes):
        downscale(card, card_boxes).save(output, "JPEG", dpi=(OUT_DPI, OUT_DPI), **jpeg_options([card_boxes]))
