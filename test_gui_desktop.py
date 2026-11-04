"""Asset resolution and desktop dispatch, with OS launches mocked, never executed."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path, PurePath
import subprocess
import sys
from types import SimpleNamespace

import pytest

from powerpdf_gui import desktop, resources


@pytest.fixture
def desktop_calls(monkeypatch):
    calls = []

    @contextmanager
    def scope():
        calls.append("enter")
        try:
            yield
        finally:
            calls.append("exit")

    monkeypatch.setattr(desktop, "_require_gui_thread", lambda: None)
    monkeypatch.setattr(desktop.pdf_runtime, "host_launch_scope", scope)
    return calls


def test_source_assets_and_original_font_provenance(monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    root = Path(__file__).resolve().parent
    assert resources.asset_path("powerpdf.png") == root / "assets/powerpdf.png"
    assert resources.license_path() == root / "LICENSE"
    manifest = json.loads(resources.asset_path("fonts/SOURCES.json").read_text())
    for font in manifest["fonts"]:
        data = resources.asset_path(Path("fonts") / font["file"]).read_bytes()
        assert data[:4] == b"\x00\x01\x00\x00"
        assert len(data) == font["size"]
        assert hashlib.sha256(data).hexdigest() == font["sha256"]
        license_text = resources.asset_path(Path("fonts") / font["license"]).read_text()
        assert "SIL OPEN FONT LICENSE Version 1.1" in license_text
        assert "TERMINATION" in license_text and "DISCLAIMER" in license_text
        assert font["url"].startswith("https://raw.githubusercontent.com/google/fonts/")
        assert manifest["revision"] in font["url"]


def test_frozen_assets_are_authoritative(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    (bundle / "assets").mkdir(parents=True)
    icon = bundle / "assets/powerpdf.png"
    icon.write_bytes(b"bundled icon")
    license_file = bundle / "LICENSE"
    license_file.write_text("bundled license")
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    assert resources.asset_path("powerpdf.png") == icon
    assert resources.license_path() == license_file
    with pytest.raises(FileNotFoundError):
        resources.asset_path("fonts/NotoSans[wdth,wght].ttf")
    license_file.unlink()
    with pytest.raises(FileNotFoundError):
        resources.license_path()


def test_installed_wheel_assets_and_license(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(resources, "__file__", str(tmp_path / "site-packages/powerpdf_gui/resources.py"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    icon = tmp_path / "share/powerpdf/assets/powerpdf.png"
    icon.parent.mkdir(parents=True)
    icon.touch()
    license_entry = PurePath("powerpdf-0.8.0.dist-info/licenses/LICENSE")
    license_file = tmp_path / "site-packages" / license_entry
    license_file.parent.mkdir(parents=True)
    license_file.write_text("installed license")
    monkeypatch.setattr(resources, "distribution", lambda name: SimpleNamespace(
        files=[license_entry], locate_file=lambda entry: tmp_path / "site-packages" / entry,
    ))
    assert resources.asset_path("powerpdf.png") == icon
    assert resources.license_path() == license_file


@pytest.mark.parametrize("name", ["../LICENSE", "/etc/passwd", "fonts/../../LICENSE"])
def test_asset_paths_cannot_escape(name):
    with pytest.raises(ValueError):
        resources.asset_path(name)


def test_qt_open_uses_local_unicode_url_and_launch_scope(tmp_path, monkeypatch, desktop_calls):
    path = tmp_path / "رخصة [1], original.pdf"
    received = []
    monkeypatch.setattr(desktop.QDesktopServices, "openUrl", lambda url: received.append(url) or True)
    desktop._open_url(path)
    assert received[0].toLocalFile() == str(path)
    assert desktop_calls == ["enter", "exit"]


def test_qt_open_failure_is_visible(tmp_path, monkeypatch, desktop_calls):
    monkeypatch.setattr(desktop.QDesktopServices, "openUrl", lambda url: False)
    with pytest.raises(OSError, match="Open With"):
        desktop._open_url(tmp_path / "file.pdf")
    assert desktop_calls == ["enter", "exit"]


@pytest.mark.parametrize("handler", [
    r"C:\Program Files\PowerPDF\powerpdf.exe",
    '"C:\\Python\\python.exe" -m powerpdf_gui "%1"',
    "org.powerpdf.PowerPDF.desktop", "powerpdf-gui.desktop", "org.powerpdf.PowerPDF",
])
def test_recognizes_self_associations(handler):
    assert desktop._is_powerpdf_handler(handler)


@pytest.mark.parametrize("handler", ["okular.desktop", r"C:\Acrobat\AcroRd32.exe", "org.gnome.Evince"])
def test_other_viewers_are_not_self_associations(handler):
    assert not desktop._is_powerpdf_handler(handler)


def test_windows_self_open_never_dispatches(tmp_path, monkeypatch, desktop_calls):
    path = tmp_path / "file.pdf"
    path.touch()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(desktop, "_windows_association", lambda query: r"C:\PowerPDF\powerpdf.exe")
    monkeypatch.setattr(desktop, "_open_url", lambda path: pytest.fail("Must not recursively launch PowerPDF"))
    with pytest.raises(RuntimeError, match="Open With"):
        desktop.open_external(path)


def test_external_viewer_dispatches_once(tmp_path, monkeypatch, desktop_calls):
    path = tmp_path / "رخصة.pdf"
    path.touch()
    received = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(desktop, "_windows_association", lambda query: r"C:\Viewer\reader.exe")
    monkeypatch.setattr(desktop, "_open_url", received.append)
    assert desktop.open_external(path)
    assert received == [path]


def test_frozen_renamed_windows_executable_is_self(tmp_path, monkeypatch, desktop_calls):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Custom\MyViewer.exe")
    monkeypatch.setattr(desktop, "_windows_association", lambda query: r"c:\custom\myviewer.exe")
    with pytest.raises(RuntimeError, match="default PDF application"):
        desktop._guard_self_open(tmp_path / "file.pdf")


def test_linux_self_association_is_blocked(tmp_path, monkeypatch, desktop_calls):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(desktop, "_run_host", lambda args: subprocess.CompletedProcess(args, 0, b"powerpdf.desktop\n", b""))
    with pytest.raises(RuntimeError, match="default PDF application"):
        desktop._guard_self_open(tmp_path / "file.pdf")


def test_mac_self_association_is_blocked(tmp_path, monkeypatch, desktop_calls):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(desktop, "_mac_pdf_handler", lambda: "org.powerpdf.PowerPDF")
    with pytest.raises(RuntimeError, match="default PDF application"):
        desktop._guard_self_open(tmp_path / "file.pdf")


def test_linux_reveal_encodes_one_uri(tmp_path, monkeypatch, desktop_calls):
    monkeypatch.setattr(sys, "platform", "linux")
    path = tmp_path / "رخصة, [1]; $(not-a-command).pdf"
    path.touch()
    received = []

    def run(arguments, timeout):
        received.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, b"", b"")

    monkeypatch.setattr(desktop, "_run_host", run)
    assert desktop.reveal(path)
    assert received[0][-2] == "array:string:" + path.as_uri()
    assert "%2C" in received[0][-2]
    assert received[0][-1] == "string:"


@pytest.mark.parametrize("failure", ["unavailable", "rejected"])
def test_linux_reveal_honestly_reports_parent_fallback(tmp_path, monkeypatch, desktop_calls, failure):
    monkeypatch.setattr(sys, "platform", "linux")
    path = tmp_path / "file.pdf"
    path.touch()
    opened = []

    def run(arguments, timeout):
        if failure == "unavailable":
            raise FileNotFoundError("dbus-send")
        return subprocess.CompletedProcess(arguments, 1, b"", b"No FileManager1")

    monkeypatch.setattr(desktop, "_run_host", run)
    monkeypatch.setattr(desktop, "_open_url", opened.append)
    assert desktop.reveal(path) is False
    assert opened == [tmp_path]


def test_mac_reveal_uses_separated_arguments(tmp_path, monkeypatch, desktop_calls):
    monkeypatch.setattr(sys, "platform", "darwin")
    path = tmp_path / "رخصة; $(no).pdf"
    path.touch()
    received = []

    def run(arguments):
        received.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, b"", b"")

    monkeypatch.setattr(desktop, "_run_host", run)
    assert desktop.reveal(path)
    assert received == [["/usr/bin/open", "-R", str(path)]]


def test_windows_reveal_uses_native_selection_adapter(tmp_path, monkeypatch, desktop_calls):
    monkeypatch.setattr(sys, "platform", "win32")
    path = tmp_path / "رخصة, [1].pdf"
    path.touch()
    selected = []
    monkeypatch.setattr(desktop, "_windows_reveal", selected.append)
    assert desktop.reveal(path)
    assert selected == [path]


def test_host_subprocess_uses_environment_and_narrow_scope(monkeypatch, desktop_calls):
    clean_environment = {"PATH": "/host/bin"}
    monkeypatch.setattr(desktop.pdf_runtime, "host_env", lambda: clean_environment)
    observed = []

    class Process:
        returncode = 0

        def __init__(self, arguments, **kwargs):
            observed.append((arguments, kwargs))
            assert desktop_calls == ["enter"]

        def communicate(self, timeout):
            assert desktop_calls == ["enter", "exit"]
            return b"result", b""

    monkeypatch.setattr(desktop.subprocess, "Popen", Process)
    arguments = ["host-tool", "literal ; $() argument"]
    result = desktop._run_host(arguments)
    assert observed == [(arguments, {"env": clean_environment, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE})]
    assert result.stdout == b"result"


def test_host_timeout_terminates_only_owned_process(monkeypatch, desktop_calls):
    events = []

    class Process:
        def __init__(self, arguments, **kwargs):
            pass

        def communicate(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("host-tool", timeout)
            events.append("reaped")
            return b"", b""

        def kill(self):
            events.append("killed")

    monkeypatch.setattr(desktop.subprocess, "Popen", Process)
    with pytest.raises(OSError, match="did not respond"):
        desktop._run_host(["host-tool"])
    assert events == ["killed", "reaped"]
    assert desktop_calls == ["enter", "exit"]
