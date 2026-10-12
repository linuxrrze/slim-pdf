"""The project's license, and the third-party notices the installers carry
(packaging/third_party_licenses.py)."""

import importlib.util
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
    # dependencies with their extras, and the bootloader (when PyInstaller is
    # installed: in the release build); not slim-pdf itself or test/build tools
    assert {"pyhanko", "pypdf", "pyside6", "pypdfium2", "pykeepass", "keyring"} <= names
    assert ("pyinstaller" in names) == (importlib.util.find_spec("PyInstaller") is not None)
    assert not names & {"slim-pdf", "pytest", "setuptools", "pyinstaller-hooks-contrib"}
    text = tpl["write"](tmp_path / "THIRD-PARTY-LICENSES.txt").read_text(encoding="utf-8")
    assert "GNU General Public License, version 3\nor later" in text
    assert "Python Software Foundation" in text  # Python's own license
    # PySide6 ships no LGPL text (none on Linux, a commercial-license note on
    # Windows): it is added, once
    assert text.count("GNU LESSER GENERAL PUBLIC LICENSE") == 1
    assert "pykeepass" in text and "GNU GENERAL PUBLIC LICENSE" in text


def test_lgpl_text_added_when_the_package_has_only_another_file(monkeypatch, tmp_path):
    """PySide6's Windows wheels ship LicenseRef-Qt-Commercial.txt only."""
    tpl = runpy.run_path(str(ROOT / "packaging" / "third_party_licenses.py"))
    render = tpl["render"]
    monkeypatch.setitem(render.__globals__, "license_files", lambda dist: (
        [("licenses/LicenseRef-Qt-Commercial.txt", "Licensees holding valid commercial Qt")]
        if dist.metadata["Name"].lower().startswith(("pyside6", "shiboken6")) else []))
    text = render()
    assert "commercial Qt" in text
    assert text.count("GNU LESSER GENERAL PUBLIC LICENSE") == 1
