"""Write THIRD-PARTY-LICENSES.txt for the installers: name, version and
license of everything the bundle contains (slim-pdf's dependencies with all
extras, the PyInstaller bootloader, Python itself), followed by each
package's own license files.

    python packaging/third_party_licenses.py OUT

Runs in the build environment, where those packages are installed;
slim-pdf.spec calls :func:`write` itself.
"""

from __future__ import annotations

import re
import sys
import sysconfig
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

HERE = Path(__file__).parent
ROOT_DIST = "slim-pdf"
EXTRAS = ("all",)
# in the bundle without being a dependency: the bootloader that starts it
ALSO_BUNDLED = ("pyinstaller",)
# packages that ship no license file: the text their license expression names
SHARED_TEXTS = {"LGPL-3.0": HERE / "licenses" / "LGPL-3.0.txt"}
LICENSE_FILE = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE)[^/]*$", re.IGNORECASE)
QT_THIRD_PARTY = "https://doc.qt.io/qt-6/licenses-used-in-qt.html"
RULE = "=" * 78


def bundled(root: str = ROOT_DIST, extras=EXTRAS) -> list[metadata.Distribution]:
    """``root`` and what it needs (with ``extras``), as installed here,
    sorted by name; requirements for other platforms are left out."""
    found: dict[str, metadata.Distribution] = {}
    seen = set()
    todo = [(root, frozenset(extras))]
    while todo:
        name, wanted = todo.pop()
        if (canonicalize_name(name), wanted) in seen:
            continue
        seen.add((canonicalize_name(name), wanted))
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue  # optional and not installed: not in the bundle either
        found[canonicalize_name(name)] = dist
        for line in dist.requires or []:
            req = Requirement(line)
            # markers: this platform, and the extras asked for (or none)
            if req.marker is None or any(req.marker.evaluate({"extra": e})
                                         for e in wanted or {""}):
                todo.append((req.name, frozenset(req.extras)))
    found.pop(canonicalize_name(root), None)  # slim-pdf's own license is LICENSE
    for name in ALSO_BUNDLED:  # without its own dependencies (build tools)
        try:
            found[canonicalize_name(name)] = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            pass
    return sorted(found.values(), key=lambda d: canonicalize_name(d.metadata["Name"]))


def license_name(dist: metadata.Distribution) -> str:
    md = dist.metadata
    if md.get("License-Expression"):
        return md["License-Expression"]
    classifiers = [c.split("::")[-1].strip() for c in md.get_all("Classifier") or []
                   if c.startswith("License ::")]
    if classifiers:
        return "; ".join(classifiers)
    text = (md.get("License") or "").strip()
    return text.splitlines()[0][:70] if text else "unknown"


def homepage(dist: metadata.Distribution) -> str:
    md = dist.metadata
    if md.get("Home-page"):
        return md["Home-page"]
    for url in md.get_all("Project-URL") or []:
        label, _, link = url.partition(",")
        if label.strip().lower() in ("homepage", "home", "source", "repository"):
            return link.strip()
    return ""


def license_files(dist: metadata.Distribution) -> list[tuple[str, str]]:
    """(name, text) of the license files the package installed."""
    out = []
    for f in dist.files or []:
        path = str(f).replace("\\", "/")
        if ".dist-info/" not in path or not (LICENSE_FILE.search(path) or "/licenses/" in path):
            continue
        try:
            out.append((path.split(".dist-info/", 1)[1], f.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            pass
    return out


def python_license() -> str | None:
    """Python's LICENSE.txt (it covers what the interpreter bundles, too)."""
    base = Path(sys.base_prefix)
    for path in (Path(sysconfig.get_paths()["stdlib"]) / "LICENSE.txt",
                 base / "LICENSE.txt", base / "LICENSE"):
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    return None


def render() -> str:
    dists = bundled()
    py = python_license()
    rows = [("Python", sys.version.split()[0], "PSF-2.0", "https://www.python.org")]
    rows += [(d.metadata["Name"], d.version, license_name(d), homepage(d)) for d in dists]
    width = max(len(r[0]) for r in rows)
    parts = [
        "Third-party software in Slim PDF",
        "",
        "Slim PDF itself is licensed under the GNU General Public License, version 3",
        "or later (see LICENSE). The installers also contain the software below,",
        "each under its own license. Its license texts follow the list.",
        "",
        "Qt (through PySide6) is used under the LGPL 3.0; its libraries are",
        "separate files in the installation and can be replaced. Qt's source and",
        "the licenses of the libraries Qt itself bundles: " + QT_THIRD_PARTY,
        "",
    ]
    parts += [f"  {name:<{width}}  {version:<12} {lic}" for name, version, lic, _ in rows]
    shared = set()
    parts += ["", RULE, f"Python {rows[0][1]} - {rows[0][3]}", RULE, "",
              py or "See https://docs.python.org/3/license.html", ""]
    for dist, (name, version, lic, url) in zip(dists, rows[1:]):
        parts += [RULE, f"{name} {version} - {lic}" + (f" - {url}" if url else ""), RULE, ""]
        files = license_files(dist)
        for fname, text in files:
            parts += [f"--- {fname} ---", "", text.rstrip(), ""]
        if not files:
            refs = [spdx for spdx in SHARED_TEXTS if spdx in lic]
            shared.update(refs)
            parts += [f"(No license file in the package; see the {', '.join(refs)} text below.)"
                      if refs else "(No license file in the package; see its homepage.)", ""]
    for spdx in sorted(shared):
        parts += [RULE, f"{spdx} (full text)", RULE, "",
                  SHARED_TEXTS[spdx].read_text(encoding="utf-8").rstrip(), ""]
    return "\n".join(parts) + "\n"


def write(out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(), encoding="utf-8")
    return out


if __name__ == "__main__":
    print(write(Path(sys.argv[1] if len(sys.argv) > 1 else "THIRD-PARTY-LICENSES.txt")))
