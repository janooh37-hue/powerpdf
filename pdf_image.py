#!/usr/bin/env python3
"""Save every page of one or more PDFs as a JPG or PNG image."""

from __future__ import annotations

import argparse
import contextlib
import shutil
import sys
from pathlib import Path

import pypdfium2 as pdfium

from pdf_compress import A4_LONG_SIDE
from pdf_split import find_pdfs, staging_dir

FORMATS = {  # extension: (Pillow format, save options, description)
    "jpg": ("JPEG", {"quality": 80, "optimize": True}, "small; best for scans and photos (default)"),
    "png": ("PNG", {}, "lossless; sharpest text, but scans come out several times larger"),
}
DPI = 150  # scans and small print stay readable, like pdfCompress ebook
DPI_RANGE = (50, 600)


def page_scale(width: float, height: float, dpi: int) -> float:
    """pdfium scale for a page of width x height points. Pages bigger than A4 render as if A4:
    phone scans (e.g. WhatsApp) claim ~26x37 inch pages, which would make huge images."""
    return dpi / 72 * min(1.0, A4_LONG_SIDE * 72 / max(width, height))


def to_images(source: Path, fmt: str, dpi: int = DPI, lock=contextlib.nullcontext()) -> list[Path]:
    """Render each page at dpi and save it as fmt (no EXIF or text metadata).
    One page: <stem>_image.<fmt> next to the PDF. More: <stem>_images/<stem>_page_001.<fmt>, ...; pages of
    that format left from an earlier run are replaced. lock guards pdfium (not thread-safe); encoding runs outside it.
    Opens from bytes: a path-opened pdfium document keeps a Windows file handle on the PDF."""
    kind, options, _ = FORMATS[fmt]
    staging = staging_dir(source.parent, f".{source.stem}_images_")
    try:
        with lock:
            pdf = pdfium.PdfDocument(source.read_bytes())
            count = len(pdf)
        try:
            if not count:
                raise ValueError("PDF has no pages")
            width = max(3, len(str(count)))
            names = ([f"{source.stem}_image.{fmt}"] if count == 1 else
                     [f"{source.stem}_page_{n:0{width}d}.{fmt}" for n in range(1, count + 1)])
            for index, name in enumerate(names):
                with lock:
                    page = pdf[index]
                    try:
                        image = page.render(scale=page_scale(*page.get_size(), dpi)).to_pil()  # BGR -> RGB copy
                    finally:
                        page.close()
                image.save(staging / name, kind, dpi=(dpi, dpi), **options)
        finally:
            with lock:
                pdf.close()

        if count == 1:
            output = source.with_name(names[0])
            (staging / names[0]).replace(output)
            return [output]
        folder = source.with_name(f"{source.stem}_images")
        folder.mkdir(exist_ok=True)
        for old in folder.iterdir():  # not glob: a [ or ] in the stem would be read as a pattern
            if old.name.startswith(f"{source.stem}_page_") and old.suffix.lower() == f".{fmt}":
                old.unlink()
        for name in names:
            (staging / name).replace(folder / name)
        return [folder / name for name in names]
    finally:
        shutil.rmtree(staging, ignore_errors=True)


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
            + "".join(f"  {name:<5} {text}\n" for name, (_, _, text) in FORMATS.items())
            + "\nexamples:\n"
            "  pdfImage                       every PDF here -> JPGs at 150 dpi\n"
            "  pdfImage license.pdf           license.pdf -> license_image.jpg (one page)\n"
            "  pdfImage report.pdf -f png     report_images\\report_page_001.png, ...\n"
            "  pdfImage .\\scans -r 300        every PDF in .\\scans at print quality"
        ),
    )
    parser.add_argument("paths", nargs="*", help="PDF files and/or folders (default: current folder)")
    parser.add_argument("-f", "--format", choices=FORMATS, default="jpg", help="jpg or png (default: jpg)")
    parser.add_argument("-r", "--dpi", type=int, default=DPI,
                        help=f"resolution, {DPI_RANGE[0]}-{DPI_RANGE[1]} (default: {DPI}; 300 for printing)")
    args = parser.parse_args()

    if not DPI_RANGE[0] <= args.dpi <= DPI_RANGE[1]:
        parser.error(f"--dpi must be between {DPI_RANGE[0]} and {DPI_RANGE[1]}")
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
            outputs = to_images(pdf, args.format, args.dpi)
            size = sum(o.stat().st_size for o in outputs)
            where = outputs[0] if len(outputs) == 1 else outputs[0].parent
            print(f"{pdf.name}: {len(outputs)} page(s), {size / 1024:,.0f} KB -> {where}")
        except Exception as error:
            failures += 1
            print(f"{pdf}: {error}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
