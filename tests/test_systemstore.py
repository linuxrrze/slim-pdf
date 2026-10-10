"""System certificate store signing, with a fake backend (runs everywhere).

The native Windows/macOS backends are exercised by test_systemstore_native.py.
"""

import sys

import pytest
from asn1crypto import x509 as ax509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils

from pdfsigner import cli, systemstore
from pdfsigner.config import CertProfile, load_config
from pdfsigner.secretstore import resolve_credentials
from pdfsigner.signing import SigningError, open_signer, sign_pdf
from pdfsigner.systemstore import StoreIdentity, p1363_to_der, select

from conftest import validate_all
from pki import MiniPKI

HASHES = {"sha256": hashes.SHA256(), "sha384": hashes.SHA384(), "sha512": hashes.SHA512()}


class FakeBackend:
    """Keys in memory, signing digests like CNG / SecKey would."""

    name = "fake store"

    def __init__(self, entries):
        self.entries = entries  # [(cryptography cert, key)]
        self.closed = False
        self.signed = []

    def identities(self):
        return [StoreIdentity(cert=ax509.Certificate.load(c.public_bytes(serialization.Encoding.DER)),
                              handle=k) for c, k in self.entries]

    def sign_digest(self, ident, digest, digest_algorithm, mechanism):
        self.signed.append((digest_algorithm, mechanism))
        algo = utils.Prehashed(HASHES[digest_algorithm])
        if mechanism == "rsassa_pkcs1v15":
            return ident.handle.sign(digest, padding.PKCS1v15(), algo)
        return ident.handle.sign(digest, ec.ECDSA(algo))  # already DER

    def close(self):
        self.closed = True


@pytest.fixture(scope="module")
def ca():
    return MiniPKI(port=1)


def issue(ca, cn, key):
    """Certificate for an existing key (keys live 'in the store')."""
    import datetime

    from cryptography import x509
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.UTC)
    return (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
            .issuer_name(ca.root.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=100))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, True, False, False, False, False, False,
                                         False, False), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca.root_key.public_key()), critical=False)
            .sign(ca.root_key, hashes.SHA256()))


@pytest.fixture
def store(ca, monkeypatch):
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ec_key = ec.generate_private_key(ec.SECP384R1())
    other = ec.generate_private_key(ec.SECP256R1())
    backend = FakeBackend([(issue(ca, "Alice RSA", rsa_key), rsa_key),
                           (issue(ca, "Alice EC", ec_key), ec_key),
                           (issue(ca, "Bob", other), other)])
    monkeypatch.setattr(systemstore, "get_backend", lambda: backend)
    return backend


def test_select(store):
    ids = store.identities()
    assert select(ids, "bob").subject == "Common Name: Bob"
    tp = ids[1].thumbprint
    spaced = " ".join(tp[i:i + 2] for i in range(0, 40, 2)).lower()
    assert select(ids, spaced) is ids[1]
    with pytest.raises(SigningError, match="matches several certificates"):
        select(ids, "Alice")
    with pytest.raises(SigningError, match="no certificate with a private key matches"):
        select(ids, "Carol")
    with pytest.raises(SigningError, match="system_cert is empty"):
        select(ids, " ")


@pytest.mark.parametrize("spec, mechanism", [("Alice RSA", "rsassa_pkcs1v15"),
                                             ("Alice EC", "ecdsa")])
def test_sign_with_store(store, ca, form_pdf, tmp_path, spec, mechanism):
    profile = CertProfile(name="store", type="system", system_cert=spec)
    profile.validate()
    out = tmp_path / "out.pdf"
    creds = resolve_credentials(profile, lambda p: pytest.fail("must not prompt"))
    with open_signer(profile, creds) as signer:
        assert spec in signer.signing_cert.subject.human_friendly
        sign_pdf(form_pdf, out, signer, profile, field_name="Applicant")
    assert store.closed
    [(digest, mech)] = store.signed  # the dry run doesn't call the store
    assert mech == mechanism and digest in ("sha256", "sha384")
    [(_, st)] = validate_all(out, ca.root)
    assert st.bottom_line


def test_cli_certs_system_and_sign(store, form_pdf, cfg_path, capsys):
    assert cli.main(["certs", "system"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 3 and "Common Name: Bob" in out[2] and "valid until" in out[2]
    assert cli.main(["certs", "add", "store", "--system-cert", "Bob"]) == 0
    cfg = load_config()
    assert cfg.certs["store"].type == "system"
    assert cli.main(["certs", "list"]) == 0
    assert "store:Bob" in capsys.readouterr().out
    assert cli.main(["sign", str(form_pdf), "-c", "store", "--field", "Applicant"]) == 0


def test_cli_certs_system_empty(monkeypatch, capsys, cfg_path):
    monkeypatch.setattr(systemstore, "get_backend", lambda: FakeBackend([]))
    assert cli.main(["certs", "system"]) == 0
    assert "no certificates with a private key in the fake store" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="store exists here")
def test_unsupported_platform(cfg_path, capsys):
    assert cli.main(["certs", "system"]) == 1
    assert "only supported on Windows and macOS" in capsys.readouterr().err


def test_profile_needs_system_cert():
    with pytest.raises(ValueError, match="system needs system_cert"):
        CertProfile(name="x", type="system").validate()


def test_p1363_to_der():
    key = ec.generate_private_key(ec.SECP256R1())
    der = key.sign(b"data", ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    assert p1363_to_der(raw) == der


def test_placeholder_is_large_enough(store):
    for ident in store.identities():
        n = systemstore._placeholder_length(ident.cert)
        for _ in range(5):
            sig = store.sign_digest(ident, bytes(32), "sha256",
                                    "rsassa_pkcs1v15" if "RSA" in ident.subject else "ecdsa")
            assert len(sig) <= n
