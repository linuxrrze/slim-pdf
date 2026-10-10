# PyInstaller spec: one folder with the GUI app and the `pdfsigner` CLI.
#
#   python packaging/make_icons.py
#   pyinstaller packaging/pdfsigner.spec --noconfirm
#
# Output: dist/PDF Signer/ (Windows, Linux) or dist/PDF Signer.app (macOS,
# with the CLI at Contents/MacOS/pdfsigner).

import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
ICONS = ROOT / "packaging" / "build-icons"
APP_NAME = "PDF Signer"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

hidden = (
    collect_submodules("keyring.backends")
    + collect_submodules("pyhanko")
    + collect_submodules("pyhanko_certvalidator")
    + ["pkcs11", "pkcs11._pkcs11", "pykeepass", "pypdf"]
)
datas = (
    collect_data_files("certifi")
    + collect_data_files("pyhanko")
    + collect_data_files("pypdfium2_raw", include_py_files=False)
    + collect_data_files("pdfsigner", includes=["gui/icon.png"])
    + [(str(ICONS / "pdfsigner.png"), ".")]
)
for dist in ("keyring", "pyhanko", "pyhanko-certvalidator", "pypdf", "pykeepass", "pdfsigner"):
    try:
        datas += copy_metadata(dist)  # keyring finds its backends via entry points
    except Exception:
        pass
try:
    datas += collect_data_files("tzdata")  # zoneinfo on Windows
except Exception:
    pass

excludes = ["tkinter", "PySide6.Qt3DCore", "PySide6.QtWebEngineCore", "PySide6.QtQuick",
            "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtCharts",
            "PySide6.QtDataVisualization", "PySide6.QtPdf"]


def analysis(script):
    return Analysis([str(ROOT / "packaging" / script)], pathex=[str(ROOT / "src")],
                    hiddenimports=hidden, datas=datas, excludes=excludes)


gui_a = analysis("entry_gui.py")
cli_a = analysis("entry_cli.py")

icon = None
if sys.platform == "win32":
    icon = str(ICONS / "pdfsigner.ico")
elif sys.platform == "darwin":
    icon = str(ICONS / "pdfsigner.icns")

gui_exe = EXE(PYZ(gui_a.pure), gui_a.scripts, [], exclude_binaries=True,
              name=APP_NAME if sys.platform != "darwin" else "pdfsigner-gui",
              console=False, icon=icon,
              argv_emulation=sys.platform == "darwin")  # files dropped on the Dock icon
cli_exe = EXE(PYZ(cli_a.pure), cli_a.scripts, [], exclude_binaries=True,
              name="pdfsigner", console=True, icon=icon)

coll = COLLECT(gui_exe, gui_a.binaries, gui_a.datas,
               cli_exe, cli_a.binaries, cli_a.datas,
               name=APP_NAME)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon,
        bundle_identifier="de.linux-ng.pdfsigner",
        version=VERSION,
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "PDF document",
                "CFBundleTypeRole": "Editor",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["com.adobe.pdf"],
            }],
        },
    )
