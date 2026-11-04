"""Page edits reference stable original-page IDs and immutable source bytes."""
from io import BytesIO
from pathlib import Path
from .jobs import CancelToken, Progress, ProgressSink


class SourceChangedError(RuntimeError):
    """The source changed after opening; no output was committed."""


class PageEditSession:
    def __init__(self, path_or_bytes: Path | str | bytes):
        from pypdf import PdfReader
        self.path = None if isinstance(path_or_bytes, bytes) else Path(path_or_bytes)
        self.source_bytes = path_or_bytes if isinstance(path_or_bytes, bytes) else self.path.read_bytes()
        reader = PdfReader(BytesIO(self.source_bytes))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("Password-protected PDFs are not supported")
        self.page_count = len(reader.pages)
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

    def _check_source(self) -> None:
        if self.path is None:
            return
        try:
            unchanged = self.path.read_bytes() == self.source_bytes
        except OSError as error:
            raise SourceChangedError("Source document is no longer available") from error
        if not unchanged:
            raise SourceChangedError("Source document changed after opening")

    def write(self, output: Path, pages, *, cancel: CancelToken | None = None,
              progress: ProgressSink | None = None) -> Path:
        from pypdf import PdfReader, PdfWriter
        from .outputs import atomic_write
        output, ids, turns = Path(output), tuple(pages), dict(self.turns)
        if not ids:
            raise ValueError("No pages left to save")
        if any(page not in self.order for page in ids):
            raise ValueError("Page ID is not in the working document")
        if self.path is not None and output.resolve() == self.path.resolve():
            raise ValueError("Output would overwrite the original document")
        self._check_source()

        def encode(staged):
            reader = PdfReader(BytesIO(self.source_bytes))
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("Password-protected PDFs are not supported")
            with PdfWriter() as writer:
                for index, page in enumerate(ids):
                    if cancel:
                        cancel.raise_if_cancelled()
                    writer.add_page(reader.pages[page]).rotate(90 * turns.get(page, 0))
                    if progress:
                        progress(Progress(index + 1, len(ids), "pages", self.path))
                if cancel:
                    cancel.raise_if_cancelled()
                writer.write(staged)

        atomic_write(output, encode, cancel=cancel, before_commit=self._check_source)
        return output

    def save(self, output: Path, *, cancel=None, progress=None) -> Path:
        return self.write(output, list(self.order), cancel=cancel, progress=progress)

    def extract(self, output: Path, *, cancel=None, progress=None) -> Path:
        return self.write(output, self.targets(), cancel=cancel, progress=progress)
