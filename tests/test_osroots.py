"""Roots from the operating system's CA store count as trusted."""

import sys

import pytest

from pdfsigner import osroots, validation
from pdfsigner.validation import UNTRUSTED, VALID, verify_pdf

from conftest import sign_with, write_pem


@pytest.fixture
def fresh_roots():
    validation.system_trust_roots.cache_clear()
    yield
    validation.system_trust_roots.cache_clear()


def test_ssl_cert_file_root_is_trusted(profile, form_pdf, p12, tmp_path, monkeypatch,
                                       fresh_roots):
    out = tmp_path / "signed.pdf"
    sign_with(profile, form_pdf, out, field_name="Applicant")
    assert verify_pdf(out)[0].status == UNTRUSTED

    bundle = tmp_path / "bundle.pem"
    bundle.write_bytes(b"junk\n" + write_pem(p12[1], tmp_path / "a.pem").read_bytes())
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    validation.system_trust_roots.cache_clear()
    assert verify_pdf(out)[0].status == VALID
    # --no-system-trust leaves out the system's roots too
    assert verify_pdf(out, system_trust=False)[0].status == UNTRUSTED


def test_system_roots_include_mozilla_without_duplicates(monkeypatch, fresh_roots):
    roots = validation.system_trust_roots()
    assert len({c.dump() for c in roots}) == len(roots)
    # (some Windows roots have several common names: compare the text)
    assert any("Common Name: USERTrust RSA Certification Authority," in c.subject.human_friendly
               for c in roots)


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="Linux/BSD bundle")
def test_unix_bundle(tmp_path, monkeypatch, p12):
    bundle = write_pem(p12[1], tmp_path / "bundle.pem")
    monkeypatch.setattr(osroots, "BUNDLES", (str(tmp_path / "missing.pem"), str(bundle)))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    [cert] = osroots.os_roots()
    assert cert.subject.native["common_name"] == "Alice Example"


def test_broken_store_is_ignored(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    def broken():
        raise RuntimeError("no store")
    for name in ("_unix", "_windows", "_macos"):
        monkeypatch.setattr(osroots, name, broken)
    assert osroots.os_roots() == []


def test_real_store_is_readable():
    """Whatever this machine has: parsed certificates, no exception."""
    roots = osroots.os_roots()
    assert all(c.subject is not None for c in roots)
