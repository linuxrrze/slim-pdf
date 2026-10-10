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

    def close(self) -> None:
        self._pdf.close()

    def __len__(self) -> int:
        return len(self._pdf)

    def display_size(self, index: int) -> tuple[float, float]:
        """(width, height) in points as displayed, i.e. after /Rotate."""
        return self._pdf[index].get_size()

    def render(self, index: int, scale: float):
        """PIL image of the page at ``scale`` pixels per point."""
        return self._pdf[index].render(scale=scale, may_draw_forms=True).to_pil()

    def to_pdf(self, index: int, x: float, y: float) -> tuple[float, float]:
        """Display point -> PDF user space point."""
        page = self._pdf[index]
        w, h = page.get_size()
        px, py = ctypes.c_double(), ctypes.c_double()
        ok = raw.FPDF_DeviceToPage(
            page.raw, 0, 0, round(w * _OVERSAMPLE), round(h * _OVERSAMPLE), 0,
            round(x * _OVERSAMPLE), round(y * _OVERSAMPLE),
            ctypes.byref(px), ctypes.byref(py),
        )
        if not ok:
            raise ValueError("coordinate conversion failed")
        return px.value, py.value

    def to_display(self, index: int, x: float, y: float) -> tuple[float, float]:
        """PDF user space point -> display point."""
        page = self._pdf[index]
        w, h = page.get_size()
        dx, dy = ctypes.c_int(), ctypes.c_int()
        ok = raw.FPDF_PageToDevice(
            page.raw, 0, 0, round(w * _OVERSAMPLE), round(h * _OVERSAMPLE), 0,
            x, y, ctypes.byref(dx), ctypes.byref(dy),
        )
        if not ok:
            raise ValueError("coordinate conversion failed")
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
