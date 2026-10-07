"""Cut ID / vehicle-license cards out of scanned PDFs, straighten them, and put front and back on one A4 page."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pypdfium2 as pdfium
from PIL import Image

DPI = 300  # render scans at true size, so cut cards print at their real size
A4 = (2480, 3508)  # A4 in pixels at DPI
MARGIN = 236  # 20 mm
GAP = 118  # 10 mm between front and back

Rect = tuple[tuple[float, float], tuple[float, float], float]  # OpenCV RotatedRect: center, size, angle


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


def cut(img: np.ndarray, rect: Rect) -> Image.Image:
    """The rect's contents, rotated upright."""
    (cx, cy), (w, h), angle = rect
    m = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)
    m[:, 2] += (w / 2 - cx, h / 2 - cy)
    size = (max(1, round(w)), max(1, round(h)))
    return Image.fromarray(cv2.warpAffine(img, m, size, flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255)))


def pair_cards(pages: list[np.ndarray]) -> list[tuple[np.ndarray, Rect]]:
    """Front and back: one card from each of two pages, or two cards from a single page."""
    if len(pages) >= 2:
        return [(page, find_cards(page, 1)[0]) for page in pages[:2]]
    return [(pages[0], rect) for rect in find_cards(pages[0], 2)]


def compose(cards: list[Image.Image], output: Path) -> None:
    """Cards stacked top to bottom on a white A4 page, real size (shrunk only if they don't fit)."""
    page = Image.new("RGB", A4, "white")
    room = ((A4[0] - 2 * MARGIN), (A4[1] - 2 * MARGIN - GAP * (len(cards) - 1)) // len(cards))
    y = MARGIN
    for card in cards:
        card = card.copy()
        card.thumbnail(room)
        page.paste(card, ((A4[0] - card.width) // 2, y))
        y += card.height + GAP
    page.save(output, "PDF", resolution=DPI, quality=92)
