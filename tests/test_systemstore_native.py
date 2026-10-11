"""The real Windows certificate store / macOS keychain backends.

These modify the user's certificate store (they import a throwaway identity
and remove it again), so they only run when SLIM_PDF_TEST_SYSTEM_STORE=1,
which CI sets on its Windows and macOS runners.
"""

import os
import subprocess
import sys
import uuid

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from slim_pdf import systemstore
from slim_pdf.config import CertProfile
from slim_pdf.secretstore import Credentials
from slim_pdf.signing import open_signer, sign_pdf

from conftest import validate_all
from pki import MiniPKI

pytestmark = pytest.mark.skipif(
    os.environ.get("SLIM_PDF_TEST_SYSTEM_STORE") != "1"
    or sys.platform not in ("win32", "darwin"),
    reason="needs Windows/macOS and SLIM_PDF_TEST_SYSTEM_STORE=1")

PFX_PASSWORD = "pfx-pass"


def run(*argv):
    r = subprocess.run(argv, capture_output=True, text=True)
    assert r.returncode == 0, f"{argv[0]} failed:\n{r.stdout}\n{r.stderr}"
    return r.stdout


@pytest.fixture(scope="module")
def ca():
    return MiniPKI(port=1)


@pytest.fixture(scope="module")
def store(ca, tmp_path_factory):
    """Import an EC and an RSA identity into the OS store once; yields
    {kind: (cn, certificate)}.

    Both go in up front: the macOS Security framework caches the keychain
    search list per process, so a keychain added after the first search
    may not be seen.
    """
    tmp = tmp_path_factory.mktemp("store")
    made = {}
    for kind in ("ec", "rsa"):
        key = (ec.generate_private_key(ec.SECP256R1()) if kind == "ec"
               else rsa.generate_private_key(public_exponent=65537, key_size=2048))
        cn = f"slim-pdf test {kind} {uuid.uuid4().hex[:8]}"
        data, cert = ca.issue(cn, PFX_PASSWORD.encode(), key=key, chain=False, legacy=True)
        pfx = tmp / f"{kind}.p12"
        pfx.write_bytes(data)
        made[kind] = (cn, cert, pfx)

    if sys.platform == "win32":
        try:
            for _, _, pfx in made.values():
                run("certutil", "-f", "-user", "-p", PFX_PASSWORD,
                    "-csp", "Microsoft Software Key Storage Provider",
                    "-importpfx", "My", str(pfx), "NoRoot")
            yield {k: (cn, cert) for k, (cn, cert, _) in made.items()}
        finally:
            for _, cert, _ in made.values():
                subprocess.run(["certutil", "-user", "-delstore", "My",
                                cert.fingerprint(hashes.SHA1()).hex().upper()],
                               capture_output=True)
    else:
        kc = tmp / "slim-pdf-test.keychain-db"
        kc_pw = "kc-pass"
        old = [line.strip().strip('"') for line in
               run("security", "list-keychains", "-d", "user").splitlines() if line.strip()]
        run("security", "create-keychain", "-p", kc_pw, str(kc))
        try:
            run("security", "unlock-keychain", "-p", kc_pw, str(kc))
            run("security", "set-keychain-settings", str(kc))  # no auto-lock
            for _, _, pfx in made.values():
                run("security", "import", str(pfx), "-k", str(kc), "-P", PFX_PASSWORD, "-A")
            run("security", "set-key-partition-list", "-S", "apple-tool:,apple:,unsigned:",
                "-s", "-k", kc_pw, str(kc))
            run("security", "list-keychains", "-d", "user", "-s", str(kc), *old)
            yield {k: (cn, cert) for k, (cn, cert, _) in made.items()}
        finally:
            subprocess.run(["security", "list-keychains", "-d", "user", "-s", *old])
            subprocess.run(["security", "delete-keychain", str(kc)])


@pytest.fixture(params=["ec", "rsa"])
def identity(request, store):
    """(cn, certificate) of an identity in the OS store."""
    return store[request.param]


def test_listed(identity):
    cn, cert = identity
    backend = systemstore.get_backend()
    try:
        ids = backend.identities()
        match = systemstore.select(ids, cn)
        assert match.cert.dump() == cert.public_bytes(serialization.Encoding.DER)
        assert not match.on_token
    finally:
        backend.close()


def test_sign_with_os_store(identity, ca, form_pdf, tmp_path, monkeypatch):
    cn, _ = identity
    if sys.platform == "win32":  # exercise the PIN-dialog parent window path
        import ctypes

        monkeypatch.setattr(systemstore, "parent_window", ctypes.windll.user32.GetDesktopWindow())
    profile = CertProfile(name="store", type="system", system_cert=cn)
    out = tmp_path / "out.pdf"
    with open_signer(profile, Credentials()) as signer:
        sign_pdf(form_pdf, out, signer, profile, field_name="Applicant")
    [(_, st)] = validate_all(out, ca.root)
    assert st.intact and st.valid and st.bottom_line
