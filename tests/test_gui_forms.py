"""GUI: filling in form fields (the form panel, Edit tab)."""

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from slim_pdf.gui import app as gui_app  # noqa: E402
from slim_pdf.gui.pageview import FormItem  # noqa: E402

from conftest import add_sig_field, sign_with  # noqa: E402
# fixtures shared with test_gui.py
from test_gui import Boxes, click_scene, qapp, save_to, secrets, window  # noqa: E402,F401


def values(window):
    return {f.name: f.value for f in window.form_fields}


def test_side_panel_follows_the_ribbon_tab(window, fill_pdf):
    """Home: the Sign panel; Edit: the form; other tabs: no panel."""
    window.open(fill_pdf)
    dock, rb = window.side_dock, window.ribbon
    assert rb.currentIndex() == gui_app.HOME_TAB and dock.isVisible()
    assert window.side_stack.currentWidget() is window.sign_panel
    rb.setCurrentIndex(gui_app.EDIT_TAB)
    assert dock.isVisible() and window.side_stack.currentWidget() is window.form_panel
    assert dock.windowTitle() == "Form"
    for tab in range(2, rb.count()):  # Pages, Help
        rb.setCurrentIndex(tab)
        assert not dock.isVisible()
    rb.setCurrentIndex(gui_app.HOME_TAB)
    assert dock.isVisible() and dock.windowTitle() == "Sign"


def test_fill_in_and_undo(window, fill_pdf):
    window.open(fill_pdf)
    assert len(window.form_fields) == 8
    panel = window.form_panel
    e = panel.editors
    assert not e["fixed"].isEnabled()  # read-only
    assert not panel.apply_btn.isEnabled()  # nothing changed yet
    e["name"].setText("Ann Example")
    e["address"].setPlainText("Street 1\nTown")
    e["agree"].setChecked(True)
    e["size"].setCurrentIndex(e["size"].findData("L"))
    e["country"].setCurrentIndex(e["country"].findText("France"))
    assert panel.apply_btn.isEnabled()
    panel.apply_btn.click()
    assert values(window) == {
        "name": "Ann Example", "address": "Street 1\nTown", "agree": True, "size": "L",
        "country": "fr", "colour": "", "fixed": "constant", "person.first": ""}
    assert window.dirty and "Filled in 5 fields" in window.statusBar().currentMessage()
    assert panel.editors["name"].text() == "Ann Example"  # the panel shows the document
    assert not panel.apply_btn.isEnabled()
    window.undo()
    assert values(window)["name"] == "" and panel.editors["name"].text() == ""


def test_discard(window, fill_pdf):
    window.open(fill_pdf)
    panel = window.form_panel
    panel.editors["name"].setText("x")
    panel.discard_btn.click()
    assert panel.editors["name"].text() == "" and not panel.apply_btn.isEnabled()
    assert not window.dirty and not window.undo_stack


def test_click_on_a_field_opens_it(window, fill_pdf, qapp):
    window.open(fill_pdf)
    items = [i for i in window.view.scene().items() if isinstance(i, FormItem)]
    assert {i.name for i in items} == {f.name for f in window.form_fields}
    assert not next(i for i in items if i.name == "fixed").fillable
    qapp.processEvents()
    centre = next(i for i in items if i.name == "address").rect().center()
    click_scene(window.view, centre.x(), centre.y())
    assert window.ribbon.currentIndex() == gui_app.EDIT_TAB
    assert window.focusWidget() is window.form_panel.editors["address"]


def test_no_form(window, blank_pdf):
    assert "Open a PDF" in window.form_panel.hint.text()
    window.open(blank_pdf)
    assert "no form fields" in window.form_panel.hint.text()
    assert not window.form_panel.scroll.isVisibleTo(window.form_panel)


def test_errors_are_shown(window, fill_pdf, monkeypatch):
    window.open(fill_pdf)
    boxes = Boxes(monkeypatch, gui_app)
    window.form_panel.editors["name"].setText("漢字")
    window.form_panel.apply_btn.click()
    assert boxes.kinds() == ["critical"] and "can't show" in boxes.text()
    assert not window.dirty
    assert window.form_panel.editors["name"].text() == "漢字"  # kept for correcting


def test_signatures_are_checked_first(window, fill_pdf, profile, tmp_path, monkeypatch):
    """Text fields can be filled in after signing; radio buttons (separate
    widgets) would break the signature, so the user is asked."""
    profile.secret = "none"
    with_sig = add_sig_field(fill_pdf, tmp_path / "sig.pdf", "Signer", 0, (300, 100, 500, 150))
    signed = tmp_path / "signed.pdf"
    sign_with(profile, with_sig, signed, field_name="Signer")
    window.open(signed)
    boxes = Boxes(monkeypatch, gui_app, answer=QtWidgets.QMessageBox.No)
    panel = window.form_panel

    panel.editors["name"].setText("Ann")
    panel.apply_btn.click()
    assert boxes.kinds() == [] and values(window)["name"] == "Ann"
    assert "then changed" in window.sigs_list.item(0).text()

    panel.editors["size"].setCurrentIndex(panel.editors["size"].findData("S"))
    panel.apply_btn.click()
    assert boxes.kinds() == ["warning"] and "Signer" in boxes.text()
    assert values(window)["size"] == ""  # not filled in


def test_signing_shows_the_sign_panel(window, blank_pdf):
    window.open(blank_pdf)
    window.ribbon.setCurrentIndex(gui_app.EDIT_TAB)
    window.sign_act.trigger()  # Ctrl+S from another tab: placing starts on Home
    assert window.ribbon.currentIndex() == gui_app.HOME_TAB and window.view.place_mode
    assert window.side_stack.currentWidget() is window.sign_panel
