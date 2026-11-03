"""Arabic terminal-cell display experiment; never use these strings as paths or Input values."""

from __future__ import annotations

import os
import re

import arabic_reshaper
from bidi.algorithm import get_display  # Mature Python API includes glyph mirroring (Unicode L4).

_ARABIC = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u0870-\u089f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")
_SEPARATORS = re.compile(r"([/\\\n])")
_RESHAPER = arabic_reshaper.ArabicReshaper(configuration={
    "delete_harakat": False,
    "shift_harakat_position": True,
})


def bidi_enabled() -> bool:
    """Best-effort terminal detection; explicit POWERPDF_BIDI=on/off always wins.

    Native bidi is terminal/configuration dependent and cannot be queried reliably.
    Unknown terminals default to cell-order compensation, including WT/conhost.
    """
    mode = os.environ.get("POWERPDF_BIDI", "auto").lower()
    if mode in ("on", "off"):
        return mode == "on"
    term = os.environ.get("TERM", "").lower()
    program = os.environ.get("TERM_PROGRAM", "").lower()
    return not (
        os.environ.get("KONSOLE_VERSION")
        or os.environ.get("VTE_VERSION")
        or program == "mintty"
        or term.startswith("mlterm")
    )


def display_name(text: str) -> str:
    """Logical name/path → joined Arabic in visual cell order, for LTR TUI labels.

    Keep path components (and separate lines) in their original order. Each name
    uses its first strong character's direction, like a standalone bidi paragraph.
    No path/model data is changed. ASCII and non-Arabic strings return unchanged.
    """
    if text.isascii() or not _ARABIC.search(text) or not bidi_enabled():
        return text
    return "".join(
        get_display(_RESHAPER.reshape(part)) if _ARABIC.search(part) else part
        for part in _SEPARATORS.split(text)
    )
