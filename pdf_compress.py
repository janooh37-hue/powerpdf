#!/usr/bin/env python3
"""Shrink PDFs for upload/sharing via Ghostscript, keeping them readable."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from pypdf import PdfReader

from pdf_split import find_pdfs

QUALITY = {  # name: (dpi, description)
    "screen": (72, "smallest; 72 dpi, fine for full pages, small print (e.g. ID cards) unreadable"),
    "ebook": (150, "balanced; 150 dpi, scans and cards stay readable (default)"),
    "printer": (300, "largest; 300 dpi, keeps print quality"),
}
SUFFIX = "_small"
INK = 235  # gray level (0-255) below which a pixel is content, not scanner white
A4_LONG_SIDE = 842 / 72  # inches


def run_gs(gs: str, *args: str) -> None:
    result = subprocess.run(
        [gs, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", *args], capture_output=True, text=True
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "Ghostscript failed")


def extent(on: np.ndarray, dpi: int) -> tuple[int, int] | None:
    """First..last index of real content; specks and thin lines under 1/4 inch are ignored."""
    edges = np.flatnonzero(np.diff(np.concatenate(([0], on.astype(np.int8), [0]))))
    merged: list[list[int]] = []
    for start, end in zip(edges[::2], edges[1::2]):
        if merged and start - merged[-1][1] < dpi // 4:
            merged[-1][1] = end  # bridge small gaps, e.g. between text lines
        else:
            merged.append([start, end])
    kept = [run for run in merged if run[1] - run[0] >= dpi // 4]
    return (int(kept[0][0]), int(kept[-1][1])) if kept else None


def content_box(page: Image.Image, dpi: int) -> tuple[int, int, int, int] | None:
    ink = np.asarray(page.convert("L")) < INK
    rows = extent(ink.mean(axis=1) > 0.01, dpi)
    if not rows:
        return None
    cols = extent(ink[rows[0]:rows[1]].mean(axis=0) > 0.01, dpi)
    if not cols:
        return None
    pad = dpi // 20
    height, width = ink.shape
    return (max(cols[0] - pad, 0), max(rows[0] - pad, 0),
            min(cols[1] + pad, width), min(rows[1] + pad, height))


def crop_to_content(gs: str, source: Path, staging: Path, dpi: int) -> None:
    # phone scans (e.g. WhatsApp) claim huge page sizes; render those as if they were A4
    longest = max(max(page.mediabox.width, page.mediabox.height) for page in PdfReader(source).pages) / 72
    render_dpi = dpi * min(1.0, A4_LONG_SIDE / float(longest))
    pages = []
    with tempfile.TemporaryDirectory() as tmp:
        run_gs(gs, "-sDEVICE=png16m", f"-r{render_dpi:.2f}", f"-sOutputFile={Path(tmp) / 'p%04d.png'}", str(source))
        for png in sorted(Path(tmp).glob("p*.png")):
            with Image.open(png) as rendered:
                page = rendered.convert("RGB")
            box = content_box(page, dpi)
            pages.append(page.crop(box) if box else page)  # blank page: keep as is
    pages[0].save(staging, "PDF", save_all=True, append_images=pages[1:], resolution=dpi, quality=80)


def compress(gs: str, source: Path, quality: str, crop: bool) -> Path:
    output = source.with_name(f"{source.stem}{SUFFIX}.pdf")
    staging = source.with_name(f".{source.stem}{SUFFIX}.tmp.pdf")
    dpi = QUALITY[quality][0]
    try:
        if crop:
            crop_to_content(gs, source, staging, dpi)
        else:
            run_gs(gs, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.5",
                   f"-dPDFSETTINGS=/{quality}", f"-sOutputFile={staging}", str(source))
            # already-optimized PDFs can come out bigger; then just ship the original
            if staging.stat().st_size >= source.stat().st_size:
                shutil.copyfile(source, staging)
        staging.replace(output)
        return output
    finally:
        staging.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pdfCompress",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            f"Make small, readable copies of PDFs for uploading or sending. Each <name>.pdf\n"
            f"is saved as <name>{SUFFIX}.pdf next to it (replaced if it exists); originals are\n"
            f"never changed. If a PDF can't get smaller, the copy is the original as-is.\n"
            f"With no path, compresses every PDF in the current folder."
        ),
        epilog=(
            "quality levels:\n"
            + "".join(f"  {name:<9} {text}\n" for name, (_, text) in QUALITY.items())
            + "\nexamples:\n"
            f"  pdfCompress                        every PDF here -> <name>{SUFFIX}.pdf\n"
            f"  pdfCompress license.pdf -c         only the license card, white page cut away\n"
            f"  pdfCompress report.pdf -q screen   smallest file\n"
            f"  pdfCompress .\\scans -q printer     every PDF in .\\scans, print quality"
        ),
    )
    parser.add_argument("paths", nargs="*", help="PDF files and/or folders (default: current folder)")
    parser.add_argument("-q", "--quality", choices=QUALITY, default="ebook",
                        help="image quality vs size: screen, ebook, printer (default: ebook)")
    parser.add_argument("-c", "--crop", action="store_true",
                        help="cut away the white page around the content, e.g. a license or ID "
                             "card scanned on A4; front and back on one page stay together. "
                             "Pages become images, so text is no longer selectable")
    args = parser.parse_args()

    gs = shutil.which("gswin64c") or shutil.which("gs")
    if not gs:
        parser.error("Ghostscript not found (gswin64c); install it with: scoop install ghostscript")

    try:
        # skip earlier outputs so re-runs don't make name_small_small.pdf
        pdfs = [p for p in find_pdfs(args.paths) if not p.stem.endswith(SUFFIX)]
    except ValueError as error:
        parser.error(str(error))

    if not pdfs:
        print("No PDF files found.", file=sys.stderr)
        return 1

    failures = 0
    for pdf in pdfs:
        try:
            output = compress(gs, pdf, args.quality, args.crop)
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
