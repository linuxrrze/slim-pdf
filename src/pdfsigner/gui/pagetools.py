"""Page thumbnails: navigation, selection and the pages' context menu."""

from __future__ import annotations

from PIL.ImageQt import ImageQt
from PySide6.QtCore import QItemSelectionModel, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem

from .document import PdfDocument

THUMB_WIDTH = 110


class PagesPanel(QListWidget):
    """Page thumbnails. Click to go to a page; Ctrl/Shift-click to select
    several; right-click for the page menu (``menuRequested``)."""

    pageActivated = Signal(int)
    menuRequested = Signal(QPoint)  # global position
    deleteRequested = Signal()  # Delete key on the selected pages

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
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.Delete) or event.key() == Qt.Key_Backspace:
            self.deleteRequested.emit()
            return
        super().keyPressEvent(event)

    def _context_menu(self, pos: QPoint) -> None:
        item = self.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():  # right-click outside the selection: just this page
            self.setCurrentItem(item, QItemSelectionModel.ClearAndSelect)
        self.pageActivated.emit(self.row(item))
        self.menuRequested.emit(self.viewport().mapToGlobal(pos))

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
