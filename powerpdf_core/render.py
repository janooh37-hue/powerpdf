"""Owned PDF snapshots and the process-wide serialized PDFium rendering lane.

Only this module handles PDFium objects. Images returned to callers own their
pixels and may be changed or closed independently of a session or its caches.
Preview policy is deliberately separate from actual-size card/image rendering.
"""

from __future__ import annotations

import atexit
import hashlib
import math
import os
import threading
import weakref
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from PIL import Image

PDFIUM_LOCK = threading.RLock()
with PDFIUM_LOCK:
    import pypdfium2 as _pdfium
    from pypdfium2 import _library_scope

# pypdfium2 registers native library destruction at import time. Keep even that
# final cleanup in the same lane (including any outstanding native objects).
atexit.unregister(_library_scope.destroy_lib)


def _shutdown_pdfium() -> None:
    with PDFIUM_LOCK:
        _library_scope.destroy_lib()


atexit.register(_shutdown_pdfium)

RenderError = _pdfium.PdfiumError
PREVIEW_CACHE_BYTES = 128 * 1024 * 1024
THUMBNAIL_CACHE_BYTES = 32 * 1024 * 1024


class SourceChangedError(OSError):
    """A source path no longer contains the session's immutable snapshot."""


class RenderLimitError(ValueError):
    """The requested render exceeds the caller's explicit pixel budget."""


@dataclass(frozen=True)
class DocumentInfo:
    """Snapshot metadata and page dimensions in PDF canvas points."""

    page_count: int
    metadata: Mapping[str, str]
    page_sizes: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class _SourceRevision:
    path: Path | None
    stat: tuple[int, int, int, int, int] | None
    digest: bytes


def _stat_revision(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def preview_scale(width: float, height: float) -> float:
    """TUI preview resolution: at most 1.5x and 1600 pixels on the long side.

    Do not use transient widget measurements here. Thumbnail adapters may pass
    their own fixed-size scale; desktop zoom/DPR requests pass an explicit scale.
    """
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        raise ValueError("Page dimensions must be finite and positive")
    return min(1.5, 1600 / max(width, height))


class _ImageCache:
    """Byte-budgeted owned RGB images; all operations hold PDFIUM_LOCK."""

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.bytes = 0
        self.images: OrderedDict[tuple, Image.Image] = OrderedDict()

    def get(self, key: tuple) -> Image.Image | None:
        image = self.images.get(key)
        if image is not None:
            self.images.move_to_end(key)
            return image.copy()
        return None

    def put(self, key: tuple, image: Image.Image) -> bool:
        # Pillow's RGB storage uses a four-byte pixel, not packed RGB triples.
        cost = image.width * image.height * 4
        if cost > self.budget:
            return False
        while self.bytes + cost > self.budget:
            _, old = self.images.popitem(last=False)
            self.bytes -= old.width * old.height * 4
            old.close()
        self.images[key] = image
        self.bytes += cost
        return True

    def clear(self) -> None:
        for image in self.images.values():
            image.close()
        self.images.clear()
        self.bytes = 0


_preview_cache = _ImageCache(PREVIEW_CACHE_BYTES)
_thumbnail_cache = _ImageCache(THUMBNAIL_CACHE_BYTES)


def clear_render_cache() -> None:
    """Release both in-memory image caches without invalidating caller images."""
    with PDFIUM_LOCK:
        _preview_cache.clear()
        _thumbnail_cache.clear()


def _close_document(document) -> None:
    # The finalizer retains the document, not the session. Thus the document's
    # own weakref finalizer cannot run before this lock-owning cleanup callback.
    with PDFIUM_LOCK:
        document.close()


class RenderSession:
    """One byte-opened PDF revision, usable from any worker thread.

    Path handles are closed before PDFium sees the snapshot, so preview never
    prevents Windows rename/trash/overwrite. A changed path does not change this
    session's images: call check_source_unchanged() before committing an export.
    close() is idempotent; render() and entering a closed session raise ValueError.
    Info and source checks remain usable after close. Always prefer a context
    manager; a lock-owning finalizer also handles abandoned sessions.
    """

    def __init__(self, path_or_bytes: str | os.PathLike[str] | bytes) -> None:
        if isinstance(path_or_bytes, bytes):
            data = path_or_bytes
            path, revision = None, None
        else:
            path = Path(path_or_bytes).absolute()
            with path.open("rb") as source:
                revision = _stat_revision(os.fstat(source.fileno()))
                data = source.read()
                if _stat_revision(os.fstat(source.fileno())) != revision:
                    raise SourceChangedError(f"Source changed while opening: {path}")
            if _stat_revision(path.stat()) != revision:
                raise SourceChangedError(f"Source changed while opening: {path}")
        self._source_path = path.resolve() if path is not None else None
        self._source_bytes = data
        self._revision = _SourceRevision(path, revision, hashlib.sha256(data).digest())
        self._document = None
        with PDFIUM_LOCK:
            document = _pdfium.PdfDocument(data)
            try:
                count = len(document)
                sizes = []
                for index in range(count):
                    page = document[index]
                    try:
                        sizes.append(tuple(page.get_size()))
                    finally:
                        page.close()
                self._info = DocumentInfo(
                    count,
                    MappingProxyType(document.get_metadata_dict(skip_empty=True)),
                    tuple(sizes),
                )
                self._finalizer = weakref.finalize(self, _close_document, document)
                self._document = document
            except BaseException:
                document.close()
                raise

    @property
    def info(self) -> DocumentInfo:
        return self._info

    @property
    def source_path(self) -> Path | None:
        """Resolved source location at open time, or None for byte inputs."""
        return self._source_path

    @property
    def source_bytes(self) -> bytes:
        """The immutable opened revision, also usable by pypdf export jobs."""
        return self._source_bytes

    def source_unchanged(self) -> bool:
        """Whether the original path still identifies the complete source revision.

        Byte inputs have no mutable external source and always return True. For
        paths, check both file identity/stat and content, not only size/mtime.
        A renamed, missing or unreadable source is considered changed.
        """
        path, revision = self._revision.path, self._revision.stat
        if path is None:
            return True
        try:
            with path.open("rb") as source:
                if _stat_revision(os.fstat(source.fileno())) != revision:
                    return False
                digest = hashlib.file_digest(source, "sha256").digest()
                if _stat_revision(os.fstat(source.fileno())) != revision:
                    return False
            return digest == self._revision.digest and _stat_revision(path.stat()) == revision
        except OSError:
            return False

    def check_source_unchanged(self) -> None:
        """Raise SourceChangedError if an export would use a stale path revision."""
        if not self.source_unchanged():
            raise SourceChangedError(f"Source changed since opening: {self._revision.path}")

    def render(
        self,
        page_index: int,
        scale: float | None = None,
        rotation: int = 0,
        purpose: str = "preview",
        *,
        max_pixels: int | None = None,
    ) -> Image.Image:
        """Render an owned RGB image, with clockwise additional quarter-turns.

        An omitted scale uses the fixed TUI preview policy. Explicit scales are
        never A4-normalized or preview-capped: image export owns its A4 policy;
        cards/printing need true physical size. Only 'preview' and 'thumbnail'
        use their separate LRUs; all other nonempty purposes render uncached.
        max_pixels rejects oversized renders *before* allocating a bitmap,
        including cache hits. Native rendering itself cannot be interrupted.
        """
        with PDFIUM_LOCK:
            if self._document is None:
                raise ValueError("RenderSession is closed")
            if not isinstance(page_index, int) or not 0 <= page_index < self.info.page_count:
                raise IndexError(f"Page index out of range: {page_index}")
            if rotation not in (0, 90, 180, 270):
                raise ValueError("Rotation must be 0, 90, 180 or 270 degrees")
            if not isinstance(purpose, str) or not purpose:
                raise ValueError("Render purpose must be a nonempty string")
            width, height = self.info.page_sizes[page_index]
            scale = preview_scale(width, height) if scale is None else float(scale)
            if not math.isfinite(scale) or scale <= 0:
                raise ValueError("Render scale must be finite and positive")
            scaled_width, scaled_height = width * scale, height * scale
            if not math.isfinite(scaled_width) or not math.isfinite(scaled_height):
                raise RenderLimitError("Requested render dimensions are too large")
            pixel_width, pixel_height = math.ceil(scaled_width), math.ceil(scaled_height)
            if max_pixels is not None:
                if not isinstance(max_pixels, int) or max_pixels < 1:
                    raise ValueError("max_pixels must be a positive integer")
                if pixel_width * pixel_height > max_pixels:
                    raise RenderLimitError(
                        f"Page {page_index + 1} would render {pixel_width} x {pixel_height} "
                        f"pixels ({pixel_width * pixel_height:,}); limit is {max_pixels:,}"
                    )
            key = (self._revision, page_index, scale, rotation, purpose)
            cache = (_preview_cache if purpose == "preview" else
                     _thumbnail_cache if purpose == "thumbnail" else None)
            if cache is not None:
                cached = cache.get(key)
                if cached is not None:
                    return cached
            page = self._document[page_index]
            bitmaps = []

            def make_bitmap(*args, **kwargs):
                # Retain even a bitmap whose render raises before returning it.
                # Its native finalizer must never escape the serialized lane.
                bitmap = _pdfium.PdfBitmap.new_native(*args, **kwargs)
                bitmaps.append(bitmap)
                return bitmap

            try:
                bitmap = page.render(scale=scale, rotation=rotation, bitmap_maker=make_bitmap)
                # Default PDFium BGR -> PIL RGB performs an owned conversion,
                # unlike RGBA/RGBX/L views which share the native allocation.
                image = bitmap.to_pil()
                if image.mode != "RGB":
                    owned = image.copy()
                    image.close()
                    image = owned
            finally:
                try:
                    for bitmap in bitmaps:
                        bitmap.close()
                finally:
                    page.close()
            if cache is not None and cache.put(key, image):
                return image.copy()
            return image

    def close(self) -> None:
        with PDFIUM_LOCK:
            if self._document is not None:
                self._finalizer()
                self._document = None

    def __enter__(self) -> RenderSession:
        with PDFIUM_LOCK:
            if self._document is None:
                raise ValueError("RenderSession is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
