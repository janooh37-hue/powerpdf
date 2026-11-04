"""Explicit desktop open/reveal actions; never print or invoke a command shell.

Call from the GUI thread. Success means a request was accepted, not that the
other application displayed the document. OS failures raise OSError or
RuntimeError for the caller to present. Association queries are best effort:
unknown handlers are delegated to the desktop rather than guessed.
"""
from __future__ import annotations

import ctypes
import ntpath
from pathlib import Path
import re
import subprocess
import sys

from PySide6.QtCore import QThread, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication

import pdf_runtime


def _require_gui_thread() -> None:
    app = QApplication.instance()
    if app is None or QThread.currentThread() != app.thread():
        raise RuntimeError("Desktop actions require the QApplication GUI thread")


def _run_host(arguments: list[str], timeout: float = 5) -> subprocess.CompletedProcess:
    """Sanitize host children and lock only their creation, not their lifetime."""
    with pdf_runtime.host_launch_scope():
        process = subprocess.Popen(
            arguments, env=pdf_runtime.host_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise OSError(f"Desktop service did not respond: {arguments[0]}") from exc
    return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)


def _windows_association(query: int) -> str | None:
    """Query the effective .pdf association, including Windows UserChoice."""
    from ctypes import wintypes

    library = ctypes.WinDLL("shlwapi", use_last_error=True)
    function = library.AssocQueryStringW
    function.argtypes = [wintypes.DWORD, ctypes.c_int, wintypes.LPCWSTR,
                         wintypes.LPCWSTR, wintypes.LPWSTR,
                         ctypes.POINTER(wintypes.DWORD)]
    function.restype = ctypes.c_long
    length = wintypes.DWORD(0)
    function(0, query, ".pdf", "open", None, ctypes.byref(length))
    if not length.value:
        return None
    buffer = ctypes.create_unicode_buffer(length.value)
    if function(0, query, ".pdf", "open", buffer, ctypes.byref(length)) != 0:
        return None
    return buffer.value


def _mac_pdf_handler() -> str | None:
    """Ask LaunchServices for the default PDF viewer's bundle identifier."""
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    services = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None
    services.LSCopyDefaultRoleHandlerForContentType.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    services.LSCopyDefaultRoleHandlerForContentType.restype = ctypes.c_void_p
    content_type = cf.CFStringCreateWithCString(None, b"com.adobe.pdf", 0x08000100)
    if not content_type:
        raise OSError("Could not query the default PDF application")
    try:
        handler = services.LSCopyDefaultRoleHandlerForContentType(content_type, 0xFFFFFFFF)
        if not handler:
            return None
        try:
            buffer = ctypes.create_string_buffer(4096)
            if not cf.CFStringGetCString(handler, buffer, len(buffer), 0x08000100):
                raise OSError("Could not read the default PDF application identifier")
            return buffer.value.decode("utf-8")
        finally:
            cf.CFRelease(handler)
    finally:
        cf.CFRelease(content_type)


def _is_powerpdf_handler(handler: str) -> bool:
    return bool(re.search(r"(?i)(?:^|[\\/\s\"'.])powerpdf(?:[-_]gui)?(?:\.exe|\.py|\.desktop|[\s\"']|$)", handler))


def _guard_self_open(path: Path) -> None:
    if path.suffix.casefold() != ".pdf":
        return
    handlers = []
    if sys.platform == "win32":
        # ASSOCSTR_EXECUTABLE and ASSOCSTR_COMMAND cover frozen and Python entrypoints.
        executable = _windows_association(2)
        command = _windows_association(1)
        handlers = [value for value in (executable, command) if value]
        if getattr(sys, "frozen", False) and executable:
            if ntpath.normcase(ntpath.normpath(executable)) == ntpath.normcase(ntpath.normpath(sys.executable)):
                handlers.append("powerpdf")
    elif sys.platform == "darwin":
        handlers = [value] if (value := _mac_pdf_handler()) else []
    else:
        try:
            result = _run_host(["xdg-mime", "query", "default", "application/pdf"])
        except OSError:
            result = None
        if result is not None and result.returncode == 0:
            handlers = [result.stdout.decode("utf-8", errors="replace").strip()]
    if any(_is_powerpdf_handler(handler) for handler in handlers):
        raise RuntimeError(
            "PowerPDF is the default PDF application. Opening externally would "
            "reopen PowerPDF. Use your file manager's Open With command to choose "
            "another PDF viewer, or change the default PDF application in system settings."
        )


def _open_url(path: Path) -> None:
    # Native Qt dispatch has no subprocess env parameter. The launch scope also
    # resets/restores frozen Windows DLL search state; never mutate os.environ.
    with pdf_runtime.host_launch_scope():
        accepted = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
    if not accepted:
        raise OSError(f"The desktop could not open {path}. Choose an application using Open With in your file manager.")


def open_external(path: str | Path) -> bool:
    """Request the associated external viewer; raise on failure or a self loop."""
    _require_gui_thread()
    path = Path(path).resolve(strict=True)
    _guard_self_open(path)
    _open_url(path)
    return True


def _windows_reveal(path: Path) -> None:
    """Select a Unicode path using the shell API, without Explorer command text."""
    from ctypes import wintypes

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    shell.SHParseDisplayName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_void_p),
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    shell.SHParseDisplayName.restype = ctypes.c_long
    shell.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                                ctypes.c_void_p, wintypes.DWORD]
    shell.SHOpenFolderAndSelectItems.restype = ctypes.c_long
    initialized = ole.CoInitializeEx(None, 2)
    # Qt may already initialize this thread with a different apartment model.
    if initialized < 0 and initialized != -2147417850:  # RPC_E_CHANGED_MODE
        raise OSError(f"Could not initialize Windows shell services (HRESULT {initialized & 0xFFFFFFFF:08X})")
    item = ctypes.c_void_p()
    try:
        result = shell.SHParseDisplayName(str(path), None, ctypes.byref(item), 0, None)
        if result < 0:
            raise OSError(f"Could not locate the item in Explorer (HRESULT {result & 0xFFFFFFFF:08X})")
        with pdf_runtime.host_launch_scope():
            result = shell.SHOpenFolderAndSelectItems(item, 0, None, 0)
        if result < 0:
            raise OSError(f"Explorer could not select the item (HRESULT {result & 0xFFFFFFFF:08X})")
    finally:
        if item.value:
            ole.CoTaskMemFree(item)
        if initialized >= 0:
            ole.CoUninitialize()


def reveal(path: str | Path) -> bool:
    """Reveal a path; True means selection, False means parent-folder fallback.

    Linux file managers without FileManager1 receive an explicit parent-folder
    open instead. Callers should describe False as 'Opened containing folder',
    not claim that a file was selected. Errors opening that folder still raise.
    """
    _require_gui_thread()
    path = Path(path).resolve(strict=True)
    if sys.platform == "win32":
        _windows_reveal(path)
        return True
    if sys.platform == "darwin":
        result = _run_host(["/usr/bin/open", "-R", str(path)])
        if result.returncode:
            raise OSError("Finder could not reveal the selected item")
        return True
    try:
        # Path.as_uri percent-encodes commas and non-ASCII bytes, preserving one
        # URI in dbus-send's array grammar even for unusual user filenames.
        result = _run_host([
            "dbus-send", "--session", "--print-reply", "--reply-timeout=2000",
            "--dest=org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
            "org.freedesktop.FileManager1.ShowItems", f"array:string:{path.as_uri()}", "string:",
        ], timeout=3)
        if result.returncode == 0:
            return True
    except OSError:
        pass
    _open_url(path.parent)
    return False
