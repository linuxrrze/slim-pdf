"""Window sizes that fit the screen.

Laptops with 1366×768 or a scaled 1920×1080 (125–150 %) leave ~650–730
logical pixels of height. Dialogs ask for a comfortable size but never
more than the screen's available area; tall content scrolls instead.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QSizePolicy, QWidget

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


class ElidedLabel(QLabel):
    """One line (e.g. a file path) that is shortened with "…" in the middle
    when space is short, instead of making the window wider than the screen.
    The full text is in the tooltip."""

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._full = text
        self.setToolTip(text)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(40)
        self.setText(text)

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.setText(self.fontMetrics().elidedText(self._full, Qt.ElideMiddle, self.width()))
