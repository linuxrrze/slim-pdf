"""GUI page tools: thumbnails, rotate, crop, undo/save, split, merge."""

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PySide6.QtCore import QItemSelectionModel  # noqa: E402

from pdfsigner import pages  # noqa: E402
from pdfsigner.gui import app as gui_app  # noqa: E402
from pdfsigner.gui import pagetools  # noqa: E402
from pdfsigner.signing import inspect_pdf  # noqa: E402

from conftest import make_pdf, sign_with, validate_all  # noqa: E402
# fixtures shared with test_gui.py
from test_gui import Boxes, drag_scene, qapp, save_to, secrets, window  # noqa: E402,F401


@pytest.fixture
def owned(qapp):
    """Destroy parentless dialogs while the QApplication still exists; left to
    Python's shutdown they get deleted after it, which crashes the process."""
    from shiboken6 import delete

    widgets = []
    yield widgets.append
    for w in widgets:
        delete(w)


def rotations(window):
    return inspect_pdf(window.data).page_rotation


def select_thumbs(window, *rows):
    window.thumbs.clearSelection()
    for r in rows:
        window.thumbs.setCurrentRow(r, QItemSelectionModel.Select)


def test_thumbnails_follow_navigation(window, blank_pdf):
    window.open(blank_pdf)
    assert window.thumbs.count() == 2
    assert window.thumbs.item(0).icon().isNull() is False
    window.thumbs.pageActivated.emit(1)
    assert window.view.page == 1 and window.page_spin.value() == 2
    window.view.go_to(0)
    assert window.thumbs.currentRow() == 0


def test_rotate_undo_save(window, blank_pdf):
    original = blank_pdf.read_bytes()
    window.open(blank_pdf)
    assert not window.save_act.isEnabled() and not window.undo_act.isEnabled()
    window.view.go_to(1)
    window.rotate_pages(90)
    assert rotations(window) == [0, 90]
    assert window.dirty and window.windowTitle().startswith("*")
    assert window.view.page == 1  # stays on the page
    assert window.save_act.isEnabled() and window.undo_act.isEnabled()
    assert blank_pdf.read_bytes() == original  # nothing written yet

    window.rotate_pages(90)
    window.undo()
    assert rotations(window) == [0, 90] and window.dirty
    window.undo()
    assert rotations(window) == [0, 0] and not window.dirty

    window.rotate_pages(-90)
    assert window.save()
    assert inspect_pdf(blank_pdf).page_rotation == [0, 270]
    assert not window.dirty and not window.windowTitle().startswith("*")


def test_rotate_selected_pages(window, tmp_path):
    src = make_pdf(tmp_path / "four.pdf", pages=4)
    window.open(src)
    select_thumbs(window, 1, 3)
    assert window.target_pages() == [1, 3]
    window.rotate_pages(180)
    assert rotations(window) == [0, 180, 0, 180]


def test_crop_by_dragging(window, blank_pdf, monkeypatch):
    window.open(blank_pdf)
    monkeypatch.setattr(gui_app.QInputDialog, "getItem",
                        staticmethod(lambda parent, title, label, items, *a: (items[0], True)))
    window.crop_act.trigger()
    assert window.view.crop_mode
    drag_scene(window.view, (100, 100), (400, 300))
    assert not window.view.crop_mode and not window.crop_act.isChecked()
    w, h = window.doc.display_size(0)
    assert (w, h) == pytest.approx((300, 200), abs=1)
    assert window.doc.display_size(1) == pytest.approx((595, 842))
    assert window.dirty

    window.reset_crop()
    assert window.doc.display_size(0) == pytest.approx((595, 842))


def test_crop_all_pages_and_escape(window, blank_pdf, monkeypatch):
    window.open(blank_pdf)
    monkeypatch.setattr(gui_app.QInputDialog, "getItem",
                        staticmethod(lambda parent, title, label, items, *a: ("All pages", True)))
    window.crop_act.trigger()
    window._escape()
    assert not window.view.crop_mode
    window.crop_act.trigger()
    drag_scene(window.view, (0, 0), (200, 100))
    assert [window.doc.display_size(i) for i in (0, 1)] == [pytest.approx((200, 100), abs=1)] * 2
    # a click without dragging doesn't crop
    before = window.data
    window.crop_act.trigger()
    drag_scene(window.view, (50, 50), (51, 51))
    assert window.data == before


def test_rotate_signed_document_asks(window, profile, form_pdf, tmp_path, monkeypatch):
    signed = tmp_path / "s.pdf"
    sign_with(profile, form_pdf, signed, field_name="Applicant")
    window.open(signed)
    boxes = Boxes(monkeypatch, gui_app, answer=QtWidgets.QMessageBox.No)
    window.rotate_pages(90)
    assert boxes.kinds() == ["warning"] and "Applicant" in boxes.text()
    assert rotations(window) == [0, 0]
    boxes.answer = QtWidgets.QMessageBox.Yes
    window.rotate_pages(90)
    assert rotations(window) == [90, 0]
    assert window.reports[0].status == "invalid"  # shown right away


def test_sign_edited_document(window, form_pdf, save_to, p12):
    window.open(form_pdf)
    window.view.go_to(1)
    window.rotate_pages(90)
    window.view.select_field("Applicant")
    window.sign()
    out = save_to["path"]
    assert inspect_pdf(out).page_rotation == [0, 90]
    assert inspect_pdf(out).field("Applicant").filled
    [(_, st)] = validate_all(out, p12[1])
    assert st.bottom_line
    assert window.path == out and not window.dirty and not window.undo_stack


def test_unsaved_changes_prompt(window, blank_pdf, tmp_path, monkeypatch):
    window.open(blank_pdf)
    window.rotate_pages(90)
    other = make_pdf(tmp_path / "other.pdf", pages=3)
    opened = []
    monkeypatch.setattr(gui_app.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (opened.append(1) or str(other), "")))
    boxes = Boxes(monkeypatch, gui_app, answer=QtWidgets.QMessageBox.Cancel)
    window.open_dialog()
    assert boxes.kinds() == ["question"] and not opened and window.dirty

    boxes.answer = QtWidgets.QMessageBox.Discard
    window.open_dialog()
    assert window.path == other and not window.dirty
    assert inspect_pdf(blank_pdf).page_rotation == [0, 0]  # discarded


def test_split_dialog(window, tmp_path, monkeypatch):
    src = make_pdf(tmp_path / "five.pdf", pages=5)
    window.open(src)

    def run_dialog(dlg):
        dlg.parts_edit.setText("1-2; 5")
        dlg.parts_radio.setChecked(True)
        dlg._accept()
        return True

    monkeypatch.setattr(pagetools.SplitDialog, "exec", run_dialog)
    boxes = Boxes(monkeypatch, gui_app)
    window.split_dialog()
    assert boxes.kinds() == ["information"]
    assert [pages.page_count((tmp_path / f"five-part{i}.pdf").read_bytes()) for i in (1, 2)] \
        == [2, 1]


def test_split_dialog_validation(owned, tmp_path):
    dlg = pagetools.SplitDialog(5, tmp_path / "x.pdf")
    owned(dlg)
    dlg.parts_radio.setChecked(True)
    dlg._accept()
    assert "at least one part" in dlg.error.text()
    dlg.parts_edit.setText("9")
    dlg._accept()
    assert "out of range" in dlg.error.text()
    dlg.every_radio.setChecked(True)
    dlg.pattern_edit.setText("x.pdf")
    dlg._accept()
    assert "{n}" in dlg.error.text()
    dlg.pattern_edit.setText("x-{n}.pdf")
    dlg.every_spin.setValue(2)
    dlg._accept()
    assert dlg.result_parts == [[0, 1], [2, 3], [4]]
    assert [p.name for p in dlg.result_paths] == ["x-1.pdf", "x-2.pdf", "x-3.pdf"]


def test_merge_dialog(window, blank_pdf, tmp_path, save_to, monkeypatch):
    other = make_pdf(tmp_path / "three.pdf", pages=3)
    window.open(blank_pdf)

    def run_dialog(dlg):
        assert dlg.inputs[0].label == "(current document)"
        assert dlg.add_file(other)
        dlg._refresh(1)
        dlg.spec_edit.setText("2-3")
        dlg._spec_edited("2-3")
        dlg.list.setCurrentRow(1)
        dlg._move(-1)  # other file first
        dlg._accept()
        return True

    monkeypatch.setattr(pagetools.MergeDialog, "exec", run_dialog)
    save_to["path"] = tmp_path / "merged.pdf"
    window.merge_dialog()
    assert window.path == tmp_path / "merged.pdf"
    assert len(window.doc) == 4  # 2 of three.pdf + 2 of blank.pdf


def test_merge_dialog_encrypted_input(owned, blank_pdf, tmp_path, monkeypatch):
    from conftest import make_encrypted

    enc = make_encrypted(blank_pdf, tmp_path / "e.pdf", "pw")
    monkeypatch.setattr(pagetools, "ask_secret", lambda parent, prompt: "pw")
    dlg = pagetools.MergeDialog()
    owned(dlg)
    assert dlg.add_file(enc)
    assert dlg.inputs[0].password == "pw" and dlg.inputs[0].page_count == 2
    monkeypatch.setattr(pagetools, "ask_secret", lambda parent, prompt: None)
    assert not dlg.add_file(enc)
    dlg.inputs[0].spec = "7"
    dlg._accept()
    assert "out of range" in dlg.error.text()
