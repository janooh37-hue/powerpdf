"""Arabic terminal-cell display experiment; never use these strings as paths or Input values."""

from __future__ import annotations

import os
import re
from unicodedata import category, normalize

import arabic_reshaper
from bidi import algorithm
from bidi.mirror import MIRRORED

_ARABIC = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u0870-\u089f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")
_SEPARATORS = re.compile(r"([/\\\n])")
_RESHAPER = arabic_reshaper.ArabicReshaper(configuration={
    "delete_harakat": False,
    "shift_harakat_position": True,
})

# Paired brackets are mirrored Ps/Pe punctuation, not every mirrored symbol
# (e.g. <, > and quotation marks). Normalize canonical angle-bracket variants
# for BD16 matching without normalizing the displayed filename itself.
_OPEN_BRACKETS = {
    char: normalize("NFD", partner)
    for char, partner in MIRRORED.items() if category(char) == "Ps"
}
_CLOSE_BRACKETS = {
    char: normalize("NFD", char)
    for char in MIRRORED if category(char) == "Pe"
}


def _bracket_pairs(chars) -> list[tuple[int, int]]:
    """UAX #9 BD16, within one embedding-level run (no bidi isolates)."""
    stack: list[tuple[str, int]] = []
    pairs: list[tuple[int, int]] = []
    for index, char in enumerate(chars):
        if char["type"] != "ON":
            continue
        glyph = char["ch"]
        if glyph in _OPEN_BRACKETS:
            if len(stack) == 63:
                return []
            stack.append((_OPEN_BRACKETS[glyph], index))
        elif glyph in _CLOSE_BRACKETS:
            for position in range(len(stack) - 1, -1, -1):
                if stack[position][0] == _CLOSE_BRACKETS[glyph]:
                    pairs.append((stack[position][1], index))
                    del stack[position:]
                    break
    return sorted(pairs)


def _strong_type(char) -> str:
    """N0 treats both European and Arabic numbers as strong R."""
    kind = char["type"]
    return "R" if kind in ("EN", "AN") else kind


def _resolve_paired_brackets(storage) -> None:
    """Add N0 to python-bidi's older pipeline, before neutral resolution.

    Resolve the pair together from its contents and preceding strong context.
    Otherwise N1/N2 can give its opening and closing glyph different levels,
    producing malformed brackets with an LTR paragraph and Arabic numbers.
    """
    for run in storage["runs"]:
        start = run["start"]
        chars = storage["chars"][start:start + run["length"]]
        embedding = ("L", "R")[chars[0]["level"] % 2]
        for opening, closing in _bracket_pairs(chars):
            enclosed = {_strong_type(char) for char in chars[opening + 1:closing]}
            if embedding in enclosed:
                direction = embedding
            elif enclosed.intersection(("L", "R")):
                direction = run["sor"]
                for index in range(opening - 1, -1, -1):
                    strong = _strong_type(chars[index])
                    if strong in ("L", "R"):
                        direction = strong
                        break
            else:
                continue
            for index in (opening, closing):
                chars[index]["type"] = direction
                # W1 already ran; original NSMs immediately after a bracket
                # must inherit its newly resolved direction (N0).
                index += 1
                while index < len(chars) and chars[index]["orig"] == "NSM":
                    chars[index]["type"] = direction
                    index += 1


def _display_segment(text: str) -> str:
    """Shape, resolve in an LTR paragraph, reorder, then mirror odd levels."""
    storage = algorithm.get_empty_storage()
    storage["base_level"] = 0
    storage["base_dir"] = "L"
    algorithm.get_embedding_levels(_RESHAPER.reshape(text), storage)
    algorithm.explicit_embed_and_overrides(storage)
    algorithm.resolve_weak_types(storage)
    _resolve_paired_brackets(storage)
    algorithm.resolve_neutral_types(storage, False)
    algorithm.resolve_implicit_levels(storage, False)
    algorithm.reorder_resolved_levels(storage, False)
    algorithm.apply_mirroring(storage, False)
    return "".join(char["ch"] for char in storage["chars"])


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
    uses an LTR paragraph so extensions remain at the right. Bracket pairs are
    resolved together before level-aware mirroring. As with python-bidi's Python
    API, modern bidi isolates are not supported; this is a display-only spike.
    No path/model data is changed. ASCII and non-Arabic strings return unchanged.
    """
    if text.isascii() or not _ARABIC.search(text) or not bidi_enabled():
        return text
    return "".join(
        _display_segment(part) if _ARABIC.search(part) else part
        for part in _SEPARATORS.split(text)
    )
