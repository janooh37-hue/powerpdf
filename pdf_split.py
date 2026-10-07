#!/usr/bin/env python3
"""Split every page of one or more PDFs into a separate PDF."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter


def find_pdfs(paths: list[str]) -> list[Path]:
    pdfs: dict[Path, Path] = {}
    for raw_path in paths or ["."]:
        path = Path(raw_path).expanduser()
        if path.is_dir():
            candidates = (
                child
                for child in path.iterdir()
                if child.is_file() and child.suffix.lower() == ".pdf"
            )
        elif path.is_file() and path.suffix.lower() == ".pdf":
            candidates = [path]
        else:
            raise ValueError(f"Not a PDF file or directory: {path}")

        for candidate in candidates:
            resolved = candidate.resolve()
            pdfs[resolved] = resolved

    return sorted(pdfs.values(), key=lambda item: str(item).lower())


def split_pdf(source: Path) -> tuple[int, Path]:
    output_dir = source.parent / f"{source.stem}_pages"
    staging_dir = Path(
        tempfile.mkdtemp(prefix=f".{source.stem}_pages_", dir=source.parent)
    )

    try:
        with source.open("rb") as source_file:
            reader = PdfReader(source_file)
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("password-protected PDF")

            page_count = len(reader.pages)
            width = max(3, len(str(page_count)))
            for page_number, page in enumerate(reader.pages, start=1):
                writer = PdfWriter()
                writer.add_page(page)
                output_name = f"{source.stem}_page_{page_number:0{width}d}.pdf"
                with (staging_dir / output_name).open("wb") as output_file:
                    writer.write(output_file)

        output_dir.mkdir(exist_ok=True)
        for old_page in output_dir.glob(f"{source.stem}_page_*.pdf"):
            old_page.unlink()
        for new_page in staging_dir.iterdir():
            new_page.replace(output_dir / new_page.name)
        return page_count, output_dir
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pdfSplit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Split each page of every PDF into its own file, saved in a <name>_pages folder\n"
            "next to the original. With no path, splits every PDF in the current folder."
        ),
        epilog=(
            "examples:\n"
            "  pdfSplit                  split every PDF in this folder\n"
            "  pdfSplit report.pdf       report.pdf -> report_pages\\report_page_001.pdf, ...\n"
            "  pdfSplit a.pdf .\\scans    split a.pdf and every PDF in .\\scans"
        ),
    )
    parser.add_argument("paths", nargs="*", help="PDF files and/or folders (default: current folder)")
    args = parser.parse_args()

    try:
        pdfs = find_pdfs(args.paths)
    except ValueError as error:
        parser.error(str(error))

    if not pdfs:
        print("No PDF files found.", file=sys.stderr)
        return 1

    failures = 0
    for pdf in pdfs:
        try:
            page_count, output_dir = split_pdf(pdf)
            print(f"{pdf.name}: {page_count} page(s) -> {output_dir}")
        except Exception as error:
            failures += 1
            print(f"{pdf}: {error}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
