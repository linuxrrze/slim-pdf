"""GUI: selecting and copying text on the page, finding text (Ctrl+F)."""

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QKeySequence  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from slim_pdf import insert  # noqa: E402

from conftest import make_pdf  # noqa: E402
# fixtures shared with test_gui.py
from test_gui import drag_scene, qapp, save_to, secrets, window  # noqa: E402,F401

TEXTS = ["Hello World", "hello again\nsecond line", "nothing here", "Hello, HELLO"]


@pytest.fixture
def text_pdf(tmp_path):
    data = make_pdf(tmp_path / "blank.pdf", pages=len(TEXTS)).read_bytes()
    for page, text in enumerate(TEXTS):
        data = insert.add_text(data, page, (50, 600, 500, 780), text, size=20)
    path = tmp_path / "text.pdf"
    path.write_bytes(data)
    return path


def char_center(window, start, count=1):
    """Scene point in the middle of characters ``start`` .. on this page."""
    x, y, w, h = window.doc.text_rects(window.view.page, start, count)[0]
    return x + w / 2, y + h / 2


def test_drag_over_text_selects_it_and_ctrl_c_copies(window, text_pdf):
    window.open(text_pdf)
    assert not window.copy_text_act.isEnabled()
    start = char_center(window, 0)
    end = char_center(window, 4)  # "Hello"
    drag_scene(window.view, start, end)
    assert window.view.selected_text() == "Hello"
    assert window.copy_text_act.isEnabled()
    window.copy_act.trigger()  # Ctrl+C
    assert QGuiApplication.clipboard().text() == "Hello"
    assert "Copied 5 characters" in window.statusBar().currentMessage()
    window.escape_act.trigger()
    assert window.view.text_selection is None and not window.copy_text_act.isEnabled()


def test_dragging_backwards_and_over_lines(window, text_pdf):
    window.open(text_pdf)
    window.view.go_to(1)
    # "hello again\r\nsecond": pdfium counts the line break as two characters
    drag_scene(window.view, char_center(window, 18), char_center(window, 6))
    assert window.view.selected_text() == "again\nsecond"


def test_drag_elsewhere_moves_the_page(window, text_pdf):
    window.open(text_pdf)
    window.view.set_zoom(3)
    bar = window.view.verticalScrollBar()
    before = bar.value()
    drag_scene(window.view, (300, 500), (300, 400))
    assert window.view.text_selection is None and bar.value() != before


def test_double_click_selects_a_word_ctrl_a_everything(window, text_pdf):
    window.open(text_pdf)
    view = window.view
    x, y = char_center(window, 8)  # in "World"
    QTest.mouseDClick(view.viewport(), Qt.LeftButton, Qt.NoModifier,
                      view.map_from_page(x, y))
    assert view.selected_text() == "World"
    QTest.keySequence(view, QKeySequence.SelectAll)
    assert view.selected_text() == "\n".join(TEXTS)


def test_selection_runs_over_pages(window, text_pdf):
    """The pages are one below the other: a drag goes on to the next."""
    window.open(text_pdf)
    view = window.view
    view.set_zoom(view.min_zoom())  # whole pages: 1 and 2 both in view
    view.go_to(0)
    start = view.map_from_page(*char_center(window, 6))  # "World"
    window.view.page = 1  # (where the drag ends)
    end = view.map_from_page(*char_center(window, 4), page=1)  # "hello"
    window.view.page = 0
    vp = view.viewport()
    QTest.mousePress(vp, Qt.LeftButton, Qt.NoModifier, start)
    for i in range(1, 6):
        QTest.mouseMove(vp, start + (end - start) * i / 5)
    QTest.mouseRelease(vp, Qt.LeftButton, Qt.NoModifier, end)
    assert view.selected_text() == "World\nhello"
    assert len(view.pages[0].decorations) == 1 and len(view.pages[1].decorations) == 1
    view.go_to(3)  # scrolling keeps it
    assert view.text_selection is not None and window.copy_text_act.isEnabled()
    window.copy_act.trigger()
    assert QGuiApplication.clipboard().text() == "World\nhello"


def test_shift_click_extends_the_selection(window, text_pdf):
    window.open(text_pdf)
    view = window.view
    view.select_text(0, 5)  # "Hello" on page 1
    view.go_to(3)
    at = view.map_from_page(*char_center(window, 11))  # the last "O" on page 4
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.ShiftModifier, at)
    assert view.selected_text() == "\n".join(TEXTS)


def test_edits_keep_the_scroll_position(window, text_pdf):
    window.open(text_pdf)
    view = window.view
    view.set_zoom(2.0)
    view.go_to(2)
    bar = view.verticalScrollBar()
    bar.setValue(bar.value() + 150)
    before = (bar.value(), view.page)
    window.apply_edit(insert.add_text(window.data, 2, (50, 50, 300, 100), "Added"), "x")
    assert (bar.value(), view.page) == before


def test_ctrl_c_copies_pages_without_text_selected(window, text_pdf, monkeypatch):
    window.open(text_pdf)
    window.copy_act.trigger()
    assert "Copied 1 page" in window.statusBar().currentMessage()
    window.view.select_all_text()
    monkeypatch.setattr(window.thumbs, "hasFocus", lambda: True)  # in the Pages panel
    window.copy_act.trigger()
    assert "Copied 1 page" in window.statusBar().currentMessage()


def test_copy_text_in_the_ribbon_and_page_menu(window, text_pdf):
    window.open(text_pdf)
    buttons = [b.defaultAction() for b in
               window.ribbon.tab_page(0).findChildren(QtWidgets.QToolButton)]
    assert window.find_act in buttons and window.copy_text_act in buttons
    assert window.copy_text_act not in window.page_menu().actions()
    window.view.select_all_text()
    assert window.page_menu().actions()[0] is window.copy_text_act


def search(window, text, case=False):
    window.find_act.trigger()
    window.find_bar.edit.setText(text)
    window.find_bar.case.setChecked(case)
    window.find_bar.search_now()


def test_find_across_pages(window, text_pdf):
    window.open(text_pdf)
    assert not window.find_bar.isVisible()
    search(window, "hello")
    bar = window.find_bar
    assert bar.isVisible() and bar.count.text() == "1 of 4"
    assert window._matches == [(0, 0, 5), (1, 0, 5), (3, 0, 5), (3, 7, 5)]

    def shown_on(page):  # the highlights on a page (those in view)
        return [i for i in window.view.pages[page].decorations if i.zValue() == 2.5]

    assert window.view.page == 0 and len(shown_on(0)) == 1
    bar.next_btn.click()
    assert window.view.page == 1 and bar.count.text() == "2 of 4"
    window.find_next_act.trigger()  # F3
    assert window.view.page == 3 and len(shown_on(3)) == 2
    window.find_next_act.trigger()
    window.find_next_act.trigger()  # wraps around
    assert window.view.page == 0 and bar.count.text() == "1 of 4"
    window.find_prev_act.trigger()  # Shift+F3
    assert bar.count.text() == "4 of 4"


def test_find_starts_on_the_current_page_and_matches_case(window, text_pdf):
    window.open(text_pdf)
    window.view.go_to(2)
    search(window, "hello")
    assert window.view.page == 3 and window.find_bar.count.text() == "3 of 4"
    search(window, "HELLO", case=True)
    assert window._matches == [(3, 7, 5)]
    search(window, "missing")
    assert window.find_bar.count.text() == "No matches"
    assert not window.find_bar.next_btn.isEnabled()


def test_enter_and_escape_in_the_find_bar(window, text_pdf):
    window.open(text_pdf)
    window.find_act.trigger()
    edit = window.find_bar.edit
    QTest.keyClicks(edit, "hello")
    QTest.keyClick(edit, Qt.Key_Return)  # searches what was typed
    assert window.find_bar.count.text() == "1 of 4"
    QTest.keyClick(edit, Qt.Key_Return)  # then: the next one
    assert window.find_bar.count.text() == "2 of 4"
    QTest.keyClick(edit, Qt.Key_Return, Qt.ShiftModifier)
    assert window.find_bar.count.text() == "1 of 4"
    QTest.keyClick(edit, Qt.Key_Escape)
    assert not window.find_bar.isVisible() and not window.view._match_items


def test_find_takes_the_selected_text(window, text_pdf):
    window.open(text_pdf)
    window.view.go_to(3)
    window.view.select_text(7, 5)  # "HELLO"
    window.find_act.trigger()
    assert window.find_bar.edit.text() == "HELLO" and window._matches


def test_search_follows_the_document(window, text_pdf, tmp_path):
    window.open(text_pdf)
    search(window, "hello")
    assert len(window._matches) == 4
    window.delete_pages_act.trigger()  # page 1 goes: its match too
    assert len(window._matches) == 3
    other = make_pdf(tmp_path / "other.pdf", pages=1)
    window.open(other)  # another tab: searched there
    assert window._matches == [] and window.find_bar.count.text() == "No matches"


def test_cursor_shows_text_and_selecting(window, text_pdf, qapp):
    window.open(text_pdf)
    view, vp = window.view, window.view.viewport()

    def at(x, y):
        return view.map_from_page(x, y)

    def move(pos):  # goes through the window system: handled after events are
        QTest.mouseMove(vp, pos)
        qapp.processEvents()

    on_text, off_text = at(*char_center(window, 1)), at(300, 500)
    # QTest's mouse may still have a button down from earlier tests' drags
    QTest.mouseRelease(vp, Qt.LeftButton, Qt.NoModifier, off_text)
    move(off_text)
    move(on_text)
    assert vp.cursor().shape() == Qt.IBeamCursor
    move(off_text)
    assert vp.cursor().shape() == Qt.OpenHandCursor
    QTest.mousePress(vp, Qt.LeftButton, Qt.NoModifier, on_text)
    move(off_text)  # still selecting, even beside the text
    assert vp.cursor().shape() == Qt.IBeamCursor
    QTest.mouseRelease(vp, Qt.LeftButton, Qt.NoModifier, off_text)
    assert vp.cursor().shape() == Qt.OpenHandCursor


def test_selecting_at_the_edge_scrolls_on(window, text_pdf):
    window.open(text_pdf)
    view = window.view
    view.set_zoom(2.0)
    view.go_to(0)
    vp = view.viewport()
    QTest.mousePress(vp, Qt.LeftButton, Qt.NoModifier, view.map_from_page(*char_center(window, 0)))
    bottom = QPointF(vp.width() / 2, vp.height() - 5)
    QTest.mouseMove(vp, bottom.toPoint())
    bar = view.verticalScrollBar()
    before = bar.value()
    for _ in range(40):
        view._scroll_at_edge()  # (the timer's ticks)
    assert bar.value() > before and view.text_selection[2] >= 1  # into page 2
    QTest.mouseRelease(vp, Qt.LeftButton, Qt.NoModifier, bottom.toPoint())


def test_scrolling_changes_the_current_page(window, text_pdf):
    window.open(text_pdf)
    view = window.view
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum())
    assert view.page == 3 and window.page_spin.value() == 4
    assert window.thumbs.currentRow() == 3
    bar.setValue(0)
    assert view.page == 0 and window.page_spin.value() == 1
