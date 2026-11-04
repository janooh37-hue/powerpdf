"""Toolkit-independent discovery, destination and rename rules."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

BAD_NAME_CHARS = frozenset('\\/:*?"<>|')


def is_pdf(path: Path) -> bool:
    return path.suffix.lower() == ".pdf" and path.is_file()


def find_pdfs(paths: Iterable[str | Path]) -> list[Path]:
    """Discover nonrecursively, deduplicate resolved files and sort globally."""
    pdfs: dict[Path, Path] = {}
    for raw in list(paths) or ["."]:
        path = Path(raw).expanduser()
        if path.is_dir():
            candidates = (child for child in path.iterdir() if is_pdf(child))
        elif is_pdf(path):
            candidates = [path]
        else:
            raise ValueError(f"Not a PDF file or directory: {path}")
        for candidate in candidates:
            resolved = candidate.resolve()
            pdfs[resolved] = resolved
    return sorted(pdfs.values(), key=lambda item: str(item).lower())


def expand(paths: Iterable[str | Path], output: Path) -> list[Path]:
    """Preserve explicit merge order (including repeats); sort each folder only."""
    pdfs: list[Path] = []
    for raw in list(paths) or ["."]:
        path = Path(raw).expanduser()
        if path.is_dir():
            pdfs.extend(sorted((child for child in path.iterdir() if is_pdf(child)),
                               key=lambda item: item.name.lower()))
        elif is_pdf(path):
            pdfs.append(path)
        else:
            raise ValueError(f"Not a PDF file or directory: {path}")
    return [path for path in pdfs if not same_path(path, output)]


def same_path(first: Path, second: Path) -> bool:
    if first.resolve() == second.resolve():
        return True
    return first.exists() and second.exists() and first.samefile(second)


def validate_output(output: str | Path, inputs: Iterable[Path] = (), *,
                    suffix: str | None = ".pdf") -> Path:
    path = Path(output).expanduser().resolve()
    if suffix is not None and path.suffix.lower() != suffix.lower():
        raise ValueError(f"Output must end in {suffix}")
    if any(same_path(path, source) for source in inputs):
        raise ValueError("Output is one of the input files")
    if path.exists() and not path.is_file():
        raise ValueError("Output is not a file")
    return path


def output_checks(inputs: list[Path]) -> tuple[Callable[[str], str | None], Callable[[str], str | None]]:
    def check(value: str) -> str | None:
        try:
            validate_output(value, inputs)
        except (ValueError, OSError) as error:
            return str(error)
        return None

    def exists(value: str) -> str | None:
        return "File exists. Enter again to overwrite" if Path(value).expanduser().exists() else None

    return check, exists


def output_error(output: Path, inputs: list[Path]) -> str | None:
    try:
        validate_output(output, inputs)
    except (ValueError, OSError) as error:
        return str(error)
    return None


def rename_error(path: Path, name: str) -> str | None:
    if not name or name in (".", "..") or BAD_NAME_CHARS.intersection(name) or any(ord(ch) < 32 for ch in name):
        return 'Not a valid name (no \\ / : * ? " < > |)'
    if name.lower() != path.name.lower() and (path.parent / name).exists():
        return "That name is taken"
    return None


def remap_path(path: Path, old: Path, new: Path | None) -> Path | None:
    """Remap a renamed/deleted path and descendants; leave others unchanged."""
    if not path.is_relative_to(old):
        return path
    return None if new is None else new / path.relative_to(old)


def numbered_outputs(folder: Path, stem: str, suffix: str) -> list[Path]:
    """Only literal-stem numeric page files belong to a page-export operation."""
    prefix = f"{stem}_page_"
    result = []
    for path in folder.iterdir():
        if path.is_file() and path.name.startswith(prefix) and path.suffix.lower() == suffix.lower():
            number = path.name[len(prefix):-len(path.suffix)]
            if number.isascii() and number.isdigit():
                result.append(path)
    return result
