import gettext
import importlib.util
import sys
from pathlib import Path

import pytest

from pdfsigner import i18n
from pdfsigner.config import Config, load_config

ROOT = Path(__file__).resolve().parent.parent


def _tool():
    spec = importlib.util.spec_from_file_location("i18n_tool", ROOT / "scripts" / "i18n.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["i18n_tool"] = mod  # dataclasses look the module up
    spec.loader.exec_module(mod)
    return mod


tool = _tool()


@pytest.mark.parametrize("env, configured, system, expected", [
    (None, None, ["de_DE.UTF-8"], "de"),
    (None, None, ["de-AT", "en-US"], "de"),
    (None, None, ["German_Germany"], "de"),  # Windows locale names
    (None, None, ["fr_FR", "C"], "en"),
    (None, "de", ["en_US"], "de"),
    (None, "en", ["de_DE"], "en"),
    (None, "system", ["de_DE"], "de"),
    ("de", "en", ["en_US"], "de"),  # $PDFSIGNER_LANG wins
    ("xx", None, ["de_DE"], "de"),
])
def test_choose(monkeypatch, env, configured, system, expected):
    if env is None:
        monkeypatch.delenv("PDFSIGNER_LANG", raising=False)
    else:
        monkeypatch.setenv("PDFSIGNER_LANG", env)
    assert i18n.choose(configured, system) == expected


def test_catalogs_are_complete_and_compiled():
    """Every marked string is in the catalogs, translated, with the same
    placeholders, and the shipped .mo files match the .po files."""
    assert tool.problems() == []


def test_mo_writer_roundtrip(tmp_path):
    po = tmp_path / "x.po"
    entries = [
        tool.Entry("Sign…", msgstr=["Signieren…"]),
        tool.Entry('a "quoted"\nline\twith tab', msgstr=['ein "zitat"\nzeile\tmit tab']),
        tool.Entry("{n} page", "{n} pages", ["{n} Seite", "{n} Seiten"]),
        tool.Entry("untranslated"),
    ]
    tool.write_po(po, tool._header("de"), entries)
    header, read = tool.read_po(po)
    assert "nplurals=2" in header
    assert [(e.msgid, e.plural, e.msgstr) for e in read.values()] == [
        (e.msgid, e.plural, e.msgstr) for e in entries]
    mo = tmp_path / "x.mo"
    mo.write_bytes(tool.mo_bytes(po))
    with open(mo, "rb") as f:
        t = gettext.GNUTranslations(f)
    assert t.gettext("Sign…") == "Signieren…"
    assert t.gettext('a "quoted"\nline\twith tab') == 'ein "zitat"\nzeile\tmit tab'
    assert t.ngettext("{n} page", "{n} pages", 1) == "{n} Seite"
    assert t.ngettext("{n} page", "{n} pages", 3) == "{n} Seiten"
    assert t.gettext("untranslated") == "untranslated"


def test_placeholder_mismatch_is_reported(tmp_path, monkeypatch):
    po = tmp_path / "de" / "LC_MESSAGES" / "pdfsigner.po"
    po.parent.mkdir(parents=True)
    tool.write_po(po, tool._header("de"), [
        tool.Entry("page {page} of {count}", msgstr=["Seite {seite} von {count}"]),
        tool.Entry("needs {{n}}", msgstr=["braucht {n}"])])
    monkeypatch.setattr(tool, "LOCALE", tmp_path)
    monkeypatch.setattr(tool, "updated_catalogs", lambda: {})
    found = tool.problems()
    assert any("page {page}" in p and "placeholders differ" in p for p in found)
    assert any("needs {{n}}" in p and "placeholders differ" in p for p in found)


def test_language_in_config(tmp_path):
    p = tmp_path / "c.toml"
    Config(path=p, language="de").save()
    assert load_config(p).language == "de"
    p.write_text('language = "fr"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="language must be one of en, de"):
        load_config(p)


def test_set_language_switches_and_falls_back():
    i18n.set_language("de")
    assert i18n.current() == "de"
    assert i18n._("no such message") == "no such message"
    i18n.set_language("fr")
    assert i18n.current() == "en"
