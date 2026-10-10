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


# --- German output ----------------------------------------------------------------------


@pytest.fixture
def german(monkeypatch):
    monkeypatch.setenv("PDFSIGNER_LANG", "de")
    i18n.set_language("de")


def test_cli_help_and_errors_in_german(german, cfg_path, tmp_path, capsys):
    from pdfsigner import cli

    with pytest.raises(SystemExit):
        cli.main(["sign", "--help"])
    out = capsys.readouterr().out
    assert out.startswith("Aufruf: pdfsigner sign")
    assert "Optionen:" in out and "diese Hilfe anzeigen und beenden" in out
    assert cli.main(["fields", str(tmp_path / "missing.pdf")]) == 1
    assert capsys.readouterr().err.startswith("Fehler: ")
    with pytest.raises(SystemExit):
        cli.main(["rotate"])
    assert "Fehler: die folgenden Argumente sind erforderlich" in capsys.readouterr().err


def test_cli_verify_in_german(german, profile, form_pdf, tmp_path, cfg_path, capsys):
    from pdfsigner import cli

    from conftest import sign_with

    signed = tmp_path / "s.pdf"
    sign_with(profile, form_pdf, signed, field_name="Applicant")
    assert cli.main(["verify", str(signed)]) == 4
    out = capsys.readouterr().out
    assert out.startswith("Applicant: NICHT VERTRAUENSWÜRDIG")
    assert "Unterzeichner:" in out and "signer:" not in out


def test_language_from_config_for_cli(cfg_path, monkeypatch, capsys):
    from pdfsigner import cli

    monkeypatch.delenv("PDFSIGNER_LANG")
    Config(path=cfg_path, language="de").save()
    assert cli.main(["certs", "list"]) == 0
    assert capsys.readouterr().out.startswith("keine Zertifikate konfiguriert")


def test_new_profiles_get_the_stamp_text_in_the_current_language(german, cfg_path, p12):
    from pdfsigner import cli
    from pdfsigner.config import DEFAULT_STAMP_TEXT

    assert cli.main(["certs", "add", "w", "--p12", str(p12[0])]) == 0
    saved = load_config(cfg_path).certs["w"]
    assert saved.stamp_text == "Digital signiert von %(signer)s\n%(ts)s"
    assert DEFAULT_STAMP_TEXT.startswith("Digitally signed")  # stored default unchanged


def test_gui_in_german(german, qapp_de):
    from PySide6.QtWidgets import QDialogButtonBox

    from pdfsigner.gui import app as gui_app
    from pdfsigner.gui.pagetools import SplitDialog

    win = gui_app.MainWindow(Config())
    dlg = None
    try:
        assert win.sign_btn.text() == "Signieren…"
        assert win.save_as_act.text() == "Speichern unter…"
        # Qt's own strings come from its German catalog
        dlg = SplitDialog(3, Path("doc.pdf"))
        box = dlg.findChild(QDialogButtonBox)
        assert box.button(QDialogButtonBox.Cancel).text().replace("&", "") == "Abbrechen"
        # page tools wrap to a second toolbar row exactly when they don't fit
        need = win.main_toolbar.sizeHint().width() + win.pages_toolbar.sizeHint().width() + 16
        for width in (700, 2400):  # the platform may limit the size (screen width)
            win.resize(width, 600)
            win._arrange_toolbars()
            assert win.toolBarBreak(win.pages_toolbar) == (need > win.width())
        win.resize(700, 600)
        win._arrange_toolbars()
        assert win.toolBarBreak(win.pages_toolbar)  # the German toolbars never fit in 700 px
    finally:
        win.close()
        from shiboken6 import delete

        delete(win)  # not after the QApplication at exit
        if dlg is not None:
            delete(dlg)


@pytest.fixture
def qapp_de():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    from pdfsigner.gui.app import setup_language

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    assert setup_language(app, None) == "de"
    yield app
    setup_language(app, "en")
