from __future__ import annotations

from PySide6.QtWidgets import QInputDialog, QLineEdit, QWidget


def ask_secret(parent: QWidget | None, prompt: str) -> str | None:
    text, ok = QInputDialog.getText(parent, "Password", prompt + ":", QLineEdit.Password)
    return text if ok else None
