"""Split PDFs without glob interpretation or deletion before successful encoding."""
from __future__ import annotations

import io
import shutil
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from . import files, outputs
from .jobs import CancelToken, Progress, ProgressSink


def split_pdf(source: Path, output: Path | None = None, *, cancel: CancelToken | None = None,
              progress: ProgressSink | None = None) -> tuple[int, Path]:
    source = Path(source)
    folder = Path(output) if output is not None else source.with_name(f"{source.stem}_pages")
    with outputs.destination_lock(folder):
        if cancel is not None:
            cancel.raise_if_cancelled()
        snapshot = source.read_bytes()
        reader = PdfReader(io.BytesIO(snapshot))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("password-protected PDF")
        count = len(reader.pages)
        folder.mkdir(exist_ok=True)
        staging = outputs.staging_dir(folder)
        try:
            width = max(3, len(str(count)))
            pairs = []
            for index, page in enumerate(reader.pages, start=1):
                if cancel is not None:
                    cancel.raise_if_cancelled()
                name = f"{source.stem}_page_{index:0{width}d}.pdf"
                destination = files.validate_output(folder / name, [source])
                with PdfWriter() as writer:
                    writer.add_page(page)
                    with (staging / name).open("wb") as stream:
                        writer.write(stream)
                pairs.append((staging / name, destination))
                if progress is not None:
                    progress(Progress(index, count, name, source))

            def unchanged() -> None:
                if source.read_bytes() != snapshot:
                    raise ValueError(f"Source changed during split: {source}")

            outputs.commit_files(pairs, cancel=cancel,
                                 remove=files.numbered_outputs(folder, source.stem, ".pdf"),
                                 before_commit=unchanged)
            return count, folder
        finally:
            shutil.rmtree(staging, ignore_errors=True)
