"""Fill in a document's form fields: one editor per field, in form order."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import forms
from ..i18n import _
from ..signing import printable
from .screen import fit_to_screen, scrollable


class FormDialog(QDialog):
    """``values()`` after OK: the fields whose value changed."""

    def __init__(self, fields: list[forms.FormField], parent=None, focus: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(_("Fill in form"))
        fit_to_screen(self, 560, 520)
        self.fields = fields
        self.editors: dict[str, QWidget] = {}

        form = QWidget()
        lay = QFormLayout(form)
        for f in fields:
            editor = self._editor(f)
            self.editors[f.name] = editor
            label = printable(f.label or f.name)
            if f.page is not None:
                label = _("{name} (p. {page})").format(name=label, page=f.page + 1)
            if not f.fillable:
                editor.setEnabled(False)
                editor.setToolTip(_("Locked by a signature") if f.locked else _("Read-only"))
            if f.label:
                editor.setToolTip("\n".join(filter(None, [editor.toolTip(), printable(f.name)])))
            lay.addRow(label, editor)

        hint = QLabel(_("Changed fields are filled in when you press OK; Undo takes it back."))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray")
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        outer = QVBoxLayout(self)
        self.scroll = scrollable(form)
        outer.addWidget(self.scroll, 1)
        outer.addWidget(hint)
        outer.addWidget(box)

        if focus in self.editors and self.editors[focus].isEnabled():
            self.editors[focus].setFocus()
            self.scroll.ensureWidgetVisible(self.editors[focus])

    @staticmethod
    def _editor(f: forms.FormField) -> QWidget:
        if f.kind == forms.CHECKBOX:
            w = QCheckBox()
            w.setChecked(bool(f.value))
            return w
        if f.kind in (forms.RADIO, forms.CHOICE):
            w = QComboBox()
            w.addItem(_("(none)"), "")
            for value, label in f.options:
                w.addItem(printable(label), value)
            if f.kind == forms.CHOICE and f.combo and f.editable:
                w.setEditable(True)
            index = w.findData(f.value)
            if index >= 0:
                w.setCurrentIndex(index)
            elif w.isEditable():
                w.setEditText(str(f.value))
            return w
        if f.multiline:
            w = QPlainTextEdit(str(f.value))
            w.setTabChangesFocus(True)
            w.setFixedHeight(w.fontMetrics().lineSpacing() * 4 + 12)
            return w
        w = QLineEdit(str(f.value))
        if f.max_len:
            w.setMaxLength(f.max_len)
        return w

    def value(self, f: forms.FormField):
        w = self.editors[f.name]
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, QComboBox):
            if w.isEditable():
                text = w.currentText()
                # a listed label stands for its value
                index = w.findText(text)
                return w.itemData(index) if index > 0 else text
            return w.currentData()
        if isinstance(w, QPlainTextEdit):
            return w.toPlainText()
        return w.text()

    def values(self) -> dict[str, str | bool]:
        return {f.name: v for f in self.fields
                if f.fillable and (v := self.value(f)) != f.value}
