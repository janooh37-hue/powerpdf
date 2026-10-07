#!/usr/bin/env python3
"""Merge PDFs into one file, in the order given."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pypdf import PdfReader, PdfWriter


def expand(paths: list[str], output: Path) -> list[Path]:
    pdfs: list[Path] = []
    for raw_path in paths or ["."]:
        path = Path(raw_path).expanduser()
        if path.is_dir():
            pdfs += sorted(
                (c for c in path.iterdir() if c.is_file() and c.suffix.lower() == ".pdf"),
                key=lambda item: item.name.lower(),
            )
        elif path.is_file() and path.suffix.lower() == ".pdf":
            pdfs.append(path)
        else:
            raise ValueError(f"Not a PDF file or directory: {path}")
    # never merge the output into itself (e.g. re-running in the same folder)
    return [p for p in pdfs if p.resolve() != output]


def merge_pdfs(pdfs: list[Path], output: Path, log=print) -> int:
    """Append pdfs in order and write them to output; returns the page count.

    Raises ValueError on a password-protected PDF (nothing is written)."""
    writer = PdfWriter()
    for pdf in pdfs:
        reader = PdfReader(pdf)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError(f"{pdf}: password-protected PDF")
        writer.append(reader)
        log(f"+ {pdf} ({len(reader.pages)} pages)")

    with output.open("wb") as output_file:
        writer.write(output_file)
    return len(writer.pages)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pdfMerge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Merge PDFs into one file, in the order given. Folders add their PDFs sorted by name.\n"
            "With no path, merges every PDF in the current folder into merged.pdf."
        ),
        epilog=(
            "examples:\n"
            "  pdfMerge                         merge every PDF in this folder -> merged.pdf\n"
            "  pdfMerge c.pdf a.pdf -o out.pdf  c.pdf then a.pdf -> out.pdf\n"
            "  pdfMerge .\\scans -o scans.pdf    every PDF in .\\scans -> scans.pdf\n"
            "  pdfMerge -f                      same as plain, but overwrite an existing merged.pdf"
        ),
    )
    parser.add_argument("paths", nargs="*", help="PDF files and/or folders (default: current folder)")
    parser.add_argument("-o", "--output", default="merged.pdf", help="output file (default: merged.pdf)")
    parser.add_argument("-f", "--force", action="store_true", help="overwrite the output file if it already exists")
    args = parser.parse_args()

    output = Path(args.output).expanduser().resolve()
    try:
        pdfs = expand(args.paths, output)
    except ValueError as error:
        parser.error(str(error))

    if output.exists() and not args.force:
        parser.error(f"{output} already exists (use -f to overwrite)")

    if len(pdfs) < 2:
        print("Need at least 2 PDFs to merge.", file=sys.stderr)
        return 1

    try:
        pages = merge_pdfs(pdfs, output)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"-> {output} ({pages} pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
