# Both executables share one native layout; BUNDLE handles macOS code/data seams.
import json
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
CONFIG = json.loads((ROOT / 'packaging/desktop-config.json').read_text())
VERSION = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
options = [('X utf8', None, 'OPTION')]
analyses = {}
executables = {}
for kind in ('application', 'terminal'):
    profile = CONFIG[kind]
    datas = collect_data_files('pypdfium2') + collect_data_files('pypdfium2_raw') + copy_metadata('powerpdf')
    for asset in profile['assets']:
        source = ROOT / asset
        datas.append((str(source), asset if source.is_dir() else '.'))
    if kind == 'terminal':
        datas += collect_data_files('textual')
    hiddenimports = list(profile['hidden_imports'])
    for module in profile['collect_submodules']:
        hiddenimports.extend(collect_submodules(module))
    analysis = Analysis([str(ROOT / profile['entry'])], pathex=[str(ROOT)],
                        binaries=[], datas=datas, hiddenimports=hiddenimports,
                        excludes=CONFIG['excluded_modules'] + (['textual', 'textual_image'] if kind == 'application' else []))
    analyses[kind] = analysis
    name = 'powerpdf' if kind == 'terminal' else ('powerpdf-gui' if sys.platform == 'win32' else 'PowerPDF')
    executables[kind] = EXE(PYZ(analysis.pure), analysis.scripts, options,
                            exclude_binaries=True, name=name,
                            console=kind == 'terminal' or sys.platform == 'linux',
                            icon=str(ROOT / 'assets/powerpdf.png') if kind == 'application' else None)

collection = COLLECT(executables['application'], executables['terminal'],
                     analyses['application'].binaries, analyses['terminal'].binaries,
                     analyses['application'].datas, analyses['terminal'].datas,
                     strip=False, upx=True, name='PowerPDF')
if sys.platform == 'darwin':
    app = BUNDLE(collection, name='PowerPDF.app', icon=str(ROOT / 'assets/powerpdf.png'),
                 bundle_identifier='org.powerpdf.PowerPDF', version=VERSION, info_plist={
                     'CFBundleVersion': VERSION, 'LSBackgroundOnly': False,
                     'LSUIElement': False, 'NSHighResolutionCapable': True,
                     'CFBundleDocumentTypes': [{'CFBundleTypeName': 'PDF document',
                                               'CFBundleTypeRole': 'Editor',
                                               'LSHandlerRank': 'Alternate',
                                               'LSItemContentTypes': ['com.adobe.pdf']}]})
