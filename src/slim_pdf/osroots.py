"""Root certificates trusted by the operating system (its TLS CA store).

These add to Mozilla's list (certifi): roots an administrator or company
installed system-wide, and those Windows or macOS trust on their own.

* everywhere: ``$SSL_CERT_FILE`` (a PEM bundle), if set;
* Linux/BSD: the distribution's bundle (ca-certificates, update-ca-trust, ...);
* Windows: the "ROOT" store (Windows fetches some roots only when first
  used, so the list may be short on a fresh system);
* macOS: the system root and system keychains (``security``).
"""

from __future__ import annotations

import logging
import os
import ssl
import subprocess
import sys

from asn1crypto import pem, x509

log = logging.getLogger(__name__)

BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Arch, Gentoo
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # Fedora, RHEL
    "/etc/pki/tls/certs/ca-bundle.crt",  # older RHEL
    "/etc/ssl/ca-bundle.pem",  # openSUSE
    "/etc/ssl/cert.pem",  # Alpine, BSD, macOS
)
MAC_KEYCHAINS = ("/System/Library/Keychains/SystemRootCertificates.keychain",
                 "/Library/Keychains/System.keychain")


def _load(data: bytes) -> list[x509.Certificate]:
    """Certificates from PEM (or a single DER); unreadable ones are skipped."""
    ders = ([der for _type, _h, der in pem.unarmor(data, multiple=True)]
            if pem.detect(data) else [data])
    certs = []
    for der in ders:
        try:
            cert = x509.Certificate.load(der)
            cert.native  # noqa: B018 - parses now: garbage raises here
            certs.append(cert)
        except (ValueError, TypeError):
            continue
    return certs


def _read(path: str) -> list[x509.Certificate]:
    try:
        with open(path, "rb") as f:
            return _load(f.read())
    except OSError:
        return []


def _unix() -> list[x509.Certificate]:
    candidates = [*BUNDLES, ssl.get_default_verify_paths().cafile]
    for path in candidates:
        if path and (certs := _read(path)):
            return certs
    return []


def _windows() -> list[x509.Certificate]:
    certs = []
    for der, encoding, trust in ssl.enum_certificates("ROOT"):  # type: ignore[attr-defined]
        if encoding == "x509_asn" and trust:  # trust: True (any use) or a set of uses
            certs += _load(der)
    return certs


def _macos() -> list[x509.Certificate]:
    certs = []
    for keychain in MAC_KEYCHAINS:
        try:
            out = subprocess.run(["/usr/bin/security", "find-certificate", "-a", "-p", keychain],
                                 capture_output=True, timeout=10, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if pem.detect(out):
            certs += _load(out)
    return certs


def os_roots() -> list[x509.Certificate]:
    """The system's trusted roots; empty where they can't be read."""
    certs = []
    if env := os.environ.get("SSL_CERT_FILE"):
        certs += _read(env)
    try:
        if sys.platform == "win32":
            certs += _windows()
        elif sys.platform == "darwin":
            certs += _macos()
        else:
            certs += _unix()
    except Exception as e:  # noqa: BLE001 - a broken store must not break validation
        log.warning("cannot read the system's CA store: %s", e)
    return certs
