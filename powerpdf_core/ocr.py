"""Local, revision-tagged card OCR; suggestions always require confirmation."""
from __future__ import annotations

import io
import re
import subprocess
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable, Iterator

import numpy as np
from PIL import Image

import pdf_runtime
from .cards import Rect, cut, _check_cancel

PLATE = re.compile(r"(?<!\d)(\d{1,2}) ?[/|] ?(\d{3,6})(?!\d)")
EMIRATES_ID = re.compile(r"(?<!\d)784 ?-? ?(\d{4}) ?-? ?(\d{7}) ?-? ?(\d)(?!\d)")
READINGS = ((0.0, 1.0, 1.0, 140), (0.0, 1.0, 1.0, 110), (0.0, 1.0, 1.5, 110), (0.22, 0.45, 1.5, 110), (0.22, 0.45, 2.0, 120))


@dataclass(frozen=True)
class OCRCard:
    card_id: str
    revision: int
    pages: tuple[np.ndarray, ...]
    rects: tuple[Rect, ...]


@dataclass(frozen=True)
class OCRResult:
    card_id: str
    revision: int
    name: str | None
    side: int = 0
    available: bool = True


def resolve_tesseract() -> str | None:
    """Discovery is an explicit worker operation, never an import side effect."""
    return pdf_runtime.resolve_tool("tesseract")


def read_text(card: Image.Image, scale: float, ink: int, *, executable: str, data_dir: Path | None = None, cancel=None) -> str:
    """Drop coloured security backgrounds and read black print with one thread."""
    _check_cancel(cancel)
    brightest = np.asarray(card.convert("RGB")).max(axis=2)
    image = Image.fromarray(np.where(brightest < ink, 0, 255).astype(np.uint8))
    if scale != 1:
        resized = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
        image.close()
        image = resized
    png = io.BytesIO()
    try:
        image.save(png, "PNG")
    finally:
        image.close()
    environment = pdf_runtime.helper_env(executable) | {"OMP_THREAD_LIMIT": "1"}
    args = [executable, "stdin", "stdout", "-l", "eng", "--psm", "6"]
    if data_dir is not None:
        args += ["--tessdata-dir", str(data_dir)]
    import time
    with pdf_runtime.helper_launch_scope():
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    with process:
        deadline = time.monotonic() + 60
        data = png.getvalue()
        try:
            while True:
                _check_cancel(cancel)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, 60)
                try:
                    stdout, stderr = process.communicate(input=data, timeout=min(0.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    data = None
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, args, output=stdout, stderr=stderr)
            return stdout.decode("utf-8", "replace")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


def name_from_text(text: str) -> str | None:
    """A file name from what a card says: a UAE traffic plate (16/16402 → 16-16402) or an Emirates ID number."""
    if plate := PLATE.search(text):
        return f"{plate[1]}-{plate[2]}"
    if eid := EMIRATES_ID.search(text):
        return f"784-{eid[1]}-{eid[2]}-{eid[3]}"
    return None


def suggest_name(sides: list[Image.Image], *, executable: str | None = None, data_dir: Path | None = None, cancel=None) -> tuple[str, int] | None:
    """Require 2+ agreeing passes and no dissent. Read back only without front
    evidence; contested or single-pass front evidence cannot be settled by back."""
    executable = executable or resolve_tesseract()
    if not executable:
        return None
    for side, card in enumerate(sides):
        names = []
        for top, bottom, scale, ink in READINGS:
            _check_cancel(cancel)
            with card.crop((0, round(card.height * top), card.width, round(card.height * bottom))) as band:
                if name := name_from_text(read_text(band, scale, ink, executable=executable, data_dir=data_dir, cancel=cancel)):
                    names.append(name)
        if len(set(names)) == 1 and len(names) >= 2:
            return names[0], side
        if names:
            return None
    return None


def read_names(cards: Iterable[OCRCard], *, executable: str | None = None, data_dir: Path | None = None, cancel=None) -> Iterator[OCRResult]:
    """At most four cards in flight, each child using one Tesseract thread.

    Requests carry immutable geometry and stable IDs/revisions; callers apply
    results through CardEditSession.apply_suggestion, which rejects stale edits.
    """
    _check_cancel(cancel)
    executable = executable or resolve_tesseract()
    if not executable:
        for card in cards:
            _check_cancel(cancel)
            yield OCRResult(card.card_id, card.revision, None, available=False)
        return

    def read(card: OCRCard) -> OCRResult:
        _check_cancel(cancel)
        sides = [cut(page, rect) for page, rect in zip(card.pages, card.rects)]
        try:
            found = suggest_name(sides, executable=executable, data_dir=data_dir, cancel=cancel)
            return OCRResult(card.card_id, card.revision, found[0], found[1]) if found else OCRResult(card.card_id, card.revision, None)
        finally:
            for side in sides:
                side.close()

    requests = iter(cards)
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="card-ocr") as pool:
        pending = set()
        for _ in range(4):
            if (card := next(requests, None)) is not None:
                pending.add(pool.submit(read, card))
        try:
            while pending:
                _check_cancel(cancel)
                done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                for future in done:
                    _check_cancel(cancel)
                    yield future.result()
                    if (card := next(requests, None)) is not None:
                        pending.add(pool.submit(read, card))
        finally:
            for future in pending:
                future.cancel()
