#!/usr/bin/env python3
"""Split every page of one or more PDFs into a separate PDF."""

from __future__ import annotations

import argparse
import sys

from powerpdf_core import files, split




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
        pdfs = files.find_pdfs(args.paths)
    except ValueError as error:
        parser.error(str(error))

    if not pdfs:
        print("No PDF files found.", file=sys.stderr)
        return 1

    failures = 0
    for pdf in pdfs:
        try:
            page_count, output_dir = split.split_pdf(pdf)
            print(f"{pdf.name}: {page_count} page(s) -> {output_dir}")
        except Exception as error:
            failures += 1
            print(f"{pdf}: {error}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
