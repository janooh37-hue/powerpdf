"""Destination-local staging and truthful, serialized output commits.

Each file replacement is atomic, but a group of replacements is not a transaction.
Locks serialize conflicting destinations across threads and local processes.
Staging uses plain mkdir so files inherit destination ACLs on Windows.
"""
from __future__ import annotations

import errno
import hashlib
import os
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from platformdirs import user_cache_path

if TYPE_CHECKING:
    from .jobs import CancelToken

_lock_guard = threading.Lock()
_locks: dict[str, tuple[threading.RLock, int]] = {}
_lock_depth = threading.local()


@contextmanager
def _process_lock(key: str) -> Iterator[None]:
    folder = user_cache_path("PowerPDF", appauthor=False) / "output-locks"
    folder.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(os.fsencode(key)).hexdigest() + ".lock"
    # Persistent sidecars avoid the split-lock race caused by unlinking a held file.
    with (folder / name).open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    time.sleep(0.05)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def destination_lock(destination: Path) -> Iterator[None]:
    key = os.path.normcase(str(Path(destination).resolve()))
    with _lock_guard:
        lock, users = _locks.get(key, (threading.RLock(), 0))
        _locks[key] = lock, users + 1
    try:
        with lock:
            depths = getattr(_lock_depth, "depths", None)
            if depths is None:
                depths = _lock_depth.depths = {}
            if key in depths:
                depths[key] += 1
                try:
                    yield
                finally:
                    depths[key] -= 1
            else:
                with _process_lock(key):
                    depths[key] = 1
                    try:
                        yield
                    finally:
                        del depths[key]
    finally:
        with _lock_guard:
            _, users = _locks[key]
            if users == 1:
                del _locks[key]
            else:
                _locks[key] = lock, users - 1


def staging_dir(parent: Path, prefix: str = ".powerpdf_") -> Path:
    """Create a unique plain-mkdir staging folder on the destination filesystem."""
    while True:
        folder = parent / f"{prefix}{secrets.token_hex(8)}"
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            continue


class OutputCommitError(RuntimeError):
    """A commit failed; committed and removed paths describe actual side effects."""

    def __init__(self, cause: Exception, committed: Iterable[Path] = (), removed: Iterable[Path] = ()):
        self.cause = cause
        self.committed = tuple(committed)
        self.removed = tuple(removed)
        detail = f" ({len(self.committed)} output(s) committed, {len(self.removed)} obsolete file(s) removed)"
        super().__init__(f"{cause}{detail}")


def commit_files(pairs: Iterable[tuple[Path, Path]], *, cancel: CancelToken | None = None,
                 remove: Iterable[Path] = (), before_commit: Callable[[], None] | None = None) -> list[Path]:
    """Replace staged files before deleting obsolete outputs; never hide partial commits."""
    pairs = [(Path(staged), Path(destination)) for staged, destination in pairs]
    destinations = [destination.resolve() for _, destination in pairs]
    if len(set(destinations)) != len(destinations):
        raise ValueError("Duplicate output destination")
    obsolete = [Path(path) for path in remove if Path(path).resolve() not in destinations]
    committed: list[Path] = []
    removed: list[Path] = []
    with ExitStack() as stack:
        for path in sorted(set(destinations + [path.resolve() for path in obsolete])):
            stack.enter_context(destination_lock(path))
        try:
            if cancel is not None:
                cancel.raise_if_cancelled()
            if before_commit is not None:
                before_commit()
            for staged, destination in pairs:
                if cancel is not None:
                    cancel.raise_if_cancelled()
                staged.replace(destination)
                committed.append(destination)
            for path in obsolete:
                if cancel is not None:
                    cancel.raise_if_cancelled()
                path.unlink()
                removed.append(path)
            if cancel is not None:
                cancel.raise_if_cancelled()
        except Exception as error:
            if committed or removed or not _is_cancelled(error):
                raise OutputCommitError(error, committed, removed) from error
            raise
    return committed


def _is_cancelled(error: Exception) -> bool:
    from .jobs import JobCancelled
    return isinstance(error, JobCancelled)


def atomic_write(destination: Path, writer: Callable[[Path], object], *,
                 cancel: CancelToken | None = None,
                 before_commit: Callable[[], None] | None = None) -> Path:
    """Encode to a unique destination-local file, then atomically replace one output."""
    destination = Path(destination)
    with destination_lock(destination):
        if cancel is not None:
            cancel.raise_if_cancelled()
        staging = staging_dir(destination.parent)
        try:
            staged = staging / destination.name
            writer(staged)
            commit_files([(staged, destination)], cancel=cancel, before_commit=before_commit)
            return destination
        finally:
            shutil.rmtree(staging, ignore_errors=True)
