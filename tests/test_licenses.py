"""The project's license, and the third-party notices the installers carry
(packaging/third_party_licenses.py)."""

import runpy
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_project_is_gpl3_or_later():
    assert "GNU GENERAL PUBLIC LICENSE\n                       Version 3" in (
        ROOT / "LICENSE").read_text(encoding="utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"] == "GPL-3.0-or-later"
    assert "LICENSE" in project["license-files"]


def test_third_party_notices_cover_the_bundle(tmp_path):
    tpl = runpy.run_path(str(ROOT / "packaging" / "third_party_licenses.py"))
    names = {d.metadata["Name"].lower() for d in tpl["bundled"]()}
    # dependencies with their extras, and the bootloader; not slim-pdf itself
    # or test/build tools
    assert {"pyhanko", "pypdf", "pyside6", "pypdfium2", "pykeepass", "keyring",
            "pyinstaller"} <= names
    assert not names & {"slim-pdf", "pytest", "setuptools", "pyinstaller-hooks-contrib"}
    text = tpl["write"](tmp_path / "THIRD-PARTY-LICENSES.txt").read_text(encoding="utf-8")
    assert "GNU General Public License, version 3\nor later" in text
    assert "Python Software Foundation" in text  # Python's own license
    # PySide6 ships no license file: the LGPL text is added
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in text
    assert "pykeepass" in text and "GNU GENERAL PUBLIC LICENSE" in text
