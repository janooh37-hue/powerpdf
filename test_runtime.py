"""Bundled engines win without leaking loader paths into host subprocesses."""
import os
import sys

import pytest

import pdf_runtime


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
@pytest.mark.parametrize("tool", ["ghostscript", "tesseract", "fzf"])
def test_runtime_resolution_order(platform, tool, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", platform)
    internal, installed = tmp_path / "_internal", tmp_path / "installed"
    relative, names = pdf_runtime.TOOLS[tool]
    if platform != "win32":
        relative = pdf_runtime._UNIX_TOOLS[tool]
        names = ("gs",) if tool == "ghostscript" else names
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
    assert os.environ["GS_LIB"] == "not-the-bundle"
    assert os.environ["TESSDATA_PREFIX"] == "not-the-bundle"
    env = pdf_runtime.helper_env(str(shipped))
    if tool == "tesseract":
        assert env["TESSDATA_PREFIX"] == str(internal / "vendor/tesseract/tessdata")
    elif tool == "ghostscript":
        assert str(internal / "vendor/ghostscript/Resource/Init") in env["GS_LIB"]
    shipped.unlink()
    assert pdf_runtime.resolve_tool(tool) == str(beside_exe)
    beside_exe.unlink()
    assert pdf_runtime.resolve_tool(tool) == f"/on-path/{names[0]}"
    monkeypatch.setattr(pdf_runtime.shutil, "which", lambda name: None)
    if platform != "win32":
        assert pdf_runtime.resolve_tool(tool) is None
        return
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


def test_host_environment_restores_loader_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "powerpdf"))
    monkeypatch.setenv("PATH", os.pathsep.join((str(tmp_path / "_internal/PySide6"), "/host/bin")))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/frozen")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/host")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/frozen")
    monkeypatch.delenv("DYLD_LIBRARY_PATH_ORIG", raising=False)
    monkeypatch.setenv("TESSDATA_PREFIX", "/frozen/tessdata")
    env = pdf_runtime.host_env()
    assert env["LD_LIBRARY_PATH"] == "/host"
    assert env["PATH"] == "/host/bin"
    assert "LD_LIBRARY_PATH_ORIG" not in env
    assert "DYLD_LIBRARY_PATH" not in env
    assert "TESSDATA_PREFIX" not in env
    assert os.environ["DYLD_LIBRARY_PATH"] == "/frozen"


def test_macos_resources_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "Contents/Frameworks"), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Contents/MacOS/PowerPDF"))
    tool = tmp_path / "Contents/Resources/vendor/ghostscript/bin/gs"
    tool.parent.mkdir(parents=True)
    tool.touch()
    assert pdf_runtime.resolve_tool("ghostscript") == str(tool)


def test_source_host_environment_preserves_user_loader_paths(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/user/libs")
    assert pdf_runtime.host_env()["LD_LIBRARY_PATH"] == "/user/libs"
