from importlib.metadata import version
from pathlib import Path
import tomllib

from PySide6.QtWidgets import QMessageBox


def test_gui_terminal_package_and_lock_versions_agree(qtbot, tmp_path, monkeypatch):
    import powerpdf
    from powerpdf_gui import VERSION
    from powerpdf_gui.window import MainWindow

    root = Path(__file__).parent
    project = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
    lock = tomllib.loads((root / 'uv.lock').read_text())
    locked = next(item['version'] for item in lock['package'] if item['name'] == 'powerpdf')
    assert VERSION == powerpdf.VERSION == version('powerpdf') == project == locked == '0.8.0'
    messages = []
    monkeypatch.setattr(QMessageBox, 'about', lambda parent, title, text: messages.append(text))
    window = MainWindow(tmp_path, settings_path=tmp_path / 'settings.json')
    qtbot.addWidget(window)
    window.show_about()
    assert messages[0].startswith('PowerPDF 0.8.0\n')
    window.close()
