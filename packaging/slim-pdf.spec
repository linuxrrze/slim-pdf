# PyInstaller spec: one folder with the GUI app and the `slim-pdf` CLI.
#
#   python packaging/make_icons.py
#   pyinstaller packaging/slim-pdf.spec --noconfirm
#
# Output: dist/Slim PDF/ (Windows, Linux) or dist/Slim PDF.app (macOS,
# with the CLI at Contents/MacOS/slim-pdf).

import runpy
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
ICONS = ROOT / "packaging" / "build-icons"
APP_NAME = "Slim PDF"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

hidden = (
    collect_submodules("keyring.backends")
    + collect_submodules("pyhanko")
    + collect_submodules("pyhanko_certvalidator")
    + ["pkcs11", "pkcs11._pkcs11", "pykeepass", "pypdf"]
    + collect_submodules("slim_pdf.ocr")  # the engines are imported when needed
)
# text recognition: the system's own engine, through its Python bindings
if sys.platform == "win32":
    hidden += collect_submodules("winrt")
elif sys.platform == "darwin":
    hidden += ["objc", "Foundation", "Vision", "Quartz", "CoreML"]
datas = (
    collect_data_files("certifi")
    + collect_data_files("pyhanko")
    + collect_data_files("pypdfium2_raw", include_py_files=False)
    + collect_data_files("slim_pdf", includes=["gui/icon.png", "gui/icons/*",
                                                     "locale/*/LC_MESSAGES/*.mo"])
    + [(str(ICONS / "slim-pdf.png"), ".")]
)
# Slim PDF's license, and the licenses of everything bundled with it
notices = runpy.run_path(str(ROOT / "packaging" / "third_party_licenses.py"))["write"](
    ROOT / "build" / "THIRD-PARTY-LICENSES.txt")
datas += [(str(ROOT / "LICENSE"), "."), (str(notices), ".")]
# Metadata the bundle can't work without (keyring finds its backends through
# entry points, others read their own version). A missing one fails the build
# instead of shipping an app without, e.g., keyring support.
for dist in ("keyring", "pyhanko", "pyhanko-certvalidator", "pypdf", "pykeepass", "slim-pdf"):
    datas += copy_metadata(dist)
if sys.platform == "win32":
    datas += collect_data_files("tzdata")  # zoneinfo has no system database on Windows

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
    icon = str(ICONS / "slim-pdf.ico")
elif sys.platform == "darwin":
    icon = str(ICONS / "slim-pdf.icns")

gui_exe = EXE(PYZ(gui_a.pure), gui_a.scripts, [], exclude_binaries=True,
              name=APP_NAME if sys.platform != "darwin" else "slim-pdf-gui",
              console=False, icon=icon,
              argv_emulation=sys.platform == "darwin")  # files dropped on the Dock icon
cli_exe = EXE(PYZ(cli_a.pure), cli_a.scripts, [], exclude_binaries=True,
              name="slim-pdf", console=True, icon=icon)

coll = COLLECT(gui_exe, gui_a.binaries, gui_a.datas,
               cli_exe, cli_a.binaries, cli_a.datas,
               name=APP_NAME)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon,
        bundle_identifier="de.linux-ng.slim-pdf",
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
