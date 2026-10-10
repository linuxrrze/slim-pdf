"""Intermediate certificates: found, downloaded via AIA, cached, embedded."""

import datetime
import http.server
import threading

import pytest
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from conftest import PASSPHRASE, make_pdf
from pdfsigner import chain, validation
from pdfsigner.config import CertProfile
from pdfsigner.secretstore import Credentials
from pdfsigner.signing import open_signer, sign_pdf

NOW = datetime.datetime.now(datetime.UTC)


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(subject, issuer, key, issuer_key, *, ca, aia=None):
    b = (x509.CertificateBuilder().subject_name(_name(subject)).issuer_name(_name(issuer))
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(NOW - datetime.timedelta(days=1))
         .not_valid_after(NOW + datetime.timedelta(days=365))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
         .add_extension(x509.KeyUsage(not ca, not ca, False, False, False, ca, ca,
                                      False, False), critical=True)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
             issuer_key.public_key()), False))
    if aia:
        b = b.add_extension(x509.AuthorityInformationAccess([x509.AccessDescription(
            x509.AuthorityInformationAccessOID.CA_ISSUERS,
            x509.UniformResourceIdentifier(aia))]), False)
    return b.sign(issuer_key, hashes.SHA256())


@pytest.fixture
def pki(monkeypatch):
    """Root -> "Personal CA" -> Alice, like USERTrust -> GEANT Personal CA 4 ->
    a person. Alice's certificate names a local URL for the intermediate,
    which the p12 (like a token) doesn't contain."""
    root_key, ca_key, key = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    root = _cert("Test Root", "Test Root", root_key, root_key, ca=True)
    served = {}
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = served.get(self.path)
            if body is None:
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/personal-ca.crt"
    ca = _cert("Test Personal CA", "Test Root", ca_key, root_key, ca=True)
    leaf = _cert("Alice Example", "Test Personal CA", key, ca_key, ca=False, aia=url)
    served["/personal-ca.crt"] = ca.public_bytes(serialization.Encoding.DER)
    # downloads allowed again, but only from this server
    monkeypatch.setattr(chain, "download", lambda u: chain.urllib.request.urlopen(
        u, timeout=chain.TIMEOUT).read() if u.startswith("http://127.0.0.1") else b"")
    p12 = pkcs12.serialize_key_and_certificates(
        b"alice", key, leaf, None, serialization.BestAvailableEncryption(PASSPHRASE.encode()))
    yield {"root": root, "ca": ca, "leaf": leaf, "p12": p12, "requests": requests,
           "served": served}
    server.shutdown()


def asn1(cert):
    return asn1_x509.Certificate.load(cert.public_bytes(serialization.Encoding.DER))


def root_pem(pki, tmp_path):
    path = tmp_path / "root.pem"
    path.write_bytes(pki["root"].public_bytes(serialization.Encoding.PEM))
    return str(path)


def test_missing_intermediate_is_downloaded_once(pki):
    leaf, roots = asn1(pki["leaf"]), [asn1(pki["root"])]
    assert chain.complete_chain(leaf, [], roots, fetch=False) == []
    found = chain.complete_chain(leaf, [], roots)
    assert [c.subject.native["common_name"] for c in found] == ["Test Personal CA"]
    assert len(pki["requests"]) == 1
    again = chain.complete_chain(leaf, [], roots)  # from the cache now
    assert [c.dump() for c in again] == [c.dump() for c in found]
    assert len(pki["requests"]) == 1


def test_issuer_formats(pki):
    ca = pki["ca"]
    der = ca.public_bytes(serialization.Encoding.DER)
    pem = ca.public_bytes(serialization.Encoding.PEM)
    from cryptography.hazmat.primitives.serialization import pkcs7

    p7c = pkcs7.serialize_certificates([ca], serialization.Encoding.DER)
    for data in (der, pem, p7c):
        assert [c.dump() for c in chain.parse(data)] == [der]
    assert chain.parse(b"<html>not found</html>") == []


def test_a_signature_carries_the_chain(pki, tmp_path, monkeypatch):
    """Signing embeds the intermediate, so the signature validates later
    without any download."""
    (tmp_path / "alice.p12").write_bytes(pki["p12"])
    profile = CertProfile(name="t", path=str(tmp_path / "alice.p12"), stamp_text="")
    src, out = make_pdf(tmp_path / "doc.pdf", 1), tmp_path / "signed.pdf"
    with open_signer(profile, Credentials(secret=PASSPHRASE)) as s:
        sign_pdf(src, out, s, profile)
    assert pki["requests"]  # fetched while signing
    monkeypatch.setenv("PDFSIGNER_CACHE_DIR", str(tmp_path / "empty-cache"))
    [report] = validation.verify_pdf(out, trust=[root_pem(pki, tmp_path)],
                                     system_trust=False, fetch_issuers=False)
    assert report.status == validation.VALID, report.problems


def test_old_signatures_without_the_chain_validate(pki, tmp_path):
    """A signature made without the intermediate (as by other software, or
    a token) is trusted once the intermediate is downloaded."""
    (tmp_path / "alice.p12").write_bytes(pki["p12"])
    profile = CertProfile(name="t", path=str(tmp_path / "alice.p12"), stamp_text="")
    src, out = make_pdf(tmp_path / "doc.pdf", 1), tmp_path / "signed.pdf"
    with open_signer(profile, Credentials(secret=PASSPHRASE)) as s:
        sign_pdf(src, out, s, profile, fetch_issuers=False)
    trust = [root_pem(pki, tmp_path)]
    [offline] = validation.verify_pdf(out, trust=trust, system_trust=False,
                                      fetch_issuers=False)
    assert offline.status == validation.UNTRUSTED
    assert "doesn't chain to a trusted root" in " ".join(offline.problems)
    [report] = validation.verify_pdf(out, trust=trust, system_trust=False)
    assert report.status == validation.VALID, report.problems


def test_unreachable_issuer_leaves_the_chain_broken(pki, tmp_path):
    pki["served"].clear()  # the CA's server answers 404
    leaf, roots = asn1(pki["leaf"]), [asn1(pki["root"])]
    assert chain.complete_chain(leaf, [], roots) == []
    assert chain.cached() == []
