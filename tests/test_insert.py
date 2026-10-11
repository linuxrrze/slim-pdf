"""Adding text and images to pages (insert.py)."""

import pytest
from conftest import (
    blue_bbox,
    draw_signature_image,
    green_bbox,
    make_encrypted,
    make_pdf,
    make_vector_signature,
    render_page,
    sign_with,
    text_bbox,
    validate_all,
)

from pdfsigner import forms, insert, pages
from pdfsigner.signing import PasswordRequired

H = 842  # A4 height: display y = H - PDF y on unrotated pages


def page_box(rot: int, x: float, y: float, w: float, h: float):
    """The PDF box shown at display rect (x, y, w, h) of a 595x842 page
    with /Rotate ``rot``."""
    W = 595
    corners = []
    for dx, dy in ((x, y), (x + w, y + h)):
        if rot == 0:
            corners.append((dx, H - dy))
        elif rot == 90:
            corners.append((dy, dx))
        elif rot == 180:
            corners.append((W - dx, dy))
        else:
            corners.append((W - dy, H - dx))
    (ax, ay), (bx, by) = corners
    return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
def test_text_and_image_land_upright_where_placed(tmp_path, rot):
    """On every rotation, what is added appears in the display rectangle it
    was placed in, the right way up (the image is wider than tall)."""
    src = make_pdf(tmp_path / "in.pdf", pages=1, rotate=rot)
    img = draw_signature_image(tmp_path / "sig.png")  # blue, 3:1
    data = src.read_bytes()
    data = insert.add_text(data, 0, page_box(rot, 60, 60, 300, 40), "Hello World", size=20)
    data = insert.add_image(data, 0, page_box(rot, 60, 200, 240, 80), img)
    out = tmp_path / "out.pdf"
    out.write_bytes(data)
    assert data.startswith(src.read_bytes())  # incremental
    page = render_page(out)
    x1, y1, x2, y2 = text_bbox(page)
    assert 60 <= x1 < 70 and 60 <= y1 < 70 and x2 < 360 and y2 < 100
    assert x2 - x1 > 3 * (y2 - y1)  # a line of text, not a column
    bx1, by1, bx2, by2 = blue_bbox(page)
    assert 60 <= bx1 and bx2 <= 300 and 200 <= by1 and by2 <= 280
    assert bx2 - bx1 > by2 - by1


def test_text_wraps_at_the_box_width(tmp_path):
    src = make_pdf(tmp_path / "in.pdf", pages=1)
    out = tmp_path / "out.pdf"
    out.write_bytes(insert.add_text(src.read_bytes(), 0, page_box(0, 50, 50, 120, 200),
                                    "several words that need more than one line", size=12))
    _x1, y1, x2, y2 = text_bbox(render_page(out))
    assert x2 <= 170 and y2 - y1 > 30  # three lines


def test_vector_image(tmp_path):
    src = make_pdf(tmp_path / "in.pdf", pages=1)
    vec = make_vector_signature(tmp_path / "vec.pdf")  # green, 4:1
    assert insert.image_aspect(vec) == pytest.approx(4.0)
    out = tmp_path / "out.pdf"
    out.write_bytes(insert.add_image(src.read_bytes(), 0, page_box(0, 100, 100, 200, 50), vec))
    x1, y1, x2, y2 = green_bbox(render_page(out))
    assert 100 <= x1 and x2 <= 300 and 100 <= y1 and y2 <= 150


def test_text_size():
    w, h = insert.text_size("Hello\nWorld!", 10)
    assert 30 < w < 40 and h == pytest.approx(2 * 12 + 4)
    with pytest.raises(forms.FormError, match="can't show"):
        insert.text_size("漢", 10)


@pytest.mark.parametrize("call, message", [
    (lambda d, tmp: insert.add_text(d, 0, (0, 0, 10, 10), "  "), "no text"),
    (lambda d, tmp: insert.add_text(d, 5, (0, 0, 10, 10), "x"), "out of range"),
    (lambda d, tmp: insert.add_image(d, 0, (0, 0, 10, 10), tmp / "missing.png"),
     "cannot read the image"),
    (lambda d, tmp: insert.add_image(d, 0, (0, 0, 10, 10), tmp / "in.pdf.txt"),
     "cannot read the image"),
])
def test_errors(tmp_path, call, message):
    src = make_pdf(tmp_path / "in.pdf", pages=1)
    (tmp_path / "in.pdf.txt").write_text("not an image")
    with pytest.raises(pages.PageError, match=message):
        call(src.read_bytes(), tmp_path)


def test_encrypted(tmp_path):
    enc = make_encrypted(make_pdf(tmp_path / "in.pdf", pages=1), tmp_path / "enc.pdf", "pw")
    with pytest.raises(PasswordRequired):
        insert.add_text(enc.read_bytes(), 0, (50, 50, 300, 100), "Zebrafish")
    out = tmp_path / "out.pdf"
    out.write_bytes(insert.add_text(enc.read_bytes(), 0, (50, 700, 300, 750), "Zebrafish",
                                    password="pw"))
    assert b"Zebrafish" not in out.read_bytes()
    assert text_bbox(render_page(out, password="pw")) is not None


def test_signatures_are_invalidated(tmp_path, profile, p12):
    """Adding to a page is a change no signature allows (callers ask first)."""
    signed = tmp_path / "signed.pdf"
    sign_with(profile, make_pdf(tmp_path / "in.pdf", pages=1), signed, page=0,
              box=(300, 100, 500, 150))
    out = tmp_path / "out.pdf"
    out.write_bytes(insert.add_text(signed.read_bytes(), 0, (50, 700, 300, 750), "Later"))
    [(_name, status)] = validate_all(out, p12[1])
    assert not status.bottom_line
    assert pages.signature_names(out.read_bytes()) == ["Signature1"]


def test_shared_resources_and_content_arrays(tmp_path):
    """Pages whose resources are inherited or whose content is an array keep
    their old content and gain the new one on top."""
    src = make_pdf(tmp_path / "in.pdf", pages=2)
    data = src.read_bytes()
    for _ in range(2):  # twice: the second insertion finds the first one's XObject name
        data = insert.add_text(data, 1, (50, 700, 300, 750), "Again")
    import io

    from pyhanko.pdf_utils.reader import PdfFileReader

    r = PdfFileReader(io.BytesIO(data))
    page = r.root["/Pages"]["/Kids"][1].get_object()
    assert sorted(page["/Resources"]["/XObject"]) == ["/PdfsInsert1", "/PdfsInsert2"]
    assert len(page["/Contents"]) >= 4
