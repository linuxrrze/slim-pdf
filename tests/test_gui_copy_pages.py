"""GUI: copying pages between documents (clipboard, drag and drop)."""

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PySide6.QtCore import QItemSelectionModel  # noqa: E402

from slim_pdf import pages  # noqa: E402

from conftest import make_pdf  # noqa: E402
# fixtures shared with test_gui.py
from test_gui import Boxes, qapp, save_to, secrets, window  # noqa: E402,F401


def widths(window):
    return [round(window.doc.display_size(i)[0]) for i in range(len(window.doc))]


def two_documents(window, tmp_path):
    a = make_pdf(tmp_path / "a.pdf", pages=2)  # 595 wide
    b = make_pdf(tmp_path / "b.pdf", pages=3, size=(300, 400))
    window.open(a)
    window.open(b)
    return window.tabs.widget(0), window.tabs.widget(1)


def select(window, *rows):
    window.thumbs.clearSelection()
    for r in rows:
        window.thumbs.setCurrentRow(r, QItemSelectionModel.Select)


def test_copy_and_paste_into_another_document(window, tmp_path):
    a, b = two_documents(window, tmp_path)
    assert not window.paste_pages_act.isEnabled()  # nothing copied yet
    select(window, 0, 2)  # in b
    window.copy_pages_act.trigger()
    assert window.paste_pages_act.isEnabled() and not window.dirty  # copying changes nothing
    window.tabs.setCurrentWidget(a)
    window.view.go_to(0)
    window.paste_pages_act.trigger()  # after the current page
    assert widths(window) == [595, 300, 300, 595]
    assert window.dirty and "Inserted 2 pages" in window.statusBar().currentMessage()
    assert window.thumbs.selected_pages() == [1, 2]  # the new pages
    window.undo()
    assert widths(window) == [595, 595]


def test_cut_moves_pages_over(window, tmp_path, monkeypatch):
    a, b = two_documents(window, tmp_path)
    select(window, 1)
    window.cut_pages_act.trigger()
    assert len(window.doc) == 2  # gone from b
    window.tabs.setCurrentWidget(a)
    window.view.go_to(1)
    window.paste_pages_act.trigger()  # after the current (last) page
    assert widths(window) == [595, 595, 300]


def test_paste_into_the_same_document(window, tmp_path):
    a = make_pdf(tmp_path / "a.pdf", pages=2)
    window.open(a)
    window.copy_pages_act.trigger()  # the current page
    window.paste_pages_act.trigger()
    assert len(window.doc) == 3


def test_drag_from_one_document_into_another(window, tmp_path, qapp):
    """A drag that ends in another document's Pages panel copies there;
    within one document it still moves."""
    a, b = two_documents(window, tmp_path)
    thumbs = window.thumbs
    select(window, 2)
    thumbs._dragging = True  # as startDrag sets it
    thumbs._drag_source, thumbs._drag_pages = b.doc, [2]
    window.tabs.setCurrentWidget(a)  # what hovering over a's tab does
    assert thumbs.document is a.doc
    thumbs.copyRequested.emit(b.doc, [2], 1)  # what the drop asks for
    thumbs._dragging = False
    assert widths(window) == [595, 300, 595]
    assert len(b.doc) == 3  # copied, not moved


def test_drop_on_a_tab_appends(window, tmp_path, qapp):
    a, b = two_documents(window, tmp_path)
    thumbs = window.thumbs
    thumbs._dragging = True
    thumbs._drag_source, thumbs._drag_pages = b.doc, [0, 1]
    window.tabs.tabBar().pagesDropped.emit(0)
    thumbs._dragging = False
    qapp.processEvents()
    assert window.tab is a and widths(window) == [595, 595, 300, 300]


def test_insert_keeps_the_target_intact(tmp_path):
    """pages.insert: an incremental update with the copied pages' size and
    rotation."""
    from conftest import make_encrypted

    a = make_pdf(tmp_path / "a.pdf", pages=2)
    b = make_pdf(tmp_path / "b.pdf", pages=3, size=(300, 400), rotate=90)
    out = pages.insert(a.read_bytes(), b.read_bytes(), [1, 2], 1)
    assert out.startswith(a.read_bytes())
    from slim_pdf.signing import inspect_pdf

    assert inspect_pdf(out).page_rotation == [0, 90, 90, 0]
    enc = make_encrypted(b, tmp_path / "enc.pdf", "pw")
    out = pages.insert(enc.read_bytes(), a.read_bytes(), [0], 0, password="pw")
    assert pages.page_count(out, "pw") == 4
    with pytest.raises(pages.PageError, match="out of range"):
        pages.insert(a.read_bytes(), b.read_bytes(), [0], 5)


REAL_DRAG = r"""
import io, sys
from pathlib import Path
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from pypdf import PdfWriter

app = QApplication([])
from slim_pdf.config import Config
from slim_pdf.gui.app import MainWindow

def pdf(path, widths):
    w = PdfWriter()
    for width in widths:
        w.add_blank_page(width, 200)
    buf = io.BytesIO()
    w.write(buf)
    Path(path).write_bytes(buf.getvalue())
    return Path(path)

a = pdf(sys.argv[1] + "/a.pdf", [100, 110])
b = pdf(sys.argv[1] + "/b.pdf", [300, 310, 320])
win = MainWindow(Config())
win.resize(900, 900)
win.show()
QTest.qWaitForWindowExposed(win)
win.open(a)
win.open(b)  # b is shown; drag its page 3 over a's tab, into a's Pages panel
panel = win.thumbs
bar = win.tabs.tabBar()
vp = panel.viewport()
src = panel.visualItemRect(panel.item(2)).center()

def over_tab():
    QTest.mouseMove(bar, bar.tabRect(0).center())
    QTimer.singleShot(150, into_panel)

def into_panel():
    dst = panel.visualItemRect(panel.item(1)).topLeft() + QPoint(20, 5)  # a, now
    QTest.mouseMove(vp, dst)
    QTest.mouseRelease(vp, Qt.LeftButton, Qt.NoModifier, dst)

QTimer.singleShot(200, over_tab)  # inside the drag's own event loop
QTest.mousePress(vp, Qt.LeftButton, Qt.NoModifier, src)
for i in range(1, 4):
    QTest.mouseMove(vp, src + QPoint(0, -8 * i))
QTest.qWait(500)
print(win.path.name, [round(win.doc.display_size(i)[0]) for i in range(len(win.doc))],
      len(win.tabs.widget(1).doc))
for i in range(win.tabs.count()):
    win.tabs.widget(i).dirty = False
win.close()
"""


def test_real_mouse_drag_between_documents(tmp_path):
    """A real drag from one document's Pages panel, over the other's tab,
    into its Pages panel: the page is copied there (on the "minimal"
    platform, which, unlike "offscreen", runs Qt's drag and drop)."""
    import os
    import subprocess
    import sys

    env = dict(os.environ, QT_QPA_PLATFORM="minimal")
    out = subprocess.run([sys.executable, "-c", REAL_DRAG, str(tmp_path)],
                         capture_output=True, text=True, timeout=60, env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "a.pdf [100, 320, 110] 3"
