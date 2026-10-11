import pytest

from slim_pdf.gui.document import PdfDocument

from conftest import make_pdf


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_roundtrip_and_orientation(tmp_path, rotate):
    doc = PdfDocument(str(make_pdf(tmp_path / "r.pdf", pages=1, rotate=rotate)))
    w, h = doc.display_size(0)
    assert (w, h) == ((595, 842) if rotate in (0, 180) else (842, 595))

    # A wide rect on screen ...
    box = doc.rect_to_pdf(0, 100, 50, 210, 70)
    bw, bh = box[2] - box[0], box[3] - box[1]
    # ... is wide in user space only if the page isn't rotated by 90/270.
    assert (bw, bh) == pytest.approx((210, 70) if rotate in (0, 180) else (70, 210), abs=0.05)
    assert doc.rect_to_display(0, box) == pytest.approx((100, 50, 210, 70), abs=0.05)


def test_top_left_maps_to_top_of_page(tmp_path):
    doc = PdfDocument(str(make_pdf(tmp_path / "r.pdf", pages=1)))
    assert doc.to_pdf(0, 0, 0) == pytest.approx((0, 842), abs=0.05)
    assert doc.to_pdf(0, 595, 842) == pytest.approx((595, 0), abs=0.05)
