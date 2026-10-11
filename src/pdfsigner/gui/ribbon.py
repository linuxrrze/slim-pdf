"""A ribbon (Office style): tabs of labelled groups of buttons, in the
place of a menu bar. Qt has none built in; this is tabs of plain widgets.

Buttons show QActions, so shortcuts, enabled state, check state and text
follow the actions the window already has. Icons: Tabler Icons (MIT), in
``icons/``.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QAction, QIcon, QIconEngine, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

ICON_DIR = Path(__file__).with_name("icons")
LARGE_ICON = 32
SMALL_ICON = 16


class SvgIcon(QIconEngine):
    """A monochrome SVG drawn in the palette's text colour (greyed when
    disabled), so it suits light and dark themes, at any size and scale."""

    def __init__(self, svg: bytes):
        super().__init__()
        self.svg = svg

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state) -> None:
        group = QPalette.Disabled if mode == QIcon.Disabled else QPalette.Active
        color = QApplication.palette().color(group, QPalette.ButtonText)
        QSvgRenderer(self.svg.replace(b"currentColor", color.name().encode())).render(
            painter, QRectF(rect))

    def pixmap(self, size: QSize, mode: QIcon.Mode, state) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state, scale: float) -> QPixmap:
        pm = QPixmap(size * scale)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        self.paint(painter, QRect(QPoint(0, 0), size * scale), mode, state)
        painter.end()
        pm.setDevicePixelRatio(scale)
        return pm

    def clone(self) -> QIconEngine:
        return SvgIcon(self.svg)


def icon(name: str) -> QIcon:
    """One of the bundled Tabler icons (icons/<name>.svg, MIT license)."""
    return QIcon(SvgIcon((ICON_DIR / f"{name}.svg").read_bytes()))


class RibbonGroup(QFrame):
    """Buttons in a row (large) or stacked in columns of three (small),
    with the group's name underneath."""

    def __init__(self, title: str):
        super().__init__()
        self.setFrameShape(QFrame.NoFrame)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 0)
        outer.setSpacing(0)
        self.row = QHBoxLayout()
        self.row.setSpacing(2)
        outer.addLayout(self.row, 1)
        self.title = QLabel(title)
        self.title.setAlignment(Qt.AlignHCenter)
        self.title.setEnabled(False)  # greyed, as a caption
        outer.addWidget(self.title)
        self._column: QVBoxLayout | None = None

    def add(self, action: QAction, large: bool = True) -> QToolButton:
        btn = QToolButton()
        btn.setDefaultAction(action)
        btn.setAutoRaise(True)
        btn.setFocusPolicy(Qt.TabFocus)
        if large:
            self._column = None
            btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon
                                   if not action.icon().isNull() else Qt.ToolButtonTextOnly)
            btn.setIconSize(QSize(LARGE_ICON, LARGE_ICON))
            btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
            btn.setMinimumWidth(56)
            self.row.addWidget(btn)
        else:
            btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            btn.setIconSize(QSize(SMALL_ICON, SMALL_ICON))
            self._small(btn)
        return btn

    def add_widget(self, widget: QWidget) -> QWidget:
        """A widget (e.g. a spin box) in the small-button columns."""
        self._small(widget)
        return widget

    def _small(self, widget: QWidget) -> None:
        if self._column is None or self._column.count() > 3:  # 3 and the stretch
            self._column = QVBoxLayout()
            self._column.setSpacing(0)
            self._column.addStretch()
            self.row.addLayout(self._column)
        self._column.insertWidget(self._column.count() - 1, widget, 0, Qt.AlignLeft)


class RibbonTab(QWidget):
    def __init__(self):
        super().__init__()
        self.groups = QHBoxLayout(self)
        self.groups.setContentsMargins(2, 2, 2, 2)
        self.groups.setSpacing(0)
        self.groups.addStretch()

    def group(self, title: str) -> RibbonGroup:
        if self.groups.count() > 1:  # a line between groups
            line = QFrame()
            line.setFrameShape(QFrame.VLine)
            line.setFrameShadow(QFrame.Sunken)
            self.groups.insertWidget(self.groups.count() - 1, line)
        g = RibbonGroup(title)
        self.groups.insertWidget(self.groups.count() - 1, g)
        return g


class Ribbon(QTabWidget):
    """Tabs of groups. A narrow window scrolls a tab sideways rather than
    growing to fit it."""

    def __init__(self):
        super().__init__()
        self.setDocumentMode(True)
        self.setUsesScrollButtons(True)

    def tab(self, title: str) -> RibbonTab:
        page = RibbonTab()
        scroll = QScrollArea()
        scroll.setWidget(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.addTab(scroll, title)
        return page

    def tab_page(self, index: int) -> RibbonTab:
        return self.widget(index).widget()

    def refit(self) -> None:
        """Once the buttons are in: room for the tallest tab (and a scroll
        bar, for narrow windows)."""
        bar = self.tabBar().sizeHint().height()
        content = max((self.tab_page(i).sizeHint().height() for i in range(self.count())),
                      default=0)
        scrollbar = self.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        self.setFixedHeight(bar + content + scrollbar + 4)
