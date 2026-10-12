"""The find bar under the document (Ctrl+F), as in a web browser."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QLineEdit, QToolButton, QWidget

from ..i18n import _
from . import ribbon

TYPING_DELAY = 250  # ms after the last key before searching


class FindBar(QWidget):
    """``searchChanged(text, match case)`` when what to look for changes
    (shortly after typing stops), ``step(+1/-1)`` for the next/previous
    match (Enter, Shift+Enter, the arrows), ``closed`` on Esc or ×."""

    searchChanged = Signal(str, bool)
    step = Signal(int)
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(_("Find in document"))
        self.edit.setClearButtonEnabled(True)
        self.edit.setMinimumWidth(220)
        self.case = QCheckBox(_("Match case"))
        self.count = QLabel()
        self.prev_btn = self._button("chevron-up", _("Previous match (Shift+F3)"), -1)
        self.next_btn = self._button("chevron-down", _("Next match (F3, Enter)"), 1)
        close = QToolButton()
        close.setIcon(ribbon.icon("x"))
        close.setToolTip(_("Close the find bar (Esc)"))
        close.setAutoRaise(True)
        close.clicked.connect(self.close_bar)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(TYPING_DELAY)
        self._timer.timeout.connect(self.search_now)
        self.edit.textChanged.connect(lambda _text: self._timer.start())
        self.case.toggled.connect(lambda _on: self.search_now())

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.addWidget(QLabel(_("Find:")))
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.prev_btn)
        lay.addWidget(self.next_btn)
        lay.addWidget(self.case)
        lay.addWidget(self.count)
        lay.addStretch()
        lay.addWidget(close)
        self.edit.installEventFilter(self)
        self.set_result(None)

    def _button(self, icon: str, tip: str, step: int) -> QToolButton:
        b = QToolButton()
        b.setIcon(ribbon.icon(icon))
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.clicked.connect(lambda: self.step.emit(step))
        return b

    def open(self, text: str = "") -> None:
        """Show it with the cursor in the field (``text`` to look for, if given)."""
        self.show()
        if text:
            self.edit.setText(text)
            self.search_now()
        self.edit.setFocus()
        self.edit.selectAll()

    def close_bar(self) -> None:
        self._timer.stop()
        self.hide()
        self.closed.emit()

    def search_now(self) -> None:
        self._timer.stop()
        self.searchChanged.emit(self.edit.text(), self.case.isChecked())

    def set_result(self, current: int | None, total: int = 0) -> None:
        """'3 of 12', 'No matches', or nothing (``current`` None, nothing searched)."""
        if current is None:
            self.count.setText("")
        elif total == 0:
            self.count.setText(_("No matches"))
        else:
            self.count.setText(_("{current} of {total}").format(current=current + 1,
                                                                total=total))
        self.prev_btn.setEnabled(total > 1)
        self.next_btn.setEnabled(total > 1)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt's name
        if (obj is self.edit and event.type() == event.Type.ShortcutOverride
                and event.key() == Qt.Key_Escape):
            event.accept()  # ours, not the window's Esc (which cancels other things)
            return True
        if obj is self.edit and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key_Escape:
                self.close_bar()
                return True
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if self._timer.isActive():  # typed just now: search first
                    self.search_now()
                else:
                    self.step.emit(-1 if event.modifiers() & Qt.ShiftModifier else 1)
                return True
        return super().eventFilter(obj, event)
