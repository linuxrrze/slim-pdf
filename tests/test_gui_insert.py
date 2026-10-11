"""GUI: adding text and images to pages (Edit tab)."""

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from slim_pdf.gui import app as gui_app  # noqa: E402
from slim_pdf.gui import insertdialog  # noqa: E402
from slim_pdf.gui.insertdialog import TextDialog  # noqa: E402

from conftest import (  # noqa: E402
    blue_bbox,
    draw_signature_image,
    make_pdf,
    render_page,
    sign_with,
    text_bbox,
)
# fixtures shared with test_gui.py
from test_gui import Boxes, click_scene, drag_scene, qapp, save_to, secrets, window  # noqa: E402,F401


def rendered(window, tmp_path):
    out = tmp_path / "shown.pdf"
    out.write_bytes(window.data)
    return render_page(out, window.view.page)


def typing(window, monkeypatch, text, size=20.0, accept=True):
    def exec_text(dlg):
        dlg.edit.setPlainText(text)
        dlg.size_spin.setValue(size)
        if accept:
            dlg._check()
        return dlg.result()

    monkeypatch.setattr(window, "_exec_text", exec_text)


def test_buttons_are_in_the_edit_tab(window):
    buttons = [b.defaultAction() for b in
               window.ribbon.tab_page(1).findChildren(QtWidgets.QToolButton)]
    assert window.insert_text_act in buttons and window.insert_image_act in buttons
    assert not window.insert_text_act.isEnabled()  # no document


def test_drag_a_box_for_text(window, tmp_path, monkeypatch):
    window.open(make_pdf(tmp_path / "in.pdf", pages=1))
    typing(window, monkeypatch, "Hello World")
    window.insert_text_act.trigger()
    assert window.view.insert_mode and "Drag a box" in window.statusBar().currentMessage()
    drag_scene(window.view, (100, 100), (400, 160))
    assert not window.view.insert_mode and window.dirty
    assert "Added text to page 1" in window.statusBar().currentMessage()
    x1, y1, x2, y2 = text_bbox(rendered(window, tmp_path))
    assert 100 <= x1 < 110 and 100 <= y1 < 110 and x2 < 400
    window.undo()
    assert text_bbox(rendered(window, tmp_path)) is None


def test_click_for_an_image(window, tmp_path, monkeypatch):
    window.open(make_pdf(tmp_path / "in.pdf", pages=1))
    img = draw_signature_image(tmp_path / "sig.png")  # 3:1
    monkeypatch.setattr(gui_app.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(img), "")))
    window.insert_image_act.trigger()
    click_scene(window.view, 300, 400)  # the image's centre, 150 pt wide
    x1, y1, x2, y2 = blue_bbox(rendered(window, tmp_path))
    assert 225 <= x1 and x2 <= 375 and 375 <= y1 and y2 <= 425
    assert "Added an image to page 1" in window.statusBar().currentMessage()


def test_escape_cancels(window, tmp_path, monkeypatch):
    window.open(make_pdf(tmp_path / "in.pdf", pages=1))
    typing(window, monkeypatch, "x")
    window.insert_text()
    window._escape()
    assert not window.view.insert_mode
    click_scene(window.view, 100, 100)
    assert not window.dirty


def test_text_dialog_refuses_what_it_cannot_show(qapp, monkeypatch):
    boxes = Boxes(monkeypatch, insertdialog)
    dlg = TextDialog()
    dlg.edit.setPlainText("漢字")
    dlg._check()
    assert boxes.kinds() == ["warning"] and not dlg.result()
    dlg.edit.setPlainText("   ")
    dlg._check()
    assert boxes.kinds() == ["warning", "warning"]
    dlg.edit.setPlainText("Grüße, 5 €")
    dlg._check()
    assert dlg.result()
    from shiboken6 import delete

    delete(dlg)


def test_signed_document_asks_first(window, tmp_path, monkeypatch, profile):
    profile.secret = "none"
    signed = tmp_path / "signed.pdf"
    sign_with(profile, make_pdf(tmp_path / "in.pdf", pages=1), signed, page=0,
              box=(300, 100, 500, 150))
    window.open(signed)
    boxes = Boxes(monkeypatch, gui_app, answer=QtWidgets.QMessageBox.No)
    typing(window, monkeypatch, "Later")
    window.insert_text()
    drag_scene(window.view, (100, 100), (400, 160))
    assert boxes.kinds() == ["warning"] and "Signature1" in boxes.text()
    assert not window.dirty
