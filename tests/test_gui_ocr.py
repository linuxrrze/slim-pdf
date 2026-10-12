"""GUI: text recognition (Pages tab)."""

import threading

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from slim_pdf import insert, ocr  # noqa: E402
from slim_pdf.config import OcrSettings  # noqa: E402
from slim_pdf.gui import app as gui_app  # noqa: E402
from slim_pdf.gui.settings import SettingsDialog  # noqa: E402

from conftest import make_pdf, sign_with  # noqa: E402
# fixtures shared with test_gui.py
from test_gui import Boxes, qapp, save_to, secrets, window  # noqa: E402,F401
from test_gui_pages import select_thumbs  # noqa: E402
from test_ocr import WORDS, FakeEngine, all_text  # noqa: E402

PAGES_TAB = 2


@pytest.fixture
def engine(monkeypatch):
    engine = FakeEngine(WORDS)
    engine.threads = []
    recognize = engine.recognize

    def record(image, languages):
        engine.threads.append(threading.current_thread())
        return recognize(image, languages)

    engine.recognize = record
    monkeypatch.setattr(ocr, "get_engine", lambda name="", tesseract="": engine)
    return engine


def test_buttons_are_in_the_pages_tab(window):
    assert window.ribbon.tabText(PAGES_TAB) == "Pages"
    buttons = [b.defaultAction() for b in
               window.ribbon.tab_page(PAGES_TAB).findChildren(QtWidgets.QToolButton)]
    assert window.ocr_act in buttons and window.ocr_all_act in buttons
    assert not window.ocr_act.isEnabled()  # no document


def test_current_page(window, tmp_path, engine, monkeypatch):
    rendered = []
    render = ocr._render
    monkeypatch.setattr(ocr, "_render", lambda *a: (
        rendered.append(threading.current_thread()), render(*a))[1])
    window.open(make_pdf(tmp_path / "in.pdf", pages=2))
    window.view.go_to(1)
    window.ocr_act.trigger()
    assert window.dirty and "Recognized text on 1 page" in window.statusBar().currentMessage()
    assert all_text(window.data, 0) == "" and "Hello" in all_text(window.data, 1)
    # pdfium only on the GUI thread, the recognition in the background
    assert rendered == [threading.main_thread()]
    assert engine.threads and engine.threads[0] is not threading.main_thread()
    window.undo()
    assert all_text(window.data, 1) == ""


def test_selected_and_all_pages(window, tmp_path, engine):
    window.open(make_pdf(tmp_path / "in.pdf", pages=3))
    select_thumbs(window, 0, 2)
    window.ocr_act.trigger()
    assert [bool(all_text(window.data, i)) for i in range(3)] == [True, False, True]
    window.ocr_all_act.trigger()
    assert all(all_text(window.data, i) for i in range(3))
    assert "Recognized text on 1 page (2 pages already had text)" in \
        window.statusBar().currentMessage()


def test_pages_with_text(window, tmp_path, engine, monkeypatch):
    src = tmp_path / "typed.pdf"
    src.write_bytes(insert.add_text(make_pdf(tmp_path / "in.pdf", pages=1).read_bytes(), 0,
                                    (50, 50, 300, 100), "Typed"))
    window.open(src)
    boxes = Boxes(monkeypatch, gui_app)
    window.ocr_act.trigger()
    assert boxes.kinds() == ["information"] and "already has text" in boxes.text()
    assert not window.dirty


def test_nothing_found(window, tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "get_engine", lambda name="", tesseract="": FakeEngine([]))
    window.open(make_pdf(tmp_path / "in.pdf", pages=1))
    boxes = Boxes(monkeypatch, gui_app)
    window.ocr_act.trigger()
    assert "No text was found" in boxes.text() and not window.dirty


def test_cancel(window, tmp_path, engine, monkeypatch):
    window.open(make_pdf(tmp_path / "in.pdf", pages=3))
    step = window._ocr_step
    monkeypatch.setattr(window, "_ocr_step", lambda dialog, done, total: (
        step(dialog, done, total) and done < 1))
    window.ocr_all_act.trigger()
    assert len(engine.images) == 1  # stopped before the second page
    assert not window.dirty and "cancelled" in window.statusBar().currentMessage()


def test_no_engine(window, tmp_path, monkeypatch):
    def missing(name="", tesseract=""):
        raise ocr.OcrError("no OCR engine found. Install Tesseract")

    monkeypatch.setattr(ocr, "get_engine", missing)
    window.open(make_pdf(tmp_path / "in.pdf", pages=1))
    boxes = Boxes(monkeypatch, gui_app)
    window.ocr_act.trigger()
    assert boxes.kinds() == ["information"] and "Install Tesseract" in boxes.text()


def test_missing_language(window, tmp_path, engine, monkeypatch):
    window.config.ocr = OcrSettings(languages=["fr"])
    window.open(make_pdf(tmp_path / "in.pdf", pages=1))
    boxes = Boxes(monkeypatch, gui_app)
    window.ocr_act.trigger()
    assert boxes.kinds() == ["information"] and "French" in boxes.text()
    assert not engine.images


def test_engine_failure(window, tmp_path, engine, monkeypatch):
    def fail(image, languages):
        raise ocr.OcrError("Tesseract failed: out of memory")

    engine.recognize = fail
    window.open(make_pdf(tmp_path / "in.pdf", pages=1))
    boxes = Boxes(monkeypatch, gui_app)
    window.ocr_act.trigger()
    assert boxes.kinds() == ["critical"] and "out of memory" in boxes.text()
    assert not window.dirty


def test_signed_document_asks(window, profile, tmp_path, engine, monkeypatch):
    signed = tmp_path / "s.pdf"
    sign_with(profile, make_pdf(tmp_path / "in.pdf", pages=1), signed)
    window.open(signed)
    boxes = Boxes(monkeypatch, gui_app, answer=QtWidgets.QMessageBox.No)
    window.ocr_act.trigger()
    assert boxes.kinds() == ["warning"] and "invalidate" in boxes.text()
    assert not engine.images and not window.dirty
    boxes.answer = QtWidgets.QMessageBox.Yes
    window.ocr_act.trigger()
    assert window.dirty


def test_in_the_page_menu(window, tmp_path):
    window.open(make_pdf(tmp_path / "in.pdf", pages=1))
    assert window.ocr_act in window.page_menu().actions()


@pytest.fixture
def settings_dialog(qapp):
    from shiboken6 import delete

    dialogs = []
    yield lambda cfg: dialogs.append(SettingsDialog(cfg)) or dialogs[-1]
    for d in dialogs:  # deleted before the application (or it crashes on exit)
        d.close()
        delete(d)


def test_settings(settings_dialog, tmp_path, monkeypatch):
    from slim_pdf.config import Config

    monkeypatch.setattr(ocr, "engines", lambda tesseract="": [FakeEngine(WORDS)])
    cfg = Config(path=tmp_path / "config.toml",
                 ocr=OcrSettings(engine="tesseract", languages=["de"]))
    dlg = settings_dialog(cfg)
    combo = dlg.ocr_engine
    assert combo.currentData() == "tesseract"
    labels = [combo.itemText(i) for i in range(combo.count())]
    assert labels[0] == "Automatic" and "Tesseract (not available)" in labels
    assert dlg.ocr_languages.text() == "de"
    dlg.ocr_languages.setText("de, en; fr")
    dlg.tesseract_path.setText("/opt/tess/tesseract")
    combo.setCurrentIndex(0)
    dlg._save()
    assert dlg.config.ocr == OcrSettings(engine="", languages=["de", "en", "fr"],
                                         tesseract="/opt/tess/tesseract")
    assert "/opt/tess/tesseract" in cfg.path.read_text()
