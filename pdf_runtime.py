"""Resolve bundled helpers and isolate their loader environment from host tools."""
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
_UNIX_TOOLS = {"ghostscript": "ghostscript/bin/gs", "tesseract": "tesseract/bin/tesseract", "fzf": "fzf/bin/fzf"}


def bundle_roots() -> list[Path]:
    if getattr(sys, "frozen", False):
        roots = [Path(sys._MEIPASS), Path(sys.executable).parent]
        if sys.platform == "darwin":
            roots.extend([Path(sys.executable).parent.parent / "Resources", Path(sys.executable).parent.parent / "Frameworks"])
    else:
        roots = [Path(__file__).resolve().parent]
    return list(dict.fromkeys(roots))


def resolve_tool(tool: str) -> str | None:
    """Prefer shipped tools, without changing the application's environment."""
    relative, names = TOOLS[tool]
    if sys.platform != "win32":
        relative = _UNIX_TOOLS[tool]
        names = ("gs",) if tool == "ghostscript" else names
    for root in bundle_roots():
        candidate = root / "vendor" / relative
        if candidate.is_file():
            return str(candidate)
    for name in names:
        if found := shutil.which(name):
            return found
    if sys.platform != "win32":
        return None
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    if tool == "ghostscript":
        candidates = sorted((program_files / "gs").glob("gs*/bin/gswin64c.exe"), reverse=True)
    elif tool == "tesseract":
        candidates = [program_files / "Tesseract-OCR/tesseract.exe"]
    else:
        candidates = [Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Microsoft/WinGet/Links/fzf.exe"]
    return next((str(path) for path in candidates if path.is_file()), None)


def host_env() -> dict[str, str]:
    """Undo freezer loader paths before spawning OS open/reveal utilities.

    On Windows, callers must additionally reset SetDllDirectoryW around a host
    launch; that process-global API cannot safely be changed by this env helper.
    """
    env = os.environ.copy()
    for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH"):
        original = env.pop(name + "_ORIG", None)
        if original is None:
            env.pop(name, None)
        else:
            env[name] = original
    for name in ("GS_LIB", "TESSDATA_PREFIX", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH"):
        env.pop(name, None)
    return env


def helper_env(executable: str) -> dict[str, str]:
    """Return a private environment for a resolved bundled helper subprocess."""
    env = host_env()
    candidate = Path(executable).absolute()
    for root in bundle_roots():
        vendor = (root / "vendor").absolute()
        if not candidate.is_relative_to(vendor):
            continue
        tool = candidate.relative_to(vendor).parts[0]
        home = vendor / tool
        if tool == "tesseract":
            env["TESSDATA_PREFIX"] = str(home / "tessdata")
        elif tool == "ghostscript":
            env["GS_LIB"] = os.pathsep.join(str(home / part) for part in ("lib", "Resource/Init", "Resource/Font"))
        if sys.platform in ("linux", "darwin"):
            key = "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
            paths = [str(vendor / "lib"), str(home / "lib")]
            if env.get(key):
                paths.append(env[key])
            env[key] = os.pathsep.join(paths)
        elif sys.platform == "win32":
            env["PATH"] = str(candidate.parent) + os.pathsep + env.get("PATH", "")
        break
    return env
