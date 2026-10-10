"""Page tools in the GUI: thumbnail panel, split and merge dialogs."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from PIL.ImageQt import ImageQt
from pyhanko.pdf_utils.reader import PdfFileReader
from PySide6.QtCore import QItemSelectionModel, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)

from .. import pages
from ..i18n import _, ngettext
from ..signing import PasswordError, SigningError
from .dialogs import ask_secret
from .document import PdfDocument

THUMB_WIDTH = 110


class PagesPanel(QListWidget):
    """Page thumbnails. Click to go to a page; Ctrl/Shift-click to select several."""

    pageActivated = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListWidget.IconMode)
        self.setFlow(QListWidget.TopToBottom)
        self.setWrapping(False)
        self.setMovement(QListWidget.Static)
        self.setResizeMode(QListWidget.Adjust)
        self.setIconSize(QSize(THUMB_WIDTH, int(THUMB_WIDTH * 1.5)))
        self.setSpacing(6)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setMinimumWidth(THUMB_WIDTH + 40)
        self.itemClicked.connect(lambda item: self.pageActivated.emit(self.row(item)))

    def set_document(self, doc: PdfDocument | None, current: int = 0) -> None:
        self.clear()
        if doc is None:
            return
        for i in range(len(doc)):
            w, h = doc.display_size(i)
            scale = min(THUMB_WIDTH / w, THUMB_WIDTH * 1.5 / h)
            pm = QPixmap.fromImage(ImageQt(doc.render(i, scale)))
            frame = QPainter(pm)  # white pages would vanish on a white list
            frame.setPen(QColor(150, 150, 150))
            frame.drawRect(0, 0, pm.width() - 1, pm.height() - 1)
            frame.end()
            item = QListWidgetItem(QIcon(pm), str(i + 1))
            item.setTextAlignment(Qt.AlignHCenter)
            self.addItem(item)
        self.show_page(current)

    def show_page(self, page: int) -> None:
        """Mark ``page`` as current without changing a multi-page selection."""
        if not 0 <= page < self.count():
            return
        if len(self.selectedItems()) > 1:
            self.setCurrentRow(page, QItemSelectionModel.NoUpdate)
        else:
            self.setCurrentRow(page, QItemSelectionModel.ClearAndSelect)
        self.scrollToItem(self.item(page))

    def selected_pages(self) -> list[int]:
        return sorted(self.row(i) for i in self.selectedItems())


class SplitDialog(QDialog):
    """Every N pages, or explicit parts; output folder and name pattern."""

    def __init__(self, page_count: int, source: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Split PDF"))
        self.page_count = page_count

        self.every_radio = QRadioButton(_("Every"))
        self.every_spin = QSpinBox()
        self.every_spin.setRange(1, max(1, page_count))
        self.parts_radio = QRadioButton(_("Parts"))
        self.parts_edit = QLineEdit()
        self.parts_edit.setPlaceholderText(_("e.g. 1-3; 4-6; 7-  (one file per part)"))
        group = QButtonGroup(self)
        group.addButton(self.every_radio)
        group.addButton(self.parts_radio)
        self.every_radio.setChecked(True)
        self.parts_edit.textEdited.connect(lambda: self.parts_radio.setChecked(True))
        self.every_spin.valueChanged.connect(lambda: self.every_radio.setChecked(True))

        every_row = QHBoxLayout()
        every_row.addWidget(self.every_radio)
        every_row.addWidget(self.every_spin)
        every_row.addWidget(QLabel(_("page(s) per file")))
        every_row.addStretch()
        parts_row = QHBoxLayout()
        parts_row.addWidget(self.parts_radio)
        parts_row.addWidget(self.parts_edit, 1)

        self.folder_edit = QLineEdit(str(source.parent))
        browse = QPushButton("…")
        browse.setFixedWidth(32)
        browse.clicked.connect(self._browse)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)
        self.pattern_edit = QLineEdit(f"{source.stem}-part{{n}}.pdf")

        form = QFormLayout()
        form.addRow(_("Folder"), folder_row)
        form.addRow(_("File names"), self.pattern_edit)
        self.error = QLabel()
        self.error.setStyleSheet("color: #c62828")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(ngettext("{count} page", "{count} pages", page_count).format(
            count=page_count)))
        lay.addLayout(every_row)
        lay.addLayout(parts_row)
        lay.addLayout(form)
        lay.addWidget(self.error)
        lay.addWidget(buttons)
        self.result_parts: list[list[int]] = []
        self.result_paths: list[Path] = []

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, _("Output folder"), self.folder_edit.text())
        if d:
            self.folder_edit.setText(d)

    def _accept(self) -> None:
        try:
            if self.every_radio.isChecked():
                parts = pages.chunks(self.page_count, self.every_spin.value())
            else:
                specs = [s for s in self.parts_edit.text().split(";") if s.strip()]
                if not specs:
                    raise pages.PageError(_("enter at least one part"))
                parts = [pages.parse_pages(s, self.page_count) for s in specs]
            pattern = self.pattern_edit.text().strip()
            if "{n}" not in pattern:
                # not formatted: {n} is shown literally, it is the placeholder in file names
                raise pages.PageError(_("the file name needs {n} for the part number"))
        except pages.PageError as e:
            self.error.setText(str(e))
            return
        folder = Path(self.folder_edit.text()).expanduser()
        self.result_parts = parts
        self.result_paths = [folder / pattern.replace("{n}", str(i))
                             for i in range(1, len(parts) + 1)]
        self.accept()


@dataclass
class MergeInput:
    label: str
    data: bytes
    password: str | None
    page_count: int
    spec: str = ""  # empty = all pages


class MergeDialog(QDialog):
    """Ordered list of documents (with optional page selections) to combine."""

    def __init__(self, first: MergeInput | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Merge PDFs"))
        self.resize(560, 380)
        self.inputs: list[MergeInput] = [first] if first else []

        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._row_changed)
        self.spec_edit = QLineEdit()
        self.spec_edit.setPlaceholderText(_("all pages, or e.g. 1-3,5"))
        self.spec_edit.textEdited.connect(self._spec_edited)

        buttons_col = QVBoxLayout()
        for text, slot in ((_("Add files…"), self._add), (_("Remove"), self._remove),
                           (_("Up"), lambda: self._move(-1)),
                           (_("Down"), lambda: self._move(1))):
            b = QPushButton(text)
            b.clicked.connect(slot)
            buttons_col.addWidget(b)
        buttons_col.addStretch()

        top = QHBoxLayout()
        top.addWidget(self.list, 1)
        top.addLayout(buttons_col)
        spec_row = QHBoxLayout()
        spec_row.addWidget(QLabel(_("Pages of the selected file:")))
        spec_row.addWidget(self.spec_edit, 1)
        self.error = QLabel()
        self.error.setStyleSheet("color: #c62828")
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText(_("Merge…"))
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(spec_row)
        lay.addWidget(self.error)
        lay.addWidget(box)
        self._refresh(0)

    def _refresh(self, select: int | None = None) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for m in self.inputs:
            self.list.addItem(self._item_text(m))
        self.list.blockSignals(False)
        if select is not None and self.inputs:
            self.list.setCurrentRow(max(0, min(select, len(self.inputs) - 1)))
        self._row_changed(self.list.currentRow())

    def _row_changed(self, row: int) -> None:
        self.spec_edit.setEnabled(0 <= row < len(self.inputs))
        self.spec_edit.setText(self.inputs[row].spec if 0 <= row < len(self.inputs) else "")

    def _spec_edited(self, text: str) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self.inputs):
            self.inputs[row].spec = text.strip()
            self.list.item(row).setText(self._item_text(self.inputs[row]))

    @staticmethod
    def _item_text(m: MergeInput) -> str:
        return ngettext("{name}  ({count} page; {pages})", "{name}  ({count} pages; {pages})",
                        m.page_count).format(name=m.label, count=m.page_count,
                                             pages=m.spec or _("all pages"))

    def add_file(self, path: Path) -> bool:
        data = path.read_bytes()
        password = None
        if _is_encrypted(data):
            for _attempt in range(3):
                password = ask_secret(self, _("Password for {name}").format(name=path.name))
                if password is None:
                    return False
                try:
                    n = pages.page_count(data, password)
                    break
                except PasswordError:  # wrong password
                    continue
            else:
                raise SigningError(_("wrong password for {name}").format(name=path.name))
        else:
            n = pages.page_count(data)
        self.inputs.append(MergeInput(path.name, data, password, n))
        return True

    def _add(self) -> None:
        files, _filter = QFileDialog.getOpenFileNames(self, _("Add PDFs"), "", "PDF (*.pdf)")
        for f in files:
            try:
                self.add_file(Path(f))
            except (OSError, SigningError, ValueError) as e:
                self.error.setText(_("{name}: {error}").format(name=Path(f).name, error=e))
        self._refresh(len(self.inputs) - 1)

    def _remove(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self.inputs):
            del self.inputs[row]
            self._refresh(row)

    def _move(self, delta: int) -> None:
        row = self.list.currentRow()
        new = row + delta
        if 0 <= row < len(self.inputs) and 0 <= new < len(self.inputs):
            self.inputs[row], self.inputs[new] = self.inputs[new], self.inputs[row]
            self._refresh(new)

    def selection(self) -> list[tuple[bytes, list[int] | None, str | None]]:
        return [(m.data, pages.parse_pages(m.spec, m.page_count) if m.spec else None,
                 m.password) for m in self.inputs]

    def _accept(self) -> None:
        if len(self.inputs) < 1:
            self.error.setText(_("add at least one file"))
            return
        try:
            self.selection()
        except pages.PageError as e:
            self.error.setText(str(e))
            return
        self.accept()


def _is_encrypted(data: bytes) -> bool:
    """Whether the PDF needs a password; unreadable files say no (and fail later)."""
    try:
        return bool(PdfFileReader(io.BytesIO(data), strict=False).encrypted)
    except Exception:
        return False


def page_label(pages_: list[int]) -> str:
    if len(pages_) == 1:
        return _("page {page}").format(page=pages_[0] + 1)
    return ngettext("{count} page", "{count} pages", len(pages_)).format(count=len(pages_))

