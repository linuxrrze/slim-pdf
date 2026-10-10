"""PKCS#11 against a real software token (SoftHSM 2).

Skipped unless SoftHSM is installed. Set ``SOFTHSM2_LIB`` / ``SOFTHSM2_UTIL``
if it lives somewhere unusual.
"""

import datetime
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pkcs11 = pytest.importorskip("pkcs11")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from pdfsigner import cli  # noqa: E402
from pdfsigner.config import CertProfile, Config  # noqa: E402
from pdfsigner.secretstore import Credentials  # noqa: E402
from pdfsigner.signing import SigningError, inspect_pdf, open_signer, sign_pdf  # noqa: E402

from conftest import validate_all  # noqa: E402

LIB_CANDIDATES = [
    "/usr/lib/softhsm/libsofthsm2.so",
    "/usr/lib/x86_64-linux-gnu/softhsm/libsofthsm2.so",
    "/usr/lib/aarch64-linux-gnu/softhsm/libsofthsm2.so",
    "/usr/local/lib/softhsm/libsofthsm2.so",
    "/opt/homebrew/lib/softhsm/libsofthsm2.so",
]
PIN = "123456"
TOKEN = "pdfsigner-test"


def _find_lib() -> str | None:
    env = os.environ.get("SOFTHSM2_LIB")
    if env:
        return env
    return next((p for p in LIB_CANDIDATES if Path(p).exists()), None)


@pytest.fixture(scope="module")
def token(tmp_path_factory):
    lib = _find_lib()
    util = os.environ.get("SOFTHSM2_UTIL") or shutil.which("softhsm2-util")
    if not lib or not util:
        pytest.skip("SoftHSM 2 not installed")

    d = tmp_path_factory.mktemp("softhsm")
    (d / "tokens").mkdir()
    conf = d / "softhsm2.conf"
    conf.write_text(f"directories.tokendir = {d / 'tokens'}\nobjectstore.backend = file\n")
    old_conf = os.environ.get("SOFTHSM2_CONF")
    os.environ["SOFTHSM2_CONF"] = str(conf)
    subprocess.run([util, "--module", lib, "--init-token", "--free", "--label", TOKEN,
                    "--so-pin", "12345678", "--pin", PIN], check=True, capture_output=True)

    # RSA key and self-signed certificate, imported as token objects
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Token Signer")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    pem = d / "key.pem"
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    subprocess.run([util, "--module", lib, "--import", str(pem), "--token", TOKEN, "--label", "sign-key",
                    "--id", "01", "--pin", PIN], check=True, capture_output=True)
    pem.unlink()

    from pkcs11 import Attribute, CertificateType, ObjectClass

    lib_obj = pkcs11.lib(lib)
    with lib_obj.get_token(token_label=TOKEN).open(rw=True, user_pin=PIN) as s:
        s.create_object({
            Attribute.CLASS: ObjectClass.CERTIFICATE,
            Attribute.CERTIFICATE_TYPE: CertificateType.X_509,
            Attribute.TOKEN: True,
            Attribute.LABEL: "sign-key",
            Attribute.ID: b"\x01",
            Attribute.SUBJECT: cert.subject.public_bytes(),
            Attribute.VALUE: cert.public_bytes(serialization.Encoding.DER),
        })

    yield lib, cert
    if old_conf is None:
        os.environ.pop("SOFTHSM2_CONF", None)
    else:
        os.environ["SOFTHSM2_CONF"] = old_conf


@pytest.fixture
def tok_profile(token):
    lib, _ = token
    return CertProfile(name="tok", type="pkcs11", pkcs11_lib=lib, token_label=TOKEN,
                       key_label="sign-key", secret="prompt")


def test_sign_with_token(token, tok_profile, form_pdf, tmp_path):
    _, cert = token
    out = tmp_path / "out.pdf"
    with open_signer(tok_profile, Credentials(secret=PIN)) as s:
        sign_pdf(form_pdf, out, s, tok_profile, field_name="Applicant")
    assert inspect_pdf(out).field("Applicant").filled
    [(name, st)] = validate_all(out, cert)
    assert name == "Applicant" and st.bottom_line


def test_two_signatures_token_then_p12(token, tok_profile, profile, form_pdf, p12, tmp_path):
    _, cert = token
    out = tmp_path / "out.pdf"
    with open_signer(tok_profile, Credentials(secret=PIN)) as s:
        sign_pdf(form_pdf, out, s, tok_profile, field_name="Applicant")
    from conftest import PASSPHRASE
    with open_signer(profile, Credentials(secret=PASSPHRASE)) as s:
        sign_pdf(out, out, s, profile, field_name="Supervisor")
    results = validate_all(out, cert, p12[1])
    assert [n for n, _ in results] == ["Applicant", "Supervisor"]
    assert all(st.bottom_line for _, st in results)


def test_wrong_pin(tok_profile):
    with pytest.raises(SigningError, match="(?i)pin"):
        with open_signer(tok_profile, Credentials(secret="000000")):
            pass


def test_unknown_token(tok_profile):
    tok_profile.token_label = "no-such-token"
    with pytest.raises(SigningError, match="no-such-token"):
        with open_signer(tok_profile, Credentials(secret=PIN)):
            pass


def test_unknown_key(tok_profile):
    tok_profile.key_label = "nope"
    with pytest.raises(SigningError, match="nope"):
        with open_signer(tok_profile, Credentials(secret=PIN)):
            pass


def test_missing_module(tok_profile, tmp_path):
    tok_profile.pkcs11_lib = str(tmp_path / "missing.so")
    with pytest.raises(SigningError, match="missing.so"):
        with open_signer(tok_profile, Credentials(secret=PIN)):
            pass


def test_cli_certs_test_and_sign(token, tok_profile, form_pdf, cfg_path, monkeypatch, capsys):
    Config(path=cfg_path, certs={"tok": tok_profile}, default="tok").save()
    monkeypatch.setattr(cli, "_ask", lambda prompt: PIN)
    assert cli.main(["certs", "test", "tok"]) == 0
    assert "Token Signer" in capsys.readouterr().out
    assert cli.main(["sign", str(form_pdf), "--field", "Applicant"]) == 0
    assert inspect_pdf(form_pdf.with_name(f"{form_pdf.stem}-signed.pdf")).field("Applicant").filled


def test_token_error_while_signing(tok_profile, form_pdf, tmp_path, monkeypatch):
    """Errors from the token during signing (card removed, touch timeout, ...)
    are reported like other token errors, not as a traceback."""
    from pkcs11.exceptions import DeviceRemoved
    from pyhanko.sign.pkcs11 import PKCS11Signer

    async def fail(self, data, digest_algorithm, dry_run=False):
        if dry_run:
            return b"0" * 512
        raise DeviceRemoved()

    monkeypatch.setattr(PKCS11Signer, "async_sign_raw", fail)
    with pytest.raises(SigningError, match="refused to sign: DeviceRemoved"):
        with open_signer(tok_profile, Credentials(secret=PIN)) as s:
            sign_pdf(form_pdf, tmp_path / "out.pdf", s, tok_profile, field_name="Applicant")
    assert not (tmp_path / "out.pdf").exists()
