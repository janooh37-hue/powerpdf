"""Display strings are visual only; the app must keep logical path identity."""

import pytest
from rich.cells import cell_len, get_character_cell_size

from pdf_display import bidi_enabled, display_name


@pytest.fixture(autouse=True)
def force_cell_order(monkeypatch):
    monkeypatch.setenv("POWERPDF_BIDI", "on")


@pytest.mark.parametrize(("logical", "visual"), [
    ("رخصة المركبة.pdf", "pdf.\ufe94\ufe92\ufedb\ufeae\ufee4\ufedf\ufe8d \ufe94\ufebc\ufea7\ufead"),
    ("شهادة 2026.pdf", "pdf.2026 \ufe93\ufea9\ufe8e\ufeec\ufeb7"),
    ("Ahmed عقد.pdf", "Ahmed \ufeaa\ufed8\ufecb.pdf"),
    ("تقرير الترخيص 2026 (2).pdf", "pdf.(2) 2026 \ufeba\ufef4\ufea7\ufeae\ufe98\ufedf\ufe8d \ufeae\ufef3\ufeae\ufed8\ufe97"),
    ("لا.pdf", "pdf.\ufefb"),
])
def test_known_visual_cell_order(logical, visual):
    assert display_name(logical) == visual
    assert cell_len(visual) == len(visual)
    assert all(get_character_cell_size(ch) == 1 for ch in visual)


@pytest.mark.parametrize("name", ["report 2026 (2).pdf", "résumé.pdf", "文件.pdf", ""])
def test_non_arabic_passthrough(name):
    assert display_name(name) is name


def test_path_segments_keep_filesystem_order():
    name = "شهادة 2026.pdf"
    folder = "مستندات السيارات"
    assert display_name(f"C:\\{folder}\\{name}") == f"C:\\{display_name(folder)}\\{display_name(name)}"
    assert display_name(f"/tmp/{folder}/{name}") == f"/tmp/{display_name(folder)}/{display_name(name)}"
    assert display_name(f"{folder}\n{name}") == f"{display_name(folder)}\n{display_name(name)}"


def test_lam_alef_changes_width_not_path():
    logical = "لا.pdf"
    assert cell_len(logical) == 6
    assert cell_len(display_name(logical)) == 5
    assert logical == "لا.pdf"


def test_explicit_toggle_overrides_detection(monkeypatch):
    monkeypatch.setenv("POWERPDF_BIDI", "off")
    assert display_name("شهادة.pdf") == "شهادة.pdf"
    monkeypatch.setenv("KONSOLE_VERSION", "240000")
    monkeypatch.setenv("POWERPDF_BIDI", "on")
    assert bidi_enabled()


@pytest.mark.parametrize(("variable", "value"), [
    ("WT_SESSION", "test"), ("TERM", "xterm-kitty"), ("TERM", "alacritty"),
    ("KONSOLE_VERSION", "240000"), ("TERM_PROGRAM", "mintty"), ("TERM", "mlterm"),
    ("VTE_VERSION", "8000"),
])
def test_auto_detection(monkeypatch, variable, value):
    for key in ("KONSOLE_VERSION", "VTE_VERSION", "TERM_PROGRAM", "TERM", "WT_SESSION"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("POWERPDF_BIDI", "auto")
    monkeypatch.setenv(variable, value)
    assert bidi_enabled() == (variable == "WT_SESSION" or value in ("xterm-kitty", "alacritty"))
