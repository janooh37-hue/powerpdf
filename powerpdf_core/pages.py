"""Page edits reference stable original-page IDs and immutable source bytes."""
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from .jobs import CancelToken, Progress, ProgressSink
from .files import same_path, validate_output


def _revision(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _check_source(path, source_bytes, revision):
    if path is None:
        return
    try:
        unchanged = _revision(path) == revision and path.read_bytes() == source_bytes and _revision(path) == revision
    except OSError as error:
        raise SourceChangedError("Source document is no longer available") from error
    if not unchanged:
        raise SourceChangedError("Source document changed after opening")


@dataclass(frozen=True)
class PageExportSnapshot:
    """One immutable edit revision; source pixels/bytes are shared, never copied."""

    path: Path | None
    source_bytes: bytes
    source_revision: tuple | None
    pages: tuple[int, ...]
    turns: tuple[int, ...]

    @property
    def state(self):
        return tuple(zip(self.pages, self.turns))

    def write(self, output, *, cancel=None, progress=None, before_commit=None):
        from pypdf import PdfReader, PdfWriter
        from .outputs import atomic_write
        if self.path is not None and same_path(Path(output), self.path):
            raise ValueError("Output would overwrite the original document")
        output = validate_output(output, (self.path,) if self.path is not None else ())
        destination_revision = _revision(output) if output.exists() else None
        if not self.pages:
            raise ValueError("No pages left to save")
        self.check_source()

        def encode(staged):
            reader = PdfReader(BytesIO(self.source_bytes))
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("Password-protected PDFs are not supported")
            with PdfWriter() as writer:
                for index, (page, turns) in enumerate(zip(self.pages, self.turns)):
                    if cancel:
                        cancel.raise_if_cancelled()
                    writer.add_page(reader.pages[page]).rotate(90 * turns)
                    if progress:
                        progress(Progress(index + 1, len(self.pages), "pages", self.path))
                if cancel:
                    cancel.raise_if_cancelled()
                writer.write(staged)

        def check_commit():
            self.check_source()
            validate_output(output, (self.path,) if self.path is not None else ())
            if (_revision(output) if output.exists() else None) != destination_revision:
                raise ValueError("Destination changed while saving; review again")
            if before_commit is not None:
                before_commit()

        atomic_write(output, encode, cancel=cancel, before_commit=check_commit)
        return output

    def check_source(self):
        _check_source(self.path, self.source_bytes, self.source_revision)


class SourceChangedError(RuntimeError):
    """The source changed after opening; no output was committed."""


class PageEditSession:
    """Stable source IDs with immutable native rotations in clockwise degrees.

    source_rotations is read once from the same source snapshot as the pages;
    turns records only additional edits, never the source PDF's /Rotate.
    """

    def __init__(self, path_or_bytes: Path | str | bytes):
        from pypdf import PdfReader
        self.path = None if isinstance(path_or_bytes, bytes) else Path(path_or_bytes)
        self.source_revision = _revision(self.path) if self.path is not None else None
        self.source_bytes = path_or_bytes if isinstance(path_or_bytes, bytes) else self.path.read_bytes()
        self._check_source()
        reader = PdfReader(BytesIO(self.source_bytes))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("Password-protected PDFs are not supported")
        self.page_count = len(reader.pages)
        self.source_rotations = tuple(page.rotation % 360 for page in reader.pages)
        self.order = list(range(self.page_count))
        self.turns: dict[int, int] = {}
        self.picked: set[int] = set()
        self.cursor = 0
        self._undo = []
        self._redo = []

    @property
    def edited(self) -> bool:
        return self.order != list(range(self.page_count)) or any(self.turns.get(page, 0) % 4 for page in self.order)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def targets(self) -> list[int]:
        picked = [page for page in self.order if page in self.picked]
        return picked or self.order[self.cursor:self.cursor + 1]

    def pick(self) -> None:
        if self.order:
            self.picked.symmetric_difference_update([self.order[self.cursor]])

    def _state(self):
        return (list(self.order), dict(self.turns), set(self.picked), self.cursor)

    def _restore(self, state):
        order, turns, picked, cursor = state
        self.order, self.turns, self.picked, self.cursor = list(order), dict(turns), set(picked), cursor

    def _record(self, before):
        if self._state() != before:
            self._undo.append(before)
            self._redo.clear()

    def _targets(self, pages):
        ids = set(self.targets() if pages is None else pages)
        if not ids.issubset(self.order):
            raise ValueError("Page ID is not in the working document")
        return [page for page in self.order if page in ids]

    def rotate(self, quarter_turns: int = 1, pages=None) -> None:
        before = self._state()
        for page in self._targets(pages):
            turns = (self.turns.get(page, 0) + quarter_turns) % 4
            if turns:
                self.turns[page] = turns
            else:
                self.turns.pop(page, None)
        self._record(before)

    def delete(self, pages=None) -> None:
        gone = set(self._targets(pages))
        if not gone:
            return
        before = self._state()
        here = self.order[self.cursor:self.cursor + 1]
        self.order = [page for page in self.order if page not in gone]
        self.picked.difference_update(gone)
        self.cursor = self.order.index(here[0]) if here and here[0] in self.order else max(0, min(self.cursor, len(self.order) - 1))
        self._record(before)

    def reorder(self, step: int, pages=None) -> None:
        if step not in (-1, 1):
            raise ValueError("Reorder step must be -1 or 1")
        before = self._state()
        targets = set(self._targets(pages))
        here = self.order[self.cursor:self.cursor + 1]
        indices = range(len(self.order)) if step < 0 else reversed(range(len(self.order)))
        for index in indices:
            other = index + step
            if self.order[index] in targets and 0 <= other < len(self.order) and self.order[other] not in targets:
                self.order[index], self.order[other] = self.order[other], self.order[index]
        if here:
            self.cursor = self.order.index(here[0])
        self._record(before)

    def move_before(self, before_id: int | None, pages=None) -> None:
        """Move stable page IDs before an ID (None appends), as one undo item."""
        ids = self._targets(pages)
        if before_id is not None and before_id not in self.order:
            raise ValueError("Insertion page ID is not in the working document")
        if not ids or before_id in ids:
            return
        before = self._state()
        here = self.order[self.cursor:self.cursor + 1]
        moving = set(ids)
        remaining = [page for page in self.order if page not in moving]
        index = len(remaining) if before_id is None else remaining.index(before_id)
        self.order = remaining[:index] + ids + remaining[index:]
        if here:
            self.cursor = self.order.index(here[0])
        self._record(before)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._state())
        self._restore(self._undo.pop())
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._state())
        self._restore(self._redo.pop())
        return True

    @property
    def state(self):
        """Output-relevant state, excluding transient cursor and selection."""
        return tuple((page, self.turns.get(page, 0) % 4) for page in self.order)

    def snapshot(self, pages=None) -> PageExportSnapshot:
        ids = tuple(self.order if pages is None else pages)
        if len(ids) != len(set(ids)) or any(page not in self.order for page in ids):
            raise ValueError("Page IDs must be unique members of the working document")
        return PageExportSnapshot(self.path, self.source_bytes, self.source_revision, ids,
                                  tuple(self.turns.get(page, 0) % 4 for page in ids))

    def _check_source(self) -> None:
        _check_source(self.path, self.source_bytes, self.source_revision)

    def write(self, output: Path, pages, *, cancel: CancelToken | None = None,
              progress: ProgressSink | None = None) -> Path:
        return self.snapshot(pages).write(output, cancel=cancel, progress=progress)

    def save(self, output: Path, *, cancel=None, progress=None) -> Path:
        return self.write(output, list(self.order), cancel=cancel, progress=progress)

    def extract(self, output: Path, *, cancel=None, progress=None) -> Path:
        return self.write(output, self.targets(), cancel=cancel, progress=progress)
