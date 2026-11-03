"""The installer must not accidentally use a machine's existing OCR/GS installation."""
import os
import sys

import pytest

import pdf_runtime


@pytest.mark.parametrize("tool", ["ghostscript", "tesseract", "fzf"])
def test_runtime_resolution_order(tool, tmp_path, monkeypatch):
    internal, installed = tmp_path / "_internal", tmp_path / "installed"
    relative, names = pdf_runtime.TOOLS[tool]
    shipped = internal / "vendor" / relative
    beside_exe = installed / "vendor" / relative
    for path in (shipped, beside_exe):
        path.parent.mkdir(parents=True)
        path.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(sys, "executable", str(installed / "powerpdf.exe"))
    monkeypatch.setenv("TESSDATA_PREFIX", "not-the-bundle")
    monkeypatch.setenv("GS_LIB", "not-the-bundle")
    monkeypatch.setattr(pdf_runtime.shutil, "which", lambda name: f"/on-path/{name}")
    assert pdf_runtime.resolve_tool(tool) == str(shipped)
    if tool == "tesseract":
        assert os.environ["TESSDATA_PREFIX"] == str(shipped.parent / "tessdata")
    if tool == "ghostscript":
        assert str(shipped.parent.parent / "Resource/Init") in os.environ["GS_LIB"]
    shipped.unlink()
    assert pdf_runtime.resolve_tool(tool) == str(beside_exe)
    beside_exe.unlink()
    assert pdf_runtime.resolve_tool(tool) == f"/on-path/{names[0]}"
    monkeypatch.setattr(pdf_runtime.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    known = {
        "ghostscript": tmp_path / "Program Files/gs/gs10.06.0/bin/gswin64c.exe",
        "tesseract": tmp_path / "Program Files/Tesseract-OCR/tesseract.exe",
        "fzf": tmp_path / "Local/Microsoft/WinGet/Links/fzf.exe",
    }[tool]
    known.parent.mkdir(parents=True)
    known.touch()
    assert pdf_runtime.resolve_tool(tool) == str(known)
