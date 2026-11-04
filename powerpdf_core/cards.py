"""Card detection, edit commands and destination-safe snapshot exports.

Page arrays are shared immutable inputs. Geometry, names and masks belong to the
session; export snapshots never copy full scan arrays or consult mutable edits.
"""
from __future__ import annotations

import io
import math
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np
from PIL import Image

from .jobs import Progress
from .outputs import commit_files, destination_lock, staging_dir
from .render import RenderSession

DPI = 300
OUT_DPI = 150
JPEG = {"quality": 80, "optimize": True}
MAX_PAGES = 12
MAX_PAGE_PIXELS = 32_000_000
MAX_SCAN_BYTES = 384 * 1024 * 1024
Rect = tuple[tuple[float, float], tuple[float, float], float]
Box = tuple[float, float, float, float]


def _check_cancel(cancel) -> None:
    if cancel is not None:
        cancel.raise_if_cancelled()


def render_pages(paths: list[Path], limit: int = MAX_PAGES, *, cancel=None, progress=None) -> tuple[list[np.ndarray], int]:
    """Render at true 300 dpi through the sole PDFium owner, within a scan budget."""
    if not 0 < limit <= MAX_PAGES:
        raise ValueError(f"Card analysis accepts 1–{MAX_PAGES} pages")
    pages, total, used = [], 0, 0
    for path in paths:
        _check_cancel(cancel)
        with RenderSession(path) as session:
            total += session.info.page_count
            for index in range(min(session.info.page_count, limit - len(pages))):
                _check_cancel(cancel)
                w, h = session.info.page_sizes[index]
                pixels = math.ceil(w * DPI / 72) * math.ceil(h * DPI / 72)
                if pixels > MAX_PAGE_PIXELS or used + pixels * 3 > MAX_SCAN_BYTES:
                    raise ValueError("Scan exceeds the card-analysis pixel/memory limit; split or resize the scan first")
                with session.render(index, scale=DPI / 72, purpose="card-analysis", max_pixels=MAX_PAGE_PIXELS) as image:
                    page = np.asarray(image.convert("RGB"))
                page.setflags(write=False)
                pages.append(page)
                used += page.nbytes
                if progress is not None:
                    progress(Progress(len(pages), min(total, limit), input_path=Path(path)))
            session.check_source_unchanged()
    return pages, total


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


def _crop_size(rect: Rect) -> tuple[int, int]:
    (cx, cy), (w, h), angle = rect
    if not all(math.isfinite(value) for value in (cx, cy, w, h, angle)) or min(w, h) <= 0:
        raise ValueError("Crop geometry must be finite with positive dimensions")
    size = max(1, round(w)), max(1, round(h))
    if size[0] * size[1] > MAX_PAGE_PIXELS or max(size) >= 32767:
        raise ValueError("Crop exceeds the 32-million-pixel/32766-pixel-side card limit; reduce its dimensions")
    return size


def cut(img: np.ndarray, rect: Rect) -> Image.Image:
    """The rect's contents, rotated upright."""
    size = _crop_size(rect)
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
    if not pages:
        raise ValueError("No pages available for card detection")
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


def _encode_cards(cards: Sequence[Image.Image], output: Path, boxes: Sequence[Sequence[Box]], titles: Sequence[str] | None = None) -> None:
    from .compress import strip_metadata
    from pypdf import PdfWriter

    if not cards or len(cards) != len(boxes):
        raise ValueError("Each card side needs a corresponding box list")
    pages = [downscale(card, card_boxes) for card, card_boxes in zip(cards, boxes)]
    try:
        pages[0].save(output, "PDF", save_all=True, append_images=pages[1:], resolution=OUT_DPI, **jpeg_options(boxes))
        if titles:
            writer = PdfWriter(clone_from=io.BytesIO(output.read_bytes()))
            try:
                for index, title in enumerate(titles):
                    writer.add_outline_item(title, index * 2)
                writer.write(output)
            finally:
                writer.close()
        strip_metadata(output)
    finally:
        for page in pages:
            page.close()


def _validate_paths(paths: Sequence[Path], sources: Sequence[Path] = ()) -> list[Path]:
    from .files import validate_output

    checked = [validate_output(Path(path), inputs=sources, suffix=Path(path).suffix) for path in paths]
    seen: set[str] = set()
    for path in checked:
        key = str(path.resolve()).casefold()
        if key in seen:
            raise ValueError("Card outputs must have unique destinations")
        seen.add(key)
        if path.exists() and any(path.samefile(other) for other in checked if other != path and other.exists()):
            raise ValueError("Card outputs refer to the same file")
    return checked


def save_cards(cards: list[Image.Image], output: Path, boxes: list[list[Box]], titles: list[str] | None = None, *, sources=(), cancel=None) -> None:
    """Encode a card-sized image PDF, strip metadata, then commit staged output."""
    output, = _validate_paths([output], sources)
    _check_cancel(cancel)
    staging = staging_dir(output.parent)
    try:
        staged = staging / output.name
        _encode_cards(cards, staged, boxes, titles)
        commit_files([(staged, output)], cancel=cancel)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _encode_jpgs(cards, outputs, boxes) -> None:
    if len(cards) != len(outputs) or len(cards) != len(boxes):
        raise ValueError("Each card side needs an output and box list")
    for card, output, card_boxes in zip(cards, outputs, boxes):
        with downscale(card, card_boxes) as image:
            image.save(output, "JPEG", dpi=(OUT_DPI, OUT_DPI), **jpeg_options([card_boxes]))


def save_jpgs(cards: list[Image.Image], outputs: list[Path], boxes: list[list[Box]], *, sources=(), cancel=None) -> None:
    """Stage every side before replacing any destination; never retain EXIF."""
    outputs = _validate_paths(outputs, sources)
    if not outputs:
        raise ValueError("No card outputs")
    _check_cancel(cancel)
    with destination_lock(outputs[0].parent):
        staging = staging_dir(outputs[0].parent)
        try:
            staged = [staging / f"{i}.jpg" for i in range(len(outputs))]
            _encode_jpgs(cards, staged, boxes)
            commit_files(zip(staged, outputs), cancel=cancel)
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def save_batch(jobs: Iterable[tuple[list[Image.Image], list[list[Box]], list[Path]]], folder: Path, *, sources=(), cancel=None, progress=None, before_commit=None) -> list[Path]:
    """Encode lazily into destination-local staging; commit only complete batches.

    Replacements are serialized, not a multi-file atomic transaction. A commit
    failure reports its committed subset through outputs.OutputCommitError.
    """
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    with destination_lock(folder):
        staging = staging_dir(folder)
        try:
            pairs, outputs = [], []
            for number, (sides, boxes, paths) in enumerate(jobs, 1):
                _check_cancel(cancel)
                paths = _validate_paths(paths, sources)
                if any(path.parent != folder for path in paths):
                    raise ValueError("Batch outputs must be inside the selected folder")
                if len(paths) not in (1, 2):
                    raise ValueError("A card requires one PDF or two JPG destinations")
                _validate_paths(outputs + paths, sources)
                staged = [staging / f"{number}_{i}{path.suffix}" for i, path in enumerate(paths)]
                if len(paths) == 1:
                    _encode_cards(sides, staged[0], boxes)
                else:
                    _encode_jpgs(sides, staged, boxes)
                pairs.extend(zip(staged, paths))
                outputs.extend(paths)
                if progress is not None:
                    progress(Progress(number, None))
            if before_commit is not None:
                before_commit()
            return commit_files(pairs, cancel=cancel)
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def card_file_name(value: str) -> str:
    """Sanitize a confirmed card name consistently on every platform."""
    value = value.translate(str.maketrans({"/": "-", "\\": "-", ":": "-"}))
    value = "".join(ch for ch in value if ch not in '<>:"/\\|?*' and ch >= " ")
    value = " ".join(value.split()).strip(" .")
    # Windows device names are forbidden even when an extension is appended.
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)}
    return f"_{value}" if value.partition(".")[0].upper() in reserved else value


def card_label(name: str) -> str:
    number, _, label = name.partition(" ")
    return label if any(ch.isdigit() for ch in number) else ""


def _source_revision(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


@dataclass(frozen=True)
class CardSnapshot:
    card_id: str
    revision: int
    pages: tuple[np.ndarray, ...]
    rects: tuple[Rect, ...]
    guessed: tuple[bool, ...]
    boxes: tuple[tuple[Box, ...], ...]
    name: str | None
    where: str
    number: int

    def cuts(self) -> list[Image.Image]:
        return [cut(page, rect) for page, rect in zip(self.pages, self.rects)]


@dataclass(frozen=True)
class ExportSnapshot:
    sources: tuple[Path, ...]
    source_revisions: tuple[tuple[int, int, int, int], ...]
    cards: tuple[CardSnapshot, ...]

    def check_sources(self) -> None:
        if any(_source_revision(path) != revision for path, revision in zip(self.sources, self.source_revisions)):
            raise ValueError("A source PDF changed after card editing began; reload before exporting")

    def export(self, paths: list[list[Path]], folder: Path | None = None, titles: list[str] | None = None, *, cancel=None, progress=None) -> list[Path]:
        """Export only captured edits. Sources and destination collisions are checked
        before encoding and sources are checked again immediately before commit."""
        if not self.cards:
            raise ValueError("Every card is skipped")
        self.check_sources()
        _check_cancel(cancel)
        outputs = _validate_paths([path for group in paths for path in group], self.sources)

        def before_commit() -> None:
            self.check_sources()
            _validate_paths(outputs, self.sources)
        if folder is not None:
            if len(paths) != len(self.cards):
                raise ValueError("Each kept card needs a destination set")

            def jobs():
                for card, destinations in zip(self.cards, paths):
                    sides = card.cuts()
                    try:
                        yield sides, card.boxes, destinations
                    finally:
                        for side in sides:
                            side.close()

            return save_batch(jobs(), folder, sources=self.sources, cancel=cancel, progress=progress, before_commit=before_commit)
        if len(paths) != 1 or len(outputs) not in (1, 2):
            raise ValueError("Choose one PDF, one side's JPG, or one card's two JPGs")
        jpeg = all(path.suffix.lower() in (".jpg", ".jpeg") for path in outputs)
        if jpeg:
            if len(self.cards) != 1 or len(self.cards[0].pages) != len(outputs):
                raise ValueError("Each JPG destination must match exactly one captured card side")
        elif len(outputs) != 1 or outputs[0].suffix.lower() != ".pdf":
            raise ValueError("Choose one PDF or matching JPG destinations")
        staging = staging_dir(outputs[0].parent)
        sides = []
        try:
            for card in self.cards:
                _check_cancel(cancel)
                sides.extend(card.cuts())
            boxes = [side for card in self.cards for side in card.boxes]
            staged = [staging / f"{i}{path.suffix}" for i, path in enumerate(outputs)]
            if jpeg:
                _encode_jpgs(sides, staged, boxes)
            else:
                _encode_cards(sides, staged[0], boxes, titles)
            before_commit()
            return commit_files(zip(staged, outputs), cancel=cancel)
        finally:
            for side in sides:
                side.close()
            shutil.rmtree(staging, ignore_errors=True)


class CardEditSession:
    """One editing model for TUI and GUI, with lightweight command history.

    Commands own all mutations; adapters may read lists for drawing, but must not
    modify them. Undo/redo restore geometry/masks/names, never scan-array copies.
    OCR revisions increase even on undo, so an older result cannot become valid
    again just because geometry happens to match an earlier state.
    """

    def __init__(self, sources: Sequence[Path], cards: Sequence[Card], labels: Sequence[str] = (), *,
                 source_revisions: tuple[tuple[int, int, int, int], ...] | None = None) -> None:
        if not sources or not cards:
            raise ValueError("A card session needs sources and detected cards")
        self.sources = [Path(path).resolve() for path in sources]
        current_revisions = tuple(_source_revision(path) for path in self.sources)
        if source_revisions is not None and source_revisions != current_revisions:
            raise ValueError("A source PDF changed during card loading; reload the scan")
        self.source_revisions = current_revisions if source_revisions is None else source_revisions
        self.cards = [Card(card.pages[:], card.rects[:], card.guessed[:], card.where) for card in cards]
        self._original_rects = tuple(tuple(card.rects) for card in cards)
        self.card_boxes: list[list[list[Box]]] = [[[], []] for _ in cards]
        self.names: list[str | None] = [None] * len(cards)
        self.suggested: list[tuple[str, int] | None] = [None] * len(cards)
        self.swapped = [False] * len(cards)
        self.skipped: set[int] = set()
        self.labels = list(dict.fromkeys(labels))[:12]
        self.index, self.side, self.active = 0, 0, None
        self.card_ids = [uuid4().hex for _ in cards]
        self.revisions = [0] * len(cards)
        self._undo: list[tuple] = []
        self._redo: list[tuple] = []
        self._edit_group: tuple[int, int] | None = None

    @property
    def card(self) -> Card:
        return self.cards[self.index]

    @property
    def pages(self) -> list[np.ndarray]:
        return self.card.pages

    @property
    def rects(self) -> list[Rect]:
        return self.card.rects

    @property
    def boxes(self) -> list[list[Box]]:
        return self.card_boxes[self.index]

    def _state(self) -> tuple:
        return (
            [(card.pages[:], card.rects[:], card.guessed[:]) for card in self.cards],
            [[side[:] for side in boxes] for boxes in self.card_boxes],
            self.names[:], self.skipped.copy(), self.labels[:], self.swapped[:],
            self.index, self.side, self.active,
        )

    def begin_edit(self) -> None:
        """Group successive commands on this card into one undoable gesture.

        Revisions still advance for every command, so in-flight OCR cannot match
        intermediate geometry. Adapters must end the gesture before navigating.
        Ordinary ungrouped commands retain the TUI's one-command undo semantics.
        """
        if self._edit_group is not None:
            raise RuntimeError("An edit gesture is already active")
        self._edit_group = (self.index, len(self._undo))

    def end_edit(self) -> None:
        """Finish a gesture; a click without commands creates no history entry."""
        self._edit_group = None

    def _record(self, index: int, *, invalidate: bool = True) -> None:
        if self._edit_group is not None and self._edit_group[0] != index:
            raise RuntimeError("End the edit gesture before changing cards")
        if self._edit_group is None or len(self._undo) == self._edit_group[1]:
            self._undo.append((index, invalidate, self._state()))
        elif invalidate and not self._undo[-1][1]:
            recorded_index, _, state = self._undo[-1]
            self._undo[-1] = (recorded_index, True, state)
        self._redo.clear()
        if invalidate:
            self.revisions[index] += 1
            self.suggested[index] = None

    def _restore(self, state: tuple) -> None:
        cards, self.card_boxes, self.names, self.skipped, self.labels, self.swapped, self.index, self.side, self.active = state
        for card, (pages, rects, guessed) in zip(self.cards, cards):
            card.pages, card.rects, card.guessed = pages, rects, guessed

    def _travel(self, source: list, target: list) -> bool:
        self.end_edit()
        if not source:
            return False
        index, invalidate, state = source.pop()
        target.append((index, invalidate, self._state()))
        self._restore(state)
        if invalidate:
            self.revisions[index] += 1
            self.suggested[index] = None
        return True

    def undo(self) -> bool:
        return self._travel(self._undo, self._redo)

    def redo(self) -> bool:
        return self._travel(self._redo, self._undo)

    def step(self, step: int) -> None:
        self.index = min(max(self.index + step, 0), len(self.cards) - 1)
        self.active = None

    def switch(self, side: int | None = None) -> None:
        if side is not None and side not in (0, 1):
            raise ValueError("Card side must be front (0) or back (1)")
        self.side = 1 - self.side if side is None else side
        self.active = None

    def cycle(self) -> None:
        step = 0 if self.active is None else self.active + 1
        self.active = step if step < len(self.boxes[self.side]) else None

    def select_box(self, index: int | None) -> None:
        if index is not None and not 0 <= index < len(self.boxes[self.side]):
            raise IndexError("No such black box")
        self.active = index

    def swap(self) -> None:
        found = self.suggested[self.index]
        self._record(self.index)
        self.card.swap()
        self.boxes.reverse()
        self.swapped[self.index] = not self.swapped[self.index]
        self.suggested[self.index] = (found[0], 1 - found[1]) if found else None
        self.active = None

    def skip(self) -> None:
        self._record(self.index, invalidate=False)
        self.skipped ^= {self.index}

    def name(self, index: int, value: str | None) -> None:
        if value is None:
            return
        value = card_file_name(value)
        self._record(index)
        self.names[index] = value or None
        if label := card_label(value):
            self.labels = ([label] + [old for old in self.labels if old != label])[:12]

    def add_box(self) -> None:
        self._record(self.index)
        _, (w, h), _ = self.rects[self.side]
        self.boxes[self.side].append((w / 2, h / 2, w * 0.4, h * 0.1))
        self.active = len(self.boxes[self.side]) - 1

    def remove_box(self) -> None:
        if self.active is not None:
            self._record(self.index)
            del self.boxes[self.side][self.active]
            self.active = None

    def change(self, dx: float, dy: float, dw: float = 0, dh: float = 0, da: float = 0) -> None:
        boxes = self.boxes[self.side]
        if self.active is not None and not da:
            self._record(self.index)
            cx, cy, w, h = boxes[self.active]
            boxes[self.active] = (cx + dx, cy + dy, max(8, w + dw), max(8, h + dh))
            return
        (cx, cy), (w, h), angle = self.rects[self.side]
        self.set_crop(((cx + dx, cy + dy), (max(20, w + dw), max(20, h + dh)), angle + da))

    def set_crop(self, rect: Rect) -> None:
        """Set native scan-pixel geometry, carrying masks through the core affine."""
        (cx, cy), (w, h), angle = rect
        if not all(math.isfinite(value) for value in (cx, cy, w, h, angle)):
            raise ValueError("Crop geometry must be finite")
        if w < 20 or h < 20:
            raise ValueError("Crop dimensions must be at least 20 source pixels")
        _crop_size(rect)
        if rect == self.rects[self.side]:
            return
        self._record(self.index)
        old = self.rects[self.side]
        self.rects[self.side] = rect
        self.boxes[self.side] = carry(self.boxes[self.side], old, rect)
        self.card.guessed[self.side] = False

    def set_box(self, box: Box) -> None:
        """Set the selected mask in cropped-side pixels, never screen pixels."""
        if self.active is None:
            raise ValueError("Select a black box first")
        if not all(math.isfinite(value) for value in box) or min(box[2:]) < 8:
            raise ValueError("Box geometry must be finite and at least 8 pixels")
        if box == self.boxes[self.side][self.active]:
            return
        self._record(self.index)
        self.boxes[self.side][self.active] = box

    def rotate_crop(self, angle: float) -> None:
        """Rotate a crop; quarter turns exchange its dimensions to retain content.

        Masks retain the existing carry() axis-aligned page-location semantics.
        Small tilt commands leave crop dimensions unchanged.
        """
        center, (w, h), current = self.rects[self.side]
        if angle % 180 in (90, -90):
            w, h = h, w
        self.set_crop((center, (w, h), current + angle))

    def reset_crop(self) -> None:
        """Restore the detected crop for this side, respecting manual side swaps."""
        side = 1 - self.side if self.swapped[self.index] else self.side
        self.set_crop(self._original_rects[self.index][side])

    def kept(self) -> list[int]:
        return [index for index in range(len(self.cards)) if index not in self.skipped]

    def file_stems(self) -> dict[int, str]:
        width = max(2, len(str(len(self.cards))))
        stems, seen = {}, set()
        for index in self.kept():
            base = self.names[index] or f"{self.sources[0].stem}_card_{index + 1:0{width}d}"
            stem, count = base, 1
            while stem.casefold() in seen:
                count += 1
                stem = f"{base} ({count})"
            seen.add(stem.casefold())
            stems[index] = stem
        return stems

    def batch_paths(self, folder: Path, kind: str) -> list[list[Path]]:
        if kind not in ("pdf", "jpg"):
            raise ValueError("Card output must be PDF or JPG")
        suffixes = [".pdf"] if kind == "pdf" else ["_front.jpg", "_back.jpg"]
        groups = [[Path(folder).resolve() / f"{stem}{suffix}" for suffix in suffixes] for stem in self.file_stems().values()]
        _validate_paths([path for group in groups for path in group], self.sources)
        return groups

    def _snapshot_card(self, index: int) -> CardSnapshot:
        card = self.cards[index]
        return CardSnapshot(self.card_ids[index], self.revisions[index], tuple(card.pages), tuple(card.rects),
                            tuple(card.guessed), tuple(tuple(side) for side in self.card_boxes[index]),
                            self.names[index], card.where, index + 1)

    def snapshot(self) -> ExportSnapshot:
        return ExportSnapshot(tuple(self.sources), self.source_revisions, tuple(self._snapshot_card(k) for k in self.kept()))

    def ocr_cards(self) -> tuple:
        from .ocr import OCRCard
        return tuple(OCRCard(self.card_ids[k], self.revisions[k], tuple(card.pages), tuple(card.rects))
                     for k, card in enumerate(self.cards) if self.names[k] is None)

    def apply_suggestion(self, result) -> bool:
        try:
            index = self.card_ids.index(result.card_id)
        except ValueError:
            return False
        if result.revision != self.revisions[index] or self.names[index] is not None:
            return False
        self.suggested[index] = (result.name, result.side) if result.name else None
        return True


def load_cards(sources: Sequence[Path], *, labels: Sequence[str] = (), limit: int = MAX_PAGES,
               cancel=None, progress=None) -> tuple[CardEditSession, int]:
    """Render, detect and bind one source revision without copying page arrays.

    Capture revisions before opening any PDF, then validate after detection.
    This closes the gap between renderer close and edit-session construction.
    """
    sources = [Path(path).resolve() for path in sources]
    revisions = tuple(_source_revision(path) for path in sources)
    pages, total = render_pages(sources, limit, cancel=cancel, progress=progress)
    _check_cancel(cancel)
    detected = pair_cards(pages)
    _check_cancel(cancel)
    return CardEditSession(sources, detected, labels, source_revisions=revisions), total
