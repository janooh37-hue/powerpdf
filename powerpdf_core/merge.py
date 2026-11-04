"""Ordered PDF merging with destination-local atomic output."""
from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from . import files, outputs
from .jobs import CancelToken, Progress, ProgressSink


def merge_pdfs(pdfs: list[Path], output: Path | None = None,
               log: Callable[[str], None] | None = None, *,
               cancel: CancelToken | None = None, progress: ProgressSink | None = None) -> int:
    output = files.validate_output(output or Path("merged.pdf"), pdfs, suffix=None)
    snapshots: list[tuple[Path, bytes]] = []
    with outputs.destination_lock(output), PdfWriter() as writer:
        for index, pdf in enumerate(pdfs):
            if cancel is not None:
                cancel.raise_if_cancelled()
            snapshot = pdf.read_bytes()
            snapshots.append((pdf, snapshot))
            reader = PdfReader(io.BytesIO(snapshot))
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError(f"{pdf}: password-protected PDF")
            writer.append(reader)
            message = f"+ {pdf} ({len(reader.pages)} pages)"
            if log is not None:
                log(message)
            if progress is not None:
                progress(Progress(index + 1, len(pdfs), message, pdf))

        def unchanged() -> None:
            for path, data in snapshots:
                if path.read_bytes() != data:
                    raise ValueError(f"Source changed during merge: {path}")

        def encode(staged: Path) -> None:
            with staged.open("wb") as stream:
                writer.write(stream)

        outputs.atomic_write(output, encode, cancel=cancel, before_commit=unchanged)
        return len(writer.pages)
