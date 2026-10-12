"""Smoke-test a frozen build: sign, verify, edit and recognize text with the
bundled CLI, start the GUI.

    python packaging/smoke_test.py "dist/Slim PDF/slim-pdf" "dist/Slim PDF/Slim PDF"

Runs with the build environment's Python (needs cryptography and pypdfium2
to create the test files); the binaries under test must not need anything.
"""

from __future__ import annotations

import datetime
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pypdfium2 as pdfium
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID


def make_files(d: Path) -> tuple[Path, Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Smoke Test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    p12 = d / "smoke.p12"
    p12.write_bytes(pkcs12.serialize_key_and_certificates(
        b"smoke", key, cert, None, serialization.NoEncryption()))
    pem = d / "smoke.pem"
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    doc = pdfium.PdfDocument.new()
    for _ in range(3):
        doc.new_page(595, 842)
    buf = io.BytesIO()
    doc.save(buf)
    pdf = d / "doc.pdf"
    pdf.write_bytes(buf.getvalue())
    return p12, pem, pdf


def make_scan(d: Path) -> Path:
    """A page that is only a picture of a line of text, like a scan."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("L", (1654, 2339), 255)  # A4 at 200 dpi
    ImageDraw.Draw(image).text((150, 200), "Slim PDF smoke test", fill=0,
                               font=ImageFont.load_default(size=80))
    scan = d / "scan.pdf"
    image.save(scan, format="PDF", resolution=200)
    return scan


def main() -> int:
    cli, gui = Path(sys.argv[1]), Path(sys.argv[2])
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        p12, pem, pdf = make_files(d)
        env = dict(os.environ, SLIM_PDF_CONFIG=str(d / "config.toml"),
                   QT_QPA_PLATFORM="offscreen")

        def run(*args, expect=0):
            nonlocal failures
            r = subprocess.run([str(cli), *map(str, args)], env=env, capture_output=True,
                               text=True, timeout=300)
            ok = r.returncode == expect
            failures += not ok
            print(f"[{'ok' if ok else 'FAIL'}] slim-pdf {' '.join(map(str, args))} "
                  f"-> {r.returncode}")
            if not ok:
                print(r.stdout, r.stderr, sep="\n")
            return r

        run("--help")
        run("certs", "add", "smoke", "--p12", p12, "--secret", "none", "--reason", "Smoke")
        run("certs", "test", "smoke")
        run("fields", pdf)
        signed = d / "signed.pdf"
        run("sign", pdf, "-o", signed, "--page", "1", "--box", "50,50,250,110")
        r = run("verify", signed, "--trust", pem)
        if "Smoke Test" not in r.stdout:
            failures += 1
            print("[FAIL] signer missing from verify output")
        run("rotate", signed, "-i", "--angle", "90", expect=3)  # refuses: signed
        run("rotate", pdf, "-i", "--angle", "90", "--pages", "2")
        run("crop", pdf, "-i", "--margins", "20")
        run("split", pdf, "--every", "2", "-o", d / "part{n}.pdf")
        run("merge", "-o", d / "merged.pdf", d / "part2.pdf", f"{d / 'part1.pdf'}@1")
        run("form", pdf)  # no fields: lists none
        # text recognition: the system's engine must be in the bundle (Linux
        # builds only check it when Tesseract is installed)
        engines = run("ocr", "--list-engines").stdout
        if sys.platform in ("win32", "darwin") or "*" in engines:
            recognized = d / "recognized.pdf"
            run("ocr", make_scan(d), "--lang", "en", "-o", recognized)
            text = (pdfium.PdfDocument(str(recognized))[0].get_textpage().get_text_range()
                    if recognized.exists() else "")
            if "smoke" not in text.lower():
                failures += 1
                print(f"[FAIL] recognized text: {text!r}\n{engines}")
        if sys.platform in ("win32", "darwin"):
            run("certs", "system")

        r = subprocess.run([str(gui), "--smoke-test", str(signed)], env=env,
                           capture_output=True, text=True, timeout=120)
        ok = r.returncode == 0
        failures += not ok
        print(f"[{'ok' if ok else 'FAIL'}] GUI --smoke-test -> {r.returncode}")
        if not ok:
            print(r.stdout, r.stderr, sep="\n")

    print("smoke test", "FAILED" if failures else "passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
