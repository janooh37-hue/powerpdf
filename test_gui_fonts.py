"""Audit app-authored UI copy against actual bundled cmaps, never OS fallback."""
import ast
from pathlib import Path
import unicodedata

from PySide6.QtGui import QFont, QRawFont

from powerpdf_gui.resources import FONTS, asset_path


def literal_strings(path):
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr):
                value = node.body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstrings.add(id(value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


def test_all_gui_and_core_literal_copy_has_bundled_glyphs(qapp):
    fonts = [QRawFont(str(asset_path(Path('fonts') / filename)), 12,
                      QFont.HintingPreference.PreferNoHinting) for filename, _ in FONTS]
    assert fonts and all(font.isValid() for font in fonts)
    root = Path(__file__).parent
    locations = {}
    # Core literals include job progress/errors surfaced by the GUI. Parsing all
    # non-docstring literals also covers f-string fragments, menus, palette text,
    # hints, tooltips, help, buttons and accessibility copy without a manual list.
    for package in ('powerpdf_gui', 'powerpdf_core'):
        for path in sorted((root / package).glob('*.py')):
            for line, text in literal_strings(path):
                for character in set(text):
                    # Whitespace and bidi controls are layout, not drawable glyphs.
                    if character.isspace() or unicodedata.category(character) in ('Cc', 'Cf'):
                        continue
                    locations.setdefault(character, []).append(f'{path.relative_to(root)}:{line}')
    missing = []
    for character, sources in sorted(locations.items(), key=lambda item: ord(item[0])):
        if not any(font.supportsCharacter(ord(character)) for font in fonts):
            missing.append(f'U+{ord(character):04X} {unicodedata.name(character, "UNKNOWN")} ({character!r}): ' + ', '.join(sources))
    assert not missing, 'Bundled font coverage gaps:\n' + '\n'.join(missing)
