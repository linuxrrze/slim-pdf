"""Page view: the pages one below the other, with signature and form fields;
places the signature, selects text.

Scene units are display points (see document.py). Each page is a container
item; what is on a page is its child, in the page's own display points, so
the placed item's geometry converts directly to PDF coordinates via
PdfDocument.rect_to_pdf.
"""

from __future__ import annotations

import bisect
import math

from PIL.ImageQt import ImageQt

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QLineEdit,
    QPlainTextEdit,
    QStyle,
    QWidget,
)

from ..i18n import _
from ..signing import SigField, printable
from .document import PdfDocument

DEFAULT_WIDTH = 150.0  # points
MIN_WIDTH = 30.0
HANDLE = 10.0  # resize handle size in screen pixels


# The whole page is rendered at most this sharp (~32 MB as RGBA); closer up,
# only the visible part is rendered at full sharpness ("detail"), after
# zooming or scrolling pauses for RENDER_DELAY ms.
MAX_PAGE_PIXELS = 8_000_000
RENDER_DELAY = 120
WHEEL_ZOOM = 1.15  # per wheel notch
MAX_ZOOM = 6.0
PAGE_GAP = 12.0  # points between pages
MARGIN = 20.0  # points around the pages

class SignatureItem(QGraphicsObject):
    """Movable signature preview; the corner handle scales it keeping the aspect ratio."""

    geometryChanged = Signal()

    def __init__(self, pixmap: QPixmap | None, aspect: float, width: float, bounds: QRectF):
        super().__init__()
        self._pixmap = pixmap
        self._aspect = aspect
        self._w = width
        self._bounds = bounds  # page rect in scene coords
        self._resizing = False
        self.setFlags(
            QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.SizeAllCursor)
        self.setZValue(10)

    # geometry -----------------------------------------------------------------

    @property
    def size(self) -> tuple[float, float]:
        return self._w, self._w / self._aspect

    def scene_rect(self) -> QRectF:
        """Its rect on the page (display points)."""
        w, h = self.size
        return QRectF(self.pos(), QPointF(self.pos().x() + w, self.pos().y() + h))

    def boundingRect(self) -> QRectF:
        w, h = self.size
        return QRectF(0, 0, w, h).adjusted(-2, -2, 2, 2)

    def set_appearance(self, pixmap: QPixmap | None, aspect: float) -> None:
        self.prepareGeometryChange()
        self._pixmap, self._aspect = pixmap, aspect
        self._set_width(self._w)

    def _handle_size(self) -> float:
        # Constant on screen regardless of zoom.
        views = self.scene().views() if self.scene() else []
        scale = views[0].transform().m11() if views else 1.0
        return HANDLE / scale

    def _handle_rect(self) -> QRectF:
        w, h = self.size
        s = self._handle_size()
        return QRectF(w - s, h - s, s, s)

    def _max_width_at(self, pos: QPointF) -> float:
        b = self._bounds
        return max(MIN_WIDTH, min(b.right() - pos.x(), (b.bottom() - pos.y()) * self._aspect))

    def _set_width(self, w: float) -> None:
        self.prepareGeometryChange()
        self._w = max(MIN_WIDTH, min(w, self._max_width_at(self.pos())))
        self.update()
        self.geometryChanged.emit()

    def scale_by(self, factor: float) -> None:
        self._set_width(self._w * factor)

    def center_on(self, p: QPointF) -> None:
        w, h = self.size
        self.setPos(p - QPointF(w / 2, h / 2))

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange:
            w, h = self.size
            b = self._bounds
            x = min(max(value.x(), b.left()), b.right() - w)
            y = min(max(value.y(), b.top()), b.bottom() - h)
            return QPointF(x, y)
        if change == QGraphicsItem.ItemPositionHasChanged:
            self.geometryChanged.emit()
        return super().itemChange(change, value)

    # painting -----------------------------------------------------------------

    def paint(self, painter: QPainter, option, widget=None) -> None:
        w, h = self.size
        r = QRectF(0, 0, w, h)
        painter.fillRect(r, QColor(255, 255, 210, 110))
        if self._pixmap is not None:
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.drawPixmap(r, self._pixmap, QRectF(self._pixmap.rect()))
        else:
            painter.setPen(QColor(60, 60, 60))
            painter.drawText(r, Qt.AlignCenter, _("Signature"))
        pen = QPen(QColor(30, 90, 200), 0, Qt.DashLine)
        painter.setPen(pen)
        painter.drawRect(r)
        painter.fillRect(self._handle_rect(), QColor(30, 90, 200))

    # interaction --------------------------------------------------------------

    def hoverMoveEvent(self, event):
        on_handle = self._handle_rect().contains(event.pos())
        self.setCursor(Qt.SizeFDiagCursor if on_handle else Qt.SizeAllCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._handle_rect().contains(event.pos()):
            self._resizing = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resizing:
            p = event.pos()
            # Follow whichever axis the mouse pulls further; aspect stays fixed.
            self._set_width(max(p.x(), p.y() * self._aspect))
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._resizing = False
        super().mouseReleaseEvent(event)


class FieldItem(QGraphicsRectItem):
    STYLES = {
        "empty": (QColor(30, 90, 200), QColor(30, 90, 200, 40)),
        "selected": (QColor(30, 90, 200), QColor(30, 90, 200, 110)),
        "signed": (QColor(20, 140, 60), QColor(20, 140, 60, 30)),
        "locked": (QColor(200, 40, 40), QColor(200, 40, 40, 40)),
    }

    def __init__(self, f: SigField, rect: QRectF):
        super().__init__(rect)
        self.field = f
        self.setZValue(5)
        name = printable(f.name)
        if f.filled:
            tip = _("{name}: signed").format(name=name)
        elif f.locked:
            tip = _("{name}: locked").format(name=name)
        else:
            tip = _("{name}: empty signature field").format(name=name)
        self.setToolTip(tip)
        if not f.locked:  # a click signs it, or shows the signature
            self.setCursor(Qt.PointingHandCursor)
        self.set_selected(False)

    @property
    def clickable(self) -> bool:
        return not self.field.filled and not self.field.locked

    def set_selected(self, selected: bool) -> None:
        f = self.field
        key = "signed" if f.filled else "locked" if f.locked else (
            "selected" if selected else "empty")
        pen_color, fill = self.STYLES[key]
        self.setPen(QPen(pen_color, 0, Qt.SolidLine if f.filled else Qt.DashLine))
        self.setBrush(QBrush(fill))


class FormItem(QGraphicsRectItem):
    """A fillable form field's widget: invisible (the page shows the field)
    until the mouse is over it; a click fills it in."""

    def __init__(self, name: str, rect: QRectF, fillable: bool, label: str = "",
                 state: str = ""):
        super().__init__(rect)
        self.name, self.fillable, self.state = name, fillable, state  # state: the button's
        self.setZValue(4)
        self.setPen(QPen(Qt.NoPen))
        self.setAcceptHoverEvents(True)
        label = printable(label or name)
        self.setToolTip(_("{name}: click to fill in").format(name=label) if fillable
                        else _("{name}: can't be changed").format(name=label))
        if fillable:
            self.setCursor(Qt.IBeamCursor)
        self.active = False

    def set_active(self, active: bool) -> None:
        """The field being edited: framed and tinted."""
        self.active = active
        pen = QPen(QColor(30, 90, 200), 2, Qt.SolidLine) if active else QPen(Qt.NoPen)
        pen.setCosmetic(True)  # 2 pixels at any zoom
        self.setPen(pen)
        self.setBrush(QBrush(QColor(30, 90, 200, 35)) if active else QBrush(Qt.NoBrush))

    def hoverEnterEvent(self, event):
        if self.fillable and not self.active:
            self.setPen(QPen(QColor(30, 90, 200), 0, Qt.DotLine))
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.set_active(self.active)
        super().hoverLeaveEvent(event)


def _tab_step(event) -> int:
    """+1 for Tab, -1 for Shift+Tab (Backtab), else 0."""
    if event.type() != QEvent.KeyPress:
        return 0
    if event.key() == Qt.Key_Backtab or (event.key() == Qt.Key_Tab
                                         and event.modifiers() & Qt.ShiftModifier):
        return -1
    return 1 if event.key() == Qt.Key_Tab else 0


class _InlineLine(QLineEdit):
    """One-line editor over a text field: Enter or leaving it fills it in,
    Esc cancels, Tab/Shift+Tab fill it in and go to the next/previous field."""

    def __init__(self, view: PageView):
        super().__init__(view.viewport())
        self.view = view

    def event(self, event):
        if step := _tab_step(event):  # before Qt moves the focus itself
            self.view.finish_text_edit(True, focus_view=True, step=step)
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            return self.view.finish_text_edit(False, focus_view=True)
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            return self.view.finish_text_edit(True, focus_view=True)
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.view.finish_text_edit(True)


class _InlineText(QPlainTextEdit):
    """Multi-line editor over a text field: Ctrl+Enter or leaving it fills
    it in, Esc cancels, Tab/Shift+Tab fill it in and go to the next/previous
    field; Enter starts a new line."""

    def __init__(self, view: PageView):
        super().__init__(view.viewport())
        self.view = view
        self.setTabChangesFocus(True)

    def event(self, event):
        if step := _tab_step(event):
            self.view.finish_text_edit(True, focus_view=True, step=step)
            return True
        return super().event(event)

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, text: str) -> None:  # noqa: N802 - like QLineEdit
        self.setPlainText(text)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            return self.view.finish_text_edit(False, focus_view=True)
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() & Qt.ControlModifier:
            return self.view.finish_text_edit(True, focus_view=True)
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.view.finish_text_edit(True)


def _join_lines(rects: list[tuple]) -> list[tuple]:
    """One rect per line: pdfium's runs overlap, which would show darker
    where the translucent highlights do."""
    out: list[tuple] = []
    for x, y, w, h in sorted(rects, key=lambda r: (r[1] + r[3] / 2, r[0])):
        if out:
            ox, oy, ow, oh = out[-1]
            if min(oy + oh, y + h) - max(oy, y) > 0.5 * min(oh, h):  # the same line
                left, top = min(ox, x), min(oy, y)
                out[-1] = (left, top, max(ox + ow, x + w) - left, max(oy + oh, y + h) - top)
                continue
        out.append((x, y, w, h))
    return out


class _Page:
    """One page in the scene: a container at the page's place (children use
    the page's display points), its picture and the sharper detail."""

    def __init__(self, scene: QGraphicsScene, index: int, x: float, y: float,
                 w: float, h: float):
        self.index, self.w, self.h = index, w, h
        self.item = QGraphicsRectItem(QRectF(0, 0, w, h))
        self.item.setPen(QPen(Qt.NoPen))
        self.item.setBrush(QBrush(Qt.white))  # until rendered
        self.item.setPos(x, y)
        scene.addItem(self.item)
        shadow = QGraphicsRectItem(QRectF(3, 3, w, h), self.item)
        shadow.setPen(QPen(Qt.NoPen))
        shadow.setBrush(QBrush(QColor(0, 0, 0, 80)))
        shadow.setFlag(QGraphicsItem.ItemStacksBehindParent)
        self.pixmap = QGraphicsPixmapItem(self.item)
        self.pixmap.setTransformationMode(Qt.SmoothTransformation)
        self.detail = QGraphicsPixmapItem(self.item)
        self.detail.setZValue(1)  # over the page, under fields and the signature
        self.detail.setTransformationMode(Qt.SmoothTransformation)
        self.detail.setVisible(False)
        self.scale = 0.0  # pixels per point of the picture; 0: none yet
        self.decorations: list[QGraphicsRectItem] = []  # selection, search matches
        self.decorated = False

    @property
    def rect(self) -> QRectF:
        return QRectF(0, 0, self.w, self.h)

    @property
    def top(self) -> float:
        return self.item.pos().y()

    def drop_picture(self) -> None:
        self.pixmap.setPixmap(QPixmap())
        self.detail.setPixmap(QPixmap())
        self.detail.setVisible(False)
        self.scale = 0.0


class PageView(QGraphicsView):
    """Shows the pages one below the other (continuous scrolling); ``page``
    is the one in the middle of the view. Emits ``targetChanged`` when the
    signing target changes, ``pageChanged`` when another page comes to the
    middle.

    By default the wheel zooms and dragging moves the pages (a placed
    signature is dragged itself). In place mode, one click places the
    signature or picks an empty field, then the view is back to the default
    (``placeModeChanged``). Outside place mode, a click (not a drag) on a
    field emits ``fieldActivated(name)``, on a form field
    ``formFieldActivated(name, button state)``; ``edit_text`` opens an
    editor over a text field, ``textEdited(name, text)`` reports its result.
    Dragging over text selects it instead, also on to further pages
    (double-click: a word), shown with ``textSelectionChanged``;
    ``selected_text`` is what is selected, and ``set_search_matches``
    highlights what a search found.
    In crop mode, dragging draws a rectangle on a page;
    ``cropSelected(page, display rect)`` is emitted on release. Insert mode
    works the same way, for one rectangle (or a click: an empty rectangle at
    that point), emitted as ``insertSelected``; then it ends.

    Positions on a page are its display points (see document.py), relative
    to its top left corner: items on a page are children of its container.
    """

    targetChanged = Signal()
    pageChanged = Signal(int)
    cropSelected = Signal(int, QRectF)
    insertSelected = Signal(int, QRectF)
    placeModeChanged = Signal(bool)
    fieldActivated = Signal(str)
    formFieldActivated = Signal(str, str)
    textEdited = Signal(str, str)
    formFieldStep = Signal(str, int)  # Tab/Shift+Tab in an editor: (field, +1/-1)
    textSelectionChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setBackgroundBrush(QColor(90, 90, 90))
        self.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.NoAnchor)  # zoom_at() anchors itself
        self.viewport().setMouseTracking(True)  # moves without a button: the text cursor

        self.doc: PdfDocument | None = None
        self.fields: list[SigField] = []
        self.form_fields: list = []  # forms.FormField
        self.active_form_field: str | None = None  # highlighted while being edited
        self.page = 0
        self.zoom = 1.0
        self._fit_width = True
        self.pages: list[_Page] = []
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.setInterval(RENDER_DELAY)
        self._detail_timer.timeout.connect(self._update_detail)
        for bar in (self.horizontalScrollBar(), self.verticalScrollBar()):
            bar.valueChanged.connect(self._scrolled)
        self._going_to = False  # go_to() sets the page itself
        # (page sizes, current page, scroll bar values) when last shown
        self._last_shown: tuple = ([], -1, (0, 0))
        self._field_items: list[FieldItem] = []
        self._form_items: list[FormItem] = []
        # (what a click does, press position) of a press on a field
        self._field_press: tuple[object, object] | None = None
        self._inline: tuple | None = None  # (field, item, editor, text, multiline)
        self._sig_pixmap: QPixmap | None = None
        self._sig_aspect = 3.0

        # Target: either a placed new signature or an existing empty field.
        self.placement: SignatureItem | None = None
        self._placement_page: int | None = None
        self.selected_field: str | None = None

        self.place_mode = False
        self.crop_mode = False
        self.insert_mode = False
        self._crop_page: _Page | None = None  # the page a crop/insert rectangle is on
        self._crop_start: QPointF | None = None
        self._crop_item: QGraphicsRectItem | None = None

        # selected text: (first page, first character, last page, last
        # character), both ends included; while dragging, (page, character)
        # where the drag began, and the mouse for scrolling at the edges
        self.text_selection: tuple[int, int, int, int] | None = None
        self._text_anchor: tuple[int, int] | None = None
        self._drag_pos: QPointF | None = None
        self._edge_timer = QTimer(self)
        self._edge_timer.setInterval(40)
        self._edge_timer.timeout.connect(self._scroll_at_edge)
        # search results: page -> [(start, count)], and the current one
        self._matches: dict[int, list[tuple[int, int]]] = {}
        self._current_match: tuple[int, int, int] | None = None

    # setup ------------------------------------------------------------------

    def set_document(self, doc: PdfDocument | None, fields: list[SigField],
                     page: int = 0, form_fields=()) -> None:
        """Show ``doc`` at ``page``; the same document again (edited, with
        pages of the same sizes) stays scrolled where it was."""
        if self.pages:  # what was shown, also across set_document(None) in between
            self._last_shown = ([(p.w, p.h) for p in self.pages], self.page,
                                (self.horizontalScrollBar().value(),
                                 self.verticalScrollBar().value()))
        layout, last_page, scroll = self._last_shown
        same_layout = (doc is not None and page == last_page
                       and layout == [doc.display_size(i) for i in range(len(doc))])
        self.doc, self.fields, self.form_fields = doc, fields, list(form_fields)
        self.set_place_mode(False)
        self.set_insert_mode(False)
        self.clear_target()
        self.page = min(page, len(doc) - 1) if doc is not None else 0
        self._going_to = True  # the new scroll range isn't a scroll to another page
        try:
            self._build()
            if doc is not None and same_layout:
                self.horizontalScrollBar().setValue(scroll[0])
                self.verticalScrollBar().setValue(scroll[1])
        finally:
            self._going_to = False
        if doc is None:
            return
        if same_layout:
            self._update_visible()
        else:
            self._scroll_to_page(self.page)

    def _update_mode(self) -> None:
        clicking = self.place_mode or self.crop_mode or self.insert_mode
        self.setDragMode(QGraphicsView.NoDrag if clicking else QGraphicsView.ScrollHandDrag)
        self.viewport().setCursor(Qt.CrossCursor if clicking else Qt.OpenHandCursor)
        for it in self._form_items:  # placing or cropping goes over them
            if it.fillable and not clicking:
                it.setCursor(Qt.IBeamCursor)
            else:
                it.unsetCursor()
        for it in self._field_items:
            if self.place_mode and it.clickable:
                it.setCursor(Qt.PointingHandCursor)
            else:
                it.unsetCursor()

    def set_place_mode(self, on: bool) -> None:
        on = on and self.doc is not None
        if on:
            self.set_crop_mode(False)
            self.set_insert_mode(False)
        changed = on != self.place_mode
        self.place_mode = on
        self._update_mode()
        if changed:
            self.placeModeChanged.emit(on)

    def set_crop_mode(self, on: bool) -> None:
        if on:
            self.set_place_mode(False)
            self.set_insert_mode(False)
        self.crop_mode = on
        self._cancel_crop_drag()
        if on:
            self.clear_target()
        self._update_mode()

    def edit_text(self, name: str, text: str, multiline: bool = False,
                  max_len: int | None = None) -> None:
        """Type into the form field ``name`` right on the page: an editor over
        (the first widget of) it, preferably on the current page;
        ``textEdited`` follows."""
        self.finish_text_edit(True)
        items = [i for i in self._form_items if i.name == name]
        item = next((i for i in items if self._page_of(i) == self.page),
                    items[0] if items else None)
        if item is None:
            return
        editor = _InlineText(self) if multiline else _InlineLine(self)
        if max_len and not multiline:
            editor.setMaxLength(max_len)
        editor.setText(text)
        self._inline = (name, item, editor, text, multiline)
        self.ensureVisible(item.mapRectToScene(item.rect()), 40, 40)
        self._place_inline()
        editor.show()
        editor.setFocus()
        editor.selectAll()

    @property
    def inline_editor(self) -> QWidget | None:
        return self._inline[2] if self._inline else None

    def _place_inline(self) -> None:
        if self._inline is None:
            return
        _name, item, editor, _text, multiline = self._inline
        r = self.mapFromScene(item.mapRectToScene(item.rect())).boundingRect()
        font = editor.font()
        if multiline:
            font.setPixelSize(max(11, min(round(11 * self.zoom), 28)))
        else:
            font.setPixelSize(max(11, min(round(r.height() * 0.65), 28)))
        editor.setFont(font)
        lines = 2 if multiline else 1
        height = max(r.height(), editor.fontMetrics().height() * lines + 8)
        editor.setGeometry(r.x(), r.y(), max(r.width(), 60), height)

    def finish_text_edit(self, commit: bool, focus_view: bool = False, step: int = 0) -> None:
        """Close the editor of ``edit_text``; with ``commit``, report the text
        (if it changed), then with ``step`` ask for the next (+1) or previous
        (-1) field. Reported later, from the event loop, in that order:
        filling it in rebuilds the page this editor sits on."""
        if self._inline is None:
            return
        name, _item, editor, old, _multiline = self._inline
        self._inline = None
        text = editor.text()
        editor.hide()
        editor.deleteLater()
        if focus_view:
            self.setFocus()
        changed = commit and text != old

        def report():  # one call, so that the text is filled in before moving on
            if changed:
                self.textEdited.emit(name, text)
            if step:
                self.formFieldStep.emit(name, step)

        if changed or step:
            QTimer.singleShot(0, report)

    def highlight_form_field(self, name: str | None) -> None:
        """Mark the form field ``name`` (None: none) and show it: scrolled
        into view."""
        self.active_form_field = name
        for it in self._form_items:
            it.set_active(it.name == name)
        shown = [it for it in self._form_items if it.name == name]
        if shown:
            f = next((f for f in self.form_fields if f.name == name), None)
            on_page = [it for it in shown if f is None or self._page_of(it) == f.page] or shown
            area = on_page[0].mapRectToScene(on_page[0].rect())
            for it in on_page[1:]:
                area = area.united(it.mapRectToScene(it.rect()))
            self._show_scene_rect(area, 40)

    def set_insert_mode(self, on: bool) -> None:
        on = on and self.doc is not None
        if on:
            self.set_place_mode(False)
            self.set_crop_mode(False)
        self.insert_mode = on
        self._cancel_crop_drag()
        self._update_mode()

    def _cancel_crop_drag(self) -> None:
        if self._crop_item is not None and self._crop_item.scene() is not None:
            self.scene().removeItem(self._crop_item)
        self._crop_item = None
        self._crop_start = None
        self._crop_page = None

    def set_signature_appearance(self, pixmap: QPixmap | None, aspect: float) -> None:
        self._sig_pixmap, self._sig_aspect = pixmap, aspect
        if self.placement is not None:
            self.placement.set_appearance(pixmap, aspect)

    def _drop_placement(self) -> None:
        if self.placement is not None and self.placement.scene() is not None:
            self.scene().removeItem(self.placement)
        self.placement = None
        self._placement_page = None

    def clear_target(self) -> None:
        self._drop_placement()
        self.selected_field = None
        for it in self._field_items:
            it.set_selected(False)
        self.targetChanged.emit()

    # pages in the scene --------------------------------------------------------

    def _page_of(self, item: QGraphicsItem) -> int:
        parent = item.parentItem()
        return next((p.index for p in self.pages if p.item is parent), -1)

    def page_at(self, scene_pos: QPointF, nearest: bool = False) -> tuple[int, QPointF] | None:
        """(page, position on it) at ``scene_pos``; with ``nearest``, the
        closest page when between or beside them."""
        if not self.pages:
            return None
        y = scene_pos.y()
        index = bisect.bisect_right([p.top for p in self.pages], y) - 1
        index = max(index, 0)
        page = self.pages[index]
        if nearest and index + 1 < len(self.pages):
            below = self.pages[index + 1]
            if y > page.top + page.h and below.top - y < y - (page.top + page.h):
                page = below
        local = page.item.mapFromScene(scene_pos)
        if not nearest and not page.rect.contains(local):
            return None
        return page.index, local

    def map_from_page(self, x: float, y: float, page: int | None = None):
        """Viewport position of point (x, y) on ``page`` (default: the current one)."""
        p = self.pages[self.page if page is None else page]
        return self.mapFromScene(p.item.mapToScene(QPointF(x, y)))

    def page_rect(self, page: int | None = None) -> QRectF:
        """The page's rect in its own display points."""
        return self.pages[self.page if page is None else page].rect

    def _build(self) -> None:
        """The scene: every page with its fields, nothing rendered yet."""
        self.finish_text_edit(True)
        scene = self.scene()
        for item in list(scene.items()):
            if item.parentItem() is None:
                scene.removeItem(item)
        self.pages = []
        self._field_items = []
        self._form_items = []
        self._text_anchor = None
        if self.text_selection is not None:
            self.text_selection = None
            self.textSelectionChanged.emit()
        if self.doc is None:
            scene.setSceneRect(QRectF())
            return

        sizes = [self.doc.display_size(i) for i in range(len(self.doc))]
        widest = max(w for w, _h in sizes)
        y = 0.0
        for i, (w, h) in enumerate(sizes):
            self.pages.append(_Page(scene, i, (widest - w) / 2, y, w, h))
            y += h + PAGE_GAP
        scene.setSceneRect(QRectF(-MARGIN, -MARGIN, widest + 2 * MARGIN,
                                  y - PAGE_GAP + 2 * MARGIN))

        for f in self.fields:
            if f.page is None or f.rect is None or not 0 <= f.page < len(self.pages):
                continue
            x, y, fw, fh = self.doc.rect_to_display(f.page, f.rect)
            item = FieldItem(f, QRectF(x, y, fw, fh))
            item.setParentItem(self.pages[f.page].item)
            item.set_selected(f.name == self.selected_field)
            self._field_items.append(item)

        for f in self.form_fields:
            for wd in f.widgets:
                if not 0 <= wd.page < len(self.pages) or wd.rect[2] <= wd.rect[0]:
                    continue
                x, y, fw, fh = self.doc.rect_to_display(wd.page, wd.rect)
                item = FormItem(f.name, QRectF(x, y, fw, fh), f.fillable, f.label, wd.state)
                item.setParentItem(self.pages[wd.page].item)
                item.set_active(f.name == self.active_form_field)
                self._form_items.append(item)

        self._update_mode()
        self._apply_zoom()

    def _visible_pages(self, margin: float = 0.0) -> list[_Page]:
        """Pages in (or within ``margin`` points of) the visible area."""
        if not self.pages:
            return []
        shown = self.mapToScene(self.viewport().rect()).boundingRect()
        top, bottom = shown.top() - margin, shown.bottom() + margin
        first = max(bisect.bisect_right([p.top for p in self.pages], top) - 1, 0)
        out = []
        for page in self.pages[first:]:
            if page.top > bottom:
                break
            if page.top + page.h >= top:
                out.append(page)
        return out

    def _scrolled(self, *args) -> None:
        self._update_visible()
        if not self._going_to:
            self._track_page()

    def _track_page(self) -> None:
        """The page in the middle of the view is the current one."""
        if not self.pages:
            return
        middle = self.mapToScene(self.viewport().rect().center())
        found = self.page_at(middle, nearest=True)
        if found is not None and found[0] != self.page:
            self.page = found[0]
            self.pageChanged.emit(self.page)

    def _update_visible(self) -> None:
        """Render the pages coming into view (and the next ones), forget the
        pictures of pages far away, decorate the visible ones."""
        self._place_inline()  # follows scrolling and zooming
        if self.doc is None or not self.pages:
            return
        visible = self._visible_pages()
        near = self._visible_pages(margin=self.viewport().height() / max(self.zoom, 0.01))
        for page in near:
            if page.scale == 0.0:
                self._render_page(page)
        keep = {p.index for p in near}
        for page in self.pages:
            if page.scale and page.index not in keep:
                page.drop_picture()
        for page in visible:
            if not page.decorated:
                self._decorate(page)
        self._detail_timer.start()  # sharper, once zooming/scrolling pauses

    def _render_page(self, page: _Page) -> None:
        """The whole page, as sharp as the current zoom needs, but at most
        MAX_PAGE_PIXELS: a huge page (or a crafted MediaBox) at high zoom
        would need gigabytes and seconds. The detail covers the rest."""
        cap = 0.999 * (MAX_PAGE_PIXELS / max(page.w * page.h, 1.0)) ** 0.5  # pdfium rounds up
        scale = min(self.zoom * self.devicePixelRatioF(), cap)
        pm = QPixmap.fromImage(ImageQt(self.doc.render(page.index, scale)))
        pm.setDevicePixelRatio(1.0)
        page.pixmap.setPixmap(pm)
        page.pixmap.setScale(1 / scale)
        page.scale = scale

    def _update_detail(self) -> None:
        """Render the visible part of each visible page sharply when its
        whole-page picture is coarser than the screen."""
        if self.doc is None:
            return
        shown = self.mapToScene(self.viewport().rect()).boundingRect()
        for page in self.pages:
            item = page.detail
            scale = self.zoom * self.devicePixelRatioF()
            visible = page.item.mapRectFromScene(shown) & page.rect
            if (page.scale == 0.0 or scale <= page.scale * 1.05
                    or visible.width() < 1 or visible.height() < 1):
                item.setVisible(False)
                item.setPixmap(QPixmap())
                continue
            w, h = page.w, page.h
            # never more than the visible area needs (plus rounding)
            scale = min(scale, (4 * MAX_PAGE_PIXELS / (visible.width() * visible.height())) ** 0.5)
            crop = (visible.left(), h - visible.bottom(), w - visible.right(), visible.top())
            img = self.doc.render(page.index, scale, crop)
            pm = QPixmap.fromImage(ImageQt(img))
            pm.setDevicePixelRatio(1.0)
            item.setPixmap(pm)
            item.setScale(1 / scale)
            # pdfium cuts whole pixels: rounded up from the crop
            item.setPos(math.ceil(crop[0] * scale) / scale, math.ceil(crop[3] * scale) / scale)
            item.setVisible(True)

    def _show_scene_rect(self, rect: QRectF, margin: int) -> None:
        """Scroll ``rect`` (scene) into view; the page it is on becomes current."""
        self.ensureVisible(rect, margin, margin)
        found = self.page_at(rect.center(), nearest=True)
        if found is not None and found[0] != self.page:
            self.page = found[0]
            self.pageChanged.emit(self.page)

    # text: selection and search ----------------------------------------------

    def _decorate(self, page: _Page) -> None:
        """The selection's and the search's highlights on ``page``."""
        for item in page.decorations:
            if item.scene() is not None:
                self.scene().removeItem(item)
        page.decorations = []
        page.decorated = True
        if self.doc is None:
            return
        for start, count in self._matches.get(page.index, []):
            current = self._current_match == (page.index, start, count)
            color = QColor(255, 140, 0, 150) if current else QColor(255, 220, 0, 110)
            page.decorations += self._highlight(page, start, count, color, 2.5)
        part = self._selected_on(page.index)
        if part is not None:
            page.decorations += self._highlight(page, *part, QColor(40, 120, 230, 90), 3)

    def _redecorate(self) -> None:
        """Highlights changed: redrawn on the visible pages, the others later."""
        for page in self.pages:
            page.decorated = False
        for page in self._visible_pages():
            self._decorate(page)
        for page in self.pages:  # pages out of view: stale highlights go now
            if not page.decorated and page.decorations:
                for item in page.decorations:
                    self.scene().removeItem(item)
                page.decorations = []

    def _highlight(self, page: _Page, start: int, count: int, color: QColor,
                   z: float) -> list:
        items = []
        for x, y, w, h in _join_lines(self.doc.text_rects(page.index, start, count)):
            item = QGraphicsRectItem(QRectF(x, y, w, h), page.item)
            item.setPen(QPen(Qt.NoPen))
            item.setBrush(QBrush(color))
            item.setZValue(z)  # over the page, under fields
            items.append(item)
        return items

    def _selected_on(self, page: int) -> tuple[int, int] | None:
        """(start, count) of the selection on ``page``, or None."""
        if self.text_selection is None:
            return None
        p0, c0, p1, c1 = self.text_selection
        if not p0 <= page <= p1:
            return None
        start = c0 if page == p0 else 0
        end = c1 if page == p1 else self.doc.char_count(page) - 1
        return (start, end - start + 1) if end >= start else None

    def select_range(self, p0: int, c0: int, p1: int, c1: int) -> None:
        """Select from character ``c0`` on page ``p0`` to ``c1`` on ``p1``
        (both included, in either order)."""
        (p0, c0), (p1, c1) = sorted([(p0, c0), (p1, c1)])
        selection = (p0, c0, p1, c1) if self.doc is not None else None
        if selection != self.text_selection:
            self.text_selection = selection
            self._redecorate()
            self.textSelectionChanged.emit()

    def select_text(self, start: int, count: int, page: int | None = None) -> None:
        """Select ``count`` characters from ``start`` on ``page`` (default:
        the current one); 0 characters: none."""
        if count <= 0:
            self.clear_text_selection()
        else:
            page = self.page if page is None else page
            self.select_range(page, start, page, start + count - 1)

    def clear_text_selection(self) -> None:
        if self.text_selection is not None:
            self.text_selection = None
            self._redecorate()
            self.textSelectionChanged.emit()

    def select_all_text(self) -> None:
        """All text of the document."""
        if self.doc is not None and self.pages:
            last = len(self.pages) - 1
            self.select_range(0, 0, last, self.doc.char_count(last) - 1)

    def selected_text(self) -> str:
        if self.doc is None or self.text_selection is None:
            return ""
        p0, _c0, p1, _c1 = self.text_selection
        parts = []
        for page in range(p0, p1 + 1):
            part = self._selected_on(page)
            if part is not None:
                parts.append(self.doc.text(page, *part))
        return "\n".join(parts)

    def _char_at(self, scene_pos: QPointF, tolerance: float = 3.0,
                 nearest_page: bool = False) -> tuple[int, int] | None:
        """(page, character) at ``scene_pos``, or None."""
        found = self.page_at(scene_pos, nearest=nearest_page)
        if found is None or self.doc is None:
            return None
        page, local = found
        char = self.doc.char_at(page, local.x(), local.y(), tolerance)
        return (page, char) if char >= 0 else None

    def _over_item(self, viewport_pos) -> bool:
        """Whether a field or the signature is under the mouse (they get the click)."""
        top = next(iter(self.items(viewport_pos)), None)
        return isinstance(top, (FieldItem, FormItem, SignatureItem))

    def set_search_matches(self, matches: dict[int, list[tuple[int, int]]],
                           current: tuple[int, int, int] | None = None) -> None:
        """Highlight ``matches`` (page -> [(start, count)]); ``current``
        (page, start, count) stands out, and is scrolled to."""
        self._matches, self._current_match = matches, current
        if current is not None and self.doc is not None:
            rects = self.doc.text_rects(*current)
            if rects:
                x, y, w, h = rects[0]
                rect = self.pages[current[0]].item.mapRectToScene(QRectF(x, y, w, h))
                self._show_scene_rect(rect, 80)
        self._redecorate()

    @property
    def _match_items(self) -> list:
        """The search highlights shown now (for the tests)."""
        return [i for p in self.pages for i in p.decorations if i.zValue() == 2.5]

    # navigation / zoom -------------------------------------------------------

    def go_to(self, page: int) -> None:
        """Scroll to the top of ``page``."""
        if self.doc is None or not 0 <= page < len(self.doc):
            return
        self._scroll_to_page(page)

    def _scroll_to_page(self, page: int) -> None:
        self._going_to = True
        try:
            p = self.pages[page]
            shown = self.mapToScene(self.viewport().rect()).boundingRect()
            centre_x = shown.center().x() if shown.width() < self.sceneRect().width() else 0
            self.centerOn(QPointF(centre_x, p.top - PAGE_GAP / 2 + shown.height() / 2))
        finally:
            self._going_to = False
        self._update_visible()
        if page != self.page:
            self.page = page
            self.pageChanged.emit(page)

    def set_zoom(self, zoom: float | None) -> None:
        """``None`` means fit width. Keeps the centre of the view in place."""
        self.zoom_at(None, zoom)

    def zoom_at(self, pos: QPointF | None, zoom: float | None) -> None:
        """Zoom so that the point under viewport position ``pos`` (default:
        the centre) stays where it is."""
        if pos is None:
            pos = QPointF(self.viewport().rect().center())
        anchor = self.mapToScene(pos.toPoint())
        self._fit_width = zoom is None
        if zoom is not None:
            self.zoom = min(zoom, MAX_ZOOM)
        self._apply_zoom()
        moved = self.mapFromScene(anchor) - pos.toPoint()
        self._going_to = True  # the page under the anchor stays the current one
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + moved.x())
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() + moved.y())
        self._going_to = False
        self._update_visible()

    def _largest_page(self) -> tuple[float, float]:
        return (max(p.w for p in self.pages), max(p.h for p in self.pages))

    def min_zoom(self) -> float:
        """Zooming out stops where a whole page fits into the view."""
        if self.doc is None or not self.pages:
            return 0.0
        w, h = self._largest_page()  # plus a margin on each side
        avail = self.maximumViewportSize()  # without scroll bars
        if len(self.pages) > 1:  # there will be a vertical bar
            avail.setWidth(avail.width() - self._bar_width())
        return max(0.01, min((avail.width() - 2) / (w + 2 * MARGIN),
                             (avail.height() - 2) / (h + 2 * MARGIN)))

    def _bar_width(self) -> int:
        return self.style().pixelMetric(QStyle.PM_ScrollBarExtent, None, self)

    def fit_width_zoom(self) -> float:
        """The zoom that fits the widest page into the view. Worked out from
        the view's size without scroll bars, plus the vertical one if the
        pages will need it, never from the bars currently shown: otherwise
        pages that fit exactly with the bars shown but not without them
        make the bars, the viewport and the zoom flip back and forth
        endlessly, freezing the window."""
        w, _h = self._largest_page()
        total = self.sceneRect().height()
        avail = self.maximumViewportSize()
        zoom = (avail.width() - 2) / (w + 2 * MARGIN)
        if total * zoom > avail.height() - 2:  # too tall: there will be a vertical bar
            zoom = (avail.width() - self._bar_width() - 2) / (w + 2 * MARGIN)
        return max(zoom, 0.01)

    def _apply_zoom(self) -> None:
        if self.doc is None or not self.pages:
            return
        if self._fit_width:
            self.zoom = self.fit_width_zoom()
        self.zoom = max(self.zoom, self.min_zoom())
        if self.transform().m11() == self.zoom:
            self._update_visible()
            return
        # what is at the top of the view stays there (zoom_at() then moves
        # its own anchor); meanwhile the scroll range shrinks and grows
        # again, which isn't scrolling to another page
        at = QPoint(self.viewport().width() // 2, 0)
        anchor = self.mapToScene(at)
        self._going_to = True
        try:
            self.resetTransform()
            self.scale(self.zoom, self.zoom)
            moved = self.mapFromScene(anchor) - at
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + moved.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() + moved.y())
        finally:
            self._going_to = False
        self._update_visible()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit_width or self.zoom < self.min_zoom():
            self._apply_zoom()
        self._update_visible()

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120  # touchpads send fractions of a notch
        if not steps:
            return super().wheelEvent(event)
        item = self.itemAt(event.position().toPoint())
        if event.modifiers() & Qt.ControlModifier and isinstance(item, SignatureItem):
            item.scale_by(1.1 ** steps)
        else:
            self.zoom_at(event.position(), self.zoom * WHEEL_ZOOM ** steps)
        event.accept()

    # interaction -------------------------------------------------------------

    def mousePressEvent(self, event):
        if self.doc is None or event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        at = event.position().toPoint()
        pos = self.mapToScene(at)
        if not (self.crop_mode or self.place_mode or self.insert_mode):
            # a drag over text selects it
            hit = None if self._over_item(at) else self._char_at(pos)
            if hit is not None:
                self._text_anchor = hit
                if event.modifiers() & Qt.ShiftModifier and self.text_selection is not None:
                    self._text_anchor = self.text_selection[:2]  # extend the selection
                    self.select_range(*self._text_anchor, *hit)
                else:
                    self.clear_text_selection()
                self._drag_pos = QPointF(at)
                self._edge_timer.start()
                self.setFocus()
                self.viewport().setCursor(Qt.IBeamCursor)  # also while dragging
                event.accept()
                return
            self.clear_text_selection()
            # a click on a field activates it; a drag moves the pages
            top = next(iter(self.items(at)), None)
            if isinstance(top, FieldItem) and not top.field.locked:
                name = top.field.name
                self._field_press = (lambda: self.fieldActivated.emit(name), at)
            elif isinstance(top, FormItem):  # also read-only ones: they are shown
                name, state = top.name, top.state
                self._field_press = (lambda: self.formFieldActivated.emit(name, state), at)
            else:
                self._field_press = None
            return super().mousePressEvent(event)  # drag the signature, or the pages
        found = self.page_at(pos)
        if self.crop_mode or self.insert_mode:
            if found is None:
                if self.insert_mode:
                    return
                found = self.page_at(pos, nearest=True)
            self._cancel_crop_drag()
            page = self.pages[found[0]]
            self._crop_page = page
            self._crop_start = self._clamp(page, found[1])
            self._crop_item = QGraphicsRectItem(QRectF(self._crop_start, self._crop_start),
                                                page.item)
            color = QColor(30, 90, 200) if self.insert_mode else QColor(220, 40, 40)
            self._crop_item.setPen(QPen(color, 0, Qt.DashLine))
            color.setAlpha(30)
            self._crop_item.setBrush(QBrush(color))
            self._crop_item.setZValue(20)
            return
        # place mode: one click, then back to zoom/move
        item = next((i for i in self.items(at) if isinstance(i, FieldItem)), None)
        if item is not None:
            if item.clickable:
                self.select_field(item.field.name)
            return
        if found is not None:
            self.place_at(found[1], found[0])

    @staticmethod
    def _clamp(page: _Page, pos: QPointF) -> QPointF:
        return QPointF(min(max(pos.x(), 0.0), page.w), min(max(pos.y(), 0.0), page.h))

    def _extend_selection(self, viewport_pos: QPointF) -> None:
        """While dragging over text: the selection runs to the mouse."""
        pos = self.mapToScene(viewport_pos.toPoint())
        hit = self._char_at(pos, nearest_page=True)
        if hit is None:
            hit = self._char_at(pos, 12.0, nearest_page=True)  # between lines: the nearest
        if hit is None:  # beside the text: the page's start or end
            found = self.page_at(pos, nearest=True)
            if found is not None:
                page, local = found
                count = self.doc.char_count(page)
                if count:  # above the middle: from its start, below: to its end
                    hit = (page, count - 1 if local.y() > self.pages[page].h / 2 else 0)
        if hit is not None:
            self.select_range(*self._text_anchor, *hit)

    def _scroll_at_edge(self) -> None:
        """Dragging a selection to the top or bottom edge scrolls on."""
        if self._text_anchor is None or self._drag_pos is None:
            self._edge_timer.stop()
            return
        y, height = self._drag_pos.y(), self.viewport().height()
        edge = 30
        step = (y - (height - edge)) if y > height - edge else (y - edge if y < edge else 0)
        if step:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() + int(max(-40, min(40, step))))
            self._extend_selection(self._drag_pos)

    def mouseMoveEvent(self, event):
        if self._text_anchor is not None:
            self.viewport().setCursor(Qt.IBeamCursor)
            self._drag_pos = event.position()
            self._extend_selection(event.position())
            return
        if (self.crop_mode or self.insert_mode) and self._crop_item is not None:
            pos = self._crop_page.item.mapFromScene(self.mapToScene(event.position().toPoint()))
            end = self._clamp(self._crop_page, pos)
            self._crop_item.setRect(QRectF(self._crop_start, end).normalized())
            return
        super().mouseMoveEvent(event)  # (sets the hand cursor again)
        if not event.buttons():
            self._hover_cursor(event.position().toPoint())

    def _hover_cursor(self, at) -> None:
        """A text cursor over text, else the hand that moves the pages."""
        if self.doc is None or self.dragMode() != QGraphicsView.ScrollHandDrag:
            return
        if self._over_item(at):
            return  # fields and the signature have their own
        over_text = self._char_at(self.mapToScene(at)) is not None
        self.viewport().setCursor(Qt.IBeamCursor if over_text else Qt.OpenHandCursor)

    def mouseReleaseEvent(self, event):
        if self._text_anchor is not None:
            self._text_anchor = None
            self._drag_pos = None
            self._edge_timer.stop()
            self._hover_cursor(event.position().toPoint())
            return
        if self.crop_mode and self._crop_item is not None:
            rect, page = self._crop_item.rect(), self._crop_page.index
            self._cancel_crop_drag()
            if rect.width() >= 5 and rect.height() >= 5:
                self.cropSelected.emit(page, rect)
            return
        if self.insert_mode and self._crop_item is not None:
            rect, page = self._crop_item.rect(), self._crop_page.index
            if rect.width() < 5 or rect.height() < 5:  # a click
                rect = QRectF(self._crop_start, self._crop_start)
            self.set_insert_mode(False)
            self.insertSelected.emit(page, rect)
            return
        press, self._field_press = self._field_press, None
        super().mouseReleaseEvent(event)
        if press is not None and event.button() == Qt.LeftButton:
            click, start = press
            moved = (event.position().toPoint() - start).manhattanLength()
            if moved < QApplication.startDragDistance():
                click()

    def mouseDoubleClickEvent(self, event):
        """On text: select the word."""
        modes = self.crop_mode or self.place_mode or self.insert_mode
        if self.doc is not None and event.button() == Qt.LeftButton and not modes:
            at = event.position().toPoint()
            hit = None if self._over_item(at) else self._char_at(self.mapToScene(at))
            if hit is not None:
                self._text_anchor = None
                self._edge_timer.stop()
                page, char = hit
                self.select_text(*self.doc.word_at(page, char), page=page)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.SelectAll) and self.doc is not None:
            self.select_all_text()
            event.accept()
            return
        super().keyPressEvent(event)

    def select_field(self, name: str) -> None:
        f = next((f for f in self.fields if f.name == name), None)
        if f is None or f.filled or f.locked:
            return
        self._drop_placement()
        self.selected_field = name
        for it in self._field_items:
            it.set_selected(it.field.name == name)
        item = next((it for it in self._field_items if it.field.name == name), None)
        if item is not None:
            self._show_scene_rect(item.mapRectToScene(item.rect()), 40)
        elif f.page is not None:
            self.go_to(f.page)
        self.set_place_mode(False)
        self.targetChanged.emit()

    def place_at(self, pos: QPointF, page: int | None = None) -> None:
        """Put the new signature's centre at ``pos`` on ``page`` (default:
        the current one)."""
        page = self.page if page is None else page
        self.selected_field = None
        for it in self._field_items:
            it.set_selected(False)
        if self.placement is None or self._placement_page != page:
            self._drop_placement()
            self.placement = SignatureItem(
                self._sig_pixmap, self._sig_aspect, DEFAULT_WIDTH, self.pages[page].rect
            )
            self.placement.geometryChanged.connect(self.targetChanged)
            self.placement.setParentItem(self.pages[page].item)
            self._placement_page = page
        self.placement.center_on(pos)
        self.placement.setSelected(True)
        self.set_place_mode(False)
        self.targetChanged.emit()

    # result ------------------------------------------------------------------

    def placement_box(self) -> tuple[int, tuple[float, float, float, float]] | None:
        """(page, PDF box) of the placed signature, or None."""
        if self.placement is None or self._placement_page is None:
            return None
        r = self.placement.scene_rect()  # on its page
        page = self._placement_page
        return page, self.doc.rect_to_pdf(page, r.x(), r.y(), r.width(), r.height())
