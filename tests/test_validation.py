"""Signature validation: pdfsigner.validation and `pdfsigner verify`."""

import io

import pytest
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.generic import NameObject, NumberObject

from pdfsigner import cli
from pdfsigner.config import CertProfile, Config
from pdfsigner.signing import SigningError
from pdfsigner.validation import INVALID, UNTRUSTED, VALID, verify_pdf

from conftest import make_encrypted, sign_with, write_pem


@pytest.fixture
def signed(profile, form_pdf, tmp_path):
    out = tmp_path / "signed.pdf"
    sign_with(profile, form_pdf, out, field_name="Applicant")
    return out


@pytest.fixture
def alice_pem(p12, tmp_path):
    return write_pem(p12[1], tmp_path / "alice.pem")


def test_unsigned_document(blank_pdf):
    assert verify_pdf(blank_pdf) == []


def test_self_signed_is_untrusted(signed):
    [r] = verify_pdf(signed)
    assert r.status == UNTRUSTED and not r.ok
    assert r.field == "Applicant"
    assert "Alice Example" in r.signer
    assert r.coverage == "entire document"
    assert r.reason == "Test"
    assert r.signed_at is not None and r.timestamp is None
    assert r.problems == ["the certificate is self-signed and not in the trust list"]


def test_trusted_root_makes_it_valid(signed, alice_pem):
    [r] = verify_pdf(signed, trust=[alice_pem])
    assert r.status == VALID and r.ok and r.problems == []
    [r] = verify_pdf(signed, trust=[alice_pem], system_trust=False)
    assert r.status == VALID
    assert "Bottom line" in r.details


def test_two_signers_and_later_changes(profile, p12_bob, signed, alice_pem, tmp_path):
    bob = CertProfile(name="bob", path=str(p12_bob[0]), secret="none")
    sign_with(bob, signed, signed, field_name="Supervisor")
    bob_pem = write_pem(p12_bob[1], tmp_path / "bob.pem")
    first, second = verify_pdf(signed, trust=[alice_pem, bob_pem])
    assert (first.field, first.status) == ("Applicant", VALID)
    assert (second.field, second.status) == ("Supervisor", VALID)
    assert first.coverage.startswith("revision ")
    assert "later changes: form filling and signatures" in first.coverage
    assert second.coverage == "entire document"
    # only Alice trusted: Bob's signature is untrusted, Alice's still valid
    first, second = verify_pdf(signed, trust=[alice_pem])
    assert (first.status, second.status) == (VALID, UNTRUSTED)


def test_disallowed_change_is_invalid(signed, alice_pem):
    w = IncrementalPdfFileWriter(io.BytesIO(signed.read_bytes()))
    page = w.root["/Pages"]["/Kids"][0].get_object()
    page[NameObject("/Rotate")] = NumberObject(90)
    w.update_container(page)
    buf = io.BytesIO()
    w.write(buf)
    signed.write_bytes(buf.getvalue())
    [r] = verify_pdf(signed, trust=[alice_pem])
    assert r.status == INVALID
    assert "changed in a way this signature doesn't allow" in " ".join(r.problems)


def test_tampered_bytes_are_invalid(signed, alice_pem):
    data = signed.read_bytes()
    assert b"0 0 595 842]" in data
    tampered = data.replace(b"0 0 595 842]", b"0 0 595 843]", 1)
    [r] = verify_pdf(tampered, trust=[alice_pem])
    assert r.status == INVALID
    assert "the signed content was modified" in r.problems


def test_bad_trust_root(signed, tmp_path):
    bad = tmp_path / "bad.pem"
    bad.write_text("not a certificate")
    with pytest.raises(SigningError, match="cannot load trust root"):
        verify_pdf(signed, trust=[bad])


def test_encrypted(profile, form_pdf, tmp_path, alice_pem):
    enc = make_encrypted(form_pdf, tmp_path / "enc.pdf", "pw")
    out = tmp_path / "enc-signed.pdf"
    sign_with(profile, enc, out, field_name="Applicant", pdf_password="pw")
    [r] = verify_pdf(out, trust=[alice_pem], password="pw")
    assert r.status == VALID


# --- CLI ------------------------------------------------------------------------


def run(*argv):
    return cli.main([str(a) for a in argv])


def test_cli_verify_exit_codes(signed, alice_pem, blank_pdf, cfg_path, capsys):
    assert run("verify", signed) == 4  # untrusted
    out = capsys.readouterr().out
    assert out.startswith("Applicant: UNTRUSTED")
    assert "signer:   Common Name: Alice Example" in out
    assert "covers:   entire document" in out
    assert "reason:   Test" in out
    assert "problem:  the certificate is self-signed and not in the trust list" in out

    assert run("verify", signed, "--trust", alice_pem) == 0
    assert "Applicant: VALID" in capsys.readouterr().out

    assert run("verify", signed, "--trust", alice_pem, "--details") == 0
    assert "Bottom line" in capsys.readouterr().out

    assert run("verify", blank_pdf) == 1
    assert "no signatures" in capsys.readouterr().err

    signed.write_bytes(signed.read_bytes().replace(b"0 0 595 842]", b"0 0 595 843]", 1))
    assert run("verify", signed, "--trust", alice_pem) == 5
    assert "INVALID" in capsys.readouterr().out


def test_cli_trust_from_config(signed, alice_pem, cfg_path):
    Config(path=cfg_path, trust=[str(alice_pem)]).save()
    assert run("verify", signed) == 0
    assert run("verify", signed, "--no-system-trust") == 0
    Config(path=cfg_path).save()
    assert run("verify", signed, "--no-system-trust") == 4


def test_cli_verify_encrypted_prompts(profile, form_pdf, tmp_path, alice_pem, monkeypatch, capsys):
    enc = make_encrypted(form_pdf, tmp_path / "enc.pdf", "pw")
    out = tmp_path / "enc-signed.pdf"
    sign_with(profile, enc, out, field_name="Applicant", pdf_password="pw")
    asked = []
    monkeypatch.setattr(cli, "_ask", lambda p: asked.append(p) or "pw")
    assert run("verify", out, "--trust", alice_pem) == 0
    assert asked == ["PDF password"]
    assert run("verify", out, "--trust", alice_pem, "--password", "pw") == 0
    assert len(asked) == 1


def test_cli_output_escapes_text_from_the_pdf(profile, blank_pdf, tmp_path, cfg_path, capsys):
    """Names and reasons come from the document: escape sequences in them
    must not reach the terminal (e.g. to overwrite a line with "VALID")."""
    profile.reason = "x\x1b[1A\x1b[2K\rContract: VALID"
    out = tmp_path / "evil.pdf"
    sign_with(profile, blank_pdf, out, field_name="Contract\x1b[8m", page=0, box=(50, 50, 200, 100))
    assert run("verify", out) == 4
    text = capsys.readouterr().out
    assert "\x1b" not in text and "\r" not in text
    assert text.startswith("Contract\\x1b[8m: UNTRUSTED")
    assert "reason:   x\\x1b[1A\\x1b[2K\\rContract: VALID" in text
    assert run("fields", out) == 0
    assert "\x1b" not in capsys.readouterr().out


# --- why "untrusted" / "invalid" -------------------------------------------------------


def _ca_profile(tmp_path, ca, cn="Carol CA-issued"):
    from conftest import PASSPHRASE

    data, cert = ca.issue(cn, PASSPHRASE.encode())
    p = tmp_path / "carol.p12"
    p.write_bytes(data)
    return CertProfile(name="carol", path=str(p)), cert


def test_ca_issued_but_untrusted_root(form_pdf, tmp_path):
    from pki import MiniPKI

    ca = MiniPKI(port=1)
    profile, _cert = _ca_profile(tmp_path, ca)
    out = tmp_path / "s.pdf"
    sign_with(profile, form_pdf, out, field_name="Applicant")
    [r] = verify_pdf(out)
    assert r.status == UNTRUSTED
    assert r.problems == ["the certificate doesn't chain to a trusted root"]
    root = write_pem(ca.root, tmp_path / "root.pem")
    [r] = verify_pdf(out, trust=[root])
    assert r.status == VALID


def test_untrusted_timestamp_authority(profile, blank_pdf, alice_pem, tmp_path, monkeypatch):
    """A timestamp from an unknown TSA: not invalid, but not trusted either,
    and its time isn't presented as the signing time."""
    from pyhanko.keys import load_cert_from_pemder
    from pyhanko.sign.timestamps.dummy_client import DummyTimeStamper

    from pdfsigner import signing
    from pki import make_tsa

    tsa_cert, tsa_key = make_tsa()
    tsa_pem = write_pem(tsa_cert, tmp_path / "tsa.pem")
    monkeypatch.setattr(signing.timestamps, "HTTPTimeStamper", lambda url: DummyTimeStamper(
        load_cert_from_pemder(str(tsa_pem)), tsa_key))
    profile.timestamp_url = "http://tsa.example"
    out = tmp_path / "ts.pdf"
    sign_with(profile, blank_pdf, out)
    [r] = verify_pdf(out, trust=[alice_pem])
    assert r.status == UNTRUSTED and r.timestamp is None
    assert r.problems == ["the timestamp authority isn't trusted"]
    [r] = verify_pdf(out, trust=[alice_pem, tsa_pem])
    assert r.status == VALID and r.timestamp is not None
