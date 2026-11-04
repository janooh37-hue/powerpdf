"""Versioned local preferences; updates merge fields from the latest on-disk revision.

The old lower-case PowerPDF directory is imported once and never modified.
An explicit path opts out of discovery/migration unless legacy_path is supplied.
"""
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Mapping
import json
import os
import threading
import uuid

SCHEMA_VERSION = 1
_LOCK = threading.RLock()


def settings_path() -> Path:
    from platformdirs import user_config_path
    return user_config_path("PowerPDF", appauthor=False) / "settings.json"


def legacy_settings_path() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / ".config") / "powerpdf" / "settings.json"


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    if value.get("schema_version", 0) > SCHEMA_VERSION:
        raise ValueError("Settings were written by a newer PowerPDF version")
    return value


def _merge(current: dict, changes: Mapping) -> dict:
    for key, value in changes.items():
        if isinstance(value, Mapping) and isinstance(current.get(key), dict):
            _merge(current[key], value)
        else:
            current[key] = value
    return current


@contextmanager
def _locked(path: Path):
    # File locking protects independent TUI/GUI processes, not just threads.
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_name(path.name + ".lock").open("a+b") as lock:
            if os.name == "nt":
                import msvcrt
                lock.seek(0, 2)
                if not lock.tell():
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class SettingsStore:
    def __init__(self, path: Path | str | None = None, legacy_path: Path | str | None = None):
        self.path = Path(path) if path is not None else settings_path()
        self.legacy_path = Path(legacy_path) if legacy_path is not None else (legacy_settings_path() if path is None else None)

    def _load(self) -> dict:
        if not self.path.exists() and self.legacy_path is not None and self.legacy_path.exists():
            data = _read(self.legacy_path)
            data["schema_version"] = SCHEMA_VERSION
            self._write(data)
        data = _read(self.path)
        data.setdefault("schema_version", SCHEMA_VERSION)
        return data

    def load(self) -> dict:
        with _locked(self.path):
            return self._load()

    def _write(self, data: dict) -> None:
        staged = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with staged.open("x", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, self.path)
        finally:
            staged.unlink(missing_ok=True)

    def update(self, changes: Mapping | None = None, **fields) -> dict:
        with _locked(self.path):
            data = _merge(self._load(), changes or {})
            _merge(data, fields)
            data["schema_version"] = SCHEMA_VERSION
            self._write(data)
            return data


def load_settings(path: Path | str | None = None) -> dict:
    return SettingsStore(path).load()


def save_settings(path: Path | str | None = None, **changes) -> dict:
    return SettingsStore(path).update(changes)
