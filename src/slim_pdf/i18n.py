"""Translations (gettext): English source strings, German catalog.

The language is chosen once at start-up by :func:`setup`: ``$SLIM_PDF_LANG``,
else ``language`` in the config file, else the system language (the GUI
passes Qt's idea of it, which also works for apps started from Finder or
the Start menu); anything without a catalog falls back to English.

Code marks strings with ``_()`` (translated now), ``ngettext()`` (plurals)
or ``N_()`` (module-level constants, translated where they are shown).
Placeholders use ``str.format`` names, so translators can reorder them:
``_("page {page} out of range").format(page=n)``.
``scripts/i18n.py`` extracts the strings and compiles the catalogs.
"""

from __future__ import annotations

import gettext
import locale
import os
from collections.abc import Iterable
from pathlib import Path

from . import environ

DOMAIN = "slim-pdf"
LOCALE_DIR = Path(__file__).with_name("locale")
LANGUAGES = {"en": "English", "de": "Deutsch"}  # code -> name in that language

_translation: gettext.NullTranslations = gettext.NullTranslations()
_current = "en"


def _(message: str) -> str:
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)


def N_(message: str) -> str:
    """Mark ``message`` for extraction without translating it yet."""
    return message


def _normalize(code: str | None) -> str | None:
    """'de_DE.UTF-8', 'de-AT', 'German_Germany' -> 'de'; unknown -> None."""
    if not code:
        return None
    code = code.strip().replace("-", "_").split(".")[0].split("@")[0]
    if code.lower().startswith("german"):
        return "de"
    lang = code.split("_")[0].lower()
    return lang if lang in LANGUAGES else None


def system_languages() -> list[str]:
    """Preferred languages from the environment / OS locale (no Qt)."""
    found = []
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            found.extend(value.split(":"))
            break
    try:
        found.append(locale.getlocale()[0] or "")
    except ValueError:
        pass
    return found


def choose(configured: str | None = None, system: Iterable[str] = ()) -> str:
    """The language to use: $SLIM_PDF_LANG, the config, then the system."""
    for candidate in (environ("LANG"), configured, *system):
        if candidate == "system":
            continue
        if lang := _normalize(candidate):
            return lang
    return "en"


def set_language(lang: str) -> None:
    global _translation, _current
    lang = _normalize(lang) or "en"
    _current = lang
    _translation = gettext.translation(DOMAIN, LOCALE_DIR, languages=[lang], fallback=True)


def setup(configured: str | None = None, system: Iterable[str] | None = None) -> str:
    """Choose and activate the language; returns its code."""
    lang = choose(configured, system_languages() if system is None else system)
    set_language(lang)
    return lang


def current() -> str:
    return _current
