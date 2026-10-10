"""Slow work (signing, tokens, KeePassXC, network) off the GUI thread.

While a card waits for a touch or a timestamp server answers, the window
must keep repainting, or the desktop reports it as not responding. The
work runs in a thread; the window ignores input meanwhile, but dialogs
(PIN prompts, which the work asks for through ``ask``) work as usual.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import TypeVar

from PySide6.QtCore import QEvent, QEventLoop, QObject, QTimer
from PySide6.QtWidgets import QApplication, QWidget

T = TypeVar("T")
Ask = Callable[[Callable[[], object]], object]

POLL_MS = 20
BLOCKED = {QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick,
           QEvent.KeyPress, QEvent.KeyRelease, QEvent.Wheel, QEvent.Shortcut,
           QEvent.ShortcutOverride, QEvent.ContextMenu, QEvent.Close}


class _IgnoreInput(QObject):
    """Swallows input to one window (not to dialogs opened meanwhile)."""

    def __init__(self, window: QWidget):
        super().__init__()
        self.window = window

    def eventFilter(self, obj, event) -> bool:
        if event.type() in BLOCKED and isinstance(obj, QWidget) and obj.window() is self.window:
            event.ignore()
            return True
        return False


def run(parent: QWidget, work: Callable[[Ask], T]) -> T:
    """Run ``work(ask)`` in a thread and return its result (or raise its
    exception) while the GUI keeps running. In the thread, ``ask(fn)`` runs
    ``fn`` on the GUI thread (e.g. a PIN dialog) and returns its result."""
    requests: queue.Queue = queue.Queue()
    done = threading.Event()
    outcome: dict = {}

    def ask(fn):
        reply: queue.Queue = queue.Queue(maxsize=1)
        requests.put((fn, reply))
        ok, value = reply.get()
        if ok:
            return value
        raise value

    def target():
        try:
            outcome["result"] = work(ask)
        except BaseException as e:  # handed to the GUI thread
            outcome["error"] = e
        finally:
            done.set()

    loop = QEventLoop()
    answering = False

    def poll():
        nonlocal answering
        if answering:  # a dialog's own event loop runs this timer, too
            return
        while True:
            try:
                fn, reply = requests.get_nowait()
            except queue.Empty:
                break
            answering = True
            try:
                reply.put((True, fn()))
            except BaseException as e:
                reply.put((False, e))
            finally:
                answering = False
        if done.is_set():
            loop.quit()

    timer = QTimer()
    timer.setInterval(POLL_MS)
    timer.timeout.connect(poll)
    guard = _IgnoreInput(parent.window())
    app = QApplication.instance()
    app.installEventFilter(guard)
    thread = threading.Thread(target=target, name="pdfsigner-work", daemon=True)
    thread.start()
    timer.start()
    try:
        loop.exec()
    finally:
        timer.stop()
        app.removeEventFilter(guard)
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]
