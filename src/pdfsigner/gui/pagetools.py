"""Page thumbnails: navigation, selection and the pages' context menu."""

from __future__ import annotations

import time

from PIL.ImageQt import ImageQt
from PySide6.QtCore import QItemSelectionModel, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem

from .document import PdfDocument

THUMB_WIDTH = 110
BATCH_SECONDS = 0.03  # thumbnails are rendered in small batches between events


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
        self._doc: PdfDocument | None = None
        self._pending: set[int] = set()  # pages still showing a placeholder
        self._timer = QTimer(self)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._render_batch)

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
        """Placeholders at once; the thumbnails follow in the background,
        the visible ones first (a long document would block the window)."""
        self.clear()
        self._timer.stop()
        self._doc, self._pending = doc, set()
        if doc is None:
            return
        for i in range(len(doc)):
            w, h = doc.display_size(i)
            scale = min(THUMB_WIDTH / w, THUMB_WIDTH * 1.5 / h)
            pm = QPixmap(max(1, round(w * scale)), max(1, round(h * scale)))
            pm.fill(QColor(245, 245, 245))
            item = QListWidgetItem(QIcon(_framed(pm)), str(i + 1))
            item.setTextAlignment(Qt.AlignHCenter)
            self.addItem(item)
            self._pending.add(i)
        self.show_page(current)
        self._timer.start()

    def _next_page(self) -> int:
        """The current page, then the visible ones, then the first one left."""
        if self.currentRow() in self._pending:
            return self.currentRow()
        top = self.indexAt(self.viewport().rect().topLeft()).row()
        bottom = self.indexAt(self.viewport().rect().bottomLeft()).row()
        top = max(top, 0)
        bottom = bottom if bottom >= 0 else self.count() - 1
        visible = [i for i in range(top, bottom + 1) if i in self._pending]
        return visible[0] if visible else min(self._pending)

    def _render_batch(self) -> None:
        start = time.monotonic()
        while self._pending and self._doc is not None:
            i = self._next_page()
            self._pending.discard(i)
            w, h = self._doc.display_size(i)
            scale = min(THUMB_WIDTH / w, THUMB_WIDTH * 1.5 / h)
            pm = QPixmap.fromImage(ImageQt(self._doc.render(i, scale)))
            self.item(i).setIcon(QIcon(_framed(pm)))
            if time.monotonic() - start > BATCH_SECONDS:
                break  # the rest after the next round of events
        if not self._pending or self._doc is None:
            self._timer.stop()

    def finish_thumbnails(self) -> None:
        """Render all remaining thumbnails now (screenshots, tests)."""
        while self._pending and self._doc is not None:
            self._render_batch()

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


def _framed(pm: QPixmap) -> QPixmap:
    frame = QPainter(pm)  # white pages would vanish on a white list
    frame.setPen(QColor(150, 150, 150))
    frame.drawRect(0, 0, pm.width() - 1, pm.height() - 1)
    frame.end()
    return pm
