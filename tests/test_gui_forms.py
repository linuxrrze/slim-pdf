"""GUI: filling in form fields."""

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from pdfsigner.gui import app as gui_app  # noqa: E402
from pdfsigner.gui.pageview import FormItem  # noqa: E402

from conftest import add_sig_field, sign_with  # noqa: E402
# fixtures shared with test_gui.py
from test_gui import Boxes, click_scene, qapp, save_to, secrets, window  # noqa: E402,F401


def values(window):
    return {f.name: f.value for f in window.form_fields}


def filling(window, monkeypatch, fill, accept=True):
    """Let the next form dialog be filled in by ``fill(dialog)``; returns the
    dialogs opened."""
    opened = []

    def exec_form(dlg):
        opened.append(dlg)
        fill(dlg)
        return accept

    monkeypatch.setattr(window, "_exec_form", exec_form)
    return opened


def test_fill_in_and_undo(window, fill_pdf, monkeypatch):
    window.open(fill_pdf)
    assert len(window.form_fields) == 8
    edit = window.ribbon.tab_page(1)
    assert window.ribbon.tabText(1) == "Edit"
    assert window.fill_form_act in [b.defaultAction()
                                    for b in edit.findChildren(QtWidgets.QToolButton)]

    def fill(dlg):
        e = dlg.editors
        assert not e["fixed"].isEnabled()  # read-only
        e["name"].setText("Ann Example")
        e["address"].setPlainText("Street 1\nTown")
        e["agree"].setChecked(True)
        e["size"].setCurrentIndex(e["size"].findData("L"))
        e["country"].setCurrentIndex(e["country"].findText("France"))

    filling(window, monkeypatch, fill)
    window.fill_form_act.trigger()
    assert values(window) == {
        "name": "Ann Example", "address": "Street 1\nTown", "agree": True, "size": "L",
        "country": "fr", "colour": "", "fixed": "constant", "person.first": ""}
    assert window.dirty and "Filled in 5 fields" in window.statusBar().currentMessage()
    window.undo()
    assert values(window)["name"] == ""


def test_unchanged_dialog_changes_nothing(window, fill_pdf, monkeypatch):
    window.open(fill_pdf)
    filling(window, monkeypatch, lambda dlg: None)
    window.fill_form()
    assert not window.dirty and not window.undo_stack
    filling(window, monkeypatch, lambda dlg: dlg.editors["name"].setText("x"), accept=False)
    window.fill_form()
    assert not window.dirty


def test_click_on_a_field_opens_it(window, fill_pdf, monkeypatch, qapp):
    window.open(fill_pdf)
    items = [i for i in window.view.scene().items() if isinstance(i, FormItem)]
    assert {i.name for i in items} == {f.name for f in window.form_fields}
    assert not next(i for i in items if i.name == "fixed").fillable
    opened = filling(window, monkeypatch, lambda dlg: None)
    qapp.processEvents()
    name = next(i for i in items if i.name == "address").rect().center()
    click_scene(window.view, name.x(), name.y())
    [dlg] = opened
    assert dlg.editors["address"].hasFocus() or dlg.focusWidget() is dlg.editors["address"]


def test_no_form(window, blank_pdf, monkeypatch):
    window.open(blank_pdf)
    boxes = Boxes(monkeypatch, gui_app)
    window.fill_form()
    assert boxes.kinds() == ["information"] and "no form fields" in boxes.text()


def test_errors_are_shown(window, fill_pdf, monkeypatch):
    window.open(fill_pdf)
    boxes = Boxes(monkeypatch, gui_app)
    filling(window, monkeypatch, lambda dlg: dlg.editors["name"].setText("漢字"))
    window.fill_form()
    assert boxes.kinds() == ["critical"] and "can't show" in boxes.text()
    assert not window.dirty


def test_signatures_are_checked_first(window, fill_pdf, profile, tmp_path, monkeypatch):
    """Text fields can be filled in after signing; radio buttons (separate
    widgets) would break the signature, so the user is asked."""
    profile.secret = "none"
    with_sig = add_sig_field(fill_pdf, tmp_path / "sig.pdf", "Signer", 0, (300, 100, 500, 150))
    signed = tmp_path / "signed.pdf"
    sign_with(profile, with_sig, signed, field_name="Signer")
    window.open(signed)
    boxes = Boxes(monkeypatch, gui_app, answer=QtWidgets.QMessageBox.No)

    filling(window, monkeypatch, lambda dlg: dlg.editors["name"].setText("Ann"))
    window.fill_form()
    assert boxes.kinds() == [] and values(window)["name"] == "Ann"
    assert "then changed" in window.sigs_list.item(0).text()

    def radio(dlg):
        dlg.editors["size"].setCurrentIndex(dlg.editors["size"].findData("S"))

    filling(window, monkeypatch, radio)
    window.fill_form()
    assert boxes.kinds() == ["warning"] and "Signer" in boxes.text()
    assert values(window)["size"] == ""  # not filled in
