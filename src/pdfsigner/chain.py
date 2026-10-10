"""Intermediate CA certificates between a signer and a trusted root.

A signature is only trusted if a chain leads from the signer's certificate
to a root (Mozilla's list, or the configured ``trust``). Roots sign
intermediates, intermediates sign people: "GEANT Personal CA 4" is issued by
"USERTrust RSA Certification Authority", which is in Mozilla's list. If a
signature doesn't carry the intermediate (tokens and the system stores
usually only hold the person's own certificate), the chain breaks.

Certificates name their issuer's download location (AIA "CA issuers").
Missing intermediates are fetched from there, as PDF viewers do, and kept
in a small cache, so each is downloaded once. Signing embeds them, so new
signatures validate without any download.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from pathlib import Path

from asn1crypto import cms, pem, x509
from platformdirs import user_cache_dir

TIMEOUT = 5  # seconds per download
MAX_SIZE = 1 << 20
MAX_DEPTH = 6  # intermediates between a signer and a root, at most


def cache_path() -> Path:
    base = os.environ.get("PDFSIGNER_CACHE_DIR") or user_cache_dir("pdfsigner", appauthor=False)
    return Path(base) / "intermediates.pem"


def _fingerprint(cert: x509.Certificate) -> str:
    return hashlib.sha256(cert.dump()).hexdigest()


def parse(data: bytes) -> list[x509.Certificate]:
    """Certificates in DER, PEM or PKCS#7 ("certs-only" .p7c) form."""
    if pem.detect(data):
        return [c for _type, _h, der in pem.unarmor(data, multiple=True)
                for c in parse(der)]
    try:
        cert = x509.Certificate.load(data)
        cert.native  # noqa: B018 - parses everything now: garbage raises here
        return [cert]
    except ValueError:
        pass
    try:
        info = cms.ContentInfo.load(data)
        certs = info["content"]["certificates"]
        return [c.chosen for c in certs if c.name == "certificate"]
    except (ValueError, KeyError, TypeError):
        return []


def cached() -> list[x509.Certificate]:
    try:
        return parse(cache_path().read_bytes())
    except OSError:
        return []


def remember(certs: list[x509.Certificate]) -> None:
    """Add CA certificates to the cache (never anything else)."""
    known = {_fingerprint(c) for c in cached()}
    new = [c for c in certs if c.ca and _fingerprint(c) not in known]
    if not new:
        return
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as f:
            for c in new:
                f.write(pem.armor("CERTIFICATE", c.dump()))
    except OSError:
        pass  # a cache only


def issuer_urls(cert: x509.Certificate) -> list[str]:
    aia = cert.authority_information_access_value
    urls = []
    for desc in aia or ():
        location = desc["access_location"]
        if (desc["access_method"].native == "ca_issuers"
                and location.name == "uniform_resource_identifier"
                and location.native.lower().startswith(("http://", "https://"))):
            urls.append(location.native)
    return urls


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:  # noqa: S310 - http(s) only
        return r.read(MAX_SIZE)


def issued_by(cert: x509.Certificate, issuer: x509.Certificate) -> bool:
    if cert.issuer != issuer.subject:
        return False
    aki, ski = cert.authority_key_identifier, issuer.key_identifier
    return aki is None or ski is None or aki == ski


def _fetch_issuer(cert: x509.Certificate) -> x509.Certificate | None:
    for url in issuer_urls(cert):
        try:
            found = [c for c in parse(download(url)) if issued_by(cert, c)]
        except (OSError, ValueError):
            continue
        if found:
            remember(found)
            return found[0]
    return None


def complete_chain(cert: x509.Certificate, pool, roots, *,
                   fetch: bool = True) -> list[x509.Certificate]:
    """The intermediates from ``cert`` up to (not including) a root in
    ``roots``: found in ``pool`` or the cache, else downloaded (``fetch``).
    Stops early where no issuer can be found; the caller's validation then
    reports the broken chain."""
    pool = list(pool) + cached()
    chain: list[x509.Certificate] = []
    current = cert
    for _ in range(MAX_DEPTH):
        if current.self_signed != "no" or any(issued_by(current, r) for r in roots):
            break
        issuer = next((c for c in pool if issued_by(current, c)), None)
        if issuer is None and fetch:
            issuer = _fetch_issuer(current)
        if issuer is None or any(c.dump() == issuer.dump() for c in chain):  # a loop
            break
        chain.append(issuer)
        current = issuer
    return chain
