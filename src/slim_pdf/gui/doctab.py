"""One open document: its page view and everything the window keeps about
it (file, password, the edited bytes, undo history, validation results).
The main window shows these as tabs; its panels follow the current one."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QVBoxLayout, QWidget

from .. import forms, validation
from ..i18n import _
from .document import PdfDocument
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
