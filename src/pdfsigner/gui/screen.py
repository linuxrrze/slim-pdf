"""Window sizes that fit the screen.

Laptops with 1366×768 or a scaled 1920×1080 (125–150 %) leave ~650–730
logical pixels of height. Dialogs ask for a comfortable size but never
more than the screen's available area; tall content scrolls instead.
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QScrollArea, QWidget

SCREEN_FRACTION = 0.9  # leave room for window decorations and the desktop


def available_size(widget: QWidget | None = None) -> QSize:
    screen = None
    if widget is not None:
        parent = widget.parentWidget()
        screen = (parent or widget).screen()
    screen = screen or QGuiApplication.primaryScreen()
    return screen.availableGeometry().size()


def fit_to_screen(widget: QWidget, width: int, height: int) -> None:
    """Resize ``widget`` to ``width``×``height``, but at most a fraction of the
    available screen area, and never below what its layout needs."""
    avail = available_size(widget)
    limit = QSize(int(avail.width() * SCREEN_FRACTION), int(avail.height() * SCREEN_FRACTION))
    widget.resize(max(min(width, limit.width()), 1), max(min(height, limit.height()), 1))


def scrollable(content: QWidget) -> QScrollArea:
    """``content`` in a frameless scroll area: it scrolls when the window is
    smaller than the content, and otherwise looks as before."""
    area = QScrollArea()
    area.setWidget(content)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setMinimumSize(QSize(200, 120))
    return area
