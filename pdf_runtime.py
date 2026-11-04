"""Resolve bundled helpers and isolate their loader environment from host tools."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import os
import shutil
import sys
import threading
from pathlib import Path


TOOLS = {
    "ghostscript": ("ghostscript/bin/gswin64c.exe", ("gswin64c", "gs")),
    "tesseract": ("tesseract/tesseract.exe", ("tesseract",)),
    "fzf": ("fzf/fzf.exe", ("fzf",)),
}
_UNIX_TOOLS = {"ghostscript": "ghostscript/bin/gs", "tesseract": "tesseract/bin/tesseract", "fzf": "fzf/bin/fzf"}
_LAUNCH_LOCK = threading.RLock()


def _dll_directory_api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetDllDirectoryW.argtypes = (ctypes.c_ulong, ctypes.c_wchar_p)
    api.GetDllDirectoryW.restype = ctypes.c_ulong
    api.SetDllDirectoryW.argtypes = (ctypes.c_wchar_p,)
    api.SetDllDirectoryW.restype = ctypes.c_int
    return api


@contextmanager
def helper_launch_scope():
    """Serialize helper process creation against frozen Windows host launches.

    Wrap Popen creation only; do not hold this scope while waiting for a helper.
    The returned process still needs env=helper_env(executable).
    """
    with _LAUNCH_LOCK:
        yield


@contextmanager
def host_launch_scope():
    """Temporarily undo PyInstaller's Windows process-global DLL directory.

    Every app-owned helper launch must use helper_launch_scope so another thread
    cannot start a child while this reset is active. Pair with env=host_env()
    for subprocess host launches; native desktop APIs can use this scope alone.
    """
    with _LAUNCH_LOCK:
        if sys.platform != "win32" or not getattr(sys, "frozen", False):
            yield
            return
        api = _dll_directory_api()
        length = api.GetDllDirectoryW(0, None)
        previous = ctypes.create_unicode_buffer(length + 1)
        if length and not api.GetDllDirectoryW(len(previous), previous):
            raise ctypes.WinError(ctypes.get_last_error())
        if not api.SetDllDirectoryW(None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            if not api.SetDllDirectoryW(previous.value or None):
                raise ctypes.WinError(ctypes.get_last_error())


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

    On frozen Windows, also wrap process creation in host_launch_scope() to
    reset the process-global DLL directory without racing helper launches.
    """
    env = os.environ.copy()
    if not getattr(sys, "frozen", False):
        return env
    roots = [root.absolute() for root in bundle_roots()]
    if "PATH" in env:
        env["PATH"] = os.pathsep.join(
            entry for entry in env["PATH"].split(os.pathsep)
            if not entry or not any(Path(entry).absolute().is_relative_to(root) for root in roots)
        )
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
