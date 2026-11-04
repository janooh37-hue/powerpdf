"""CI-only packaging probe. This is NOT the PowerPDF desktop application."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import pypdfium2 as pdfium
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont, QFontDatabase, QImage, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QVBoxLayout, QWidget

import pdf_runtime


def run_helper(tool: str, *arguments: str) -> str:
    executable = pdf_runtime.resolve_tool(tool)
    if not executable or "vendor" not in Path(executable).parts:
        raise RuntimeError(f"Missing bundled {tool}: {executable}")
    with pdf_runtime.helper_launch_scope():
        process = subprocess.Popen([executable, *arguments], env=pdf_runtime.helper_env(executable),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, process.args, stdout, stderr)
    return (stdout + stderr).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv[:1])
    families = []
    for filename in ("smoke-latin-font.ttf", "smoke-font.ttf"):
        font_path = next(root / filename for root in pdf_runtime.bundle_roots()
                         if (root / filename).is_file())
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id == -1:
            raise RuntimeError(f"Bundled font did not load: {filename}")
        families.extend(QFontDatabase.applicationFontFamilies(font_id))
    font = QFont(families[0], 18)
    font.setFamilies(families)
    app.setFont(font)
    window = QWidget()
    window.setWindowTitle("PowerPDF — CI packaging harness (not the app)")
    window.setStyleSheet("QWidget { background: #f6f5f0; color: #171d27; } QLineEdit { background: white; padding: 10px; border: 1px solid #bac2cb; }")
    layout = QVBoxLayout(window)
    layout.setContentsMargins(28, 24, 28, 24)
    layout.addWidget(QLabel("PowerPDF · Arabic / PDFium / bundled engines"))
    for name in ("رخصة المركبة.pdf", "شهادة 2026.pdf", "Ahmed عقد.pdf"):
        label = QLabel(name)
        label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(label)
        edit = QLineEdit(name)
        edit.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        layout.addWidget(edit)
    with tempfile.TemporaryDirectory(prefix="powerpdf-smoke-") as directory:
        work = Path(directory)
        # A real PDF with a vector rectangle; no external fixture or reportlab.
        document = pdfium.PdfDocument.new()
        page = document.new_page(width=320, height=120)
        rectangle = pdfium.raw.FPDFPageObj_CreateNewRect(12, 12, 296, 96)
        pdfium.raw.FPDFPageObj_SetFillColor(rectangle, 28, 106, 128, 255)
        pdfium.raw.FPDFPath_SetDrawMode(rectangle, pdfium.raw.FPDF_FILLMODE_WINDING, False)
        page.insert_obj(pdfium.PdfObject(rectangle))
        page.gen_content()
        page.close()
        source, compressed = work / "شهادة 2026.pdf", work / "compressed.pdf"
        document.save(str(source))
        document.close()
        run_helper("ghostscript", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", "-dPDFSETTINGS=/ebook",
                   f"-sOutputFile={compressed}", str(source))
        if compressed.stat().st_size < 100:
            raise RuntimeError("Ghostscript produced an invalid output")
        with closing(pdfium.PdfDocument(compressed.read_bytes())) as rendered:
            with closing(rendered[0]) as rendered_page:
                with closing(rendered_page.render(scale=1.5)) as bitmap:
                    image = bitmap.to_pil().convert("RGB")
                    owned = QImage(image.tobytes(), image.width, image.height, image.width * 3, QImage.Format.Format_RGB888).copy()
        preview = QLabel()
        preview.setPixmap(QPixmap.fromImage(owned))
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(preview)
        # Also exercise actual OCR loading/leptonica, not only --version.
        image.save(work / "ocr.png")
        run_helper("tesseract", str(work / "ocr.png"), "stdout", "-l", "eng", "--psm", "6")
        languages = run_helper("tesseract", "--list-langs")
        if "function not present" in languages:
            raise RuntimeError(f"Incomplete bundled Leptonica codec closure: {languages}")
        if not all(language in languages.splitlines() for language in ("eng", "ara", "osd")):
            raise RuntimeError(f"Missing OCR models: {languages}")
        evidence = {tool: {"path": pdf_runtime.resolve_tool(tool), "version": run_helper(tool, "--version")}
                    for tool in ("ghostscript", "tesseract")}
        evidence["languages"] = languages
        evidence["fonts"] = app.font().families()
        (args.output / "runtime.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    layout.addWidget(QLabel("PASS · PDFium rendered · Ghostscript compressed · OCR engines loaded"))
    window.resize(900, 760)
    window.show()
    status = 1

    def capture() -> None:
        nonlocal status
        if window.grab().save(str(args.output / "qt-smoke.png")):
            status = 0
        app.quit()

    QTimer.singleShot(1000, capture)
    app.exec()
    print(json.dumps(evidence, ensure_ascii=False), flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
