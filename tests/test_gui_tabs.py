"""GUI: several documents, one tab each."""

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from slim_pdf.gui import app as gui_app  # noqa: E402

from conftest import make_pdf  # noqa: E402
# fixtures shared with test_gui.py
from test_gui import Boxes, qapp, save_to, secrets, window  # noqa: E402,F401


def test_each_document_gets_a_tab(window, tmp_path):
    a = make_pdf(tmp_path / "a.pdf", pages=2)
    b = make_pdf(tmp_path / "b.pdf", pages=5)
    tabs = window.tabs
    assert tabs.count() == 1 and tabs.tabBar().isHidden()  # one: no tab bar
    window.open(a)
    assert tabs.count() == 1  # the empty start tab is used
    window.open(b)
    assert tabs.count() == 2 and not tabs.tabBar().isHidden()
    assert [tabs.tabText(i) for i in range(2)] == ["a.pdf", "b.pdf"]
    assert window.path == b and window.thumbs.count() == 5
    assert window.page_count.text() == " / 5 "
    tabs.setCurrentIndex(0)  # the panels follow
    assert window.path == a and window.thumbs.count() == 2
    assert window.page_count.text() == " / 2 " and "a.pdf" in window.windowTitle()
    window.open(b)  # already open: its tab
    assert tabs.count() == 2 and window.path == b


def test_edits_stay_with_their_document(window, tmp_path):
    a = make_pdf(tmp_path / "a.pdf", pages=2)
    b = make_pdf(tmp_path / "b.pdf", pages=3)
    window.open(a)
    window.rotate_pages(90)
    first = window.tab
    window.open(b)
    assert not window.dirty and not window.undo_act.isEnabled()  # b's own state
    window.tabs.setCurrentWidget(first)
    assert window.dirty and window.undo_act.isEnabled()
    assert window.tabs.tabText(0) == "*a.pdf"
    window.open(a)  # open again with unsaved edits: they are kept
    assert window.dirty and window.tab is first


def test_closing(window, tmp_path, monkeypatch):
    a = make_pdf(tmp_path / "a.pdf", pages=2)
    b = make_pdf(tmp_path / "b.pdf", pages=3)
    window.open(a)
    window.open(b)
    window.close_act.trigger()  # Ctrl+W: the current one
    assert window.tabs.count() == 1 and window.path == a
    window.close_act.trigger()
    assert window.tabs.count() == 1 and window.doc is None  # an empty tab is left
    assert not window.save_as_act.isEnabled() and window.thumbs.count() == 0
    assert window.page_count.text() == " / 0 "


def test_quitting_asks_for_every_edited_document(window, tmp_path, monkeypatch):
    a = make_pdf(tmp_path / "a.pdf", pages=2)
    b = make_pdf(tmp_path / "b.pdf", pages=3)
    window.open(a)
    window.rotate_pages(90)
    window.open(b)  # the edited one is in the background now
    boxes = Boxes(monkeypatch, gui_app, answer=QtWidgets.QMessageBox.Cancel)
    assert not window.close()
    assert boxes.kinds() == ["question"] and "a.pdf" in boxes.text()
    assert window.path == a  # shown while asking
    boxes.answer = QtWidgets.QMessageBox.Discard
    assert window.close()


def test_text_typed_in_one_tab_stays_there(window, fill_pdf, tmp_path, qapp):
    """Switching tabs while typing on the page fills in the field of the
    document it was typed in."""
    from PySide6.QtTest import QTest

    from test_gui_forms import click_field, values

    window.open(fill_pdf)
    form_tab = window.tab
    qapp.processEvents()
    click_field(window, "name")
    QTest.keyClicks(window.view.inline_editor, "Tab test")
    window.open(make_pdf(tmp_path / "other.pdf", pages=1))  # another tab comes up
    qapp.processEvents()
    assert window.tab is not form_tab and not window.dirty
    window.tabs.setCurrentWidget(form_tab)
    assert values(window)["name"] == "Tab test" and window.dirty


def test_command_line_opens_every_file(qapp, tmp_path, monkeypatch):
    a = make_pdf(tmp_path / "a.pdf", pages=1)
    b = make_pdf(tmp_path / "b.pdf", pages=1)
    made = []
    real = gui_app.MainWindow

    def keep(cfg):
        made.append(real(cfg))
        return made[-1]

    monkeypatch.setattr(gui_app, "MainWindow", keep)
    monkeypatch.setenv("SLIM_PDF_CONFIG", str(tmp_path / "c.toml"))
    assert gui_app.main(["--smoke-test", str(a), str(b)]) == 0
    assert made[0].tabs.count() == 2


def test_open_several_files_at_once(window, tmp_path, monkeypatch):
    files = [make_pdf(tmp_path / f"{n}.pdf", pages=1) for n in "abc"]
    monkeypatch.setattr(gui_app.QFileDialog, "getOpenFileNames",
                        staticmethod(lambda *a, **k: ([str(f) for f in files], "")))
    window.open_act.trigger()
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [
        "a.pdf", "b.pdf", "c.pdf"]
    assert window.path == files[-1]  # the last one is shown


def test_drop_pdf_files_onto_the_window(window, tmp_path, qapp):
    """PDFs dragged from a file manager open (other files are refused)."""
    from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDropEvent

    a = make_pdf(tmp_path / "a.pdf", pages=1)
    b = make_pdf(tmp_path / "b.pdf", pages=2)
    text = tmp_path / "notes.txt"
    text.write_text("x")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in (a, text, b)])
    enter = QDragEnterEvent(QPointF(50, 50), Qt.CopyAction, mime, Qt.LeftButton,
                            Qt.NoModifier)
    window.dragEnterEvent(enter)
    assert enter.isAccepted()
    window.dropEvent(QDropEvent(QPointF(50, 50), Qt.CopyAction, mime, Qt.LeftButton,
                                Qt.NoModifier))
    qapp.processEvents()
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ["a.pdf", "b.pdf"]

    only_text = QMimeData()
    only_text.setUrls([QUrl.fromLocalFile(str(text))])
    enter = QDragEnterEvent(QPointF(50, 50), Qt.CopyAction, only_text,
                            Qt.LeftButton, Qt.NoModifier)
    window.dragEnterEvent(enter)
    assert not enter.isAccepted()


def test_side_panels_can_be_hidden_independently(window, fill_pdf, qapp, profile):
    """View: Pages panel (left) and Tools panel (right) are shown or hidden
    on their own; the choice is remembered for the next start."""
    from slim_pdf.config import Config

    window.open(fill_pdf)
    left, right = window.pages_dock, window.side_dock
    assert left.isVisible() and right.isVisible()  # Edit: the form panel
    window.tools_panel_act.trigger()
    assert left.isVisible() and not right.isVisible()
    window.show_tab(gui_app.SIGN_TAB)  # hidden on Signing too
    assert not right.isVisible()
    window.pages_dock.toggleViewAction().trigger()
    assert not left.isVisible() and not right.isVisible()
    window.tools_panel_act.trigger()
    assert not left.isVisible() and right.isVisible()
    window.show_tab(2)  # Pages: no tools panel there anyway
    assert not right.isVisible()
    window.show_tab(gui_app.EDIT_TAB)
    window.tools_panel_act.trigger()  # off again, then a new window remembers
    other = gui_app.MainWindow(Config(certs={"alice": profile}, default="alice"))
    other.show()
    qapp.processEvents()
    assert not other.tools_panel_act.isChecked() and not other.side_dock.isVisible()
    assert not other.pages_dock.isVisible()
    other.close()
