#!/usr/bin/env python3
"""Save every page of one or more PDFs as a JPG or PNG image."""

from __future__ import annotations

import argparse
import sys

from powerpdf_core import files, images



def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pdfImage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Save every page of each PDF as an image. A one-page PDF becomes <name>_image.jpg\n"
            "next to it; a longer one becomes <name>_images\\<name>_page_001.jpg, ... Earlier\n"
            "images of the same format are replaced; originals are never changed. Pages bigger\n"
            "than A4 (phone scans) are rendered as A4. With no path, converts every PDF in the\n"
            "current folder."
        ),
        epilog=(
            "formats:\n"
            + "".join(f"  {name:<5} {text}\n" for name, (_, _, text) in images.FORMATS.items())
            + "\nexamples:\n"
            "  pdfImage                       every PDF here -> JPGs at 150 dpi\n"
            "  pdfImage license.pdf           license.pdf -> license_image.jpg (one page)\n"
            "  pdfImage report.pdf -f png     report_images\\report_page_001.png, ...\n"
            "  pdfImage .\\scans -r 300        every PDF in .\\scans at print quality"
        ),
    )
    parser.add_argument("paths", nargs="*", help="PDF files and/or folders (default: current folder)")
    parser.add_argument("-f", "--format", choices=images.FORMATS, default="jpg", help="jpg or png (default: jpg)")
    parser.add_argument("-r", "--dpi", type=int, default=images.DPI,
                        help=f"resolution, {images.DPI_RANGE[0]}-{images.DPI_RANGE[1]} (default: {images.DPI}; 300 for printing)")
    args = parser.parse_args()

    if not images.DPI_RANGE[0] <= args.dpi <= images.DPI_RANGE[1]:
        parser.error(f"--dpi must be between {images.DPI_RANGE[0]} and {images.DPI_RANGE[1]}")
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
            outputs = images.to_images(pdf, args.format, args.dpi)
            size = sum(o.stat().st_size for o in outputs)
            where = outputs[0] if len(outputs) == 1 else outputs[0].parent
            print(f"{pdf.name}: {len(outputs)} page(s), {size / 1024:,.0f} KB -> {where}")
        except Exception as error:
            failures += 1
            print(f"{pdf}: {error}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
