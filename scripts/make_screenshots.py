"""Regenerate the GUI screenshots in docs/images/.

    QT_QPA_PLATFORM=offscreen python scripts/make_screenshots.py

Builds a sample document with two prepared signature fields, throwaway
certificates and a signature image in a temp dir, then drives the GUI.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from conftest import add_form_fields, draw_signature_image, make_p12, write_pem  # noqa: E402
from PySide6.QtCore import QMarginsF, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QFont, QPageLayout, QPageSize, QPainter, QPdfWriter, QPen  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit  # noqa: E402
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter  # noqa: E402
from pyhanko.sign import fields  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

from slim_pdf.config import CertProfile, Config  # noqa: E402
from slim_pdf.gui import app as gui_app  # noqa: E402
from slim_pdf.gui.certdialog import CertEditDialog  # noqa: E402
from slim_pdf.gui.settings import SettingsDialog  # noqa: E402

OUT = ROOT / "docs" / "images"
A4_H = 842

# Signature block in display coordinates (points, origin top-left).
APPLICANT = QRectF(60, 560, 200, 66)
SUPERVISOR = QRectF(335, 560, 200, 66)


def sample_pdf(path: Path) -> Path:
    """Two-page A4 'expense claim' with signature lines on page 1."""
    w = QPdfWriter(str(path))
    w.setResolution(72)
    w.setPageLayout(QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait,
                                QMarginsF(0, 0, 0, 0)))
    p = QPainter(w)

    def text(x, y, s, size=11, bold=False):
        f = QFont("Liberation Sans")
        f.setPointSizeF(size)
        f.setBold(bold)
        p.setFont(f)
        p.drawText(QPointF(x, y), s)

    text(60, 90, "Travel Expense Claim", 20, True)
    text(60, 115, "Department of Computer Science · Reference TR-2026-0417", 10)
    rows = [("Traveller", "Alice Example"), ("Purpose", "Workshop on Applied Cryptography"),
            ("Destination", "Berlin"), ("Dates", "2026-09-14 – 2026-09-17")]
    y = 160
    for k, v in rows:
        text(60, y, k, 11, True)
        text(180, y, v)
        y += 22
    y += 15
    text(60, y, "Item", 11, True); text(400, y, "Amount (EUR)", 11, True)
    p.setPen(QPen(Qt.black, 0.7))
    p.drawLine(QPointF(60, y + 6), QPointF(535, y + 6))
    for item, amount in [("Train tickets", "189.40"), ("Hotel, 3 nights", "387.00"),
                         ("Conference fee", "250.00"), ("Per diem", "84.00")]:
        y += 22
        text(60, y, item); text(400, y, amount)
    y += 10
    p.drawLine(QPointF(60, y), QPointF(535, y))
    text(60, y + 20, "Total", 11, True); text(400, y + 20, "910.40", 11, True)
    text(60, 500, "I confirm that the information above is correct and complete.", 10)

    for rect, label in ((APPLICANT, "Applicant"), (SUPERVISOR, "Supervisor")):
        p.drawLine(QPointF(rect.left(), rect.bottom() + 4), QPointF(rect.right(), rect.bottom() + 4))
        text(rect.left(), rect.bottom() + 18, label, 9)

    w.newPage()
    text(60, 90, "Attachments", 16, True)
    text(60, 120, "Receipts are attached as scanned copies.", 11)
    p.end()

    # prepared (empty) signature fields on page 1
    wr = IncrementalPdfFileWriter(io.BytesIO(path.read_bytes()))
    for rect, name in ((APPLICANT, "Applicant"), (SUPERVISOR, "Supervisor")):
        box = (rect.left(), A4_H - rect.bottom(), rect.right(), A4_H - rect.top())
        fields.append_signature_field(
            wr, fields.SigFieldSpec(name, on_page=0, box=tuple(round(v) for v in box)))
    buf = io.BytesIO()
    wr.write(buf)
    path.write_bytes(buf.getvalue())
    return path


def registration_pdf(path: Path) -> Path:
    """A one-page form with the fields of the tests' add_form_fields(),
    labelled to their right."""
    w = QPdfWriter(str(path))
    w.setResolution(72)
    w.setPageLayout(QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait,
                                QMarginsF(0, 0, 0, 0)))
    p = QPainter(w)
    f = QFont("Liberation Sans")
    f.setPointSizeF(16)
    f.setBold(True)
    p.setFont(f)
    p.drawText(QPointF(50, 50), "Workshop registration")
    f.setPointSizeF(10)
    f.setBold(False)
    p.setFont(f)
    # (label, PDF y of the field's top): the fields are at x 50..300
    for label, top, x in (("Name", 780, 310), ("Address", 750, 310),
                          ("I take part in the workshop dinner", 664, 72),
                          ("S", 634, 66), ("L", 634, 96), ("T-shirt size", 634, 112),
                          ("Country", 600, 210),
                          ("Workshop", 570, 210), ("Reference (filled in by us)", 480, 210),
                          ("First name for the badge", 440, 210)):
        p.drawText(QPointF(x, A4_H - top + 11), label)
    p.end()
    return add_form_fields(path, path.with_name("registration.pdf"))


def scanned(src: Path, path: Path) -> Path:
    """``src`` as a scanner would deliver it: pictures of the pages."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(src))
    images = [pdf[i].render(scale=150 / 72, grayscale=True).to_pil() for i in range(len(pdf))]
    images[0].save(path, format="PDF", resolution=150, save_all=True, append_images=images[1:])
    return path


def click(view, x, y):
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier,
                     view.mapFromScene(QPointF(x, y)))


def grab(widget, name):
    if hasattr(widget, "thumbs"):
        widget.thumbs.finish_thumbnails()
    QTest.qWait(60)
    widget.grab().save(str(OUT / name))
    print("wrote", OUT / name)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Liberation Sans", 10))
    tmp = Path(tempfile.mkdtemp(prefix="slim-pdf-shots-"))
    # Let "~/certs/..." in the profiles resolve to the throwaway files.
    os.environ["HOME"] = str(tmp)
    (tmp / "certs").mkdir()

    alice_p12, alice_cert = make_p12("Alice Example")
    bob_p12, _ = make_p12("Bob Supervisor")
    (tmp / "certs/fau-smime.p12").write_bytes(alice_p12)
    (tmp / "certs/supervisor.p12").write_bytes(bob_p12)
    draw_signature_image(tmp / "certs/signature.png")
    draw_signature_image(tmp / "certs/supervisor.png", seed=7)
    doc = sample_pdf(tmp / "expense-claim.pdf")

    # the certificate as the GUI stores it once read: real values in the previews
    alice_pem = alice_cert.public_bytes(serialization.Encoding.PEM).decode()
    alice = CertProfile(name="FAU S/MIME", path="~/certs/fau-smime.p12", image="~/certs/signature.png",
                        reason="Expense claim", location="Erlangen", secret="keyring",
                        certificate=alice_pem)
    vault = CertProfile(name="Vault (KeePass)", secret="keepass", keepass_db="~/Passwords.kdbx",
                        keepass_entry="Certificates/FAU S/MIME", keepass_attachment="fau-smime.p12",
                        image="~/certs/signature.png", stamp_text="", certificate=alice_pem)
    token = CertProfile(name="YubiKey PIV", type="pkcs11",
                        pkcs11_lib="/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so",
                        token_label="PIV_II", key_label="SIGN key", secret="prompt",
                        image="~/certs/signature.png", text_position="right",
                        stamp_text="%(cn)s\n%(date)s %(time)s\nReason: %(reason)s",
                        reason="Approval", certificate=alice_pem)
    run_alice = CertProfile(name="FAU S/MIME", path="~/certs/fau-smime.p12",
                            image="~/certs/signature.png", reason="Expense claim",
                            location="Erlangen", secret="prompt", certificate=alice_pem)
    run_bob = CertProfile(name="Supervisor", path="~/certs/supervisor.p12",
                          image="~/certs/supervisor.png",
                          stamp_text="Approved by %(signer)s\n%(ts)s", secret="prompt")
    # Trust only Alice, so the Signatures list shows a valid and an untrusted one.
    cfg = Config(certs={"FAU S/MIME": run_alice, "Supervisor": run_bob}, default="FAU S/MIME",
                 trust=[str(write_pem(alice_cert, tmp / "certs/alice.pem"))],
                 path=tmp / "config.toml")

    gui_app.ask_secret = lambda parent, prompt: "secret"
    out = tmp / "expense-claim-signed.pdf"
    gui_app.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(out), ""))

    win = gui_app.MainWindow(cfg)
    win.resize(1320, 1010)
    win.show()
    QTest.qWaitForWindowExposed(win)
    win.open(doc)
    view = win.view
    win.show_tab(gui_app.SIGN_TAB)  # a document with signature fields: signing
    grab(win, "gui-overview.png")

    # free placement next to the closing sentence: Place signature…, then a click
    win.sign()
    click(view, 420, 470)
    view.placement.scale_by(1.2)
    grab(win, "gui-place.png")

    # prepared field selected
    view.select_field("Applicant")
    grab(win, "gui-field.png")
    win.sign()

    # second signer
    win.cert_combo.setCurrentIndex(win.cert_combo.findData("Supervisor"))
    view.select_field("Supervisor")
    win.sign()
    grab(win, "gui-signed.png")

    # page tools: a scanned attachment page that needs rotating and cropping
    win.open(doc)
    win.ribbon.setCurrentIndex(2)  # the Pages tab
    win.view.go_to(1)
    win.thumbs.show_page(1)
    # the page menu on the second thumbnail; a popup is its own window, so it
    # is painted onto the window's picture where it would appear
    rect = win.thumbs.visualItemRect(win.thumbs.item(1))
    at = win.thumbs.viewport().mapTo(win, rect.center())
    menu = win.page_menu()
    menu.popup(win.mapToGlobal(at))
    QTest.qWait(60)
    shot = win.grab()
    painter = QPainter(shot)
    painter.drawPixmap(at, menu.grab())
    painter.end()
    menu.close()
    shot.save(str(OUT / "gui-page-menu.png"))
    print("wrote", OUT / "gui-page-menu.png")
    win.crop_act.setChecked(True)
    win._toggle_crop()
    vp = view.viewport()
    a, b = view.mapFromScene(QPointF(40, 60)), view.mapFromScene(QPointF(555, 300))
    QTest.mousePress(vp, Qt.LeftButton, Qt.NoModifier, a)
    QTest.mouseMove(vp, b)  # leave the rectangle visible
    grab(win, "gui-pages.png")
    win._escape()
    win.dirty = False

    # text recognition on a scan of the claim: the progress, over the window
    scan = scanned(doc, tmp / "expense-claim-scan.pdf")
    win.open(scan)
    win.ribbon.setCurrentIndex(2)
    progress = win._ocr_progress(2)
    win._ocr_step(progress, 0, 2)
    QTest.qWait(60)
    shot = win.grab()
    at = win.rect().center() - progress.rect().center()
    painter = QPainter(shot)
    painter.drawPixmap(at, progress.grab())
    painter.end()
    progress.close()
    shot.save(str(OUT / "gui-ocr.png"))
    print("wrote", OUT / "gui-ocr.png")
    win.show_tab(gui_app.SIGN_TAB)

    # filling in a form: the form panel (Edit tab), cursor in a field
    from slim_pdf import forms

    form = registration_pdf(tmp / "registration-blank.pdf")
    win.open(form)
    win.apply_edit(forms.fill(win.data, {"name": "Alice Example", "agree": True,
                                         "country": "de", "colour": "green"}),
                   "Filled in 4 fields")
    win.show_form_field("address")
    # typing right on the page, into the badge name field
    win.view.highlight_form_field("person.first")
    win.view.edit_text("person.first", "")
    win.view.inline_editor.setText("Alice")
    QTest.qWait(60)
    shot = win.grab()
    win.view.finish_text_edit(False)
    shot.save(str(OUT / "gui-form.png"))
    print("wrote", OUT / "gui-form.png")

    # adding text (done: dragged at the top right) and an image (being dragged)
    win.statusBar().clearMessage()
    win._insert = ("text", "Registered 2026-10-11\nAlice Example", 14.0, (0.0, 0.2, 0.65))
    win.view.set_insert_mode(True)
    vp = view.viewport()
    QTest.mousePress(vp, Qt.LeftButton, Qt.NoModifier, view.mapFromScene(QPointF(380, 470)))
    QTest.mouseMove(vp, view.mapFromScene(QPointF(560, 520)))
    QTest.mouseRelease(vp, Qt.LeftButton, Qt.NoModifier, view.mapFromScene(QPointF(560, 520)))
    win._insert = ("image", str(tmp / "certs/signature.png"), 3.0)
    win.view.set_insert_mode(True)
    win.statusBar().showMessage(win.tr("Drag a box for the image on the page, or click where "
                                       "its centre should be. Esc cancels."))
    QTest.mousePress(vp, Qt.LeftButton, Qt.NoModifier, view.mapFromScene(QPointF(380, 540)))
    QTest.mouseMove(vp, view.mapFromScene(QPointF(560, 600)))
    grab(win, "gui-insert.png")
    QTest.mouseRelease(vp, Qt.LeftButton, Qt.NoModifier, view.mapFromScene(QPointF(560, 600)))
    win.dirty = False
    win.show_tab(gui_app.SIGN_TAB)

    # password prompt (shown non-modally just for the picture)
    dlg = QInputDialog(win)
    dlg.setWindowTitle("Password")
    dlg.setLabelText("passphrase for certificate 'FAU S/MIME':")
    dlg.setTextEchoMode(QLineEdit.Password)
    dlg.setTextValue("secret")
    dlg.show()
    grab(dlg, "gui-password.png")
    dlg.close()

    docs_cfg = Config(certs={"FAU S/MIME": alice, "Vault (KeePass)": vault, "YubiKey PIV": token},
                      default="FAU S/MIME", path=Path("~/.config/slim-pdf/config.toml"))
    d = SettingsDialog(docs_cfg, win, SettingsDialog.CERTIFICATES)
    d.show()
    QTest.qWaitForWindowExposed(d)
    grab(d, "gui-certs.png")
    d.close()
    for name, shot in (("FAU S/MIME", "gui-certs-p12.png"),
                       ("Vault (KeePass)", "gui-certs-keepass.png"),
                       ("YubiKey PIV", "gui-certs-pkcs11.png")):
        d = CertEditDialog(docs_cfg, name, win)
        d.show()
        QTest.qWaitForWindowExposed(d)
        grab(d, shot)
        d.close()


if __name__ == "__main__":
    main()
