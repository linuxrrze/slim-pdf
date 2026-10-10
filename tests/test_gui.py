"""GUI tests; run headless (conftest sets QT_QPA_PLATFORM=offscreen)."""

import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from pyhanko.sign import fields  # noqa: E402

from pdfsigner.config import CertProfile, Config, load_config  # noqa: E402
from pdfsigner.gui import app as gui_app  # noqa: E402
from pdfsigner.gui import certdialog  # noqa: E402
from pdfsigner.gui.certdialog import CertEditDialog, CertManagerDialog  # noqa: E402
from pdfsigner.gui.pageview import FieldItem, SignatureItem  # noqa: E402
from pdfsigner.signing import inspect_pdf  # noqa: E402

from conftest import PASSPHRASE, add_sig_field, make_encrypted, validate_all  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class Boxes:
    """Records QMessageBox calls instead of showing them."""

    def __init__(self, monkeypatch, module, answer=None):
        self.calls = []
        mb = module.QMessageBox
        answer = answer if answer is not None else mb.Yes
        for kind in ("critical", "warning", "information"):
            monkeypatch.setattr(mb, kind, self._rec(kind, mb.Ok))
        monkeypatch.setattr(mb, "question", self._rec("question", answer))
        self.answer = answer

    def _rec(self, kind, ret):
        def f(parent, title, text, *a, **k):
            self.calls.append((kind, title, text))
            return self.answer if kind in ("question", "warning") and a else ret
        return staticmethod(f)

    def kinds(self):
        return [c[0] for c in self.calls]

    def text(self):
        return "\n".join(c[2] for c in self.calls)


READ_CERTIFICATE = CertEditDialog._read_certificate


@pytest.fixture(autouse=True)
def no_certificate_reading(monkeypatch):
    """Saving a new profile reads its certificate from the key, which asks for
    a PIN; tests that don't test that save without it."""
    monkeypatch.setattr(CertEditDialog, "_read_certificate", lambda self, p: "")


@pytest.fixture
def secrets(monkeypatch):
    asked = []

    def ask(parent, prompt):
        asked.append(prompt)
        return PASSPHRASE

    monkeypatch.setattr(gui_app, "ask_secret", ask)
    return asked


@pytest.fixture
def save_to(monkeypatch, tmp_path):
    target = {"path": tmp_path / "signed.pdf"}
    monkeypatch.setattr(gui_app.QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(target["path"]), ""))
    return target


@pytest.fixture
def window(qapp, profile, p12_bob, secrets, save_to, tmp_path):
    bob = CertProfile(name="bob", path=str(p12_bob[0]), secret="prompt")  # no image
    profile.secret = "prompt"
    cfg = Config(certs={"alice": profile, "bob": bob}, default="alice",
                 path=tmp_path / "window-config.toml")
    win = gui_app.MainWindow(cfg)
    win.resize(900, 1000)
    win.show()
    QTest.qWaitForWindowExposed(win)
    yield win
    win.dirty = False  # don't ask about unsaved edits
    win.close()


def click_scene(view, x, y):
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier,
                     view.mapFromScene(QPointF(x, y)))


def stamp_aspect(window, name="alice"):
    """width / height of the profile's whole stamp (image and text)."""
    from pdfsigner import certinfo
    from pdfsigner.signing import stamp_size

    p = window.config.certs[name]
    w, h = stamp_size(p, certinfo.text_params(certinfo.load(p.certificate), p),
                      with_image=window.image_check.isChecked(),
                      with_text=window.text_check.isChecked())
    return w / h


def place(view, x, y):
    """Place mode (as after Place signature…), then a click."""
    view.set_place_mode(True)
    click_scene(view, x, y)
    assert not view.place_mode  # back to zoom/move


def drag_scene(view, start, end):
    vp = view.viewport()
    a, b = view.mapFromScene(QPointF(*start)), view.mapFromScene(QPointF(*end))
    QTest.mousePress(vp, Qt.LeftButton, Qt.NoModifier, a)
    for i in range(1, 6):
        QTest.mouseMove(vp, a + (b - a) * i / 5)
    QTest.mouseRelease(vp, Qt.LeftButton, Qt.NoModifier, b)



def test_open_document_does_not_hold_file(window, form_pdf):
    """The viewer must not keep the PDF open, or Windows can't overwrite it."""
    window.open(form_pdf)
    fd_dir = "/proc/self/fd"
    if not os.path.isdir(fd_dir):
        pytest.skip("needs /proc")
    open_files = set()
    for fd in os.listdir(fd_dir):
        try:
            open_files.add(os.path.realpath(os.readlink(os.path.join(fd_dir, fd))))
        except OSError:
            pass
    assert os.path.realpath(form_pdf) not in open_files


def test_sign_in_place_while_viewing(window, form_pdf, save_to, p12):
    save_to["path"] = form_pdf  # overwrite the file that is being shown
    window.open(form_pdf)
    window.view.select_field("Applicant")
    window.sign()
    assert inspect_pdf(form_pdf).field("Applicant").filled
    assert window.path == form_pdf
    [(_, st)] = validate_all(form_pdf, p12[1])
    assert st.bottom_line

# --- placement --------------------------------------------------------------------


def test_place_resize_sign_twice(window, form_pdf, save_to, p12, p12_bob):
    out = save_to["path"]
    window.open(form_pdf)
    view = window.view
    assert window.sign_btn.text() == "Place signature…"
    window.sign()  # nothing placed yet: starts placing
    assert view.place_mode and view.placement is None
    assert "Click where the signature should go" in window.statusBar().currentMessage()
    click_scene(view, 200, 300)
    assert not view.place_mode  # one click, then back to zoom/move
    item = view.placement
    assert isinstance(item, SignatureItem)
    w, h = item.size
    assert w / h == pytest.approx(stamp_aspect(window))
    assert window.sign_act.isEnabled() and window.sign_btn.text() == "Sign…"
    assert "New signature on page 1" in window.target_label.text()

    # drag the resize handle outwards: aspect ratio stays 3:1
    r = item.scene_rect()
    handle = (r.right() - 2 / view.zoom, r.bottom() - 2 / view.zoom)
    drag_scene(view, handle, (handle[0] + 90, handle[1] + 5))
    w2, h2 = item.size
    assert w2 > w + 60
    assert w2 / h2 == pytest.approx(stamp_aspect(window))

    page, box = view.placement_box()
    assert page == 0
    r2 = item.scene_rect()
    assert r2.topLeft() == r.topLeft()  # resizing keeps the top-left corner
    assert box[0] == pytest.approx(r2.left(), abs=0.1)
    assert box[3] == pytest.approx(842 - r2.top(), abs=0.1)  # display y from the top
    assert (box[2] - box[0]) / (box[3] - box[1]) == pytest.approx(stamp_aspect(window), rel=0.01)

    window.sign()
    info = inspect_pdf(out)
    assert info.field("Signature1").filled
    assert info.field("Signature1").rect == pytest.approx(box, abs=0.05)
    assert window.path == out  # reloaded for further signatures
    assert window.view.placement is None

    # second signature: Bob signs the prepared field on page 2
    window.cert_combo.setCurrentIndex(window.cert_combo.findData("bob"))
    window.view.select_field("Applicant")
    assert window.view.page == 1
    window.sign()
    info = inspect_pdf(out)
    assert info.field("Applicant").filled and info.field("Signature1").filled
    assert not info.field("Supervisor").filled
    assert all(st.bottom_line for _, st in validate_all(out, p12[1], p12_bob[1]))


def test_drag_moves_and_stays_on_page(window, blank_pdf):
    window.open(blank_pdf)
    view = window.view
    place(view, 200, 300)
    r = view.placement.scene_rect()
    centre = (r.center().x(), r.center().y())
    drag_scene(view, centre, (centre[0] + 100, centre[1] + 50))
    r2 = view.placement.scene_rect()
    assert r2.left() == pytest.approx(r.left() + 100, abs=2)
    assert r2.top() == pytest.approx(r.top() + 50, abs=2)
    assert r2.size() == r.size()
    # dragging far outside clamps to the page
    c = (r2.center().x(), r2.center().y())
    drag_scene(view, c, (c[0] + 2000, c[1] + 2000))
    r3 = view.placement.scene_rect()
    assert r3.right() == pytest.approx(595, abs=0.5) and r3.bottom() == pytest.approx(842, abs=0.5)


def test_placement_clamped_to_page(window, blank_pdf):
    window.open(blank_pdf)
    view = window.view
    place(view, 590, 835)  # near the bottom-right corner
    r = view.placement.scene_rect()
    assert r.right() <= 595 + 0.01 and r.bottom() <= 842 + 0.01
    view.placement.scale_by(10)
    r = view.placement.scene_rect()
    assert r.right() <= 595 + 0.01 and r.bottom() <= 842 + 0.01
    w, h = view.placement.size
    assert w / h == pytest.approx(stamp_aspect(window))
    view.placement.scale_by(0.01)
    assert view.placement.size[0] == pytest.approx(30)  # minimum width


def test_click_again_moves_existing_placement(window, blank_pdf):
    window.open(blank_pdf)
    view = window.view
    place(view, 200, 300)
    first = view.placement
    place(view, 400, 600)
    assert view.placement is first
    assert view.placement.scene_rect().center().x() == pytest.approx(400, abs=1)


def test_placement_survives_page_navigation(window, blank_pdf):
    window.open(blank_pdf)
    view = window.view
    place(view, 200, 300)
    item = view.placement
    view.go_to(1)
    assert item.scene() is None  # not shown on page 2
    assert view.placement_box()[0] == 0
    assert window.sign_act.isEnabled()
    view.go_to(0)
    assert item.scene() is view.scene()
    # clicking on page 2 moves the placement there
    view.go_to(1)
    place(view, 300, 300)
    assert view.placement_box()[0] == 1


def test_page_spin_and_label(window, blank_pdf):
    window.open(blank_pdf)
    assert window.page_spin.maximum() == 2 and window.page_count.text().strip() == "/ 2"
    window.page_spin.setValue(2)
    assert window.view.page == 1
    window.view.go_to(0)
    assert window.page_spin.value() == 1


def test_escape_clears_target(window, blank_pdf):
    window.open(blank_pdf)
    place(window.view, 200, 300)
    assert window.sign_act.isEnabled()
    window.view.clear_target()
    assert window.view.placement is None
    assert window.sign_btn.text() == "Place signature…"
    assert "None yet" in window.target_label.text()
    window.sign()
    assert window.view.place_mode
    window._escape()  # Esc ends place mode without placing
    assert not window.view.place_mode and window.view.placement is None


def test_default_is_zoom_and_move(window, blank_pdf, qapp):
    """Without Place signature…, a click places nothing, the wheel zooms
    around the mouse and dragging moves the page."""
    window.open(blank_pdf)
    view = window.view
    assert view.dragMode() == QtWidgets.QGraphicsView.ScrollHandDrag
    click_scene(view, 200, 300)
    assert view.placement is None and view.selected_field is None

    view.set_zoom(1.0)
    qapp.processEvents()
    under = view.mapFromScene(QPointF(300, 400))
    zoom = view.zoom
    wheel(view, (300, 400), 120, Qt.NoModifier)
    assert view.zoom == pytest.approx(zoom * 1.15)
    after = view.mapFromScene(QPointF(300, 400))  # the point under the mouse stays
    assert abs(after.x() - under.x()) <= 2 and abs(after.y() - under.y()) <= 2
    wheel(view, (300, 400), -120, Qt.NoModifier)
    assert view.zoom == pytest.approx(zoom)

    view.set_zoom(3.0)
    qapp.processEvents()
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    y0 = bar.value()
    drag_scene(view, (300, 400), (300, 350))  # drag the page up: shows more below
    assert bar.value() > y0
    assert view.placement is None


def test_zoom_out_stops_when_the_page_fits(window, blank_pdf, qapp):
    window.open(blank_pdf)
    view = window.view
    qapp.processEvents()
    for _ in range(30):
        wheel(view, (300, 400), -120, Qt.NoModifier)
    view.set_zoom(view.zoom / 1.2)  # toolbar "−"
    qapp.processEvents()
    assert view.zoom == pytest.approx(view.min_zoom())
    shown = view.mapToScene(view.viewport().rect()).boundingRect()
    assert shown.contains(QRectF(0, 0, 595, 842))  # the whole page is visible
    assert not view.verticalScrollBar().isVisible()
    # and it fills the view in one direction: no smaller than needed
    slack = 4 / view.zoom  # whole pixels
    assert shown.height() <= 842 + 40 + slack or shown.width() <= 595 + 40 + slack
    window.resize(window.width(), window.height() - 200)  # a smaller window: still fits
    qapp.processEvents()
    assert view.zoom == pytest.approx(view.min_zoom())


def test_place_mode_picks_a_field(window, form_pdf):
    window.open(form_pdf)
    window.view.go_to(1)
    field = next(i for i in window.view.scene().items()
                 if isinstance(i, FieldItem) and i.field.name == "Applicant")
    c = field.rect().center()
    click_scene(window.view, c.x(), c.y())  # default mode: nothing happens
    assert window.view.selected_field is None
    window.sign()
    click_scene(window.view, c.x(), c.y())
    assert window.view.selected_field == "Applicant" and not window.view.place_mode
    assert window.sign_btn.text() == "Sign…"


def test_zoomed_in_only_the_visible_part_is_rendered_sharply(window, blank_pdf, qapp):
    from pdfsigner.gui.pageview import MAX_PAGE_PIXELS

    window.open(blank_pdf)
    view = window.view
    view.set_zoom(6.0)
    view._update_detail()  # normally after a short pause
    detail = view._detail_item
    assert detail.isVisible()
    pm = detail.pixmap()
    vp = view.viewport()
    dpr = view.devicePixelRatioF()
    assert pm.width() <= vp.width() * dpr + 2 and pm.height() <= vp.height() * dpr + 2
    shown = view.mapToScene(vp.rect()).boundingRect() & QRectF(0, 0, 595, 842)
    assert detail.sceneBoundingRect().left() == pytest.approx(shown.left(), abs=0.5)
    assert detail.sceneBoundingRect().top() == pytest.approx(shown.top(), abs=0.5)
    page = view._page_item.pixmap()
    assert page.width() * page.height() <= MAX_PAGE_PIXELS
    view.set_zoom(None)
    view._update_detail()
    assert not detail.isVisible()  # the whole-page pixmap is sharp enough


def test_fit_width_has_no_horizontal_scrollbar(window, blank_pdf, qapp):
    window.open(blank_pdf)
    window.view.set_zoom(None)
    qapp.processEvents()
    assert not window.view.horizontalScrollBar().isVisible()
    window.view.set_zoom(2.0)
    qapp.processEvents()
    assert window.view.zoom == 2.0
    assert window.view.horizontalScrollBar().isVisible()


# --- certificate / appearance --------------------------------------------------------


def test_switching_certificate_updates_preview(window, blank_pdf):
    window.open(blank_pdf)
    place(window.view, 200, 300)
    item = window.view.placement
    with_image = item._pixmap
    assert with_image is not None
    window.cert_combo.setCurrentIndex(window.cert_combo.findData("bob"))  # no image
    assert item._pixmap is not None and item._pixmap.cacheKey() != with_image.cacheKey()
    assert item.size[0] / item.size[1] == pytest.approx(stamp_aspect(window, "bob"))
    window.cert_combo.setCurrentIndex(window.cert_combo.findData("alice"))
    window.image_check.setChecked(False)  # the text alone
    assert item.size[0] / item.size[1] == pytest.approx(stamp_aspect(window))
    window.text_check.setChecked(False)  # neither: a plain text stamp, as when signing
    assert item._pixmap is not None


def test_image_and_text_switches_are_passed(window, blank_pdf, save_to, monkeypatch):
    seen = {}
    real = gui_app.sign_pdf

    def spy(*a, **k):
        import threading

        seen.update(k, thread=threading.current_thread().name)
        return real(*a, **k)

    monkeypatch.setattr(gui_app, "sign_pdf", spy)
    window.open(blank_pdf)
    place(window.view, 200, 300)
    window.text_check.setChecked(False)
    window.sign()
    assert seen["with_text"] is False and seen["with_image"] is True
    assert seen["thread"] == "pdfsigner-work"  # not on the GUI thread


# --- prepared fields ------------------------------------------------------------------


def test_field_overlays_and_list(window, form_pdf):
    window.open(form_pdf)
    assert window.fields_list.count() == 2
    window.view.go_to(1)
    items = [i for i in window.view.scene().items() if isinstance(i, FieldItem)]
    assert {i.field.name for i in items} == {"Applicant", "Supervisor"}

    # click the field on the page
    applicant = next(i for i in items if i.field.name == "Applicant")
    c = applicant.rect().center()
    place(window.view, c.x(), c.y())
    assert window.view.selected_field == "Applicant"
    assert window.view.placement is None
    assert "Prepared field 'Applicant'" in window.target_label.text()
    selected = [window.fields_list.item(i).data(Qt.UserRole)
                for i in range(window.fields_list.count())
                if window.fields_list.item(i).isSelected()]
    assert selected == ["Applicant"]


def test_list_click_jumps_to_page(window, form_pdf):
    window.open(form_pdf)
    assert window.view.page == 0
    window._field_clicked(window.fields_list.item(1))
    assert window.view.page == 1 and window.view.selected_field == "Supervisor"


def test_signed_field_not_selectable(window, form_pdf, save_to):
    window.open(form_pdf)
    window.view.select_field("Applicant")
    window.sign()
    window.view.select_field("Applicant")
    assert window.view.selected_field is None
    assert window.sign_btn.text() == "Place signature…"  # no target


# --- error paths ---------------------------------------------------------------------------


def test_wrong_passphrase_shows_error(window, form_pdf, save_to, monkeypatch):
    monkeypatch.setattr(gui_app, "ask_secret", lambda parent, prompt: "wrong")
    boxes = Boxes(monkeypatch, gui_app)
    window.open(form_pdf)
    window.view.select_field("Applicant")
    window.sign()
    assert boxes.kinds() == ["critical"]
    assert "wrong passphrase" in boxes.text()
    assert not save_to["path"].exists()
    assert window.path == form_pdf  # still on the original


def test_cancel_passphrase_is_silent(window, form_pdf, save_to, monkeypatch):
    monkeypatch.setattr(gui_app, "ask_secret", lambda parent, prompt: None)
    boxes = Boxes(monkeypatch, gui_app)
    window.open(form_pdf)
    window.view.select_field("Applicant")
    window.sign()
    assert boxes.calls == [] and not save_to["path"].exists()


def test_cancel_save_dialog(window, form_pdf, secrets, monkeypatch):
    monkeypatch.setattr(gui_app.QFileDialog, "getSaveFileName", lambda *a, **k: ("", ""))
    window.open(form_pdf)
    window.view.select_field("Applicant")
    window.sign()
    assert secrets == []  # nothing asked when the user cancels saving


def test_locked_field_warns(window, blank_pdf, tmp_path, save_to, monkeypatch):
    lock = fields.FieldMDPSpec(fields.FieldMDPAction.INCLUDE, fields=["Second"])
    src = add_sig_field(blank_pdf, tmp_path / "a.pdf", "First", 0, (10, 10, 100, 40),
                        field_mdp_spec=lock)
    src = add_sig_field(src, tmp_path / "b.pdf", "Second", 0, (110, 10, 200, 40))
    window.open(src)
    window.view.select_field("First")
    window.sign()
    signed = save_to["path"]
    assert inspect_pdf(signed).field("Second").locked
    # Locked fields can't be picked on the page ...
    window.view.select_field("Second")
    assert window.view.selected_field is None
    # ... and if targeted anyway, the user is warned and can back out.
    boxes = Boxes(monkeypatch, gui_app, answer=gui_app.QMessageBox.No)
    window.view.selected_field = "Second"
    save_to["path"] = tmp_path / "second.pdf"
    window.sign()
    assert boxes.kinds() == ["warning"] and "locked" in boxes.text()
    assert not (tmp_path / "second.pdf").exists()


def test_frozen_document_refused(window, blank_pdf, tmp_path, save_to, monkeypatch, profile):
    import io

    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers

    from pdfsigner.secretstore import Credentials
    from pdfsigner.signing import open_signer

    certified = tmp_path / "cert.pdf"
    with open_signer(profile, Credentials(PASSPHRASE)) as s:
        w = IncrementalPdfFileWriter(io.BytesIO(blank_pdf.read_bytes()))
        meta = signers.PdfSignatureMetadata(field_name="Author", certify=True,
                                            docmdp_permissions=fields.MDPPerm.NO_CHANGES)
        with open(certified, "wb") as f:
            signers.PdfSigner(meta, s).sign_pdf(w, output=f)
    boxes = Boxes(monkeypatch, gui_app)
    window.open(certified)
    assert "certified" in window.statusBar().currentMessage()
    place(window.view, 200, 300)
    window.sign()
    assert boxes.kinds() == ["critical"] and "cannot be signed again" in boxes.text()
    assert not save_to["path"].exists()


def test_new_box_on_certified_document_warns(window, form_pdf, tmp_path, save_to, monkeypatch,
                                             profile):
    import io

    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers

    from pdfsigner.secretstore import Credentials
    from pdfsigner.signing import open_signer

    certified = tmp_path / "cert.pdf"
    with open_signer(profile, Credentials(PASSPHRASE)) as s:
        w = IncrementalPdfFileWriter(io.BytesIO(form_pdf.read_bytes()))
        meta = signers.PdfSignatureMetadata(field_name="Author", certify=True,
                                            docmdp_permissions=fields.MDPPerm.FILL_FORMS)
        with open(certified, "wb") as f:
            signers.PdfSigner(meta, s).sign_pdf(w, output=f)
    window.open(certified)
    boxes = Boxes(monkeypatch, gui_app, answer=gui_app.QMessageBox.No)
    place(window.view, 200, 300)
    window.sign()
    assert boxes.kinds() == ["warning"] and "breaks the certification" in boxes.text()
    assert not save_to["path"].exists()
    window.view.select_field("Applicant")  # an existing empty field is fine
    window.sign()
    assert boxes.kinds() == ["warning"]
    assert save_to["path"].exists()


def test_open_encrypted_asks_password(window, form_pdf, tmp_path, monkeypatch, save_to):
    enc = make_encrypted(form_pdf, tmp_path / "enc.pdf", "docpw")
    answers = iter(["wrong", "docpw"])
    asked = []

    def ask(parent, prompt):
        asked.append(prompt)
        return next(answers) if prompt.startswith("Password for") else PASSPHRASE

    monkeypatch.setattr(gui_app, "ask_secret", ask)
    assert window.open(enc)
    assert asked == ["Password for enc.pdf", "Password for enc.pdf"]
    window.view.select_field("Applicant")
    window.sign()
    assert inspect_pdf(save_to["path"], "docpw").field("Applicant").filled


def test_open_garbage_shows_error(window, tmp_path, monkeypatch):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    boxes = Boxes(monkeypatch, gui_app)
    assert not window.open(bad)
    assert boxes.kinds() == ["critical"]


# --- certificate manager ---------------------------------------------------------------------


@pytest.fixture
def certs_cfg(tmp_path, p12, sig_image):
    cfg = Config(path=tmp_path / "config.toml")
    cfg.put(CertProfile(name="work", path=str(p12[0]), image=str(sig_image)))
    cfg.put(CertProfile(name="tok", type="pkcs11", pkcs11_lib="/x.so", key_id="02",
                        secret="keyring"))
    return cfg


@pytest.fixture
def manager(qapp, certs_cfg):
    d = CertManagerDialog(certs_cfg)
    d.show()
    yield d
    d.close()
    from shiboken6 import delete

    delete(d)  # not at interpreter exit, after the QApplication (crashes)


@pytest.fixture
def open_editor(qapp, certs_cfg):
    """open_editor(name) -> an editor for that profile (None: a new one)."""
    made = []

    def make(name, cfg=None):
        d = CertEditDialog(cfg or certs_cfg, name)
        d.show()
        made.append(d)
        return d

    yield make
    from shiboken6 import delete

    for d in made:
        d.close()
        delete(d)


@pytest.fixture
def dialog(open_editor):
    return open_editor("work")


def rows(manager):
    return [[manager.list.topLevelItem(i).text(c) for c in range(4)]
            for i in range(manager.list.topLevelItemCount())]


def select(manager, name):
    for i in range(manager.list.topLevelItemCount()):
        item = manager.list.topLevelItem(i)
        if item.data(0, Qt.UserRole) == name:
            manager.list.setCurrentItem(item)
            return
    raise AssertionError(name)


def editing(manager, monkeypatch, fill, save=True):
    """Let the next editor the manager opens be filled in by ``fill`` and saved
    (or cancelled); returns the editors opened."""
    opened = []

    def exec_editor(dlg):
        opened.append(dlg)
        fill(dlg)
        if save:
            dlg._save()
        return dlg.result()

    monkeypatch.setattr(manager, "_exec_editor", exec_editor)
    return opened


def test_manager_lists_profiles(manager, p12):
    """Only the list: the profiles' settings are in the editor."""
    assert rows(manager) == [["work  (default)", "PKCS#12 file", "–", str(p12[0])],
                             ["tok", "PKCS#11 token", "–", "x.so – key ID 02"]]
    assert manager.current() == "work"
    assert manager.edit_btn.isEnabled() and manager.remove_btn.isEnabled()
    assert not manager.default_btn.isEnabled()  # already the default
    select(manager, "tok")
    assert manager.default_btn.isEnabled()
    assert manager.findChild(QtWidgets.QGroupBox) is None  # no form in the list window


def test_manager_empty(qapp, tmp_path):
    d = CertManagerDialog(Config(path=tmp_path / "config.toml"))
    assert rows(d) == [] and d.current() is None
    assert not d.edit_btn.isEnabled() and not d.remove_btn.isEnabled()
    assert not d.default_btn.isEnabled() and d.add_btn.isEnabled()
    from shiboken6 import delete

    delete(d)


def test_editor_loads_profile(open_editor):
    work = open_editor("work")
    assert work.windowTitle() == "Edit certificate 'work'"
    assert work.name_edit.text() == "work" and work.name_edit.isReadOnly()
    assert work.p12_box.isVisible() and not work.p11_box.isVisible()
    assert work.preview.pixmap() is not None and not work.preview.pixmap().isNull()
    tok = open_editor("tok")
    assert tok.p11_box.isVisible() and not tok.p12_box.isVisible()
    assert tok.keyring_btn.isVisible()
    assert tok.e["pkcs11_lib"].text() == "/x.so"
    new = open_editor(None)
    assert new.windowTitle() == "New certificate"
    assert new.name_edit.text() == "" and not new.name_edit.isReadOnly()


def test_manager_edit_add_default(manager, monkeypatch, tmp_path):
    def approve(dlg):
        dlg.e["reason"].setText("Approval")

    opened = editing(manager, monkeypatch, approve)
    manager._edit()
    assert [e.name for e in opened] == ["work"]
    assert manager.changed
    assert load_config(tmp_path / "config.toml").certs["work"].reason == "Approval"

    def vault(dlg):
        dlg.name_edit.setText("vault")
        dlg.secret_combo.setCurrentIndex(dlg.secret_combo.findData("keepass"))
        assert dlg._secret_form.isRowVisible(dlg.e["keepass_entry"])
        dlg.e["keepass_db"].setText("~/Passwords.kdbx")
        dlg.e["keepass_entry"].setText("Certificates/FAU S/MIME")
        dlg.e["keepass_attachment"].setText("fau.p12")
        dlg.stamp_text.setPlainText("")

    editing(manager, monkeypatch, vault)
    manager._add()
    assert manager.current() == "vault"  # the new profile is selected
    assert rows(manager)[2] == ["vault", "PKCS#12 file", "–", "KeePass: Certificates/FAU S/MIME"]
    manager._make_default()

    cfg = load_config(tmp_path / "config.toml")
    assert cfg.default == "vault"
    assert cfg.certs["work"].reason == "Approval"
    v = cfg.certs["vault"]
    assert (v.secret, v.keepass_entry, v.keepass_attachment, v.stamp_text) == (
        "keepass", "Certificates/FAU S/MIME", "fau.p12", "")
    assert rows(manager)[2][0] == "vault  (default)"


def test_manager_cancelled_edit_changes_nothing(manager, monkeypatch, tmp_path):
    opened = editing(manager, monkeypatch, lambda dlg: dlg.e["reason"].setText("Approval"),
                     save=False)
    manager.list.itemActivated.emit(manager.list.currentItem(), 0)  # double-click / Enter
    assert [e.name for e in opened] == ["work"]
    assert not manager.changed and not (tmp_path / "config.toml").exists()
    assert manager.config.certs["work"].reason == ""


def test_dialog_rejects_invalid(dialog, monkeypatch, tmp_path):
    boxes = Boxes(monkeypatch, certdialog)
    dialog.e["path"].setText("")
    dialog._save()
    assert boxes.kinds() == ["warning"] and "needs a path" in boxes.text()
    assert dialog.result() != QtWidgets.QDialog.Accepted
    assert not (tmp_path / "config.toml").exists()
    assert dialog.config.certs["work"].path  # unchanged


def test_editor_name_checks(open_editor, monkeypatch, tmp_path):
    boxes = Boxes(monkeypatch, certdialog)
    new = open_editor(None)
    new.e["path"].setText("x.p12")
    new._save()
    assert boxes.kinds() == ["warning"] and "needs a name" in boxes.text()
    new.name_edit.setText("work")
    new._save()
    assert boxes.kinds()[-1] == "warning" and "already exists" in boxes.text()
    assert not (tmp_path / "config.toml").exists()
    new.name_edit.setText("second")
    new._save()
    assert new.result() == QtWidgets.QDialog.Accepted
    assert list(load_config(tmp_path / "config.toml").certs) == ["work", "tok", "second"]


def test_manager_remove(manager, monkeypatch, tmp_path):
    Boxes(monkeypatch, certdialog)  # question answered Yes
    select(manager, "work")
    manager._remove()
    assert list(manager.config.certs) == ["tok"] and manager.config.default == "tok"
    assert rows(manager) == [["tok  (default)", "PKCS#11 token", "–", "x.so – key ID 02"]]
    cfg = load_config(tmp_path / "config.toml")
    assert list(cfg.certs) == ["tok"] and cfg.default == "tok"


def test_dialog_test_button(dialog, monkeypatch):
    monkeypatch.setattr(certdialog, "ask_secret", lambda parent, prompt: PASSPHRASE)
    boxes = Boxes(monkeypatch, certdialog)
    dialog._test()
    assert boxes.kinds() == ["information"] and "Alice Example" in boxes.text()
    monkeypatch.setattr(certdialog, "ask_secret", lambda parent, prompt: "wrong")
    dialog._test()
    assert boxes.kinds()[-1] == "critical"


def test_dialog_store_keyring(open_editor, monkeypatch, mem_keyring):
    monkeypatch.setattr(certdialog, "ask_secret", lambda parent, prompt: "1234")
    boxes = Boxes(monkeypatch, certdialog)
    open_editor("tok")._store_keyring()
    assert mem_keyring.store[("pdfsigner", "tok")] == "1234"
    new = open_editor(None)
    new._store_keyring()  # a new profile needs its name first
    assert boxes.kinds()[-1] == "warning" and "name" in boxes.text()
    new.name_edit.setText("card")
    new._store_keyring()
    assert mem_keyring.store[("pdfsigner", "card")] == "1234"


def test_manage_updates_main_window(window, monkeypatch, tmp_path):
    def fake_exec(self):
        self.config.certs["new"] = CertProfile(name="new", path="x.p12")
        self.changed = True
        return 0  # closed

    monkeypatch.setattr(CertManagerDialog, "exec", fake_exec)
    window.manage_certs()
    names = [window.cert_combo.itemData(i) for i in range(window.cert_combo.count())]
    assert names == ["alice", "bob", "new"]
    assert window.cert_combo.currentData() == "alice"  # selection kept


def wheel(view, scene_pos, delta, modifiers=Qt.ControlModifier):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QWheelEvent

    p = QPointF(view.mapFromScene(QPointF(*scene_pos)))
    ev = QWheelEvent(p, QPointF(view.viewport().mapToGlobal(p.toPoint())), QPoint(0, 0),
                     QPoint(0, delta), Qt.NoButton, modifiers, Qt.NoScrollPhase, False)
    QtWidgets.QApplication.sendEvent(view.viewport(), ev)


def test_ctrl_wheel_resizes_signature_and_zooms_page(window, blank_pdf):
    window.open(blank_pdf)
    view = window.view
    place(view, 200, 300)
    w0 = view.placement.size[0]
    wheel(view, (200, 300), 120)  # over the signature: resize
    w1, h1 = view.placement.size
    assert w1 == pytest.approx(w0 * 1.1) and w1 / h1 == pytest.approx(stamp_aspect(window))
    zoom = view.zoom
    wheel(view, (450, 700), 120)  # over the page: zoom
    assert view.zoom == pytest.approx(zoom * 1.15)
    assert view.placement.size[0] == pytest.approx(w1)


# --- signature validation panel ---------------------------------------------------


def sig_rows(window):
    return [window.sigs_list.item(i).text() for i in range(window.sigs_list.count())]


def test_signatures_panel(window, form_pdf, save_to, p12, tmp_path, monkeypatch):
    from conftest import sign_with, write_pem

    window.open(form_pdf)
    assert sig_rows(window) == []

    out = tmp_path / "s.pdf"
    sign_with(window.config.certs["alice"], form_pdf, out, field_name="Applicant")
    window.open(out)
    [row] = sig_rows(window)
    assert row.startswith("Applicant: untrusted\n   Alice Example, ")
    assert "1 signature: 1 untrusted" in window.statusBar().currentMessage()

    window.config.trust = [str(write_pem(p12[1], tmp_path / "alice.pem"))]
    window.open(out)
    assert sig_rows(window)[0].startswith("Applicant: valid")
    assert "1 signature: 1 valid" in window.statusBar().currentMessage()

    shown = []
    monkeypatch.setattr(gui_app.QMessageBox, "exec",
                        lambda self: shown.append((self.text(), self.detailedText())))
    window.view.go_to(0)
    item = window.sigs_list.item(0)
    window._signature_clicked(item)
    assert window.view.page == 1  # the field is on page 2
    window.show_signature_details(item)
    [(text, details)] = shown
    assert "<b>VALID</b>" in text and "Alice Example" in text
    assert "Bottom line" in details


def test_signatures_panel_unsupported_format(window, form_pdf, tmp_path):
    """A legacy adbe.x509.rsa_sha1 signature is listed, greyed, not dropped."""
    from conftest import sign_with

    out = tmp_path / "s.pdf"
    sign_with(window.config.certs["alice"], form_pdf, out, field_name="Applicant")
    out.write_bytes(out.read_bytes().replace(b"/ETSI.CAdES.detached", b"/adbe.x509.rsa_sha1 "))
    window.open(out)
    [row] = sig_rows(window)
    assert row.startswith("Applicant: not checked\n")
    assert "1 signature: 1 not checked" in window.statusBar().currentMessage()
    assert "adbe.x509.rsa_sha1" in window.sigs_list.item(0).toolTip()


def test_lists_escape_text_from_the_pdf(window, blank_pdf, tmp_path):
    """A field name with a line break can't fake an extra (green) row."""
    from conftest import sign_with

    out = tmp_path / "evil.pdf"
    sign_with(window.config.certs["alice"], blank_pdf, out, field_name="X\nApplicant: valid",
              page=0, box=(50, 50, 200, 100))
    window.open(out)
    [row] = sig_rows(window)
    assert row.startswith("X\\nApplicant: valid: untrusted\n")
    field_row = window.fields_list.item(0).text()
    assert field_row.startswith("X\\nApplicant: valid\n")


def test_signatures_panel_after_gui_signing(window, form_pdf, save_to):
    window.open(form_pdf)
    window.view.select_field("Applicant")
    window.sign()
    assert [r.split(":")[0] for r in sig_rows(window)] == ["Applicant"]


def test_validation_failure_does_not_block_open(window, form_pdf, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(gui_app.validation, "verify_pdf", boom)
    assert window.open(form_pdf)
    assert sig_rows(window) == ["Validation failed: broken"]


def test_dialog_ltv_checkbox(dialog, open_editor):
    assert not dialog.ltv_check.isChecked()
    dialog.ltv_check.setChecked(True)
    assert not open_editor("tok").ltv_check.isChecked()
    dialog._save()
    assert open_editor("work").ltv_check.isChecked()
    assert load_config(dialog.config.path).certs["work"].ltv is True
    assert load_config(dialog.config.path).certs["tok"].ltv is False


# --- vector signature images ------------------------------------------------------


def test_pdf_signature_image_preview(window, form_pdf, tmp_path):
    from conftest import make_vector_signature

    window.config.certs["alice"].image = str(make_vector_signature(tmp_path / "v.pdf", 200, 50))
    window._cert_changed()
    window.open(form_pdf)
    place(window.view, 200, 300)
    w, h = window.view.placement.size
    assert w / h == pytest.approx(stamp_aspect(window))
    window.text_check.setChecked(False)  # the image alone: its own 4:1
    assert window.view.placement.size[0] / window.view.placement.size[1] == pytest.approx(4.0)
    pm = window.view.placement._pixmap
    assert pm is not None and not pm.isNull()


def test_dialog_pdf_image_preview(dialog, tmp_path):
    from conftest import make_vector_signature

    dialog.e["image"].setText(str(make_vector_signature(tmp_path / "v.pdf")))
    assert not dialog.preview.pixmap().isNull()
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"nope")
    dialog.e["image"].setText(str(bad))  # no crash, just no preview


def test_dialog_keepassxc_rows(dialog):
    combo = dialog.secret_combo
    form = dialog._secret_form
    combo.setCurrentIndex(combo.findData("keepass"))
    assert form.isRowVisible(dialog.e["keepass_db"].parentWidget())
    assert not form.isRowVisible(dialog.yubikey_row)
    combo.setCurrentIndex(combo.findData("keepassxc"))
    assert form.isRowVisible(dialog.yubikey_row)
    combo.setCurrentIndex(combo.findData("prompt"))
    assert not form.isRowVisible(dialog.yubikey_row)


def test_dialog_system_store(open_editor, monkeypatch):
    from test_systemstore import FakeBackend
    from cryptography.hazmat.primitives.asymmetric import ec
    from pki import MiniPKI
    from test_systemstore import issue

    ca = MiniPKI(port=1)
    key = ec.generate_private_key(ec.SECP256R1())
    backend = FakeBackend([(issue(ca, "Store Alice", key), key)])
    monkeypatch.setattr(certdialog.systemstore, "get_backend", lambda: backend)
    # as on Windows/macOS, where the type is offered
    monkeypatch.setattr(certdialog, "supported_cert_types",
                        lambda: ("pkcs12", "pkcs11", "system"))
    dialog = open_editor("work")
    dialog.type_combo.setCurrentText("system")
    assert dialog.sys_box.isVisible()
    assert not dialog.p12_box.isVisible() and not dialog.p11_box.isVisible()
    assert not dialog.secret_box.isVisible()  # the OS asks for PINs

    monkeypatch.setattr(certdialog.QInputDialog, "getItem",
                        staticmethod(lambda parent, title, label, items, *a: (items[0], True)))
    dialog._choose_system_cert()
    tp = backend.identities()[0].thumbprint
    assert dialog.e["system_cert"].text() == tp
    dialog._save()
    saved = load_config(dialog.config.path).certs["work"]
    assert (saved.type, saved.system_cert) == ("system", tp)


def test_dialog_offers_only_supported_types(open_editor, certs_cfg, monkeypatch):
    """On Linux there is no "system" type to choose; a profile of that type
    from a shared config file is still shown (and kept) as it is."""
    monkeypatch.setattr(certdialog, "supported_cert_types", lambda: ("pkcs12", "pkcs11"))
    cfg = certs_cfg
    cfg.put(CertProfile(name="store", type="system", system_cert="AB" * 20))
    types = [open_editor(None).type_combo.itemData(i) for i in range(2)]
    assert types == ["pkcs12", "pkcs11"] and open_editor(None).type_combo.count() == 2
    d = open_editor("store")
    assert d.type_combo.currentData() == "system"
    assert "not available on this system" in d.type_combo.currentText()
    d._save()
    assert load_config(cfg.path).certs["store"].type == "system"


def test_dialog_system_store_unavailable(dialog, monkeypatch):
    from pdfsigner.signing import SigningError

    def unsupported():
        raise SigningError("only supported on Windows and macOS")

    monkeypatch.setattr(certdialog.systemstore, "get_backend", unsupported)
    boxes = Boxes(monkeypatch, certdialog)
    dialog._choose_system_cert()
    assert boxes.kinds() == ["critical"] and "only supported" in boxes.text()


def test_main_smoke_test(qapp, form_pdf, cfg_path, monkeypatch):
    # the QMessageBox for a broken file would block; there is none for a good file
    assert gui_app.main(["--smoke-test", str(form_pdf)]) == 0
    assert gui_app.main(["--smoke-test"]) == 0
    assert gui_app.ICON.exists()
    assert not QtWidgets.QApplication.windowIcon().isNull()


def test_main_broken_config(qapp, cfg_path, monkeypatch):
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text('[certs.x]\npath = "a.p12"\nsecret = "foo"\n', encoding="utf-8")
    boxes = Boxes(monkeypatch, gui_app)
    assert gui_app.main(["--smoke-test"], config_path=cfg_path) == 1
    assert boxes.kinds() == ["critical"] and "unknown secret source" in boxes.text()


def test_dialog_choose_token_key(open_editor, monkeypatch):
    from pdfsigner import tokens

    dialog = open_editor("tok")
    dialog.e["key_label"].setText("old label")
    asked = []
    monkeypatch.setattr(certdialog, "ask_secret",
                        lambda parent, prompt: asked.append(prompt) or "123456")

    def list_tokens(module, token_label=None, ask_pin=None):
        assert module == "/x.so"
        ask_pin("YubiKey PIV #1")
        return [tokens.TokenInfo("/x.so", "YubiKey PIV #1", "Yubico", "YubiKey", "1", True, [
            tokens.TokenEntry(b"\x01", "Private key for PIV Authentication", "EC"),
            tokens.TokenEntry(b"\x02", "Private key for Digital Signature", "EC", True,
                              "X.509 Certificate for Digital Signature", "CN=Alice",
                              "2027-01-01"),
            tokens.TokenEntry(None, cert_label="CA", subject="CN=CA", not_after="2030-01-01"),
        ])]

    monkeypatch.setattr(certdialog.tokens, "list_tokens", list_tokens)
    boxes = Boxes(monkeypatch, certdialog)  # a failure must not block on a real dialog
    offered = []

    def get_item(parent, title, label, items, *a):
        offered.extend(items)
        return items[1], True

    monkeypatch.setattr(certdialog.QInputDialog, "getItem", get_item)
    dialog._choose_token_key()
    assert asked == ["PIN for token 'YubiKey PIV #1' (Cancel: certificates only)"]
    assert len(offered) == 2  # objects without an ID can't be selected
    assert "PIN for every signature" in offered[1]
    assert boxes.kinds() == []
    assert (dialog.e["token_label"].text(), dialog.e["key_id"].text(),
            dialog.e["key_label"].text()) == ("YubiKey PIV #1", "02", "")


def test_dialog_choose_token_key_errors(open_editor, monkeypatch):
    from pdfsigner.signing import SigningError

    boxes = Boxes(monkeypatch, certdialog)
    dialog = open_editor("tok")
    dialog.e["pkcs11_lib"].setText("")
    monkeypatch.setattr(certdialog.tokens, "find_modules", lambda: [])
    dialog._choose_token_key()
    assert boxes.kinds() == ["information"] and "No PKCS#11 module found" in boxes.text()

    dialog.e["pkcs11_lib"].setText("/x.so")

    def fail(*a, **k):
        raise SigningError("PKCS#11 (/x.so): cannot load")

    monkeypatch.setattr(certdialog.tokens, "list_tokens", fail)
    dialog._choose_token_key()
    assert boxes.kinds()[-1] == "critical" and "cannot load" in boxes.text()
    monkeypatch.setattr(certdialog.tokens, "list_tokens", lambda *a, **k: [])
    dialog._choose_token_key()
    assert boxes.kinds()[-1] == "information" and "No token found" in boxes.text()


# --- settings ---------------------------------------------------------------------------


def test_settings_trust_and_language(window, form_pdf, p12, tmp_path, monkeypatch):
    from conftest import sign_with, write_pem

    from pdfsigner.gui import settings as gui_settings

    window.config.path = tmp_path / "config.toml"
    signed = tmp_path / "s.pdf"
    sign_with(window.config.certs["alice"], form_pdf, signed, field_name="Applicant")
    window.open(signed)
    assert sig_rows(window)[0].startswith("Applicant: untrusted")

    pem = write_pem(p12[1], tmp_path / "alice.pem")
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate", encoding="utf-8")
    monkeypatch.setattr(gui_settings.QFileDialog, "getOpenFileNames",
                        staticmethod(lambda *a, **k: ([str(junk), str(pem), str(pem)], "")))
    boxes = Boxes(monkeypatch, gui_settings)  # the same QMessageBox class as gui_app's

    def edit(dlg):
        dlg._add_trust()
        assert boxes.kinds() == ["warning"] and "junk.pem" in boxes.text()
        assert dlg.trust_paths() == [str(pem)]  # junk refused, duplicate ignored
        assert "Alice Example" in dlg.trust_list.item(0).text()
        dlg.language_combo.setCurrentIndex(dlg.language_combo.findData("de"))
        dlg._save()
        return dlg.result()

    monkeypatch.setattr(gui_settings.SettingsDialog, "exec", edit)
    window.settings_dialog()
    assert sig_rows(window)[0].startswith("Applicant: valid")  # re-checked right away
    assert boxes.kinds() == ["warning", "information"] and "restart" in boxes.text()
    saved = load_config(tmp_path / "config.toml")
    assert (saved.trust, saved.language) == ([str(pem)], "de")

    def remove(dlg):
        dlg.trust_list.setCurrentRow(0)
        dlg._remove_trust()
        dlg._save()
        return dlg.result()

    monkeypatch.setattr(gui_settings.SettingsDialog, "exec", remove)
    window.settings_dialog()
    assert sig_rows(window)[0].startswith("Applicant: untrusted")
    assert load_config(tmp_path / "config.toml").trust == []


def test_settings_cancel_changes_nothing(window, tmp_path, monkeypatch):
    from pdfsigner.gui import settings as gui_settings

    window.config.path = tmp_path / "config.toml"

    def cancel(dlg):
        dlg.language_combo.setCurrentIndex(dlg.language_combo.findData("de"))
        dlg.reject()
        return dlg.result()

    monkeypatch.setattr(gui_settings.SettingsDialog, "exec", cancel)
    window.settings_dialog()
    assert window.config.language == ""
    assert not (tmp_path / "config.toml").exists()


def test_signature_followed_by_changes_is_marked(window, form_pdf, p12_bob, tmp_path):
    from conftest import sign_with

    out = tmp_path / "two.pdf"
    sign_with(window.config.certs["alice"], form_pdf, out, field_name="Applicant")
    bob = CertProfile(name="bob", path=str(p12_bob[0]), secret="none")
    sign_with(bob, out, out, field_name="Supervisor")
    window.open(out)
    first, second = sig_rows(window)
    assert first.endswith("then changed (allowed): see tooltip")
    assert "then changed" not in second


def test_huge_page_render_is_capped(window, tmp_path):
    """14400 pt (the PDF maximum) at 600 % would be ~7 gigapixels."""
    import io

    from pypdf import PdfWriter

    from pdfsigner.gui.pageview import MAX_PAGE_PIXELS

    w = PdfWriter()
    w.add_blank_page(14400, 14400)
    buf = io.BytesIO()
    w.write(buf)
    big = tmp_path / "big.pdf"
    big.write_bytes(buf.getvalue())
    window.open(big)
    window.view.set_zoom(6.0)
    pm = window.view._page_item.pixmap()
    assert pm.width() * pm.height() <= MAX_PAGE_PIXELS
    # geometry stays right: the item still covers the whole page in scene units
    rect = window.view._page_item.sceneBoundingRect()
    assert rect.width() == pytest.approx(14400, rel=0.01)


def test_dialog_choose_token_key_searches_modules(open_editor, monkeypatch):
    """Empty module field: every module found is asked, and the choice fills
    in the module too."""
    from pdfsigner import tokens
    from pdfsigner.signing import SigningError

    dialog = open_editor("tok")
    dialog.e["pkcs11_lib"].setText("")
    monkeypatch.setattr(certdialog.tokens, "find_modules",
                        lambda: ["/a/opensc.so", "/b/ykcs11.so"])
    monkeypatch.setattr(certdialog, "ask_secret", lambda parent, prompt: None)

    def list_tokens(module, token_label=None, ask_pin=None):
        if module == "/a/opensc.so":
            raise SigningError("no token found")
        return [tokens.TokenInfo(module, "YubiKey PIV #2", "Yubico", "YubiKey", "2", False, [
            tokens.TokenEntry(b"\x02", cert_label="X.509 Certificate for Digital Signature",
                              subject="CN=Bob", not_after="2027-01-01")])]

    monkeypatch.setattr(certdialog.tokens, "list_tokens", list_tokens)
    Boxes(monkeypatch, certdialog)
    monkeypatch.setattr(certdialog.QInputDialog, "getItem",
                        staticmethod(lambda parent, title, label, items, *a: (items[0], True)))
    dialog._choose_token_key()
    assert (dialog.e["pkcs11_lib"].text(), dialog.e["token_label"].text(),
            dialog.e["key_id"].text()) == ("/b/ykcs11.so", "YubiKey PIV #2", "02")


def test_signing_with_a_token_shows_a_hint(window, form_pdf, monkeypatch):
    from pdfsigner.signing import SigningError

    window.config.certs["card"] = CertProfile(name="card", type="pkcs11", pkcs11_lib="/x.so",
                                              secret="none")
    window._reload_certs("card")
    window.open(form_pdf)
    window.view.select_field("Applicant")
    seen = []

    def open_signer(profile, creds):
        seen.append(window.statusBar().currentMessage())
        raise SigningError("no token")

    monkeypatch.setattr(gui_app, "open_signer", open_signer)
    boxes = Boxes(monkeypatch, gui_app)
    window.sign()
    assert seen and "security key blinks" in seen[0]
    assert boxes.kinds() == ["critical"]


# --- window sizes ----------------------------------------------------------------------


# 1280×720 at 125 % scaling leaves 1024×576 logical pixels (minus a taskbar).
SMALL_SCREEN = (1024, 540)


@pytest.mark.parametrize("lang", ["en", "de"])
def test_windows_fit_small_screens(qapp, p12, lang, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialogButtonBox

    from pdfsigner import i18n
    from pdfsigner.gui.settings import SettingsDialog

    i18n.set_language(lang)
    # a long config path (as in CI's temp folders) must not widen the dialogs
    cfg = Config(path=tmp_path / ("x" * 120) / ("y" * 120) / "c.toml")
    # the tallest profile form: KeePassXC with all its rows
    cfg.put(CertProfile(name="w", path=str(p12[0]), secret="keepassxc", keepass_db="d.kdbx",
                        keepass_entry="e"))
    avail = qapp.primaryScreen().availableGeometry()
    windows = [gui_app.MainWindow(cfg), CertManagerDialog(cfg), CertEditDialog(cfg, "w"),
               SettingsDialog(cfg)]
    try:
        for w in windows:
            w.show()
            qapp.processEvents()
            name = type(w).__name__
            assert w.width() <= avail.width() and w.height() <= avail.height(), name
            need = w.minimumSizeHint()
            assert need.width() <= SMALL_SCREEN[0] and need.height() <= SMALL_SCREEN[1], (
                name, need.toTuple())
            box = w.findChild(QDialogButtonBox)
            if box is not None and box.isVisible():  # OK/Save/Cancel are reachable
                bottom = box.mapTo(w, box.rect().bottomRight())
                assert bottom.y() <= w.height() and bottom.x() <= w.width(), name
    finally:
        from shiboken6 import delete

        for w in windows:
            w.close()
            delete(w)


def test_dialog_module_field_offers_the_modules_found(open_editor, monkeypatch):
    """The module field lists the modules found; picking one fills the profile,
    and a path that isn't in the list can still be entered."""
    monkeypatch.setattr(certdialog.tokens, "find_modules",
                        lambda: ["/usr/lib/opensc-pkcs11.so", "/usr/lib/libykcs11.so"])
    d = open_editor("tok")
    combo = d.module_combo
    assert [combo.itemText(i) for i in range(combo.count())] == [
        "/usr/lib/opensc-pkcs11.so", "/usr/lib/libykcs11.so"]
    assert d.e["pkcs11_lib"].text() == "/x.so"  # the profile's, not the first found
    combo.setCurrentIndex(1)
    assert d._store_form().pkcs11_lib == "/usr/lib/libykcs11.so"
    d.e["pkcs11_lib"].setText("/opt/vendor/lib/libvendor-pkcs11.so")
    assert d._store_form().pkcs11_lib == "/opt/vendor/lib/libvendor-pkcs11.so"
    assert combo.count() == 2


# --- certificate details and stamp appearance -------------------------------------------


def test_new_profile_reads_its_certificate_once(open_editor, monkeypatch, p12, tmp_path):
    """Saving a new profile asks for the PIN/passphrase once and keeps the
    certificate, so its details and the stamp need no PIN later."""
    monkeypatch.setattr(CertEditDialog, "_read_certificate", READ_CERTIFICATE)
    asked = []
    monkeypatch.setattr(certdialog, "ask_secret",
                        lambda parent, prompt: asked.append(prompt) or PASSPHRASE)
    new = open_editor(None)
    assert not new.details_btn.isEnabled() and "Not read yet" in new.cert_label.text()
    new.name_edit.setText("card")
    new.e["path"].setText(str(p12[0]))
    new._save()
    assert new.result() == QtWidgets.QDialog.Accepted and len(asked) == 1
    saved = load_config(tmp_path / "config.toml").certs["card"]
    from pdfsigner import certinfo

    assert certinfo.signer_name(certinfo.load(saved.certificate)) == "Alice Example"
    assert "Alice Example, valid until" in new.cert_label.text()

    # editing other settings doesn't ask again; another key does
    again = open_editor("card")
    again.e["reason"].setText("Approval")
    again._save()
    assert len(asked) == 1
    again = open_editor("card")
    again.e["path"].setText(str(p12[0]) + "x")
    boxes = Boxes(monkeypatch, certdialog, answer=QtWidgets.QMessageBox.No)
    again._save()  # unreadable: the user goes back to the form
    assert boxes.kinds() == ["question"] and "could not be read" in boxes.text()
    assert again.result() != QtWidgets.QDialog.Accepted


def test_unreadable_certificate_can_be_saved_without(open_editor, monkeypatch, tmp_path):
    monkeypatch.setattr(CertEditDialog, "_read_certificate", READ_CERTIFICATE)
    monkeypatch.setattr(certdialog, "ask_secret", lambda parent, prompt: None)  # Cancel
    boxes = Boxes(monkeypatch, certdialog)  # "Save without?" Yes
    new = open_editor(None)
    new.name_edit.setText("later")
    new.e["path"].setText("missing.p12")
    new._save()
    assert boxes.kinds() == ["question"] and "cancelled" in boxes.text()
    assert new.result() == QtWidgets.QDialog.Accepted
    assert load_config(tmp_path / "config.toml").certs["later"].certificate == ""


def test_test_button_stores_the_certificate(dialog, monkeypatch, tmp_path):
    monkeypatch.setattr(certdialog, "ask_secret", lambda parent, prompt: PASSPHRASE)
    Boxes(monkeypatch, certdialog)
    dialog._test()
    assert dialog.details_btn.isEnabled()
    dialog._save()  # no second unlock: the test read it
    assert load_config(tmp_path / "config.toml").certs["work"].certificate


def test_certificate_details_dialog(manager, monkeypatch, p12):
    from cryptography.hazmat.primitives import serialization

    pem_text = p12[1].public_bytes(serialization.Encoding.PEM).decode()
    assert not manager.details_btn.isEnabled()  # nothing read yet
    manager.config.certs["work"].certificate = pem_text
    manager._refresh_list("work")
    assert manager.details_btn.isEnabled()
    assert rows(manager)[0][2] == f"{p12[1].not_valid_after_utc:%Y-%m-%d}"
    shown = []
    monkeypatch.setattr(manager, "_exec_details", shown.append, raising=False)
    manager._details()
    [dlg] = shown
    items = {dlg.tree.topLevelItem(i).text(0): dlg.tree.topLevelItem(i).text(1)
             for i in range(dlg.tree.topLevelItemCount())}
    assert "Alice Example" in items["Subject"] and items["Key"] == "EC secp256r1"
    assert items["Status"].startswith("valid, ")


def test_stamp_preview_follows_the_form(dialog, qapp):
    """The editor shows the stamp as signing draws it: image and text."""
    dialog._update_preview()
    first = dialog.preview.pixmap()
    assert first is not None and not first.isNull()
    dialog.stamp_text.setPlainText("")
    dialog._insert_field("cn")
    assert dialog.stamp_text.toPlainText() == "%(cn)s"
    dialog.position_combo.setCurrentIndex(dialog.position_combo.findData("right"))
    dialog._update_preview()
    beside = dialog.preview.pixmap()
    assert beside.width() / beside.height() > first.width() / first.height()
    assert dialog._store_form().text_position == "right"
    dialog.stamp_text.setPlainText("%(nope)s")
    dialog._update_preview()
    assert "unknown placeholder" in dialog.preview.text()


def test_signing_keeps_the_stored_certificate_current(window, form_pdf, save_to, p12):
    from pdfsigner import certinfo

    window.config.certs["alice"].certificate = ""
    window.open(form_pdf)
    window.view.select_field("Applicant")
    window.sign()
    stored = certinfo.load(window.config.certs["alice"].certificate)
    assert stored is not None and certinfo.signer_name(stored) == "Alice Example"
