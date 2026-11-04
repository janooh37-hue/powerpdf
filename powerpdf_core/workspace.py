"""Toolkit-independent ordered queue, recent documents and card-label history."""
from pathlib import Path
from collections.abc import Iterable

RECENT_MAX = 30
LABEL_MAX = 12


def _paths(paths: Iterable[Path | str]) -> list[Path]:
    return list(dict.fromkeys(Path(path) for path in paths))


class Workspace:
    def __init__(self, queue=(), recent=(), card_labels=()):
        self.queue = _paths(queue)
        self.recent = _paths(path for path in recent if Path(path).is_file())[:RECENT_MAX]
        self.card_labels = list(dict.fromkeys(card_labels))[:LABEL_MAX]

    def targets(self, focused: Path | None = None) -> list[Path]:
        if self.queue:
            return list(self.queue)
        return [focused] if focused and focused.is_file() and focused.suffix.lower() == ".pdf" else []

    def add(self, paths: Iterable[Path | str]) -> int:
        added = 0
        known = set(self.queue)
        for path in paths:
            path = Path(path)
            if path.is_file() and path.suffix.lower() == ".pdf" and path not in known:
                self.queue.append(path)
                known.add(path)
                added += 1
        return added

    def toggle(self, path: Path) -> bool:
        path = Path(path)
        if path in self.queue:
            self.queue.remove(path)
            return False
        self.add([path])
        return path in self.queue

    def move(self, index: int | None, step: int) -> int | None:
        if index is not None and 0 <= index < len(self.queue) and 0 <= index + step < len(self.queue):
            self.queue[index], self.queue[index + step] = self.queue[index + step], self.queue[index]
            return index + step
        return index

    def drop(self, index: int | None) -> None:
        if index is not None and 0 <= index < len(self.queue):
            del self.queue[index]

    def clear(self) -> None:
        self.queue.clear()

    def add_folder(self, folder: Path) -> int:
        folder = Path(folder)
        if not folder.is_dir():
            folder = folder.parent
        return self.add(sorted(folder.iterdir(), key=lambda path: path.name.lower()))

    def remember(self, *paths: Path) -> None:
        pdfs = [Path(path) for path in paths if Path(path).suffix.lower() == ".pdf"]
        self.recent = _paths(pdfs + self.recent)[:RECENT_MAX]

    def forget(self, path: Path) -> None:
        self.recent.remove(Path(path))

    def moved(self, old: Path, new: Path | None) -> None:
        old, new = Path(old), Path(new) if new is not None else None
        def remap(path):
            if not path.is_relative_to(old):
                return path
            return None if new is None else new / path.relative_to(old)
        self.queue = _paths(p for path in self.queue if (p := remap(path)) is not None)
        self.recent = _paths(p for path in self.recent if (p := remap(path)) is not None)[:RECENT_MAX]

    def remember_label(self, label: str) -> None:
        if label:
            self.card_labels = [label] + [old for old in self.card_labels if old != label]
            del self.card_labels[LABEL_MAX:]
