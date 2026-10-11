"""The form panel (right-hand side, Edit tab): one editor per form field,
in form order. A change is filled in as soon as it is made: a check box,
radio button or choice when it is picked, text when the editor is left
(Enter, Tab or a click elsewhere). Every change is one Undo step."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QRadioButton,
    QSizePolicy,
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
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)  # no gap below
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
        # only the newly chosen button, not the one that went off
        self.group.buttonToggled.connect(lambda button, on: on and changed())

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
    """``applyRequested(values)``: a field was changed ({name: value});
    ``fieldFocused(name)``: the cursor went into a field's editor."""

    applyRequested = Signal(dict)
    fieldFocused = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fields: list[forms.FormField] = []
        self.editors: dict[str, QWidget] = {}
        self._owner: dict[QWidget, str] = {}
        self.labels: dict[tuple[str, str], str] = {}
        self._quiet = False  # setting values ourselves: no commits

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

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>{}</b>".format(_("Form fields"))))
        lay.addWidget(self.hint)
        lay.addWidget(self.scroll, 1)
        self.set_fields([], has_document=False)

    def set_fields(self, fields: list[forms.FormField], labels=None,
                   has_document: bool = True) -> None:
        """Editors for ``fields`` with their current values; ``labels`` names
        radio buttons: {(field name, state): label}. The same fields as
        before (the document after a change) keep their editors, so the
        cursor stays where it is."""
        fields = list(fields)
        if labels is not None:
            self.labels = labels
        same = [(f.name, f.kind) for f in fields] == [(f.name, f.kind) for f in self.fields]
        self.fields = fields
        if same and self.editors:
            self.refresh()
            return
        while self.form_layout.rowCount():
            self.form_layout.removeRow(0)
        self.editors = {}
        self._owner = {}
        self._quiet = True
        try:
            for f in self.fields:
                editor = self._editor(f)
                self.editors[f.name] = editor
                for w in [editor, *editor.findChildren(QWidget)]:  # e.g. radio buttons
                    self._owner[w] = f.name
                    w.installEventFilter(self)
                label = printable(f.label or f.name)
                if f.page is not None:
                    label = _("{name} (p. {page})").format(name=label, page=f.page + 1)
                if not f.fillable:
                    editor.setEnabled(False)
                    editor.setToolTip(_("Locked by a signature") if f.locked
                                      else _("Read-only"))
                if f.label:
                    editor.setToolTip("\n".join(filter(None, [editor.toolTip(),
                                                              printable(f.name)])))
                caption = QLabel(label)
                caption.setToolTip(self.describe(f))
                self.form_layout.addRow(caption, editor)
        finally:
            self._quiet = False
        if not has_document:
            self.hint.setText(_("Open a PDF with a form to fill it in here."))
        elif not self.fields:
            self.hint.setText(_("This document has no form fields to fill in."))
        else:
            self.hint.setText(_("Click a field on the page, or edit it here. Changes are "
                                "filled in right away; Undo takes them back."))
        self.scroll.setVisible(bool(self.fields))

    def describe(self, f: forms.FormField) -> str:
        """What kind of input a field takes, for its label's tooltip."""
        lines = [_("Field: {name}").format(name=printable(f.name))]
        if f.label:
            lines.append(_("Label: {label}").format(label=printable(f.label)))
        if f.kind == forms.TEXT:
            kind = _("text, several lines") if f.multiline else _("text, one line")
            if f.flags & forms.PASSWORD:
                kind = _("password (shown as *)")
            lines.append(_("Input: {kind}").format(kind=kind))
            if f.max_len and f.flags & forms.COMB and not f.multiline:
                lines.append(_("{n} character boxes").format(n=f.max_len))
            elif f.max_len:
                lines.append(_("At most {n} characters").format(n=f.max_len))
        elif f.kind == forms.CHECKBOX:
            lines.append(_("Input: check box"))
        elif f.kind == forms.RADIO:
            choices = [self.labels.get((f.name, state), state) for state, _l in f.options]
            lines.append(_("Input: radio buttons ({choices})").format(
                choices=", ".join(map(printable, choices))))
        else:
            kind = (_("drop-down, other text allowed") if f.combo and f.editable
                    else _("drop-down") if f.combo else _("list"))
            lines.append(_("Input: {kind} with {n} choices").format(kind=kind,
                                                                   n=len(f.options)))
        if f.flags & forms.REQUIRED:
            lines.append(_("Required"))
        if f.locked:
            lines.append(_("Locked by a signature"))
        elif f.read_only:
            lines.append(_("Read-only"))
        for w in f.widgets[:1]:
            if w.page is not None:
                width, height = w.rect[2] - w.rect[0], w.rect[3] - w.rect[1]
                lines.append(_("Page {page}, {width:.0f} × {height:.0f} pt").format(
                    page=w.page + 1, width=width, height=height))
        return "\n".join(lines)

    def refresh(self) -> None:
        """Show the fields' values (e.g. after a change was refused)."""
        self._quiet = True
        try:
            for f in self.fields:
                w = self.editors[f.name]
                if isinstance(w, QCheckBox):
                    w.setChecked(bool(f.value))
                elif isinstance(w, RadioGroup):
                    w.set_value(str(f.value))
                elif isinstance(w, QComboBox):
                    index = w.findData(f.value)
                    if index >= 0:
                        w.setCurrentIndex(index)
                    elif w.isEditable():
                        w.setEditText(str(f.value))
                elif isinstance(w, QPlainTextEdit):
                    if w.toPlainText() != f.value:
                        w.setPlainText(str(f.value))
                elif w.text() != f.value:
                    w.setText(str(f.value))
                w.setEnabled(f.fillable)
        finally:
            self._quiet = False

    def eventFilter(self, obj, event) -> bool:
        if obj in self._owner:
            if event.type() == QEvent.FocusIn:
                self.fieldFocused.emit(self._owner[obj])
            elif event.type() == QEvent.FocusOut and isinstance(obj, QPlainTextEdit):
                self.commit(self._owner[obj])  # multi-line text: when it is left
        return False

    def focus(self, name: str) -> None:
        editor = self.editors.get(name)
        if editor is not None and editor.isEnabled():
            editor.setFocus()
            self.scroll.ensureWidgetVisible(editor)

    def show_field(self, name: str) -> None:
        """Scroll to a field's editor (also a disabled one)."""
        editor = self.editors.get(name)
        if editor is not None:
            self.scroll.ensureWidgetVisible(editor)

    def commit(self, name: str) -> None:
        """Ask for the field's new value to be filled in, if it changed."""
        if self._quiet:
            return
        f = next((f for f in self.fields if f.name == name), None)
        if f is None or not f.fillable or name not in self.editors:
            return
        value = self.value(f)
        if value != f.value:
            self.applyRequested.emit({name: value})

    def _editor(self, f: forms.FormField) -> QWidget:
        def changed(*_args, name=f.name):
            self.commit(name)

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
            if f.combo and f.editable:
                w.setEditable(True)
                w.lineEdit().editingFinished.connect(changed)
            index = w.findData(f.value)
            if index >= 0:
                w.setCurrentIndex(index)
            elif w.isEditable():
                w.setEditText(str(f.value))
            w.activated.connect(changed)  # picked by the user, not set by us
            return w
        if f.multiline:
            w = QPlainTextEdit(str(f.value))
            w.setTabChangesFocus(True)  # Tab leaves it (and fills it in)
            w.setFixedHeight(w.fontMetrics().lineSpacing() * 4 + 12)
            return w
        w = QLineEdit(str(f.value))
        if f.max_len:
            w.setMaxLength(f.max_len)
        w.editingFinished.connect(changed)  # Enter, Tab or a click elsewhere
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
