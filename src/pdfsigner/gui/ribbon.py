"""A ribbon (Office style): tabs of labelled groups of buttons, in the
place of a menu bar. Qt has none built in; this is tabs of plain widgets.

Buttons show QActions, so shortcuts, enabled state, check state and text
follow the actions the window already has.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QIcon
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

LARGE_ICON = 32
SMALL_ICON = 16


def icon(theme: str, fallback: QStyle.StandardPixmap | None = None) -> QIcon:
    """The platform's icon (Windows, macOS, a Linux icon theme), else Qt's
    built-in one, else none (the button shows its text only)."""
    themed = getattr(QIcon.ThemeIcon, theme, None)  # Qt >= 6.7
    result = QIcon.fromTheme(themed) if themed is not None else QIcon()
    if result.isNull() and fallback is not None:
        result = QApplication.style().standardIcon(fallback)
    return result


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
        if self._column is None or self._column.count() >= 3:
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
