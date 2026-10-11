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
    """Edit: the form; Signing: the Sign panel; other tabs: no panel. A
    document opens on the Edit tab."""
    window.show_tab(gui_app.SIGN_TAB)
    window.open(fill_pdf)
    dock, rb = window.side_dock, window.ribbon
    assert rb.currentIndex() == gui_app.EDIT_TAB and dock.isVisible()
    assert window.side_stack.currentWidget() is window.form_panel
    assert dock.windowTitle() == "Form"
    rb.setCurrentIndex(gui_app.SIGN_TAB)
    assert dock.isVisible() and window.side_stack.currentWidget() is window.sign_panel
    for tab in range(2, rb.count()):  # Pages, Help
        rb.setCurrentIndex(tab)
        assert not dock.isVisible()
    rb.setCurrentIndex(gui_app.SIGN_TAB)
    assert dock.isVisible() and dock.windowTitle() == "Sign"


def test_fill_in_and_undo(window, fill_pdf):
    """Each change in the panel is filled in at once, as one undo step."""
    window.open(fill_pdf)
    assert len(window.form_fields) == 8
    e = window.form_panel.editors
    assert not e["fixed"].isEnabled()  # read-only
    e["name"].setText("Ann Example")
    assert values(window)["name"] == ""  # text: once the editor is left
    e["name"].editingFinished.emit()
    assert values(window)["name"] == "Ann Example"
    e["agree"].setChecked(True)  # check boxes, radio buttons, choices: at once
    e["size"].buttons["L"].setChecked(True)
    e["country"].setCurrentIndex(e["country"].findText("France"))
    e["country"].activated.emit(e["country"].currentIndex())
    assert values(window) == {
        "name": "Ann Example", "address": "", "agree": True, "size": "L",
        "country": "fr", "colour": "", "fixed": "constant", "person.first": ""}
    assert window.dirty and len(window.undo_stack) == 4
    assert "Filled in 1 field" in window.statusBar().currentMessage()
    window.undo()
    assert values(window)["country"] == "" and e["country"].currentData() == ""
    assert e["name"].text() == "Ann Example"  # the panel shows the document


def test_keyboard_only(window, fill_pdf, qapp):
    """Typing and Tab, Space on a check box: no mouse needed."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window.open(fill_pdf)
    window.show_form_field("name")
    e = window.form_panel.editors
    QTest.keyClicks(e["name"], "Bo")
    QTest.keyClick(e["name"], Qt.Key_Tab)  # leaves it: filled in
    assert values(window)["name"] == "Bo"
    assert window.focusWidget() is e["address"]  # the cursor moved on, not lost
    QTest.keyClicks(e["address"], "Line")
    QTest.keyClick(e["address"], Qt.Key_Tab)
    assert values(window)["address"] == "Line"
    QTest.keyClick(e["agree"], Qt.Key_Space)
    assert values(window)["agree"] is True


def click_field(window, name, index=0):
    items = [i for i in window.view.scene().items() if isinstance(i, FormItem) and i.name == name]
    items.sort(key=lambda i: i.rect().x())
    centre = items[index].rect().center()
    click_scene(window.view, centre.x(), centre.y())


def test_click_on_the_page_fills_in(window, fill_pdf, qapp, monkeypatch):
    """Text is typed over the field, a check box toggles, a radio button is
    chosen, a choice comes from a menu."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window.open(fill_pdf)
    qapp.processEvents()
    click_field(window, "name")
    assert window.ribbon.currentIndex() == gui_app.EDIT_TAB
    editor = window.view.inline_editor
    assert editor is not None and editor.hasFocus()
    QTest.keyClicks(editor, "Zoe")
    QTest.keyClick(editor, Qt.Key_Return)
    qapp.processEvents()
    assert values(window)["name"] == "Zoe" and window.view.inline_editor is None
    assert window.form_panel.editors["name"].text() == "Zoe"

    click_field(window, "name")  # Esc cancels
    QTest.keyClicks(window.view.inline_editor, "x")
    QTest.keyClick(window.view.inline_editor, Qt.Key_Escape)
    qapp.processEvents()
    assert values(window)["name"] == "Zoe"

    click_field(window, "agree")
    assert values(window)["agree"] is True
    click_field(window, "agree")
    assert values(window)["agree"] is False
    click_field(window, "size", 1)  # the second button: L
    assert values(window)["size"] == "L"

    def pick(menu, pos):
        [france] = [a for a in menu.actions() if a.text() == "France"]
        return france

    monkeypatch.setattr(window, "_exec_choice_menu", pick)
    click_field(window, "country")
    assert values(window)["country"] == "fr"
    assert window.view.active_form_field == "country"  # still marked


def test_read_only_fields_are_shown_not_changed(window, fill_pdf, qapp):
    window.open(fill_pdf)
    qapp.processEvents()
    window.show_tab(gui_app.SIGN_TAB)
    click_field(window, "fixed")
    assert window.ribbon.currentIndex() == gui_app.EDIT_TAB  # shown in the form
    assert window.view.active_form_field == "fixed"
    assert window.view.inline_editor is None and not window.dirty
    assert "read-only" in window.statusBar().currentMessage()


@pytest.mark.parametrize("name", ["name", "agree", "size", "country", "colour"])
def test_any_click_on_a_form_field_switches_to_edit(window, fill_pdf, qapp, monkeypatch,
                                                    name):
    monkeypatch.setattr(window, "_exec_choice_menu", lambda menu, pos: None)
    window.open(fill_pdf)
    qapp.processEvents()
    for tab in (gui_app.SIGN_TAB, 2, 3):  # Signing, Pages, Help
        window.show_tab(tab)
        click_field(window, name)
        assert window.ribbon.currentIndex() == gui_app.EDIT_TAB, (name, tab)
        if window.view.inline_editor is not None:
            window.view.finish_text_edit(False)


def test_no_form(window, blank_pdf):
    assert "Open a PDF" in window.form_panel.hint.text()
    window.open(blank_pdf)
    assert "no form fields" in window.form_panel.hint.text()
    assert not window.form_panel.scroll.isVisibleTo(window.form_panel)


def test_errors_are_shown(window, fill_pdf, monkeypatch):
    window.open(fill_pdf)
    boxes = Boxes(monkeypatch, gui_app)
    window.form_panel.editors["name"].setText("漢字")
    window.form_panel.editors["name"].editingFinished.emit()
    assert boxes.kinds() == ["critical"] and "can't show" in boxes.text()
    assert not window.dirty
    assert window.form_panel.editors["name"].text() == ""  # back to the document's value


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
    panel.editors["name"].editingFinished.emit()
    assert boxes.kinds() == [] and values(window)["name"] == "Ann"
    assert "then changed" in window.sigs_list.item(0).text()

    panel.editors["size"].buttons["S"].setChecked(True)
    assert boxes.kinds() == ["warning"] and "Signer" in boxes.text()
    assert values(window)["size"] == ""  # not filled in
    assert panel.editors["size"].value == ""  # and not shown as if it were


def test_signing_shows_the_sign_panel(window, blank_pdf):
    window.open(blank_pdf)
    window.ribbon.setCurrentIndex(gui_app.EDIT_TAB)
    window.sign_act.trigger()  # Ctrl+S from another tab: placing starts on Home
    assert window.ribbon.currentIndex() == gui_app.SIGN_TAB and window.view.place_mode
    assert window.side_stack.currentWidget() is window.sign_panel


def test_radio_buttons_are_labelled_with_the_page_text(window, tmp_path, qapp):
    """Radio buttons show the words printed next to them on the page, not
    their state names."""
    from conftest import add_form_fields, make_pdf
    from slim_pdf import insert

    # text that reads back the same everywhere (Qt's PDF writer may draw
    # text as shapes, e.g. with a substituted font on Windows); the size
    # buttons are at x 50..64 and 80..94, y 620..634
    base = make_pdf(tmp_path / "base.pdf", pages=1)
    data = insert.add_text(base.read_bytes(), 0, (64, 618, 80, 636), "Sm", size=7)
    data = insert.add_text(data, 0, (94, 618, 140, 636), "Large", size=9)
    base.write_bytes(data)
    window.open(add_form_fields(base, tmp_path / "form.pdf"))
    size = window.form_panel.editors["size"]
    assert {s: b.text() for s, b in size.buttons.items()} == {"S": "Sm", "L": "Large"}
    agree = window.form_panel.editors["agree"]
    assert isinstance(agree, QtWidgets.QCheckBox)  # a real check box stays one


def test_editing_in_the_panel_shows_the_field_on_the_page(window, tmp_path, qapp):
    """The cursor in a field's editor: the view goes to its page and marks
    it; leaving the Edit tab removes the mark."""
    from conftest import add_form_fields, make_pdf

    pdf = add_form_fields(make_pdf(tmp_path / "b.pdf", pages=2), tmp_path / "f.pdf", page=1)
    window.open(pdf)
    window.show_tab(gui_app.EDIT_TAB)
    assert window.view.page == 0
    window.form_panel.editors["address"].setFocus()
    qapp.processEvents()
    assert window.view.page == 1
    marked = [i.name for i in window.view.scene().items()
              if isinstance(i, FormItem) and i.active]
    assert marked == ["address"]
    window.form_panel.editors["size"].buttons["L"].setFocus()  # a radio button, too
    qapp.processEvents()
    assert window.view.active_form_field == "size"
    window.show_tab(gui_app.SIGN_TAB)
    assert not any(i.active for i in window.view.scene().items() if isinstance(i, FormItem))


def test_tab_moves_on_while_typing_on_the_page(window, fill_pdf, qapp):
    """Tab fills in the field and goes to the next one: text fields are
    typed into on the page again, the others get the cursor in the panel;
    Shift+Tab goes back, and the end wraps round to the start."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window.open(fill_pdf)
    qapp.processEvents()
    click_field(window, "name")
    QTest.keyClicks(window.view.inline_editor, "Ann")
    QTest.keyClick(window.view.inline_editor, Qt.Key_Tab)
    qapp.processEvents()
    assert values(window)["name"] == "Ann"
    editor = window.view.inline_editor  # now over the address field
    assert editor is not None and window.view.active_form_field == "address"
    QTest.keyClicks(editor, "Street")
    QTest.keyClick(editor, Qt.Key_Tab)
    qapp.processEvents()
    assert values(window)["address"] == "Street"
    assert window.view.inline_editor is None  # a check box: the panel has it
    assert window.focusWidget() is window.form_panel.editors["agree"]
    assert window.view.active_form_field == "agree"
    QTest.keyClick(window.focusWidget(), Qt.Key_Space)  # toggles it
    assert values(window)["agree"] is True

    click_field(window, "person.first")  # the last fillable field
    QTest.keyClick(window.view.inline_editor, Qt.Key_Tab)
    qapp.processEvents()
    assert window.view.active_form_field == "name"  # round to the first
    QTest.keyClick(window.view.inline_editor, Qt.Key_Backtab, Qt.ShiftModifier)
    qapp.processEvents()
    assert window.view.active_form_field == "person.first"
    QTest.keyClick(window.view.inline_editor, Qt.Key_Backtab, Qt.ShiftModifier)
    qapp.processEvents()
    assert window.focusWidget() is window.form_panel.editors["colour"]  # "fixed" is skipped


def test_field_labels_describe_the_input(window, fill_pdf):
    """Hovering over a field's name tells what it takes."""
    window.open(fill_pdf)
    panel = window.form_panel

    def tip(name):
        return panel.form_layout.labelForField(panel.editors[name]).toolTip()

    assert tip("name").splitlines() == ["Field: name", "Label: Your name",
                                        "Input: text, one line", "Page 1, 250 × 20 pt"]
    assert "Input: text, several lines" in tip("address")
    assert "Input: check box" in tip("agree")
    assert "Input: radio buttons (S, L)" in tip("size")
    assert "Input: drop-down with 2 choices" in tip("country")
    assert "Input: list with 3 choices" in tip("colour")
    assert "Read-only" in tip("fixed")
