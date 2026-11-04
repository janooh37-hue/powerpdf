"""Page image exports; all PDFium ownership belongs to RenderSession."""
from __future__ import annotations

import shutil
from pathlib import Path

from . import files, outputs
from .jobs import CancelToken, Progress, ProgressSink
from .render import RenderSession

FORMATS = {
    "jpg": ("JPEG", {"quality": 80, "optimize": True}, "small; best for scans and photos (default)"),
    "png": ("PNG", {}, "lossless; sharpest text, but scans come out several times larger"),
}
DPI = 150
DPI_RANGE = (50, 600)
A4_LONG_SIDE = 842 / 72


def page_scale(width: float, height: float, dpi: int) -> float:
    """Normalize oversized phone scans to A4; do not apply to previews/printing."""
    return dpi / 72 * min(1.0, A4_LONG_SIDE * 72 / max(width, height))


def to_images(source: Path, fmt: str = "jpg", dpi: int = DPI, output: Path | None = None, *,
              cancel: CancelToken | None = None, progress: ProgressSink | None = None) -> list[Path]:
    """Export one page to a file or multiple pages to a folder, optionally explicit."""
    source = Path(source)
    kind, options, _ = FORMATS[fmt]
    if not DPI_RANGE[0] <= dpi <= DPI_RANGE[1]:
        raise ValueError(f"dpi must be between {DPI_RANGE[0]} and {DPI_RANGE[1]}")
    if cancel is not None:
        cancel.raise_if_cancelled()
    with RenderSession(source) as session:
        count = session.info.page_count
        if not count:
            raise ValueError("PDF has no pages")
        width = max(3, len(str(count)))
        names = ([f"{source.stem}_image.{fmt}"] if count == 1 else
                 [f"{source.stem}_page_{number:0{width}d}.{fmt}" for number in range(1, count + 1)])
        target = Path(output) if output is not None else source.with_name(
            names[0] if count == 1 else f"{source.stem}_images")
        with outputs.destination_lock(target):
            if count == 1:
                destinations = [files.validate_output(target, [source], suffix=f".{fmt}")]
                parent = target.parent
            else:
                target.mkdir(exist_ok=True)
                destinations = [files.validate_output(target / name, [source], suffix=f".{fmt}") for name in names]
                parent = target
            staging = outputs.staging_dir(parent)
            try:
                pairs = []
                for index, (name, destination) in enumerate(zip(names, destinations)):
                    if cancel is not None:
                        cancel.raise_if_cancelled()
                    image = session.render(index, scale=page_scale(*session.info.page_sizes[index], dpi),
                                           purpose="image-export")
                    try:
                        image.info.clear()
                        image.save(staging / name, kind, dpi=(dpi, dpi), **options)
                    finally:
                        image.close()
                    pairs.append((staging / name, destination))
                    if progress is not None:
                        progress(Progress(index + 1, count, name, source))
                obsolete = files.numbered_outputs(target, source.stem, f".{fmt}") if count > 1 else []
                return outputs.commit_files(pairs, cancel=cancel, remove=obsolete,
                                            before_commit=session.check_source_unchanged)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
