"""GUI tests; run headless (conftest sets QT_QPA_PLATFORM=offscreen)."""

import os

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from pyhanko.sign import fields  # noqa: E402

from pdfsigner.config import CertProfile, Config, load_config  # noqa: E402
from pdfsigner.gui import app as gui_app  # noqa: E402
from pdfsigner.gui import certdialog  # noqa: E402
from pdfsigner.gui.certdialog import CertManagerDialog  # noqa: E402
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
def window(qapp, profile, p12_bob, secrets, save_to):
    bob = CertProfile(name="bob", path=str(p12_bob[0]), secret="prompt")  # no image
    profile.secret = "prompt"
    cfg = Config(certs={"alice": profile, "bob": bob}, default="alice")
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
    assert not window.sign_act.isEnabled()

    click_scene(view, 200, 300)
    item = view.placement
    assert isinstance(item, SignatureItem)
    w, h = item.size
    assert w / h == pytest.approx(3.0)
    assert window.sign_act.isEnabled() and window.sign_btn.isEnabled()
    assert "New signature on page 1" in window.target_label.text()

    # drag the resize handle outwards: aspect ratio stays 3:1
    r = item.scene_rect()
    handle = (r.right() - 2 / view.zoom, r.bottom() - 2 / view.zoom)
    drag_scene(view, handle, (handle[0] + 90, handle[1] + 5))
    w2, h2 = item.size
    assert w2 > w + 60
    assert w2 / h2 == pytest.approx(3.0)

    page, box = view.placement_box()
    assert page == 0
    r2 = item.scene_rect()
    assert r2.topLeft() == r.topLeft()  # resizing keeps the top-left corner
    assert box[0] == pytest.approx(r2.left(), abs=0.1)
    assert box[3] == pytest.approx(842 - r2.top(), abs=0.1)  # display y from the top
    assert (box[2] - box[0]) / (box[3] - box[1]) == pytest.approx(3.0, rel=0.01)

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
    click_scene(view, 200, 300)
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
    click_scene(view, 590, 835)  # near the bottom-right corner
    r = view.placement.scene_rect()
    assert r.right() <= 595 + 0.01 and r.bottom() <= 842 + 0.01
    view.placement.scale_by(10)
    r = view.placement.scene_rect()
    assert r.right() <= 595 + 0.01 and r.bottom() <= 842 + 0.01
    w, h = view.placement.size
    assert w / h == pytest.approx(3.0)
    view.placement.scale_by(0.01)
    assert view.placement.size[0] == pytest.approx(30)  # minimum width


def test_click_again_moves_existing_placement(window, blank_pdf):
    window.open(blank_pdf)
    view = window.view
    click_scene(view, 200, 300)
    first = view.placement
    click_scene(view, 400, 600)
    assert view.placement is first
    assert view.placement.scene_rect().center().x() == pytest.approx(400, abs=1)


def test_placement_survives_page_navigation(window, blank_pdf):
    window.open(blank_pdf)
    view = window.view
    click_scene(view, 200, 300)
    item = view.placement
    view.go_to(1)
    assert item.scene() is None  # not shown on page 2
    assert view.placement_box()[0] == 0
    assert window.sign_act.isEnabled()
    view.go_to(0)
    assert item.scene() is view.scene()
    # clicking on page 2 moves the placement there
    view.go_to(1)
    click_scene(view, 300, 300)
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
    click_scene(window.view, 200, 300)
    assert window.sign_act.isEnabled()
    window.view.clear_target()
    assert window.view.placement is None
    assert not window.sign_act.isEnabled()
    assert "Click on the page" in window.target_label.text()


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
    click_scene(window.view, 200, 300)
    item = window.view.placement
    assert item._pixmap is not None
    window.cert_combo.setCurrentIndex(window.cert_combo.findData("bob"))  # no image
    assert item._pixmap is None
    window.cert_combo.setCurrentIndex(window.cert_combo.findData("alice"))
    window.image_check.setChecked(False)
    assert item._pixmap is None


def test_image_and_text_switches_are_passed(window, blank_pdf, save_to, monkeypatch):
    seen = {}
    real = gui_app.sign_pdf

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)

    monkeypatch.setattr(gui_app, "sign_pdf", spy)
    window.open(blank_pdf)
    click_scene(window.view, 200, 300)
    window.text_check.setChecked(False)
    window.sign()
    assert seen["with_text"] is False and seen["with_image"] is True


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
    click_scene(window.view, c.x(), c.y())
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
    assert not window.sign_act.isEnabled()


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
    click_scene(window.view, 200, 300)
    window.sign()
    assert boxes.kinds() == ["critical"] and "cannot be signed again" in boxes.text()
    assert not save_to["path"].exists()


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
def dialog(qapp, tmp_path, p12, sig_image):
    cfg = Config(path=tmp_path / "config.toml")
    cfg.put(CertProfile(name="work", path=str(p12[0]), image=str(sig_image)))
    cfg.put(CertProfile(name="tok", type="pkcs11", pkcs11_lib="/x.so", secret="keyring"))
    d = CertManagerDialog(cfg)
    d.show()
    yield d
    d.close()


def select(d, name):
    for i in range(d.list.count()):
        if d.list.item(i).data(Qt.UserRole) == name:
            d.list.setCurrentRow(i)
            return
    raise AssertionError(name)


def test_dialog_loads_profiles(dialog):
    assert [dialog.list.item(i).text() for i in range(dialog.list.count())] == [
        "work  (default)", "tok"]
    assert dialog._current == "work"
    assert dialog.p12_box.isVisible() and not dialog.p11_box.isVisible()
    assert dialog.preview.pixmap() is not None and not dialog.preview.pixmap().isNull()
    select(dialog, "tok")
    assert dialog.p11_box.isVisible() and not dialog.p12_box.isVisible()
    assert dialog.keyring_btn.isVisible()
    assert dialog.e["pkcs11_lib"].text() == "/x.so"


def test_dialog_edit_add_default_save(dialog, monkeypatch, tmp_path):
    dialog.e["reason"].setText("Approval")
    select(dialog, "tok")  # edits are kept when switching
    select(dialog, "work")
    assert dialog.e["reason"].text() == "Approval"

    monkeypatch.setattr(certdialog.QInputDialog, "getText", lambda *a, **k: ("vault", True))
    dialog._add()
    assert dialog._current == "vault"
    dialog.secret_combo.setCurrentIndex(dialog.secret_combo.findData("keepass"))
    assert dialog._secret_form.isRowVisible(dialog.e["keepass_entry"])
    dialog.e["keepass_db"].setText("~/Passwords.kdbx")
    dialog.e["keepass_entry"].setText("Certificates/FAU S/MIME")
    dialog.e["keepass_attachment"].setText("fau.p12")
    dialog.stamp_text.setPlainText("")
    dialog._make_default()
    dialog._save()
    assert dialog.result() == QtWidgets.QDialog.Accepted

    cfg = load_config(tmp_path / "config.toml")
    assert cfg.default == "vault"
    assert cfg.certs["work"].reason == "Approval"
    v = cfg.certs["vault"]
    assert (v.secret, v.keepass_entry, v.keepass_attachment, v.stamp_text) == (
        "keepass", "Certificates/FAU S/MIME", "fau.p12", "")


def test_dialog_rejects_invalid(dialog, monkeypatch, tmp_path):
    boxes = Boxes(monkeypatch, certdialog)
    dialog.e["path"].setText("")
    dialog._save()
    assert boxes.kinds() == ["warning"] and "needs a path" in boxes.text()
    assert not (tmp_path / "config.toml").exists()


def test_dialog_add_duplicate_and_remove(dialog, monkeypatch):
    boxes = Boxes(monkeypatch, certdialog)
    monkeypatch.setattr(certdialog.QInputDialog, "getText", lambda *a, **k: ("work", True))
    dialog._add()
    assert boxes.kinds() == ["warning"]
    select(dialog, "tok")
    dialog._remove()  # question answered Yes
    assert list(dialog.config.certs) == ["work"]
    assert dialog.list.count() == 1


def test_dialog_test_button(dialog, monkeypatch):
    monkeypatch.setattr(certdialog, "ask_secret", lambda parent, prompt: PASSPHRASE)
    boxes = Boxes(monkeypatch, certdialog)
    dialog._test()
    assert boxes.kinds() == ["information"] and "Alice Example" in boxes.text()
    monkeypatch.setattr(certdialog, "ask_secret", lambda parent, prompt: "wrong")
    dialog._test()
    assert boxes.kinds()[-1] == "critical"


def test_dialog_store_keyring(dialog, monkeypatch, mem_keyring):
    monkeypatch.setattr(certdialog, "ask_secret", lambda parent, prompt: "1234")
    Boxes(monkeypatch, certdialog)
    select(dialog, "tok")
    dialog._store_keyring()
    assert mem_keyring.store[("pdfsigner", "tok")] == "1234"


def test_manage_updates_main_window(window, monkeypatch, tmp_path):
    def fake_exec(self):
        self.config.certs["new"] = CertProfile(name="new", path="x.p12")
        return True

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
    click_scene(view, 200, 300)
    w0 = view.placement.size[0]
    wheel(view, (200, 300), 120)  # over the signature: resize
    w1, h1 = view.placement.size
    assert w1 == pytest.approx(w0 * 1.1) and w1 / h1 == pytest.approx(3.0)
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


def test_dialog_ltv_checkbox(dialog):
    assert not dialog.ltv_check.isChecked()
    dialog.ltv_check.setChecked(True)
    select(dialog, "tok")
    assert not dialog.ltv_check.isChecked()
    select(dialog, "work")
    assert dialog.ltv_check.isChecked()
    dialog._save()
    assert load_config(dialog.config.path).certs["work"].ltv is True
    assert load_config(dialog.config.path).certs["tok"].ltv is False


# --- vector signature images ------------------------------------------------------


def test_pdf_signature_image_preview(window, form_pdf, tmp_path):
    from conftest import make_vector_signature

    window.config.certs["alice"].image = str(make_vector_signature(tmp_path / "v.pdf", 200, 50))
    window._cert_changed()
    window.open(form_pdf)
    click_scene(window.view, 200, 300)
    w, h = window.view.placement.size
    assert w / h == pytest.approx(4.0)
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


def test_dialog_system_store(dialog, monkeypatch):
    from test_systemstore import FakeBackend
    from cryptography.hazmat.primitives.asymmetric import ec
    from pki import MiniPKI
    from test_systemstore import issue

    ca = MiniPKI(port=1)
    key = ec.generate_private_key(ec.SECP256R1())
    backend = FakeBackend([(issue(ca, "Store Alice", key), key)])
    monkeypatch.setattr(certdialog.systemstore, "get_backend", lambda: backend)

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


def test_dialog_system_store_unavailable(dialog, monkeypatch):
    from pdfsigner.signing import SigningError

    def unsupported():
        raise SigningError("only supported on Windows and macOS")

    monkeypatch.setattr(certdialog.systemstore, "get_backend", unsupported)
    boxes = Boxes(monkeypatch, certdialog)
    dialog._choose_system_cert()
    assert boxes.kinds() == ["critical"] and "only supported" in boxes.text()
