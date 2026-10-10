"""A small CA with a CRL served over HTTP on localhost, for LTV tests."""

from __future__ import annotations

import datetime
import http.server
import threading
from dataclasses import dataclass, field

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

NOW = datetime.datetime.now(datetime.UTC)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


@dataclass
class MiniPKI:
    """Root CA -> leaf signer; the leaf's CRL is served at ``crl_url``."""

    port: int
    root_key: ec.EllipticCurvePrivateKey = field(init=False)
    root: x509.Certificate = field(init=False)
    revoked: set[int] = field(default_factory=set)
    crl_requests: int = 0

    def __post_init__(self):
        self.root_key = ec.generate_private_key(ec.SECP256R1())
        ski = x509.SubjectKeyIdentifier.from_public_key(self.root_key.public_key())
        self.root = (
            x509.CertificateBuilder().subject_name(_name("Test Root CA"))
            .issuer_name(_name("Test Root CA")).public_key(self.root_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(NOW - datetime.timedelta(days=10))
            .not_valid_after(NOW + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True,
                                         False, False), critical=True)
            .add_extension(ski, critical=False)
            .sign(self.root_key, hashes.SHA256())
        )

    @property
    def crl_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/root.crl"

    def issue(self, cn: str, passphrase: bytes, *, key=None, chain: bool = True,
              legacy: bool = False, non_repudiation: bool = True
              ) -> tuple[bytes, x509.Certificate]:
        """PKCS#12 with a new leaf. ``legacy`` uses 3DES/SHA-1 encryption, which
        every Windows and macOS importer understands. Without
        ``non_repudiation`` the key usage is digitalSignature + keyEncipherment,
        as in typical S/MIME certificates (e.g. GEANT Personal CA 4)."""
        key = key or ec.generate_private_key(ec.SECP256R1())
        cert = (
            x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(self.root.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(NOW - datetime.timedelta(days=1))
            .not_valid_after(NOW + datetime.timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, non_repudiation, not non_repudiation, False,
                                         False, False, False, False, False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]),
                           critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                self.root_key.public_key()), critical=False)
            .add_extension(x509.CRLDistributionPoints([x509.DistributionPoint(
                full_name=[x509.UniformResourceIdentifier(self.crl_url)],
                relative_name=None, reasons=None, crl_issuer=None)]), critical=False)
            .sign(self.root_key, hashes.SHA256())
        )
        if legacy:
            enc = (serialization.PrivateFormat.PKCS12.encryption_builder()
                   .kdf_rounds(50000)
                   .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC)
                   .hmac_hash(hashes.SHA1()).build(passphrase))
        else:
            enc = serialization.BestAvailableEncryption(passphrase)
        data = pkcs12.serialize_key_and_certificates(
            cn.encode(), key, cert, [self.root] if chain else None, enc)
        return data, cert

    def crl(self) -> bytes:
        b = (
            x509.CertificateRevocationListBuilder().issuer_name(self.root.subject)
            .last_update(NOW - datetime.timedelta(hours=1))
            .next_update(NOW + datetime.timedelta(days=7))
            .add_extension(x509.CRLNumber(1), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                self.root_key.public_key()), critical=False)
        )
        for serial in self.revoked:
            b = b.add_revoked_certificate(
                x509.RevokedCertificateBuilder().serial_number(serial)
                .revocation_date(NOW - datetime.timedelta(minutes=30)).build())
        return b.sign(self.root_key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)


def serve(pki_factory) -> tuple[MiniPKI, http.server.HTTPServer]:
    """Start a CRL server on a free port; returns (pki, server)."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/root.crl":
                self.send_error(404)
                return
            pki.crl_requests += 1
            body = pki.crl()
            self.send_response(200)
            self.send_header("Content-Type", "application/pkix-crl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    pki = pki_factory(server.server_address[1])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return pki, server


def make_tsa():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # dummy TSA is RSA-only
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test TSA")])
    now = NOW
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), critical=True)
        .sign(key, hashes.SHA256())
    )
    from asn1crypto import keys

    key_info = keys.PrivateKeyInfo.load(key.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return cert, key_info
