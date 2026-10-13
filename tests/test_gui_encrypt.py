"""GUI: encrypting a document with a password (Edit tab)."""

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from slim_pdf.gui import app as gui_app  # noqa: E402
from slim_pdf.gui import dialogs  # noqa: E402
from slim_pdf.signing import SigningError, inspect_pdf  # noqa: E402

from conftest import make_encrypted, make_pdf, sign_with  # noqa: E402
# fixtures shared with test_gui.py
from test_gui import Boxes, qapp, save_to, secrets, window  # noqa: E402,F401


def new_password(monkeypatch, answer):
    asked = []

    def ask(parent, encrypted):
        asked.append(encrypted)
        return answer

    monkeypatch.setattr(gui_app, "ask_new_password", ask)
    return asked


def test_encrypt_button_in_the_edit_tab(window, tmp_path):
    buttons = [b.defaultAction() for b in
               window.ribbon.tab_page(0).findChildren(QtWidgets.QToolButton)]
    assert window.encrypt_act in buttons
    assert not window.encrypt_act.isEnabled()  # no document
    window.open(make_pdf(tmp_path / "a.pdf", pages=1))
    assert window.encrypt_act.isEnabled()


def test_encrypt_save_and_undo(window, tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "a.pdf", pages=2)
    window.open(src)
    asked = new_password(monkeypatch, "secret")
    window.encrypt_act.trigger()
    assert asked == [False]  # not encrypted yet
    assert window.dirty and window.pdf_password == "secret"
    assert inspect_pdf(window.data, "secret").encrypted and len(window.doc) == 2
    window.save()
    with pytest.raises(SigningError, match="password"):
        inspect_pdf(src)
    assert inspect_pdf(src, "secret").encrypted
    # undo brings back the document without a password
    window.undo()
    assert window.pdf_password is None and not inspect_pdf(window.data).encrypted


def test_encrypted_document_gets_a_new_password(window, tmp_path, monkeypatch):
    enc = make_encrypted(make_pdf(tmp_path / "a.pdf", pages=1), tmp_path / "e.pdf", "old")
    monkeypatch.setattr(gui_app, "ask_secret", lambda parent, prompt: "old")
    window.open(enc)
    asked = new_password(monkeypatch, "new")
    window.encrypt_act.trigger()
    assert asked == [True] and window.pdf_password == "new"
    assert inspect_pdf(window.data, "new").encrypted
    with pytest.raises(SigningError, match="password"):
        inspect_pdf(window.data, "old")
    window.undo()
    assert window.pdf_password == "old" and inspect_pdf(window.data, "old").encrypted


def test_cancel_changes_nothing(window, tmp_path, monkeypatch):
    window.open(make_pdf(tmp_path / "a.pdf", pages=1))
    before = window.data
    new_password(monkeypatch, None)
    window.encrypt_act.trigger()
    assert window.data is before and not window.dirty and window.pdf_password is None


def test_signed_document_asks_first(window, profile, form_pdf, tmp_path, monkeypatch):
    signed = tmp_path / "s.pdf"
    sign_with(profile, form_pdf, signed, field_name="Applicant")
    window.open(signed)
    asked = new_password(monkeypatch, "secret")
    boxes = Boxes(monkeypatch, gui_app, answer=gui_app.QMessageBox.No)
    window.encrypt_act.trigger()
    assert boxes.kinds() == ["warning"] and "Applicant" in boxes.text()
    assert asked == [] and not window.dirty  # declined: not even asked for a password
    boxes.answer = gui_app.QMessageBox.Yes
    window.encrypt_act.trigger()
    assert asked == [False] and window.dirty
    names = [f.name for f in inspect_pdf(window.data, "secret").fields]
    assert "Applicant" not in names and "Supervisor" in names  # signed one removed


def test_new_password_dialog(qapp):
    from shiboken6 import delete

    dialog = dialogs.NewPasswordDialog(None, encrypted=False)
    try:
        ok = dialog.buttons.button(QtWidgets.QDialogButtonBox.Ok)
        assert not ok.isEnabled()
        dialog.first.setText("secret")
        assert not ok.isEnabled() and not dialog.problem.text()
        dialog.second.setText("secrte")
        assert not ok.isEnabled() and "don't match" in dialog.problem.text()
        dialog.second.setText("secret")
        assert ok.isEnabled() and not dialog.problem.text()
        assert dialog.password() == "secret"
        assert dialog.first.echoMode() == QtWidgets.QLineEdit.Password
    finally:
        delete(dialog)
