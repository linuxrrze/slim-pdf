from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QWidget,
)

from ..i18n import _


def ask_secret(parent: QWidget | None, prompt: str) -> str | None:
    text, ok = QInputDialog.getText(parent, _("Password"), _("{prompt}:").format(prompt=prompt),
                                   QLineEdit.Password)
    return text if ok else None


class NewPasswordDialog(QDialog):
    """A new password for a document, typed twice; OK only when both match."""

    def __init__(self, parent: QWidget | None, encrypted: bool):
        super().__init__(parent)
        self.setWindowTitle(_("Encrypt"))
        lay = QFormLayout(self)
        intro = QLabel(
            _("The document is encrypted already; it gets this password instead of its "
              "current one.") if encrypted else
            _("The document will open only with this password (AES-256 encryption)."))
        intro.setWordWrap(True)
        lay.addRow(intro)
        self.first, self.second = QLineEdit(), QLineEdit()
        for edit in (self.first, self.second):
            edit.setEchoMode(QLineEdit.Password)
            edit.textChanged.connect(self._check)
        lay.addRow(_("New password:"), self.first)
        lay.addRow(_("Repeat:"), self.second)
        self.problem = QLabel()
        lay.addRow(self.problem)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addRow(self.buttons)
        self._check()

    def _check(self) -> None:
        a, b = self.first.text(), self.second.text()
        self.problem.setText(_("The passwords don't match.") if b and a != b else "")
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(bool(a) and a == b)

    def password(self) -> str:
        return self.first.text()


def ask_new_password(parent: QWidget | None, encrypted: bool) -> str | None:
    dialog = NewPasswordDialog(parent, encrypted)
    try:
        return dialog.password() if dialog.exec() == QDialog.Accepted else None
    finally:
        dialog.deleteLater()
