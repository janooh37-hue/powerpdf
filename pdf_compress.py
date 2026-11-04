#!/usr/bin/env python3
"""Shrink PDFs for upload/sharing via Ghostscript, keeping them readable."""

from __future__ import annotations

import argparse
import sys

from powerpdf_core import compress, files
from pdf_runtime import resolve_tool



def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pdfCompress",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            f"Make small, readable copies of PDFs for uploading or sending. Each <name>.pdf\n"
            f"is saved as <name>{compress.SUFFIX}.pdf next to it (replaced if it exists); originals are\n"
            f"never changed. If it can't get smaller, the original's content is copied with metadata removed.\n"
            f"With no path, compresses every PDF in the current folder."
        ),
        epilog=(
            "quality levels:\n"
            + "".join(f"  {name:<9} {text}\n" for name, (_, text) in compress.QUALITY.items())
            + "\nexamples:\n"
            f"  pdfCompress                        every PDF here -> <name>{compress.SUFFIX}.pdf\n"
            f"  pdfCompress license.pdf -c         only the license card, white page cut away\n"
            f"  pdfCompress report.pdf -q screen   smallest file\n"
            f"  pdfCompress .\\scans -q printer     every PDF in .\\scans, print quality"
        ),
    )
    parser.add_argument("paths", nargs="*", help="PDF files and/or folders (default: current folder)")
    parser.add_argument("-q", "--quality", choices=compress.QUALITY, default="ebook",
                        help="image quality vs size: screen, ebook, printer (default: ebook)")
    parser.add_argument("-c", "--crop", action="store_true",
                        help="cut away the white page around the content, e.g. a license or ID "
                             "card scanned on A4; front and back on one page stay together. "
                             "Pages become images, so text is no longer selectable")
    args = parser.parse_args()

    gs = resolve_tool("ghostscript")
    if not gs:
        parser.error("Ghostscript not found; use the PowerPDF installer or install Ghostscript.")

    try:
        # skip earlier outputs so re-runs don't make name_small_small.pdf
        pdfs = [p for p in files.find_pdfs(args.paths) if not p.stem.endswith(compress.SUFFIX)]
    except ValueError as error:
        parser.error(str(error))

    if not pdfs:
        print("No PDF files found.", file=sys.stderr)
        return 1

    failures = 0
    for pdf in pdfs:
        try:
            output = compress.compress(gs, pdf, args.quality, args.crop)
            before, after = pdf.stat().st_size, output.stat().st_size
            change = (f"{(before - after) * 100 // before}% smaller" if after <= before
                      else f"{(after - before) * 100 // before}% larger")
            print(f"{pdf.name}: {before / 1024:,.0f} KB -> {after / 1024:,.0f} KB ({change}) -> {output.name}")
        except Exception as error:
            failures += 1
            print(f"{pdf}: {error}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
