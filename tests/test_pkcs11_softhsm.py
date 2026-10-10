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
from cryptography.hazmat.primitives.asymmetric import ec, rsa  # noqa: E402
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
    # SoftHSM reads its token list when the module is first loaded, so both
    # tokens (this one and the PIV-style one, see `piv`) are created now.
    for label in (TOKEN, PIV_TOKEN, TYPES_TOKEN):
        subprocess.run([util, "--module", lib, "--init-token", "--free", "--label", label,
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


# A second token laid out like a YubiKey through Yubico's ykcs11 module:
# keys and certificates carry different labels but share the slot's ID.
PIV_TOKEN = "YubiKey PIV #12345678"
TYPES_TOKEN = "key types"
# Slot 9c asks for the PIN before every signature (YubiKey's default PIN
# policy for 9c; ykcs11 reports it as CKA_ALWAYS_AUTHENTICATE).
PIV_SLOTS = {  # slot: (ID, key label, certificate label)
    "9a": (b"\x01", "Private key for PIV Authentication", "X.509 Certificate for PIV Authentication"),
    "9c": (b"\x02", "Private key for Digital Signature", "X.509 Certificate for Digital Signature"),
}


def _issue(ca_key, ca_cert, cn, public_key):
    now = datetime.datetime.now(datetime.UTC)
    return (
        x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .issuer_name(ca_cert.subject).public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.KeyUsage(True, True, False, False, False, False, False, False, False),
                       critical=True)  # digitalSignature, nonRepudiation
        .sign(ca_key, hashes.SHA256())
    )


@pytest.fixture(scope="module")
def piv(token):
    """EC keys generated on the token (slots 9a and 9c) with certificates
    from a software CA. Yields (module, CA certificate, {slot: certificate})."""
    from pkcs11 import Attribute, CertificateType, KeyType, ObjectClass
    from pkcs11.util.ec import encode_ec_public_key, encode_named_curve_parameters

    lib, _ = token
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PIV Test CA")])
    now = datetime.datetime.now(datetime.UTC)
    ca_cert = (
        x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    certs = {}
    with pkcs11.lib(lib).get_token(token_label=PIV_TOKEN).open(rw=True, user_pin=PIN) as s:
        params = s.create_domain_parameters(
            KeyType.EC, {Attribute.EC_PARAMS: encode_named_curve_parameters("secp256r1")},
            local=True)
        for slot, (oid, key_label, cert_label) in PIV_SLOTS.items():
            pub, _ = params.generate_keypair(
                store=True,
                public_template={Attribute.ID: oid, Attribute.LABEL: f"Public key for {slot}"},
                private_template={Attribute.ID: oid, Attribute.LABEL: key_label,
                                  Attribute.ALWAYS_AUTHENTICATE: slot == "9c"})
            public_key = serialization.load_der_public_key(encode_ec_public_key(pub))
            cert = _issue(ca_key, ca_cert, f"PIV {slot}", public_key)
            s.create_object({
                Attribute.CLASS: ObjectClass.CERTIFICATE,
                Attribute.CERTIFICATE_TYPE: CertificateType.X_509,
                Attribute.TOKEN: True,
                Attribute.LABEL: cert_label,
                Attribute.ID: oid,
                Attribute.SUBJECT: cert.subject.public_bytes(),
                Attribute.VALUE: cert.public_bytes(serialization.Encoding.DER),
            })
            certs[slot] = cert
    yield lib, ca_cert, certs


def piv_profile(lib, **kw):
    return CertProfile(name="yk", type="pkcs11", pkcs11_lib=lib, token_label=PIV_TOKEN, **kw)


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
    assert "if your card reader asks for the PIN" in capsys.readouterr().err
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


# --- selecting key and certificate (YubiKey-style labels) -----------------------------


@pytest.mark.parametrize("select", [
    {"key_label": "Private key for Digital Signature"},
    {"key_id": "02"},
    {"key_id": "0x02"},
    {"key_label": "Private key for Digital Signature", "key_id": "02"},
    {"key_id": "02", "cert_label": "X.509 Certificate for Digital Signature"},
])
def test_piv_select_signature_key(piv, select, form_pdf, tmp_path):
    """The certificate is found through the key's ID although the labels differ."""
    lib, ca, certs = piv
    profile = piv_profile(lib, **select)
    out = tmp_path / "out.pdf"
    with open_signer(profile, Credentials(secret=PIN)) as s:
        assert s.signing_cert.subject.human_friendly == "Common Name: PIV 9c"
        sign_pdf(form_pdf, out, s, profile, field_name="Applicant")
    [(_, st)] = validate_all(out, ca)
    assert st.bottom_line


def test_piv_several_keys_need_a_choice(piv):
    lib, _, _ = piv
    with pytest.raises(SigningError, match=r"2 private keys match.*\(ID 01\).*\(ID 02\)"):
        with open_signer(piv_profile(lib), Credentials(secret=PIN)):
            pass


def test_piv_unknown_key_lists_keys(piv):
    lib, _, _ = piv
    with pytest.raises(SigningError,
                       match=r"no private key with ID '05'.*'Private key for Digital Signature' \(ID 02\)"):
        with open_signer(piv_profile(lib, key_id="05"), Credentials(secret=PIN)):
            pass


def test_piv_wrong_cert_label_lists_certificates(piv):
    lib, _, _ = piv
    with pytest.raises(SigningError, match=r"no certificate with label 'nope'.*"
                                           r"'X.509 Certificate for Digital Signature' \(ID 02\)"):
        with open_signer(piv_profile(lib, key_id="02", cert_label="nope"),
                         Credentials(secret=PIN)):
            pass


# --- keys that need the PIN for every signature -----------------------------------------


def test_piv_key_without_always_authenticate(piv, form_pdf, tmp_path):
    """Slot 9a: the PIN is only sent once (SoftHSM rejects an unneeded
    per-signature login, so this also checks it isn't sent)."""
    lib, ca, _ = piv
    profile = piv_profile(lib, key_id="01")
    with open_signer(profile, Credentials(secret=PIN)) as s:
        assert s.signing_cert.subject.human_friendly == "Common Name: PIV 9a"
        sign_pdf(form_pdf, tmp_path / "out.pdf", s, profile, field_name="Applicant")
    [(_, st)] = validate_all(tmp_path / "out.pdf", ca)
    assert st.bottom_line


def test_piv_always_authenticate_without_pin(piv, form_pdf, tmp_path):
    """A token with a PIN pad (no PIN from us) that still wants a
    per-signature login: a clear error."""
    lib, _, _ = piv
    profile = piv_profile(lib, key_id="02")
    with pytest.raises(SigningError, match="PIN for every signature"):
        with open_signer(profile, Credentials(secret=PIN)) as s:
            s._sign_kwargs = {}  # what happens if the PIN isn't passed
            sign_pdf(form_pdf, tmp_path / "out.pdf", s, profile, field_name="Applicant")


# --- listing a token (certs pkcs11) -------------------------------------------------------


def test_list_token_without_login(piv):
    from pdfsigner import tokens

    lib, _, _ = piv
    [t] = tokens.list_tokens(lib, PIV_TOKEN)
    assert t.label == PIV_TOKEN and not t.logged_in
    assert [(e.id_hex, e.key_label, e.cert_label, e.subject) for e in t.entries] == [
        ("01", None, "X.509 Certificate for PIV Authentication", "Common Name: PIV 9a"),
        ("02", None, "X.509 Certificate for Digital Signature", "Common Name: PIV 9c"),
    ]


def test_list_token_with_login(piv):
    from pdfsigner import tokens

    lib, _, _ = piv
    asked = []
    found = tokens.list_tokens(lib, ask_pin=lambda label: asked.append(label) or PIN)
    assert sorted(asked) == sorted([TOKEN, PIV_TOKEN, TYPES_TOKEN])  # not the empty slot
    t = next(t for t in found if t.label == PIV_TOKEN)
    assert [(e.id_hex, e.key_label, e.key_type, e.always_auth) for e in t.entries] == [
        ("01", "Private key for PIV Authentication", "EC", False),
        ("02", "Private key for Digital Signature", "EC", True),
    ]
    assert "PIN for every signature" in t.entries[1].describe()


def test_list_token_wrong_pin(piv):
    from pdfsigner import tokens

    lib, _, _ = piv
    with pytest.raises(SigningError, match="wrong PIN"):
        tokens.list_tokens(lib, PIV_TOKEN, ask_pin=lambda label: "000000")


def test_cli_certs_pkcs11(piv, cfg_path, monkeypatch, capsys):
    lib, _, _ = piv
    monkeypatch.setattr(cli, "_ask", lambda prompt: PIN)
    assert cli.main(["certs", "pkcs11", lib, "--token-label", PIV_TOKEN, "--login"]) == 0
    out = capsys.readouterr().out
    assert f"token '{PIV_TOKEN}'" in out
    assert ("ID 02    key 'Private key for Digital Signature' (EC, PIN for every signature)"
            "  certificate 'X.509 Certificate for Digital Signature': Common Name: PIV 9c") in out
    assert out.rstrip().endswith("--key-id ID")  # one token: no --token-label
    assert cli.main(["certs", "pkcs11", lib, "--token-label", "nope"]) == 0
    assert "no token found" in capsys.readouterr().out
    assert cli.main(["certs", "pkcs11", "/no/such/module.so"]) == 1


# --- key types cards use ------------------------------------------------------------------


KEY_TYPES = {  # ID: (description, always authenticate)
    b"\x01": ("Ed25519", False),
    b"\x02": ("EC P-384", False),
    b"\x03": ("RSA 2048", False),
    b"\x04": ("RSA 2048, PIN for every signature", True),
}


@pytest.fixture(scope="module")
def typed_keys(token):
    """Keys generated on the token (as on a card), certificates from a software CA."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from pkcs11 import Attribute, CertificateType, KeyType, ObjectClass
    from pkcs11.util.ec import encode_ec_public_key, encode_named_curve_parameters
    from pkcs11.util.rsa import encode_rsa_public_key

    lib, _ = token
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Types CA")])
    now = datetime.datetime.now(datetime.UTC)
    ca_cert = (
        x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    with pkcs11.lib(lib).get_token(token_label=TYPES_TOKEN).open(rw=True, user_pin=PIN) as s:
        for oid, (what, always) in KEY_TYPES.items():
            private = {Attribute.ID: oid, Attribute.LABEL: what,
                       Attribute.ALWAYS_AUTHENTICATE: always}
            public = {Attribute.ID: oid, Attribute.LABEL: what}
            if what == "Ed25519":
                params = s.create_domain_parameters(KeyType.EC_EDWARDS, {
                    Attribute.EC_PARAMS: encode_named_curve_parameters("1.3.101.112")},
                    local=True)
                pub, _ = params.generate_keypair(store=True, public_template=public,
                                                 private_template=private)
                point = pub[Attribute.EC_POINT]
                public_key = Ed25519PublicKey.from_public_bytes(point[-32:])
            elif what == "EC P-384":
                params = s.create_domain_parameters(KeyType.EC, {
                    Attribute.EC_PARAMS: encode_named_curve_parameters("secp384r1")}, local=True)
                pub, _ = params.generate_keypair(store=True, public_template=public,
                                                 private_template=private)
                public_key = serialization.load_der_public_key(encode_ec_public_key(pub))
            else:
                pub, _ = s.generate_keypair(KeyType.RSA, 2048, store=True,
                                            public_template=public, private_template=private)
                public_key = serialization.load_der_public_key(encode_rsa_public_key(pub))
            cert = _issue(ca_key, ca_cert, what, public_key)
            s.create_object({
                Attribute.CLASS: ObjectClass.CERTIFICATE,
                Attribute.CERTIFICATE_TYPE: CertificateType.X_509,
                Attribute.TOKEN: True, Attribute.LABEL: what, Attribute.ID: oid,
                Attribute.SUBJECT: cert.subject.public_bytes(),
                Attribute.VALUE: cert.public_bytes(serialization.Encoding.DER),
            })
    return lib, ca_cert


@pytest.mark.parametrize("oid", list(KEY_TYPES), ids=[v[0] for v in KEY_TYPES.values()])
def test_sign_with_key_types_cards_use(typed_keys, oid, blank_pdf, tmp_path):
    lib, ca = typed_keys
    profile = CertProfile(name="t", type="pkcs11", pkcs11_lib=lib, token_label=TYPES_TOKEN,
                          key_id=oid.hex())
    out = tmp_path / "out.pdf"
    with open_signer(profile, Credentials(secret=PIN)) as s:
        sign_pdf(blank_pdf, out, s, profile, page=0, box=(50, 50, 250, 110))
    [(_, st)] = validate_all(out, ca)
    assert st.bottom_line


def test_find_modules(tmp_path, monkeypatch):
    from pdfsigner import tokens

    (tmp_path / "lib").mkdir()
    opensc = tmp_path / "lib" / "opensc-pkcs11.so"
    proxy = tmp_path / "lib" / "p11-kit-proxy.so"
    proxy.write_bytes(b"")
    monkeypatch.setattr(tokens, "KNOWN_MODULES", {"linux": [str(tmp_path / "*" / "opensc-*.so")]})
    monkeypatch.setattr(tokens, "P11_KIT_PROXY", [str(proxy)])
    assert tokens.find_modules("linux") == [str(proxy)]  # fallback: nothing found directly
    opensc.write_bytes(b"")
    (tmp_path / "lib" / "link-opensc-pkcs11.so").symlink_to(opensc)  # same module twice
    monkeypatch.setattr(tokens, "KNOWN_MODULES",
                        {"linux": [str(opensc), str(tmp_path / "lib" / "link-*.so")]})
    assert tokens.find_modules("linux") == [str(opensc)]
    assert tokens.find_modules("win32") == []


def test_cli_certs_pkcs11_without_module(piv, cfg_path, monkeypatch, capsys):
    from pdfsigner import tokens

    lib, _, _ = piv
    monkeypatch.setattr(tokens, "find_modules", lambda: [lib, "/no/such/module.so"])
    assert cli.main(["certs", "pkcs11", "--token-label", PIV_TOKEN]) == 0
    captured = capsys.readouterr()
    assert f"module {lib}:" in captured.out and f"token '{PIV_TOKEN}'" in captured.out
    assert "/no/such/module.so" in captured.err  # reported, but doesn't stop the others
    monkeypatch.setattr(tokens, "find_modules", lambda: [])
    assert cli.main(["certs", "pkcs11"]) == 1
    assert "no PKCS#11 module found" in capsys.readouterr().err
