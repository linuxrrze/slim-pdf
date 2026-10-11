"""Page thumbnails: navigation, selection and the pages' context menu."""

from __future__ import annotations

import time

from PIL.ImageQt import ImageQt
from PySide6.QtCore import QItemSelectionModel, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem

from .document import PdfDocument

THUMB_WIDTH = 110
BATCH_SECONDS = 0.03  # thumbnails are rendered in small batches between events


class PagesPanel(QListWidget):
    """Page thumbnails. Click to go to a page; Ctrl/Shift-click to select
    several; right-click for the page menu (``menuRequested``); drag pages
    (or Alt+Up/Down) to reorder them (``moveRequested``). A drag that ends
    in another document (the main window switches documents while it goes
    on) asks for a copy instead (``copyRequested``)."""

    pageActivated = Signal(int)
    menuRequested = Signal(QPoint)  # global position
    deleteRequested = Signal()  # Delete key on the selected pages
    moveRequested = Signal(list, int)  # pages, to go before this page (count: the end)
    # the document the pages were dragged from, its pages, before this page here
    copyRequested = Signal(object, list, int)

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
        # Qt starts the drag; the drop only asks for the edit (moveRequested),
        # and the list is rebuilt from the edited document.
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.viewport().setAcceptDrops(True)  # off with Static movement
        self._dragging = False
        self._drag_source: PdfDocument | None = None  # document and pages being dragged
        self._drag_pages: list[int] = []
        self._drop_row: int | None = None
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
        if event.modifiers() == Qt.AltModifier and event.key() in (Qt.Key_Up, Qt.Key_Down):
            pages = self.selected_pages() or [self.currentRow()]
            if event.key() == Qt.Key_Up and pages[0] > 0:
                self.moveRequested.emit(pages, pages[0] - 1)
            elif event.key() == Qt.Key_Down and pages[-1] < self.count() - 1:
                self.moveRequested.emit(pages, pages[-1] + 2)
            return
        super().keyPressEvent(event)

    # reordering by drag and drop ---------------------------------------------------

    def drop_row(self, pos: QPoint) -> int:
        """Where pages dropped at ``pos`` (viewport) go: before this row."""
        for row in range(self.count()):
            if pos.y() < self.visualItemRect(self.item(row)).center().y():
                return row
        return self.count()

    @property
    def dragged(self) -> tuple[PdfDocument | None, list[int]]:
        """(document, pages) of the drag going on, else (None, [])."""
        return (self._drag_source, list(self._drag_pages)) if self._dragging else (None, [])

    def startDrag(self, actions) -> None:
        self._dragging = True
        self._drag_source, self._drag_pages = self._doc, self.selected_pages()
        try:
            super().startDrag(actions | Qt.CopyAction)  # copied into another document
        finally:
            self._dragging = False
            self._drag_source, self._drag_pages = None, []
            self._set_drop_row(None)

    def dragEnterEvent(self, event) -> None:
        if self._dragging:  # only our own pages, no files from outside
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if not self._dragging:
            event.ignore()
            return
        super().dragMoveEvent(event)  # scrolls near the edges
        self._set_drop_row(self.drop_row(event.position().toPoint()))
        if self._doc is not self._drag_source:  # into another document: a copy
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self._set_drop_row(None)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        row = self.drop_row(event.position().toPoint())
        self._set_drop_row(None)
        if not self._dragging:
            event.ignore()
            return
        # IgnoreAction: Qt must not move or remove the items itself
        event.setDropAction(Qt.IgnoreAction)
        event.accept()
        source, pages = self._drag_source, list(self._drag_pages)
        if not pages:
            return
        # after the drag has finished: the edit rebuilds this list
        if source is self._doc:
            QTimer.singleShot(0, lambda: self.moveRequested.emit(pages, row))
        else:
            QTimer.singleShot(0, lambda: self.copyRequested.emit(source, pages, row))

    def _set_drop_row(self, row: int | None) -> None:
        if row != self._drop_row:
            self._drop_row = row
            self.viewport().update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._drop_row is None or not self.count():
            return
        if self._drop_row < self.count():
            y = self.visualItemRect(self.item(self._drop_row)).top() - self.spacing() // 2
        else:
            y = self.visualItemRect(self.item(self.count() - 1)).bottom() + self.spacing() // 2
        painter = QPainter(self.viewport())
        painter.setPen(QPen(self.palette().color(QPalette.Highlight), 3))
        painter.drawLine(4, y, self.viewport().width() - 4, y)
        painter.end()

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

    @property
    def document(self) -> PdfDocument | None:
        return self._doc

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
