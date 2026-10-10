from __future__ import annotations

from PySide6.QtWidgets import QInputDialog, QLineEdit, QWidget

from ..i18n import _


def ask_secret(parent: QWidget | None, prompt: str) -> str | None:
    text, ok = QInputDialog.getText(parent, _("Password"), _("{prompt}:").format(prompt=prompt),
                                   QLineEdit.Password)
    return text if ok else None
