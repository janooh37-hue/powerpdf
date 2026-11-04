#!/usr/bin/env python3
"""Merge PDFs into one file, in the order given."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from powerpdf_core import files, merge




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
        pdfs = files.expand(args.paths, output)
    except ValueError as error:
        parser.error(str(error))

    if output.exists() and not args.force:
        parser.error(f"{output} already exists (use -f to overwrite)")

    if len(pdfs) < 2:
        print("Need at least 2 PDFs to merge.", file=sys.stderr)
        return 1

    try:
        pages = merge.merge_pdfs(pdfs, output, log=print)
    except Exception as error:
        print(error, file=sys.stderr)
        return 1
    print(f"-> {output} ({pages} pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
