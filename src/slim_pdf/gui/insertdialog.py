"""The text to add to a page: what, how big, which colour."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
)

from .. import insert
from ..i18n import N_, _
from ..signing import SigningError
from .screen import fit_to_screen

COLORS = [(N_("Black"), (0.0, 0.0, 0.0)), (N_("Blue"), (0.0, 0.2, 0.65)),
          (N_("Red"), (0.75, 0.0, 0.0)), (N_("Green"), (0.0, 0.45, 0.1))]


class TextDialog(QDialog):
    """``text``, ``size`` and ``color`` after OK."""

    def __init__(self, parent=None, text: str = "", size: float = 11.0, color=None):
        super().__init__(parent)
        self.setWindowTitle(_("Add text"))
        fit_to_screen(self, 420, 300)
        self.edit = QPlainTextEdit(text)
        self.edit.setTabChangesFocus(True)
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(4, 96)
        self.size_spin.setDecimals(1)
        self.size_spin.setSuffix(" pt")
        self.size_spin.setValue(size)
        self.color_combo = QComboBox()
        for name, rgb in COLORS:
            self.color_combo.addItem(_(name), rgb)
        if color is not None:
            self.color_combo.setCurrentIndex(max(0, self.color_combo.findData(color)))

        form = QFormLayout()
        form.addRow(_("Size"), self.size_spin)
        form.addRow(_("Colour"), self.color_combo)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self._check)
        box.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(self.edit, 1)
        lay.addLayout(form)
        lay.addWidget(box)
        self.edit.setFocus()

    @property
    def text(self) -> str:
        return self.edit.toPlainText()

    @property
    def size(self) -> float:
        return self.size_spin.value()

    @property
    def color(self) -> tuple[float, float, float]:
        return tuple(self.color_combo.currentData())

    def _check(self) -> None:
        """OK only with text the page font can show."""
        if not self.text.strip():
            QMessageBox.warning(self, _("Add text"), _("Please enter the text to add."))
            return
        try:
            insert.text_size(self.text, self.size)
        except SigningError as e:  # characters outside Windows-1252
            QMessageBox.warning(self, _("Add text"), str(e))
            return
        self.accept()
