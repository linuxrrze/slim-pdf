"""Slow work off the GUI thread: results, errors, dialogs, blocked input."""

import threading
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from pdfsigner.gui import background  # noqa: E402
from test_gui import qapp  # noqa: E402,F401


@pytest.fixture
def button(qapp):
    b = QPushButton("x")
    b.show()
    yield b
    b.close()
    from shiboken6 import delete

    delete(b)


def test_result_and_events_keep_flowing(button):
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(5)
    gui = threading.get_ident()

    def work(ask):
        time.sleep(0.2)  # e.g. waiting for a card or a server
        return threading.get_ident()

    try:
        worker = background.run(button, work)
    finally:
        timer.stop()
    assert worker != gui
    assert len(ticks) >= 5  # the event loop ran meanwhile: no "not responding"


def test_errors_are_raised_in_the_gui_thread(button):
    def work(ask):
        raise ValueError("bad PIN")

    with pytest.raises(ValueError, match="bad PIN"):
        background.run(button, work)


def test_ask_runs_on_the_gui_thread(button):
    gui = threading.get_ident()

    def work(ask):
        where = ask(threading.get_ident)
        with pytest.raises(KeyError):
            ask(lambda: {}["missing"])  # the dialog's errors reach the work
        return where

    assert background.run(button, work) == gui


def test_window_ignores_input_while_working(button, qapp):
    clicked = []
    button.clicked.connect(lambda: clicked.append(1))

    def press():  # the user presses the button's key while the work runs
        qapp.sendEvent(button, QKeyEvent(QEvent.KeyPress, Qt.Key_Space, Qt.NoModifier, " "))
        qapp.sendEvent(button, QKeyEvent(QEvent.KeyRelease, Qt.Key_Space, Qt.NoModifier, " "))

    def work(ask):
        ask(press)
        return True

    QApplication.processEvents()
    assert background.run(button, work)
    assert clicked == []
    button.animateClick()
    for _ in range(50):
        QApplication.processEvents()
        if clicked:
            break
        time.sleep(0.01)
    assert clicked == [1]  # works again afterwards
