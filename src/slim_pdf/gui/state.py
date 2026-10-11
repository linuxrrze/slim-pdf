"""What the GUI remembers between runs that isn't a setting: the folder of
the last opened file, and whether the side panels are shown. A small JSON file in the user state folder
(``$SLIM_PDF_STATE_DIR`` overrides it); unreadable or missing means "nothing
remembered", and failing to write it is ignored."""

from __future__ import annotations

import json
from pathlib import Path

from platformdirs import user_state_dir

from .. import environ


def state_path() -> Path:
    return Path(environ("STATE_DIR") or user_state_dir("slim-pdf", appauthor=False)) / "gui.json"


def _load() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass  # only a convenience


def last_folder() -> str:
    """The folder of the last opened file, if it still exists, else ""."""
    folder = _load().get("last_folder")
    return folder if isinstance(folder, str) and Path(folder).is_dir() else ""


def remember_folder(folder: Path) -> None:
    data = _load()
    data["last_folder"] = str(Path(folder).resolve())
    _save(data)


def flag(name: str, default: bool = True) -> bool:
    """A remembered on/off choice (e.g. whether a panel is shown)."""
    value = _load().get("flags", {}).get(name, default)
    return value if isinstance(value, bool) else default


def set_flag(name: str, value: bool) -> None:
    data = _load()
    flags = data.get("flags")
    data["flags"] = {**(flags if isinstance(flags, dict) else {}), name: bool(value)}
    _save(data)
