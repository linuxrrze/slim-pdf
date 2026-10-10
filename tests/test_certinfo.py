"""Certificate details and the stamp text's placeholders."""

import datetime

import pytest
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from pdfsigner import certinfo
from pdfsigner.config import CertProfile


@pytest.fixture(scope="module")
def cert():
    """A certificate with the attributes a stamp might show."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Alice Example"),
        x509.NameAttribute(NameOID.EMAIL_ADDRESS, "alice@example.org"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Example University"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Computer Science"),
        x509.NameAttribute(NameOID.COUNTRY_NAME, "DE"),
    ])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Example CA"),
                        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Example Trust")])
    now = datetime.datetime.now(datetime.UTC)
    c = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
         .public_key(key.public_key()).serial_number(0x1A2B3C)
         .not_valid_before(now - datetime.timedelta(days=1))
         .not_valid_after(now + datetime.timedelta(days=10))
         .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]), False)
         .add_extension(x509.SubjectAlternativeName([x509.RFC822Name("alice@example.org")]),
                        False)
         .sign(key, hashes.SHA256()))
    return asn1_x509.Certificate.load(c.public_bytes(serialization.Encoding.DER))


def test_pem_round_trip(cert):
    pem_text = certinfo.to_pem(cert)
    assert pem_text.startswith("-----BEGIN CERTIFICATE-----")
    assert certinfo.load(pem_text).dump() == cert.dump()
    assert certinfo.load("") is None
    assert certinfo.load("not a certificate") is None


def test_text_params_from_the_certificate(cert):
    profile = CertProfile(name="p", path="x.p12", reason="Approval", location="Erlangen")
    when = datetime.datetime(2026, 10, 10, 14, 5, tzinfo=datetime.UTC)
    params = certinfo.text_params(cert, profile, when)
    assert {k: params[k] for k in ("signer", "cn", "email", "org", "unit", "country",
                                   "issuer", "serial")} == {
        "signer": "Alice Example", "cn": "Alice Example", "email": "alice@example.org",
        "org": "Example University", "unit": "Computer Science", "country": "DE",
        "issuer": "Example CA", "serial": "1A2B3C"}
    assert (params["date"], params["time"]) == ("2026-10-10", "14:05")
    assert params["ts"].startswith("2026-10-10 14:05:00")
    assert (params["reason"], params["location"], params["contact"]) == (
        "Approval", "Erlangen", "")
    assert set(params) == set(certinfo.STAMP_FIELDS)
    # without a certificate: sample values, so a preview still shows something
    assert certinfo.text_params(None, profile)["cn"] == "Alice Example"


def test_fill_drops_lines_left_empty(cert):
    params = certinfo.text_params(cert, CertProfile(name="p", path="x"))
    template = "%(cn)s\n%(reason)s\n\nReason: %(reason)s\n100%% sure"
    assert certinfo.fill(template, params) == "Alice Example\n\nReason: \n100% sure"


def test_check_template():
    certinfo.check_template("%(cn)s, %(date)s 100%%")
    with pytest.raises(ValueError, match=r"unknown placeholder %\(name\)s"):
        certinfo.check_template("%(name)s")
    with pytest.raises(ValueError, match="stray %"):
        certinfo.check_template("100% sure")


def test_details(cert):
    rows = dict(certinfo.details(cert))
    assert "Alice Example" in rows["Subject"] and "Example CA" in rows["Issuer"]
    assert rows["Serial number"] == "1A2B3C"
    assert rows["Key"] == "RSA 2048"
    assert rows["Extended key usage"] == "email_protection"
    assert rows["Alternative names"] == "alice@example.org"
    assert rows["Status"].startswith("valid, ")
    assert len(rows["SHA-256 fingerprint"]) == 64
    assert certinfo.expires_soon(cert) and not certinfo.expires_soon(cert, days=5)
