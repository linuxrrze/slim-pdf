"""One open document: its page view and everything the window keeps about
it (file, password, the edited bytes, undo history, validation results).
The main window shows these as tabs; its panels follow the current one."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QTabBar, QTabWidget, QVBoxLayout, QWidget

from .. import forms, validation
from ..i18n import _
from .document import PdfDocument
from .pagetools import PagesPanel
from .pageview import PageView


class DocumentTab(QWidget):
    def __init__(self, view: PageView):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(view)
        self.view = view
        self.doc: PdfDocument | None = None
        # where it came from or was last saved to; edits are in self.data
        self.path: Path | None = None
        self.pdf_password: str | None = None
        self.info = None  # signing.DocInfo
        self.form_fields: list[forms.FormField] = []
        self.data: bytes | None = None
        self.dirty = False
        self.undo_stack: list[bytes] = []
        self.reports: list[validation.SignatureReport] = []
        self.verify_error = ""  # why validation failed, if it did
        self.status = ""  # the status bar message for this document

    def title(self) -> str:
        """The tab's text: the file name, * while there are unsaved edits."""
        name = self.path.name if self.path else _("No document")
        return ("*" if self.dirty else "") + name

    def close_document(self) -> None:
        if self.doc is not None:
            self.view.set_document(None, [])  # nothing left to draw from it
            self.doc.close()
            self.doc = None


class _DropTabBar(QTabBar):
    """Pages dragged from the Pages panel over a tab show that document (to
    drop them into its Pages panel), or are dropped onto the tab: copied to
    the end of that document (``pagesDropped(tab index)``)."""

    pagesDropped = Signal(int)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)

    @staticmethod
    def _ours(event) -> bool:
        return isinstance(event.source(), PagesPanel)

    def dragEnterEvent(self, event) -> None:
        if self._ours(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if not self._ours(event):
            event.ignore()
            return
        index = self.tabAt(event.position().toPoint())
        if index >= 0 and index != self.currentIndex():
            self.setCurrentIndex(index)
        event.setDropAction(Qt.CopyAction)
        event.accept()

    def dropEvent(self, event) -> None:
        index = self.tabAt(event.position().toPoint())
        if not self._ours(event) or index < 0:
            event.ignore()
            return
        event.setDropAction(Qt.IgnoreAction)  # the edit is ours, not Qt's
        event.accept()
        self.pagesDropped.emit(index)


class DocumentTabs(QTabWidget):
    """The documents' tabs; pages can be dragged onto them."""

    def __init__(self):
        super().__init__()
        self.setTabBar(_DropTabBar())
        self.pagesDropped = self.tabBar().pagesDropped
