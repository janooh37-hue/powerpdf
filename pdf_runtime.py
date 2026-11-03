"""Find external tools without requiring system-wide installations."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


TOOLS = {
    "ghostscript": ("ghostscript/bin/gswin64c.exe", ("gswin64c", "gs")),
    "tesseract": ("tesseract/tesseract.exe", ("tesseract",)),
    "fzf": ("fzf/fzf.exe", ("fzf",)),
}


def bundle_roots() -> list[Path]:
    if getattr(sys, "frozen", False):
        roots = [Path(sys._MEIPASS), Path(sys.executable).parent]
    else:
        roots = [Path(__file__).resolve().parent]
    return list(dict.fromkeys(roots))


def resolve_tool(tool: str) -> str | None:
    """Prefer the shipped runtime, then PATH, then conventional Windows installs."""
    relative, names = TOOLS[tool]
    for root in bundle_roots():
        candidate = root / "vendor" / relative
        if candidate.is_file():
            if tool == "tesseract":
                os.environ["TESSDATA_PREFIX"] = str(candidate.parent / "tessdata")
            elif tool == "ghostscript":
                home = candidate.parent.parent
                os.environ["GS_LIB"] = os.pathsep.join(str(home / part) for part in
                    ("lib", "Resource/Init", "Resource/Font"))
            return str(candidate)
    for name in names:
        if found := shutil.which(name):
            return found
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    if tool == "ghostscript":
        candidates = sorted((program_files / "gs").glob("gs*/bin/gswin64c.exe"), reverse=True)
    elif tool == "tesseract":
        candidates = [program_files / "Tesseract-OCR/tesseract.exe"]
    else:
        candidates = [Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Microsoft/WinGet/Links/fzf.exe"]
    return next((str(path) for path in candidates if path.is_file()), None)
