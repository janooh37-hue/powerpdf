"""Ghostscript compression, content crop and mandatory metadata removal."""
from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject

import pdf_runtime
from . import files, outputs
from .jobs import CancelToken, Progress, ProgressSink

QUALITY = {
    "screen": (72, "smallest; 72 dpi, fine for full pages, small print (e.g. ID cards) unreadable"),
    "ebook": (150, "balanced; 150 dpi, scans and cards stay readable (default)"),
    "printer": (300, "largest; 300 dpi, keeps print quality"),
}
SUFFIX = "_small"
INK = 235
A4_LONG_SIDE = 842 / 72


def run_gs(gs: str, *args: str, cancel: CancelToken | None = None) -> None:
    if cancel is not None:
        cancel.raise_if_cancelled()
    process = subprocess.Popen(
        [gs, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        env=pdf_runtime.helper_env(gs),
    )
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None:
                    cancel.raise_if_cancelled()
        if cancel is not None:
            cancel.raise_if_cancelled()
        if process.returncode:
            raise RuntimeError((stderr or stdout).strip() or "Ghostscript failed")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


def extent(on: np.ndarray, dpi: int) -> tuple[int, int] | None:
    """Content extent, ignoring specks and bridging gaps under one quarter inch."""
    edges = np.flatnonzero(np.diff(np.concatenate(([0], on.astype(np.int8), [0]))))
    merged: list[list[int]] = []
    for start, end in zip(edges[::2], edges[1::2]):
        if merged and start - merged[-1][1] < dpi // 4:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    kept = [run for run in merged if run[1] - run[0] >= dpi // 4]
    return (int(kept[0][0]), int(kept[-1][1])) if kept else None


def content_box(page: Image.Image, dpi: int) -> tuple[int, int, int, int] | None:
    with page.convert("L") as gray:
        ink = np.asarray(gray) < INK
    rows = extent(ink.mean(axis=1) > 0.01, dpi)
    if not rows:
        return None
    cols = extent(ink[rows[0]:rows[1]].mean(axis=0) > 0.01, dpi)
    if not cols:
        return None
    pad = dpi // 20
    height, width = ink.shape
    return (max(cols[0] - pad, 0), max(rows[0] - pad, 0),
            min(cols[1] + pad, width), min(rows[1] + pad, height))


def crop_to_content(gs: str, source: Path, staging: Path, dpi: int, *,
                    cancel: CancelToken | None = None, progress: ProgressSink | None = None) -> None:
    reader = PdfReader(io.BytesIO(source.read_bytes()))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("password-protected PDF")
    if not reader.pages:
        raise ValueError("PDF has no pages")
    longest = max(max(page.mediabox.width, page.mediabox.height) for page in reader.pages) / 72
    render_dpi = dpi * min(1.0, A4_LONG_SIDE / float(longest))
    pages: list[Image.Image] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            run_gs(gs, "-sDEVICE=png16m", f"-r{render_dpi:.2f}",
                   f"-sOutputFile={Path(tmp) / 'p%04d.png'}", str(source), cancel=cancel)
            for index, png in enumerate(sorted(Path(tmp).glob("p*.png")), start=1):
                if cancel is not None:
                    cancel.raise_if_cancelled()
                with Image.open(png) as rendered:
                    page = rendered.convert("RGB")
                box = content_box(page, dpi)
                if box:
                    cropped = page.crop(box)
                    page.close()
                    page = cropped
                pages.append(page)
                if progress is not None:
                    progress(Progress(index, len(reader.pages), f"Cropped page {index}", source))
        if cancel is not None:
            cancel.raise_if_cancelled()
        if not pages:
            raise ValueError("Ghostscript produced no page images")
        pages[0].save(staging, "PDF", save_all=True, append_images=pages[1:],
                      resolution=dpi, quality=80, optimize=True)
    finally:
        for page in pages:
            page.close()


def strip_metadata(path: Path) -> None:
    """Remove both PDF Info and XMP, including when shipping original-sized content."""
    # PdfWriter.__enter__ reinitializes the writer and discards cloned pages.
    writer = PdfWriter(clone_from=io.BytesIO(path.read_bytes()))
    try:
        writer.metadata = None
        writer._root_object.pop(NameObject("/Metadata"), None)
        outputs.atomic_write(path, lambda staged: writer.write(staged))
    finally:
        writer.close()


def compress(gs: str, source: Path, quality: str = "ebook", crop: bool = False,
             output: Path | None = None, *, cancel: CancelToken | None = None,
             progress: ProgressSink | None = None) -> Path:
    source = Path(source)
    output = files.validate_output(output or source.with_name(f"{source.stem}{SUFFIX}.pdf"), [source])
    dpi = QUALITY[quality][0]
    with outputs.destination_lock(output):
        if cancel is not None:
            cancel.raise_if_cancelled()
        snapshot = source.read_bytes()
        staging = outputs.staging_dir(output.parent)
        try:
            original = staging / "source.pdf"
            original.write_bytes(snapshot)
            staged = staging / "compressed.pdf"
            if crop:
                def crop_progress(event: Progress) -> None:
                    if progress is not None:
                        progress(Progress(event.completed, event.total, event.message, source))

                crop_to_content(gs, original, staged, dpi, cancel=cancel,
                                progress=crop_progress if progress is not None else None)
            else:
                run_gs(gs, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.5",
                       f"-dPDFSETTINGS=/{quality}", f"-sOutputFile={staged}", str(original), cancel=cancel)
                if staged.stat().st_size >= len(snapshot):
                    shutil.copyfile(original, staged)
            if cancel is not None:
                cancel.raise_if_cancelled()
            strip_metadata(staged)
            if progress is not None:
                progress(Progress(1, 1, f"Compressed {source.name}", source))

            def unchanged() -> None:
                if source.read_bytes() != snapshot:
                    raise ValueError(f"Source changed during compression: {source}")

            outputs.commit_files([(staged, output)], cancel=cancel, before_commit=unchanged)
            return output
        finally:
            shutil.rmtree(staging, ignore_errors=True)
