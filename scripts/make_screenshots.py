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

from conftest import draw_signature_image, make_p12  # noqa: E402
from PySide6.QtCore import QMarginsF, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QFont, QPageLayout, QPageSize, QPainter, QPdfWriter, QPen  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit  # noqa: E402
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter  # noqa: E402
from pyhanko.sign import fields  # noqa: E402

from pdfsigner.config import CertProfile, Config  # noqa: E402
from pdfsigner.gui import app as gui_app  # noqa: E402
from pdfsigner.gui.certdialog import CertManagerDialog  # noqa: E402

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


def click(view, x, y):
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier,
                     view.mapFromScene(QPointF(x, y)))


def grab(widget, name):
    QTest.qWait(60)
    widget.grab().save(str(OUT / name))
    print("wrote", OUT / name)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Liberation Sans", 10))
    tmp = Path(tempfile.mkdtemp(prefix="pdfsigner-shots-"))
    # Let "~/certs/..." in the profiles resolve to the throwaway files.
    os.environ["HOME"] = str(tmp)
    (tmp / "certs").mkdir()

    alice_p12, _ = make_p12("Alice Example")
    bob_p12, _ = make_p12("Bob Supervisor")
    (tmp / "certs/fau-smime.p12").write_bytes(alice_p12)
    (tmp / "certs/supervisor.p12").write_bytes(bob_p12)
    draw_signature_image(tmp / "certs/signature.png")
    draw_signature_image(tmp / "certs/supervisor.png", seed=7)
    doc = sample_pdf(tmp / "expense-claim.pdf")

    alice = CertProfile(name="FAU S/MIME", path="~/certs/fau-smime.p12", image="~/certs/signature.png",
                        reason="Expense claim", location="Erlangen", secret="keyring")
    vault = CertProfile(name="Vault (KeePass)", secret="keepass", keepass_db="~/Passwords.kdbx",
                        keepass_entry="Certificates/FAU S/MIME", keepass_attachment="fau-smime.p12",
                        image="~/certs/signature.png", stamp_text="")
    token = CertProfile(name="YubiKey PIV", type="pkcs11",
                        pkcs11_lib="/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so",
                        token_label="PIV_II", key_label="SIGN key", secret="prompt")
    run_alice = CertProfile(name="FAU S/MIME", path="~/certs/fau-smime.p12",
                            image="~/certs/signature.png", reason="Expense claim",
                            location="Erlangen", secret="prompt")
    run_bob = CertProfile(name="Supervisor", path="~/certs/supervisor.p12",
                          image="~/certs/supervisor.png",
                          stamp_text="Approved by %(signer)s\n%(ts)s", secret="prompt")
    cfg = Config(certs={"FAU S/MIME": run_alice, "Supervisor": run_bob}, default="FAU S/MIME",
                 path=tmp / "config.toml")

    gui_app.ask_secret = lambda parent, prompt: "secret"
    out = tmp / "expense-claim-signed.pdf"
    gui_app.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(out), ""))

    win = gui_app.MainWindow(cfg)
    win.resize(1180, 1010)
    win.show()
    QTest.qWaitForWindowExposed(win)
    win.open(doc)
    view = win.view
    grab(win, "gui-overview.png")

    # free placement next to the closing sentence
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
                      default="FAU S/MIME", path=Path("~/.config/pdfsigner/config.toml"))
    for name, shot in (("FAU S/MIME", "gui-certs-p12.png"),
                       ("Vault (KeePass)", "gui-certs-keepass.png"),
                       ("YubiKey PIV", "gui-certs-pkcs11.png")):
        d = CertManagerDialog(docs_cfg, win)
        d._refresh_list(name)
        d.show()
        QTest.qWaitForWindowExposed(d)
        grab(d, shot)
        d.close()


if __name__ == "__main__":
    main()
