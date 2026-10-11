"""Page view: renders one page, shows signature fields, places the signature.

Scene units are display points (see document.py), so the placed item's
geometry converts directly to PDF coordinates via PdfDocument.rect_to_pdf.
"""

from __future__ import annotations

from PIL.ImageQt import ImageQt
import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
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

    def __init__(self, name: str, rect: QRectF, fillable: bool, label: str = ""):
        super().__init__(rect)
        self.name, self.fillable = name, fillable
        self.setZValue(4)
        self.setPen(QPen(Qt.NoPen))
        self.setAcceptHoverEvents(True)
        label = printable(label or name)
        self.setToolTip(_("{name}: click to fill in").format(name=label) if fillable
                        else _("{name}: can't be changed").format(name=label))
        if fillable:
            self.setCursor(Qt.IBeamCursor)

    def hoverEnterEvent(self, event):
        if self.fillable:
            self.setPen(QPen(QColor(30, 90, 200), 0, Qt.DotLine))
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.setPen(QPen(Qt.NoPen))
        super().hoverLeaveEvent(event)


class PageView(QGraphicsView):
    """Shows one page. Emits ``targetChanged`` when the signing target changes.

    By default the wheel zooms and dragging moves the page (a placed
    signature is dragged itself). In place mode, one click places the
    signature or picks an empty field, then the view is back to the default
    (``placeModeChanged``). Outside place mode, a click (not a drag) on a
    field emits ``fieldActivated(name)``, on a form field
    ``formFieldActivated(name)``. In crop mode, dragging draws a rectangle;
    ``cropSelected(page, display rect)`` is emitted on release.
    """

    targetChanged = Signal()
    pageChanged = Signal(int)
    cropSelected = Signal(int, QRectF)
    placeModeChanged = Signal(bool)
    fieldActivated = Signal(str)
    formFieldActivated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setBackgroundBrush(QColor(90, 90, 90))
        self.setAlignment(Qt.AlignCenter)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.NoAnchor)  # zoom_at() anchors itself

        self.doc: PdfDocument | None = None
        self.fields: list[SigField] = []
        self.form_fields: list = []  # forms.FormField
        self.page = 0
        self.zoom = 1.0
        self._fit_width = True
        self._page_item: QGraphicsPixmapItem | None = None
        self._page_scale = 0.0  # pixels per point of _page_item
        self._detail_item: QGraphicsPixmapItem | None = None
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.setInterval(RENDER_DELAY)
        self._detail_timer.timeout.connect(self._update_detail)
        for bar in (self.horizontalScrollBar(), self.verticalScrollBar()):
            bar.valueChanged.connect(self._schedule_detail)
        self._field_items: list[FieldItem] = []
        self._form_items: list[FormItem] = []
        # (signal, field name, press position) of a click on a field
        self._field_press: tuple[Signal, str, object] | None = None
        self._sig_pixmap: QPixmap | None = None
        self._sig_aspect = 3.0

        # Target: either a placed new signature or an existing empty field.
        self.placement: SignatureItem | None = None
        self._placement_page: int | None = None
        self.selected_field: str | None = None

        self.place_mode = False
        self.crop_mode = False
        self._crop_start: QPointF | None = None
        self._crop_item: QGraphicsRectItem | None = None

    # setup ------------------------------------------------------------------

    def set_document(self, doc: PdfDocument | None, fields: list[SigField],
                     page: int = 0, form_fields=()) -> None:
        self.doc, self.fields, self.form_fields = doc, fields, list(form_fields)
        self.set_place_mode(False)
        self.clear_target()
        self.page = min(page, len(doc) - 1) if doc is not None else 0
        self._render()

    def _update_mode(self) -> None:
        clicking = self.place_mode or self.crop_mode
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
        changed = on != self.place_mode
        self.place_mode = on
        self._update_mode()
        if changed:
            self.placeModeChanged.emit(on)

    def set_crop_mode(self, on: bool) -> None:
        if on:
            self.set_place_mode(False)
        self.crop_mode = on
        self._cancel_crop_drag()
        if on:
            self.clear_target()
        self._update_mode()

    def _cancel_crop_drag(self) -> None:
        if self._crop_item is not None and self._crop_item.scene() is not None:
            self.scene().removeItem(self._crop_item)
        self._crop_item = None
        self._crop_start = None

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

    # navigation / zoom -------------------------------------------------------

    def go_to(self, page: int) -> None:
        if self.doc is None or not 0 <= page < len(self.doc) or page == self.page:
            return
        self.page = page
        self._render()
        self.pageChanged.emit(page)

    def set_zoom(self, zoom: float | None) -> None:
        """``None`` means fit width. Keeps the centre of the view in place."""
        self.zoom_at(None, zoom)

    def zoom_at(self, pos: QPointF | None, zoom: float | None) -> None:
        """Zoom so that the page point under viewport position ``pos`` (default:
        the centre) stays where it is."""
        if pos is None:
            pos = QPointF(self.viewport().rect().center())
        anchor = self.mapToScene(pos.toPoint())
        self._fit_width = zoom is None
        if zoom is not None:
            self.zoom = min(zoom, MAX_ZOOM)
        self._apply_zoom()
        if not self._fit_width:
            moved = self.mapFromScene(anchor) - pos.toPoint()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + moved.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() + moved.y())

    def min_zoom(self) -> float:
        """Zooming out stops where the whole page fits into the view."""
        if self.doc is None:
            return 0.0
        w, h = self.doc.display_size(self.page)  # plus a 20pt margin on each side
        avail = self.maximumViewportSize()  # without scroll bars
        return max(0.01, min((avail.width() - 2) / (w + 40), (avail.height() - 2) / (h + 40)))

    def _apply_zoom(self) -> None:
        if self.doc is None:
            return
        if self._fit_width:
            # scene rect has a 20pt margin on each side
            w, _h = self.doc.display_size(self.page)
            avail = self.viewport().width() - 2
            self.zoom = avail / (w + 40)
        self.zoom = max(self.zoom, self.min_zoom())
        self.resetTransform()
        self.scale(self.zoom, self.zoom)
        self._schedule_detail()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit_width or self.zoom < self.min_zoom():
            self._apply_zoom()
        self._schedule_detail()

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

    # rendering ---------------------------------------------------------------

    def _render(self) -> None:
        scene = self.scene()
        for item in list(scene.items()):
            if item.parentItem() is None:
                scene.removeItem(item)
        self._page_item = self._detail_item = None
        self._field_items = []
        self._form_items = []
        if self.doc is None:
            return

        w, h = self.doc.display_size(self.page)
        scene.setSceneRect(QRectF(-20, -20, w + 40, h + 40))
        self._page_item = QGraphicsPixmapItem()
        self._page_item.setZValue(0)
        scene.addItem(self._page_item)
        page_shadow = scene.addRect(QRectF(3, 3, w, h), QPen(Qt.NoPen), QBrush(QColor(0, 0, 0, 80)))
        page_shadow.setZValue(-1)
        self._detail_item = QGraphicsPixmapItem()
        self._detail_item.setZValue(1)  # over the page, under fields and the signature
        self._detail_item.setTransformationMode(Qt.SmoothTransformation)
        scene.addItem(self._detail_item)

        for f in self.fields:
            if f.page != self.page or f.rect is None:
                continue
            x, y, fw, fh = self.doc.rect_to_display(self.page, f.rect)
            item = FieldItem(f, QRectF(x, y, fw, fh))
            item.set_selected(f.name == self.selected_field)
            scene.addItem(item)
            self._field_items.append(item)

        for f in self.form_fields:
            for wd in f.widgets:
                if wd.page != self.page or wd.rect[2] <= wd.rect[0]:
                    continue
                x, y, fw, fh = self.doc.rect_to_display(self.page, wd.rect)
                item = FormItem(f.name, QRectF(x, y, fw, fh), f.fillable, f.label)
                scene.addItem(item)
                self._form_items.append(item)

        # The placed signature stays alive while browsing other pages.
        if self.placement is not None and self._placement_page == self.page:
            scene.addItem(self.placement)
        self._update_mode()
        self._apply_zoom()
        self._update_page_pixmap()

    def _update_page_pixmap(self) -> None:
        """The whole page, as sharp as the current zoom needs, but at most
        MAX_PAGE_PIXELS: a huge page (or a crafted MediaBox) at high zoom
        would need gigabytes and seconds. The detail covers the rest."""
        if self.doc is None or self._page_item is None:
            return
        w, h = self.doc.display_size(self.page)
        cap = 0.999 * (MAX_PAGE_PIXELS / max(w * h, 1.0)) ** 0.5  # pdfium rounds up
        scale = min(self.zoom * self.devicePixelRatioF(), cap)
        pm = QPixmap.fromImage(ImageQt(self.doc.render(self.page, scale)))
        pm.setDevicePixelRatio(1.0)
        self._page_item.setPixmap(pm)
        self._page_item.setScale(1 / scale)
        self._page_item.setTransformationMode(Qt.SmoothTransformation)
        self._page_scale = scale

    def _schedule_detail(self, *args) -> None:
        if self._detail_item is not None:
            self._detail_timer.start()  # restarts: renders once zooming/scrolling pauses

    def _update_detail(self) -> None:
        """Render the visible part of the page sharply when the whole-page
        pixmap is coarser than the screen."""
        item = self._detail_item
        if self.doc is None or item is None or item.scene() is None:
            return
        scale = self.zoom * self.devicePixelRatioF()
        if scale <= self._page_scale * 1.05:
            item.setVisible(False)
            return
        w, h = self.doc.display_size(self.page)
        visible = self.mapToScene(self.viewport().rect()).boundingRect() & QRectF(0, 0, w, h)
        if visible.width() < 1 or visible.height() < 1:
            item.setVisible(False)
            return
        # never more than the visible area needs (plus rounding)
        scale = min(scale, (4 * MAX_PAGE_PIXELS / (visible.width() * visible.height())) ** 0.5)
        crop = (visible.left(), h - visible.bottom(), w - visible.right(), visible.top())
        img = self.doc.render(self.page, scale, crop)
        pm = QPixmap.fromImage(ImageQt(img))
        pm.setDevicePixelRatio(1.0)
        item.setPixmap(pm)
        item.setScale(1 / scale)
        # pdfium cuts whole pixels: rounded up from the crop
        item.setPos(math.ceil(crop[0] * scale) / scale, math.ceil(crop[3] * scale) / scale)
        item.setVisible(True)

    # interaction -------------------------------------------------------------

    def page_rect(self) -> QRectF:
        w, h = self.doc.display_size(self.page)
        return QRectF(0, 0, w, h)

    def _clamp(self, pos: QPointF) -> QPointF:
        r = self.page_rect()
        return QPointF(min(max(pos.x(), r.left()), r.right()),
                       min(max(pos.y(), r.top()), r.bottom()))

    def mousePressEvent(self, event):
        if self.doc is None or event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        pos = self.mapToScene(event.position().toPoint())
        if not (self.crop_mode or self.place_mode):
            # a click on a field activates it; a drag moves the page
            top = next(iter(self.items(event.position().toPoint())), None)
            at = event.position().toPoint()
            if isinstance(top, FieldItem) and not top.field.locked:
                self._field_press = (self.fieldActivated, top.field.name, at)
            elif isinstance(top, FormItem) and top.fillable:
                self._field_press = (self.formFieldActivated, top.name, at)
            else:
                self._field_press = None
            return super().mousePressEvent(event)  # drag the signature, or the page
        if self.crop_mode:
            self._cancel_crop_drag()
            self._crop_start = self._clamp(pos)
            self._crop_item = QGraphicsRectItem(QRectF(self._crop_start, self._crop_start))
            pen = QPen(QColor(220, 40, 40), 0, Qt.DashLine)
            self._crop_item.setPen(pen)
            self._crop_item.setBrush(QBrush(QColor(220, 40, 40, 30)))
            self._crop_item.setZValue(20)
            self.scene().addItem(self._crop_item)
            return
        # place mode: one click, then back to zoom/move
        item = next((i for i in self.items(event.position().toPoint())
                     if isinstance(i, FieldItem)), None)
        if item is not None:
            if item.clickable:
                self.select_field(item.field.name)
            return
        if self.page_rect().contains(pos):
            self.place_at(pos)

    def mouseMoveEvent(self, event):
        if self.crop_mode and self._crop_item is not None:
            end = self._clamp(self.mapToScene(event.position().toPoint()))
            self._crop_item.setRect(QRectF(self._crop_start, end).normalized())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.crop_mode and self._crop_item is not None:
            rect = self._crop_item.rect()
            self._cancel_crop_drag()
            if rect.width() >= 5 and rect.height() >= 5:
                self.cropSelected.emit(self.page, rect)
            return
        press, self._field_press = self._field_press, None
        super().mouseReleaseEvent(event)
        if press is not None and event.button() == Qt.LeftButton:
            signal, name, start = press
            moved = (event.position().toPoint() - start).manhattanLength()
            if moved < QApplication.startDragDistance():
                signal.emit(name)

    def select_field(self, name: str) -> None:
        f = next((f for f in self.fields if f.name == name), None)
        if f is None or f.filled or f.locked:
            return
        self._drop_placement()
        if f.page is not None and f.page != self.page:
            self.go_to(f.page)
        self.selected_field = name
        for it in self._field_items:
            it.set_selected(it.field.name == name)
        if f.rect is not None:
            self.ensureVisible(self._field_items_by_name(name).rect(), 40, 40)
        self.set_place_mode(False)
        self.targetChanged.emit()

    def _field_items_by_name(self, name: str) -> FieldItem:
        return next(it for it in self._field_items if it.field.name == name)

    def place_at(self, pos: QPointF) -> None:
        self.selected_field = None
        for it in self._field_items:
            it.set_selected(False)
        bounds = self.page_rect()
        if self.placement is None or self._placement_page != self.page:
            self._drop_placement()
            self.placement = SignatureItem(
                self._sig_pixmap, self._sig_aspect, DEFAULT_WIDTH, bounds
            )
            self.placement.geometryChanged.connect(self.targetChanged)
            self.scene().addItem(self.placement)
            self._placement_page = self.page
        self.placement.center_on(pos)
        self.placement.setSelected(True)
        self.set_place_mode(False)
        self.targetChanged.emit()

    # result ------------------------------------------------------------------

    def placement_box(self) -> tuple[int, tuple[float, float, float, float]] | None:
        """(page, PDF box) of the placed signature, or None."""
        if self.placement is None or self._placement_page is None:
            return None
        r = self.placement.scene_rect()
        page = self._placement_page
        return page, self.doc.rect_to_pdf(page, r.x(), r.y(), r.width(), r.height())
