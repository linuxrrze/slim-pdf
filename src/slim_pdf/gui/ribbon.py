"""A ribbon (Office style): tabs of labelled groups of buttons, in the
place of a menu bar. Qt has none built in; this is tabs of plain widgets.

Buttons show QActions, so shortcuts, enabled state, check state and text
follow the actions the window already has. Icons: Tabler Icons (MIT), in
``icons/``.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QIcon, QIconEngine, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QTabBar,
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


# a small checkable button that is on: a pressed-looking box
CHECKED_STYLE = ("QToolButton { padding: 1px 3px; border: 1px solid transparent; }"
                 "QToolButton:checked { background: palette(midlight);"
                 " border: 1px solid palette(mid); border-radius: 3px; }"
                 "QToolButton:hover { border: 1px solid palette(mid); border-radius: 3px; }")


class RibbonGroup(QFrame):
    """Small buttons (text beside the icon) stacked in columns of three, or
    large ones (text under it) in a row, with the group's name underneath."""

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

    def add(self, action: QAction, large: bool = False) -> QToolButton:
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
            if action.isCheckable():  # small flat buttons don't show "on" by themselves
                btn.setStyleSheet(CHECKED_STYLE)
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


class _SideScroll(QScrollArea):
    """Holds a group at its full width when there is room, and scrolls it
    sideways when the window is too narrow, so it doesn't set the ribbon's
    minimum width (which depends on the fonts: wide on Windows)."""

    MIN_WIDTH = 120

    def __init__(self, widget: QWidget):
        super().__init__()
        self.setWidget(widget)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt's name
        return self.widget().sizeHint()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt's name
        return QSize(min(self.MIN_WIDTH, self.widget().sizeHint().width()),
                     self.widget().minimumSizeHint().height())


class Ribbon(QWidget):
    """Tabs of groups, with one group that stays whatever the tab (see
    :meth:`set_view_controls`) at the right. A narrow window scrolls a tab
    sideways rather than growing to fit it. Offers the parts of
    QTabWidget's interface the window uses (count, tabText, currentIndex,
    setCurrentIndex, currentChanged)."""

    currentChanged = Signal(int)

    def __init__(self):
        super().__init__()
        self.bar = QTabBar()
        self.bar.setDocumentMode(True)
        self.bar.setUsesScrollButtons(True)
        self.bar.setExpanding(False)
        self.stack = QStackedWidget()
        self.bar.currentChanged.connect(self._switch)
        self.body = QHBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)
        self.body.addWidget(self.stack, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.bar)
        lay.addLayout(self.body)
        self.view_controls: RibbonGroup | None = None

    def _switch(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.currentChanged.emit(index)

    def tab(self, title: str) -> RibbonTab:
        page = RibbonTab()
        scroll = QScrollArea()
        scroll.setWidget(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.stack.addWidget(scroll)
        self.bar.addTab(title)
        return page

    def set_view_controls(self, title: str, columns: list[list]) -> RibbonGroup:
        """A group that stays at the ribbon's right end on every tab:
        ``columns`` of small buttons, text beside the icon (actions, or
        widgets such as a page number box)."""
        group = RibbonGroup(title)
        for column in columns:
            group._column = None  # each list is a column of its own
            for item in column:
                if isinstance(item, QAction):
                    group.add(item, large=False)
                else:
                    group.add_widget(item)
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setFrameShadow(QFrame.Sunken)
        self.body.addWidget(line)
        self.body.addWidget(_SideScroll(group))  # narrow windows: scrolls, like a tab
        self.view_controls = group
        return group

    # the QTabWidget-like part
    def count(self) -> int:
        return self.bar.count()

    def tabText(self, index: int) -> str:  # noqa: N802 - Qt's name
        return self.bar.tabText(index)

    def currentIndex(self) -> int:  # noqa: N802 - Qt's name
        return self.bar.currentIndex()

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802 - Qt's name
        self.bar.setCurrentIndex(index)

    def tab_page(self, index: int) -> RibbonTab:
        return self.stack.widget(index).widget()

    def refit(self) -> None:
        """Once the buttons are in: room for the tallest tab or the view
        group (and a scroll bar, for narrow windows)."""
        bar = self.bar.sizeHint().height()
        heights = [self.tab_page(i).sizeHint().height() for i in range(self.count())]
        if self.view_controls is not None:
            heights.append(self.view_controls.sizeHint().height())
        scrollbar = self.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        self.setFixedHeight(bar + max(heights, default=0) + scrollbar + 4)
