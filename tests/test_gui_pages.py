"""GUI page tools: thumbnails, rotate, crop, undo/save, split, merge."""

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PySide6.QtCore import QItemSelectionModel, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from pdfsigner.gui import app as gui_app  # noqa: E402
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


def test_thumbnails_are_rendered_in_the_background(window, tmp_path, qapp, monkeypatch):
    """A long document opens with placeholders; the thumbnails follow in
    batches between events, the current page first."""
    from conftest import make_pdf

    pdf = make_pdf(tmp_path / "long.pdf", pages=60)
    order = []
    real = window.thumbs._next_page

    def next_page():
        order.append(real())
        return order[-1]

    monkeypatch.setattr(window.thumbs, "_next_page", next_page)
    window.open(pdf, page=40)
    assert window.thumbs.count() == 60
    assert len(window.thumbs._pending) == 60 and order == []  # nothing rendered yet
    qapp.processEvents()
    assert order[0] == 40  # the current page first
    window.thumbs.finish_thumbnails()
    assert sorted(order) == list(range(60))  # each page once
    assert not window.thumbs._pending and not window.thumbs._timer.isActive()
    window.open(pdf)  # a new document starts over
    assert len(window.thumbs._pending) == 60


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
    pixel = 1 / window.view.transform().m11()  # each end of the drag is a whole pixel
    assert (w, h) == pytest.approx((300, 200), abs=2 * pixel + 0.01)
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
    pixel = 1 / window.view.transform().m11()
    assert [window.doc.display_size(i) for i in (0, 1)] == [
        pytest.approx((200, 100), abs=2 * pixel + 0.01)] * 2
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


def page_widths(window):
    return [round(window.doc.display_size(i)[0]) for i in range(len(window.doc))]


def numbered(tmp_path, n=5):
    """n pages; page i (1-based) is 100 + i*10 pt wide."""
    import io

    from pypdf import PdfWriter

    w = PdfWriter()
    for i in range(1, n + 1):
        w.add_blank_page(100 + i * 10, 200)
    buf = io.BytesIO()
    w.write(buf)
    path = tmp_path / "numbered.pdf"
    path.write_bytes(buf.getvalue())
    return path


def test_page_tools_are_in_the_context_menu(window, blank_pdf):
    window.open(blank_pdf)
    texts = [a.text() for a in window.page_menu().actions() if not a.isSeparator()]
    assert texts == ["Rotate left", "Rotate right", "Crop…", "Uncrop", "Delete page 1"]
    toolbar = [a.text() for a in window.pages_toolbar.actions() if not a.isSeparator()]
    assert toolbar == ["Save", "Save as…", "Undo"]  # editing moved out of the toolbar
    assert window.rotate_left_act.shortcut().toString() == "Ctrl+L"  # shortcuts still work


def test_right_click_selects_the_page_and_opens_the_menu(window, tmp_path, monkeypatch):
    window.open(numbered(tmp_path))
    shown = []
    monkeypatch.setattr(window, "_exec_menu", lambda menu, pos: shown.append(
        [a.text() for a in menu.actions() if not a.isSeparator()]))
    thumbs = window.thumbs
    rect = thumbs.visualItemRect(thumbs.item(2))
    thumbs.customContextMenuRequested.emit(rect.center())  # right-click on page 3
    assert thumbs.selected_pages() == [2] and window.view.page == 2
    assert shown[-1][-1] == "Delete page 3"
    select_thumbs(window, 0, 2, 3)
    rect = thumbs.visualItemRect(thumbs.item(3))
    thumbs.customContextMenuRequested.emit(rect.center())  # inside the selection: kept
    assert thumbs.selected_pages() == [0, 2, 3]
    assert shown[-1][-1] == "Delete 3 pages"
    window.view.customContextMenuRequested.emit(window.view.rect().center())
    assert len(shown) == 3  # the page view has the same menu


def test_delete_pages_and_undo(window, tmp_path, monkeypatch):
    window.open(numbered(tmp_path))
    select_thumbs(window, 1, 3)
    window.delete_act.trigger()
    assert page_widths(window) == [110, 130, 150] and window.dirty
    assert "Deleted 2 pages" in window.statusBar().currentMessage()
    window.undo()
    assert page_widths(window) == [110, 120, 130, 140, 150]
    # the Delete key in the thumbnail panel deletes too
    select_thumbs(window, 0)
    window.view.go_to(0)
    QTest.keyClick(window.thumbs, Qt.Key_Delete)
    assert page_widths(window) == [120, 130, 140, 150]
    # not every page
    boxes = Boxes(monkeypatch, gui_app)
    select_thumbs(window, 0, 1, 2, 3)
    window.delete_pages()
    assert boxes.kinds() == ["information"] and len(window.doc) == 4


def test_delete_from_signed_document_asks(window, profile, form_pdf, tmp_path, monkeypatch):
    signed = tmp_path / "s.pdf"
    sign_with(profile, form_pdf, signed, field_name="Applicant")
    window.open(signed)
    boxes = Boxes(monkeypatch, gui_app, answer=QtWidgets.QMessageBox.No)
    window.view.go_to(0)
    select_thumbs(window, 0)
    window.delete_pages()
    assert boxes.kinds() == ["warning"] and "Applicant" in boxes.text()
    assert len(window.doc) == 2
