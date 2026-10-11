"""The form panel (right-hand side, Edit tab): one editor per form field,
in form order; Apply fills in the changed fields, Discard resets them."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import forms
from ..i18n import _
from ..signing import printable
from .screen import scrollable


class FormPanel(QWidget):
    """``applyRequested(values)``: the fields whose value changed."""

    applyRequested = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fields: list[forms.FormField] = []
        self.editors: dict[str, QWidget] = {}

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.form = QWidget()
        self.form_layout = QFormLayout()
        self.form_layout.setRowWrapPolicy(QFormLayout.WrapLongRows)  # a narrow panel
        holder = QVBoxLayout(self.form)  # the fields at the top, not spread out
        holder.setContentsMargins(0, 0, 0, 0)
        holder.addLayout(self.form_layout)
        holder.addStretch()
        self.scroll = scrollable(self.form)
        self.apply_btn = QPushButton(_("Apply"))
        self.apply_btn.setDefault(True)
        self.apply_btn.setToolTip(_("Fill in the changed fields (Undo takes it back)"))
        self.apply_btn.clicked.connect(lambda: self.applyRequested.emit(self.values()))
        self.discard_btn = QPushButton(_("Discard"))
        self.discard_btn.setToolTip(_("Back to the values in the document"))
        self.discard_btn.clicked.connect(self.discard)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.discard_btn)
        buttons.addWidget(self.apply_btn)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>{}</b>".format(_("Form fields"))))
        lay.addWidget(self.hint)
        lay.addWidget(self.scroll, 1)
        lay.addLayout(buttons)
        self.set_fields([], has_document=False)

    def set_fields(self, fields: list[forms.FormField], has_document: bool = True) -> None:
        """Editors for ``fields`` with their current values."""
        self.fields = list(fields)
        while self.form_layout.rowCount():
            self.form_layout.removeRow(0)
        self.editors = {}
        for f in self.fields:
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
            self.form_layout.addRow(label, editor)
        if not has_document:
            self.hint.setText(_("Open a PDF with a form to fill it in here."))
        elif not self.fields:
            self.hint.setText(_("This document has no form fields to fill in."))
        else:
            self.hint.setText(_("Click a field on the page, or edit it here; Apply fills "
                                "in what you changed."))
        self.scroll.setVisible(bool(self.fields))
        self._update_buttons()

    def discard(self) -> None:
        self.set_fields(self.fields)

    def focus(self, name: str) -> None:
        editor = self.editors.get(name)
        if editor is not None and editor.isEnabled():
            editor.setFocus()
            self.scroll.ensureWidgetVisible(editor)

    def _editor(self, f: forms.FormField) -> QWidget:
        changed = self._update_buttons
        if f.kind == forms.CHECKBOX:
            w = QCheckBox()
            w.setChecked(bool(f.value))
            w.toggled.connect(changed)
            return w
        if f.kind in (forms.RADIO, forms.CHOICE):
            w = QComboBox()
            w.addItem(_("(none)"), "")
            for value, label in f.options:
                w.addItem(printable(label), value)
            if f.kind == forms.CHOICE and f.combo and f.editable:
                w.setEditable(True)
                w.editTextChanged.connect(changed)
            index = w.findData(f.value)
            if index >= 0:
                w.setCurrentIndex(index)
            elif w.isEditable():
                w.setEditText(str(f.value))
            w.currentIndexChanged.connect(changed)
            return w
        if f.multiline:
            w = QPlainTextEdit(str(f.value))
            w.setTabChangesFocus(True)
            w.setFixedHeight(w.fontMetrics().lineSpacing() * 4 + 12)
            w.textChanged.connect(changed)
            return w
        w = QLineEdit(str(f.value))
        if f.max_len:
            w.setMaxLength(f.max_len)
        w.textChanged.connect(changed)
        w.returnPressed.connect(self.apply_btn.click)
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
        return {f.name: v for f in self.fields  # (editors still being built: skipped)
                if f.fillable and f.name in self.editors and (v := self.value(f)) != f.value}

    def _update_buttons(self, *_args) -> None:
        changed = bool(self.fields) and bool(self.values())
        self.apply_btn.setEnabled(changed)
        self.discard_btn.setEnabled(changed)
