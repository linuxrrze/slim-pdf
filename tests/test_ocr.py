"""Text recognition (ocr/): the invisible text layer, engines, CLI."""

import io

import pypdfium2 as pdfium
import pytest
from conftest import make_pdf, render_page, sign_with
from PIL import ImageChops

from slim_pdf import config, insert, ocr, pages
from slim_pdf.cli import main
from slim_pdf.gui.document import PdfDocument
from slim_pdf.ocr import _tesseract, textlayer


class FakeEngine(ocr.Engine):
    """Finds ``words`` = [(text, (left, top, right, bottom) as fractions of
    the image, line)] on every page."""
    name, label = "fake", "Fake"

    def __init__(self, words):
        self.words, self.images = words, []

    def available(self):
        return True

    def languages(self):
        return ["de", "en"]

    def recognize(self, image, languages):
        self.images.append(image.size)
        w, h = image.size
        return [ocr.Word(t, (a * w, b * h, c * w, d * h), line) for t, (a, b, c, d), line
                in self.words]


def scanned_pdf(tmp_path, text: str, name="scan.pdf", rotate=0, dpi=200) -> bytes:
    """A page that is only an image of ``text``, like a scan."""
    src = make_pdf(tmp_path / f"{name}.src.pdf", pages=1)
    data = insert.add_text(src.read_bytes(), 0, (60, 640, 560, 760), text, size=22)
    image = pdfium.PdfDocument(data)[0].render(scale=dpi / 72, grayscale=True).to_pil()
    if rotate:  # scanned the wrong way round, then the page turned in the viewer
        image = image.rotate(rotate, expand=True)
    buf = io.BytesIO()
    image.save(buf, format="PDF", resolution=dpi)
    data = buf.getvalue()
    if rotate:
        data = pages.rotate(data, [0], rotate)
    (tmp_path / name).write_bytes(data)
    return data


def blank_image_pdf(tmp_path, rotate=0, crop=None) -> bytes:
    data = make_pdf(tmp_path / "blank.pdf", pages=2, rotate=rotate).read_bytes()
    if crop:
        data = pages.crop(data, [0, 1], box=crop)
    return data


def text_in(data: bytes, page: int, rect) -> str:
    """Text pdfium finds in display rect (x, y, w, h) of ``page``."""
    doc = PdfDocument(data)
    try:
        x1, y1, x2, y2 = doc.rect_to_pdf(page, *rect)
        textpage = doc.page(page).get_textpage()
        return textpage.get_text_bounded(x1, y1, x2, y2)
    finally:
        doc.close()


def all_text(data: bytes, page: int = 0) -> str:
    pdf = pdfium.PdfDocument(data)
    try:
        return pdf[page].get_textpage().get_text_range()
    finally:
        pdf.close()


WORDS = [("Hello", (0.1, 0.1, 0.3, 0.13), 0), ("Wörld", (0.35, 0.1, 0.6, 0.13), 0),
         ("Zweite", (0.1, 0.5, 0.4, 0.54), 1)]


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_text_lands_where_the_words_are(tmp_path, rotate):
    data = blank_image_pdf(tmp_path, rotate=rotate)
    engine = FakeEngine(WORDS)
    out, done, skipped = ocr.recognize(data, [0], engine, ["de"])
    assert done == [0] and skipped == []
    assert out.startswith(data)  # incremental
    w, h = (842, 595) if rotate in (90, 270) else (595, 842)
    assert engine.images[0][0] / engine.images[0][1] == pytest.approx(w / h, rel=0.01)
    assert all_text(out).split() == ["Hello", "Wörld", "Zweite"]
    assert "Hello" in text_in(out, 0, (0.1 * w - 2, 0.1 * h - 2, 0.2 * w + 4, 0.03 * h + 4))
    assert "Zweite" in text_in(out, 0, (0.1 * w - 2, 0.5 * h - 2, 0.3 * w + 4, 0.04 * h + 4))
    assert "Hello" not in text_in(out, 0, (0.1 * w - 2, 0.5 * h - 2, 0.3 * w + 4, 0.04 * h + 4))
    assert all_text(out, 1) == ""  # the other page is untouched


def test_cropped_page(tmp_path):
    data = blank_image_pdf(tmp_path, crop=(100, 100, 400, 500))  # 300 x 400 shown
    out, _done, _skipped = ocr.recognize(data, [0], FakeEngine(WORDS), ["de"])
    assert "Zweite" in text_in(out, 0, (0.1 * 300 - 2, 0.5 * 400 - 2, 0.3 * 300 + 4, 20))


def test_the_page_looks_the_same(tmp_path):
    data = scanned_pdf(tmp_path, "Nothing changes")
    out, _done, _skipped = ocr.recognize(data, [0], FakeEngine(WORDS), ["de"])
    (tmp_path / "out.pdf").write_bytes(out)
    before, after = render_page(tmp_path / "scan.pdf"), render_page(tmp_path / "out.pdf")
    assert ImageChops.difference(before, after).getbbox() is None


def test_pages_with_text_are_skipped(tmp_path):
    data = blank_image_pdf(tmp_path)
    data = insert.add_text(data, 1, (50, 50, 300, 100), "Typed")
    engine = FakeEngine(WORDS)
    out, done, skipped = ocr.recognize(data, [0, 1], engine, ["de"])
    assert done == [0] and skipped == [1] and len(engine.images) == 1
    # recognized pages have text now: nothing left to do
    assert ocr.recognize(out, [0, 1], engine, ["de"]) == (None, [], [0, 1])
    _out, done, skipped = ocr.recognize(data, [1], engine, ["de"], force=True)
    assert done == [1] and skipped == []


def test_nothing_found(tmp_path):
    data = blank_image_pdf(tmp_path)
    assert ocr.recognize(data, [0], FakeEngine([]), ["de"]) == (None, [], [])


def test_progress_and_cancel(tmp_path):
    data = blank_image_pdf(tmp_path)
    calls = []
    ocr.recognize(data, [0, 1], FakeEngine(WORDS), ["de"],
                  progress=lambda done, total: calls.append((done, total)))
    assert calls == [(0, 2), (1, 2), (2, 2)]
    with pytest.raises(ocr.Cancelled):
        ocr.recognize(data, [0, 1], FakeEngine(WORDS), ["de"],
                      progress=lambda done, total: done < 1)


def test_unknown_language(tmp_path):
    with pytest.raises(ocr.OcrError, match="xx"):
        ocr.recognize(blank_image_pdf(tmp_path), [0], FakeEngine(WORDS), ["xx"])


def test_characters_outside_cp1252_become_question_marks(tmp_path):
    out, _done, _skipped = ocr.recognize(
        blank_image_pdf(tmp_path), [0], FakeEngine([("Ωmega", (0.1, 0.1, 0.3, 0.13), 0)]),
        ["de"])
    assert all_text(out).strip() == "?mega"


def test_lines_share_size_and_baseline():
    words = [ocr.Word("a", (10, 100, 20, 110), 0), ocr.Word("gy", (30, 100, 50, 114), 0)]
    ops = textlayer._content(words, 800).decode().splitlines()
    sizes = {line.split(" Tf")[0] for line in ops if " Tf" in line}
    baselines = {line.split(" Tm")[0].split()[-1] for line in ops if " Tm" in line}
    assert len(sizes) == 1 and len(baselines) == 1
    assert ops[0] == "BT 3 Tr" and ops[-1] == "ET"
    assert "(a ) Tj" in ops[1] and "(gy) Tj" in ops[2]  # a space between words


def test_parse_tsv():
    tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight"
           "\tconf\ttext\n"
           "1\t1\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t\n"
           "5\t1\t1\t1\t1\t1\t10\t20\t30\t40\t96.5\tHallo\n"
           "5\t1\t1\t1\t1\t2\t50\t20\t30\t40\t90\tWelt\n"
           "5\t1\t1\t1\t2\t1\t10\t80\t30\t40\t91\t\"quoted\"\n"
           "5\t1\t1\t1\t2\t2\t10\t80\t30\t40\t-1\t \n")
    words = _tesseract.parse_tsv(tsv)
    assert [(w.text, w.box, w.line) for w in words] == [
        ("Hallo", (10, 20, 40, 60), 0), ("Welt", (50, 20, 80, 60), 0),
        ('"quoted"', (10, 80, 40, 120), 1)]
    assert words[0].confidence == 96.5


def test_engine_choice(monkeypatch):
    with pytest.raises(ocr.OcrError, match="unknown OCR engine"):
        ocr.get_engine("nonsense")
    monkeypatch.setattr(ocr, "DEFAULT_ORDER", ("tesseract",))
    monkeypatch.setattr(_tesseract, "find_tesseract", lambda configured="": None)
    with pytest.raises(ocr.OcrError, match="no OCR engine found.*[Ii]nstall"):
        ocr.get_engine()
    with pytest.raises(ocr.OcrError, match="not available"):
        ocr.get_engine("tesseract")


def test_tesseract_path_from_config(tmp_path):
    fake = tmp_path / "tesseract.exe"
    fake.write_text("")
    assert _tesseract.find_tesseract(str(fake)) == str(fake)
    assert _tesseract.find_tesseract(str(tmp_path / "missing")) is None


def test_config_roundtrip(tmp_path):
    path = tmp_path / "config.toml"
    cfg = config.Config()
    cfg.ocr = config.OcrSettings(engine="tesseract", languages=["de"], tesseract="/x/tess")
    cfg.save(path)
    assert config.load_config(path).ocr == cfg.ocr
    config.Config().save(path)
    assert "ocr" not in path.read_text()
    path.write_text('[ocr]\nengine = "magic"\n')
    with pytest.raises(ValueError, match="engine"):
        config.load_config(path)
    path.write_text('[ocr]\nlanguage = "de"\n')
    with pytest.raises(ValueError, match="unknown keys"):
        config.load_config(path)


# --- command line ---------------------------------------------------------------------


@pytest.fixture
def fake_engine(monkeypatch):
    engine = FakeEngine(WORDS)
    monkeypatch.setattr(ocr, "get_engine", lambda name="", tesseract="": engine)
    return engine


def test_cli(tmp_path, cfg_path, fake_engine, capsys):
    blank_image_pdf(tmp_path)
    src = tmp_path / "blank.pdf"
    assert main(["ocr", str(src), "--pages", "1"]) == 0
    out = tmp_path / "blank-ocr.pdf"
    assert "Hello" in all_text(out.read_bytes()) and all_text(out.read_bytes(), 1) == ""
    assert "1 page" in capsys.readouterr().out
    assert main(["ocr", str(out), "--pages", "1"]) == 0  # page 1 is done
    printed = capsys.readouterr().out
    assert "skipped 1 page" in printed and "nothing written" in printed
    assert not (tmp_path / "blank-ocr-ocr.pdf").exists()
    assert main(["ocr", str(src), "-i", "--lang", "de,en"]) == 0
    assert "Hello" in all_text(src.read_bytes(), 1)


def test_cli_without_text(tmp_path, cfg_path, monkeypatch, capsys):
    monkeypatch.setattr(ocr, "get_engine", lambda name="", tesseract="": FakeEngine([]))
    blank_image_pdf(tmp_path)
    assert main(["ocr", str(tmp_path / "blank.pdf")]) == 0
    assert "nothing written" in capsys.readouterr().out
    assert not (tmp_path / "blank-ocr.pdf").exists()


def test_cli_signed(tmp_path, cfg_path, fake_engine, profile):
    signed = tmp_path / "signed.pdf"
    sign_with(profile, make_pdf(tmp_path / "in.pdf", pages=1), signed)
    assert main(["ocr", str(signed)]) == 3
    assert main(["ocr", str(signed), "--force"]) == 0


def test_cli_needs_a_file(cfg_path, capsys):
    assert main(["ocr"]) == 1
    assert "--list-engines" in capsys.readouterr().err


def test_cli_list_engines(cfg_path, monkeypatch, capsys):
    monkeypatch.setattr(ocr, "engines", lambda tesseract="": [FakeEngine(WORDS)])
    monkeypatch.setattr(ocr, "get_engine", lambda name="", tesseract="": FakeEngine(WORDS))
    assert main(["ocr", "--list-engines"]) == 0
    out = capsys.readouterr().out
    assert "* fake" in out and "de, en" in out


# --- the real engines, where installed --------------------------------------------------

REAL = ocr.engines()


@pytest.mark.parametrize("engine", REAL, ids=[e.name for e in REAL])
def test_real_engine(tmp_path, engine):
    data = scanned_pdf(tmp_path, "Slim PDF recognizes scanned text.\nThe second line 2026")
    out, done, _skipped = ocr.recognize(data, [0], engine, ["en"])
    assert done == [0]
    text = all_text(out)
    for word in ("Slim", "recognizes", "scanned", "second", "2026"):
        assert word in text
    assert "recognizes" in text_in(out, 0, (60, 82, 400, 40))  # where it is on the page


@pytest.mark.parametrize("engine", [e for e in REAL if "de" in e.languages()],
                         ids=[e.name for e in REAL if "de" in e.languages()])
def test_real_engine_german(tmp_path, engine):
    data = scanned_pdf(tmp_path, "Größe über Straße", rotate=90)
    out, _done, _skipped = ocr.recognize(data, [0], engine, ["de"])
    text = all_text(out)
    assert "Größe" in text and "Straße" in text


def test_scanned_helper_is_text_free(tmp_path):
    assert all_text(scanned_pdf(tmp_path, "Only pixels")) == ""
