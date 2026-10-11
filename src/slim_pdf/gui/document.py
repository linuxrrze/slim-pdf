"""PDF rendering and screen <-> PDF coordinate conversion via pdfium.

"Display" coordinates are points in the page as shown on screen: origin
top-left, /Rotate and CropBox applied. PDF coordinates are user space as
pyHanko expects them. pdfium does the mapping, so rotation and box offsets
are handled the same way as when rendering.
"""

from __future__ import annotations

import ctypes
import io

import pypdfium2 as pdfium
import pypdfium2.raw as raw

from ..i18n import _

# FPDF_DeviceToPage works on integer device pixels; oversample for precision.
_OVERSAMPLE = 64


def render_signature_image(path, height_px: int = 300):
    """PIL RGBA image of page 1 of a PDF signature, transparent background."""
    pdf = pdfium.PdfDocument(str(path))
    try:
        page = pdf[0]
        scale = height_px / page.get_height()
        return page.render(scale=scale, fill_color=(255, 255, 255, 0)).to_pil()
    finally:
        pdf.close()


def signature_pixmap(path):
    """QPixmap of a signature image (raster or PDF); null pixmap if unreadable."""
    from PySide6.QtGui import QPixmap

    if str(path).lower().endswith(".pdf"):
        try:
            buf = io.BytesIO()
            render_signature_image(path).save(buf, "PNG")
        except Exception:  # unreadable PDF: show no preview, signing reports it
            return QPixmap()
        pm = QPixmap()
        pm.loadFromData(buf.getvalue(), "PNG")
        return pm
    return QPixmap(str(path))


def render_stamp(profile, params: dict[str, str], *, with_image: bool = True,
                 with_text: bool = True, height_px: int = 120):
    """QPixmap of the stamp exactly as signing draws it (pyHanko lays it
    out on an empty page, pdfium renders it), at its natural proportions;
    a null pixmap if it can't be built (e.g. a missing image)."""
    from pyhanko.pdf_utils import generic
    from pyhanko.pdf_utils.layout import BoxConstraints
    from pyhanko.pdf_utils.writer import PdfFileWriter
    from PySide6.QtGui import QPixmap

    from ..signing import build_stamp_style, stamp_size

    try:
        w, h = stamp_size(profile, params, with_image=with_image, with_text=with_text)
        style = build_stamp_style(profile, with_image=with_image, with_text=with_text,
                                  params=params, box_size=(w, h))
        writer = PdfFileWriter()  # one empty page of the stamp's size
        writer.insert_page(generic.DictionaryObject({
            generic.pdf_name("/Type"): generic.pdf_name("/Page"),
            generic.pdf_name("/MediaBox"): generic.ArrayObject(
                generic.FloatObject(v) for v in (0, 0, w, h)),
            generic.pdf_name("/Resources"): generic.DictionaryObject(),
            generic.pdf_name("/Contents"): writer.add_object(generic.StreamObject(stream_data=b"")),
        }))
        box = BoxConstraints(width=w, height=h)
        style.create_stamp(writer, box, {}).apply(0, 0, 0)
        out = io.BytesIO()
        writer.write(out)
        pdf = pdfium.PdfDocument(out.getvalue())
        try:
            img = pdf[0].render(scale=height_px / h).to_pil()
        finally:
            pdf.close()
    except Exception:  # noqa: BLE001 - a preview never breaks the dialog
        return QPixmap()
    png = io.BytesIO()
    img.save(png, "PNG")
    pm = QPixmap()
    pm.loadFromData(png.getvalue(), "PNG")
    return pm


class PdfDocument:
    def __init__(self, source: str | bytes, password: str | None = None):
        """``source`` is a path or the PDF bytes."""
        # Load into memory so the file isn't held open: Windows would otherwise
        # refuse to overwrite it when signing in place.
        if isinstance(source, bytes):
            data = source
        else:
            with open(source, "rb") as f:
                data = f.read()
        self._pdf = pdfium.PdfDocument(data, password=password)
        self._pdf.init_forms()  # needed to render signature widget appearances
        # Each page is loaded once: with forms, pdfium keeps every loaded
        # page in its form environment until it is closed, so loading a page
        # per call (sizes are asked for on every resize) piles them up.
        self._pages: dict[int, pdfium.PdfPage] = {}

    def page(self, index: int) -> pdfium.PdfPage:
        if index not in self._pages:
            self._pages[index] = self._pdf[index]
        return self._pages[index]

    def close(self) -> None:
        for page in self._pages.values():
            page.close()
        self._pages.clear()
        self._pdf.close()

    def __len__(self) -> int:
        return len(self._pdf)

    def display_size(self, index: int) -> tuple[float, float]:
        """(width, height) in points as displayed, i.e. after /Rotate."""
        return self.page(index).get_size()

    def render(self, index: int, scale: float, crop=(0, 0, 0, 0)):
        """PIL image of the page at ``scale`` pixels per point; ``crop`` cuts
        (left, bottom, right, top) points off the page as displayed."""
        return self.page(index).render(scale=scale, crop=crop, may_draw_forms=True).to_pil()

    def to_pdf(self, index: int, x: float, y: float) -> tuple[float, float]:
        """Display point -> PDF user space point."""
        page = self.page(index)
        w, h = page.get_size()
        px, py = ctypes.c_double(), ctypes.c_double()
        ok = raw.FPDF_DeviceToPage(
            page.raw, 0, 0, round(w * _OVERSAMPLE), round(h * _OVERSAMPLE), 0,
            round(x * _OVERSAMPLE), round(y * _OVERSAMPLE),
            ctypes.byref(px), ctypes.byref(py),
        )
        if not ok:
            raise ValueError(_("coordinate conversion failed"))
        return px.value, py.value

    def to_display(self, index: int, x: float, y: float) -> tuple[float, float]:
        """PDF user space point -> display point."""
        page = self.page(index)
        w, h = page.get_size()
        dx, dy = ctypes.c_int(), ctypes.c_int()
        ok = raw.FPDF_PageToDevice(
            page.raw, 0, 0, round(w * _OVERSAMPLE), round(h * _OVERSAMPLE), 0,
            x, y, ctypes.byref(dx), ctypes.byref(dy),
        )
        if not ok:
            raise ValueError(_("coordinate conversion failed"))
        return dx.value / _OVERSAMPLE, dy.value / _OVERSAMPLE

    def rect_to_pdf(self, index: int, x: float, y: float, w: float, h: float):
        """Display rect (top-left x, y, width, height) -> PDF box (x1, y1, x2, y2)."""
        ax, ay = self.to_pdf(index, x, y)
        bx, by = self.to_pdf(index, x + w, y + h)
        return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))

    def rect_to_display(self, index: int, box) -> tuple[float, float, float, float]:
        """PDF box -> display rect (x, y, width, height)."""
        ax, ay = self.to_display(index, box[0], box[1])
        bx, by = self.to_display(index, box[2], box[3])
        return (min(ax, bx), min(ay, by), abs(bx - ax), abs(by - ay))
