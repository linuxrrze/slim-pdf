"""The form panel (right-hand side, Edit tab): one editor per form field,
in form order; Apply fills in the changed fields, Discard resets them."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from .. import forms
from ..i18n import _
from ..signing import printable
from .screen import scrollable


class RadioGroup(QWidget):
    """A radio button field: one button per choice, in a row (wrapping into
    a column when there are many). ``value`` is the chosen state, or ""."""

    def __init__(self, options: list[tuple[str, str]], value: str, changed):
        super().__init__()
        lay = (QHBoxLayout if len(options) <= 3 else QVBoxLayout)(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QRadioButton] = {}
        for state, label in options:
            b = QRadioButton(label)
            b.setChecked(state == value)
            self.group.addButton(b)
            self.buttons[state] = b
            lay.addWidget(b)
        if isinstance(lay, QHBoxLayout):
            lay.addStretch()
        self.group.buttonToggled.connect(lambda *_a: changed())

    @property
    def value(self) -> str:
        return next((s for s, b in self.buttons.items() if b.isChecked()), "")

    def set_value(self, value: str) -> None:
        self.group.setExclusive(False)  # so that none can be checked
        for state, b in self.buttons.items():
            b.setChecked(state == value)
        self.group.setExclusive(True)

    def setFocus(self) -> None:  # noqa: N802 - Qt's name
        target = self.buttons.get(self.value) or next(iter(self.buttons.values()), None)
        if target is not None:
            target.setFocus()


class FormPanel(QWidget):
    """``applyRequested(values)``: the fields whose value changed."""

    applyRequested = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fields: list[forms.FormField] = []
        self.editors: dict[str, QWidget] = {}
        self.labels: dict[tuple[str, str], str] = {}

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

    def set_fields(self, fields: list[forms.FormField], labels=None,
                   has_document: bool = True) -> None:
        """Editors for ``fields`` with their current values; ``labels`` names
        radio buttons: {(field name, state): label}."""
        self.fields = list(fields)
        if labels is not None:
            self.labels = labels
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
        self.set_fields(self.fields, self.labels)

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
        if f.kind == forms.RADIO:
            options = [(state, self.labels.get((f.name, state), state))
                       for state, _label in f.options]
            return RadioGroup(options, str(f.value), changed)
        if f.kind == forms.CHOICE:
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
        if isinstance(w, RadioGroup):
            return w.value
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
