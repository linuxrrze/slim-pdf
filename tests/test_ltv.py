"""Long-term validation (PAdES B-LT / B-LTA) against a local test CA."""

import pytest
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko.sign.validation.dss import DocumentSecurityStore

from slim_pdf import cli, signing
from slim_pdf.config import CertProfile, Config, load_config
from slim_pdf.signing import SigningError
from slim_pdf.validation import VALID, verify_pdf

from conftest import PASSPHRASE, asn1_cert, sign_with, write_pem
from pki import MiniPKI, make_tsa, serve


@pytest.fixture(scope="module")
def ca():
    pki, server = serve(MiniPKI)
    yield pki
    server.shutdown()


@pytest.fixture
def ca_files(ca, tmp_path):
    ca.revoked.clear()
    data, cert = ca.issue("Carol LTV", PASSPHRASE.encode())
    p12 = tmp_path / "carol.p12"
    p12.write_bytes(data)
    root_pem = write_pem(ca.root, tmp_path / "root.pem")
    profile = CertProfile(name="carol", path=str(p12), secret="none", ltv=True)
    return profile, cert, root_pem


@pytest.fixture
def tsa(monkeypatch, tmp_path):
    from pyhanko.sign.timestamps.dummy_client import DummyTimeStamper

    cert, key = make_tsa()
    monkeypatch.setattr(signing.timestamps, "HTTPTimeStamper",
                        lambda url: DummyTimeStamper(asn1_cert(cert), key))
    return write_pem(cert, tmp_path / "tsa.pem")


def ltv_check(path, *roots):
    """Offline, revocation info *required*: only works if the DSS has it all."""
    with open(path, "rb") as f:
        r = PdfFileReader(f)
        [sig] = r.embedded_regular_signatures
        vc = DocumentSecurityStore.read_dss(r).as_validation_context(dict(
            trust_roots=[asn1_cert(c) for c in roots], allow_fetching=False,
            revocation_mode="require"))
        return validate_pdf_signature(sig, vc)


def test_ltv_embeds_revocation_info(ca, ca_files, form_pdf, tmp_path):
    profile, cert, root_pem = ca_files
    out = tmp_path / "ltv.pdf"
    before = ca.crl_requests
    sign_with(profile, form_pdf, out, field_name="Applicant", trust=[str(root_pem)])
    assert ca.crl_requests > before  # revocation info was fetched while signing

    with open(out, "rb") as f:
        r = PdfFileReader(f)
        assert "/DSS" in r.root
        dss = r.root["/DSS"]
        assert len(dss["/CRLs"]) >= 1 and len(dss["/Certs"]) >= 2

    # verifiable offline from embedded data alone
    count = ca.crl_requests
    [rep] = verify_pdf(out, trust=[root_pem])
    assert rep.status == VALID and rep.ltv
    assert ca.crl_requests == count
    assert ltv_check(out, ca.root).bottom_line


def test_ltv_with_timestamp_is_lta(ca, ca_files, tsa, form_pdf, tmp_path):
    profile, cert, root_pem = ca_files
    profile.timestamp_url = "http://tsa.example"
    out = tmp_path / "lta.pdf"
    sign_with(profile, form_pdf, out, field_name="Applicant", trust=[str(root_pem), str(tsa)])
    with open(out, "rb") as f:
        r = PdfFileReader(f)
        assert len(r.embedded_timestamp_signatures) == 1  # document timestamp (B-LTA)
        assert len(r.embedded_regular_signatures) == 1
    reports = verify_pdf(out, trust=[root_pem, tsa])
    assert [(x.field, x.status) for x in reports] == [("Applicant", VALID)]
    assert reports[0].timestamp is not None and reports[0].ltv


def test_ltv_needs_trusted_root(ca_files, form_pdf, tmp_path):
    profile, _, _ = ca_files
    with pytest.raises(SigningError, match="LTV: .*no issuer matching"):
        sign_with(profile, form_pdf, tmp_path / "x.pdf", field_name="Applicant")
    assert not (tmp_path / "x.pdf").exists()


def test_ltv_refuses_revoked_certificate(ca, ca_files, form_pdf, tmp_path):
    profile, cert, root_pem = ca_files
    ca.revoked.add(cert.serial_number)
    with pytest.raises(SigningError, match="LTV: .*revoked"):
        sign_with(profile, form_pdf, tmp_path / "x.pdf", field_name="Applicant",
                  trust=[str(root_pem)])


def test_ltv_unreachable_revocation_info(ca_files, form_pdf, tmp_path, monkeypatch):
    profile, cert, root_pem = ca_files
    (_, server) = serve(MiniPKI)  # a CA whose CRL server we stop at once
    server.shutdown()
    server.server_close()
    pki2 = MiniPKI(server.server_address[1])
    data, _ = pki2.issue("Dave", PASSPHRASE.encode())
    (tmp_path / "dave.p12").write_bytes(data)
    dave = CertProfile(name="dave", path=str(tmp_path / "dave.p12"), secret="none", ltv=True)
    root2 = write_pem(pki2.root, tmp_path / "root2.pem")
    with pytest.raises(SigningError, match="LTV"):
        sign_with(dave, form_pdf, tmp_path / "x.pdf", field_name="Applicant",
                  trust=[str(root2)])


def test_without_ltv_no_dss(ca, ca_files, form_pdf, tmp_path):
    profile, _, root_pem = ca_files
    profile.ltv = False
    out = tmp_path / "plain.pdf"
    sign_with(profile, form_pdf, out, field_name="Applicant")
    with open(out, "rb") as f:
        assert "/DSS" not in PdfFileReader(f).root
    [rep] = verify_pdf(out, trust=[root_pem])
    assert rep.status == VALID and not rep.ltv
    # requiring revocation info offline fails without LTV data
    with open(out, "rb") as f:
        [sig] = PdfFileReader(f).embedded_regular_signatures
        from pyhanko_certvalidator import ValidationContext
        vc = ValidationContext(trust_roots=[asn1_cert(ca.root)], allow_fetching=False,
                               revocation_mode="require", crls=[], ocsps=[])
        assert not validate_pdf_signature(sig, vc).bottom_line


def test_cli_ltv(ca_files, form_pdf, cfg_path, capsys, monkeypatch):
    profile, _, root_pem = ca_files
    monkeypatch.setattr(cli, "_ask", lambda prompt: PASSPHRASE)
    assert cli.main(["certs", "add", "carol", "--p12", profile.path, "--ltv", "yes"]) == 0
    assert load_config().certs["carol"].ltv is True
    cfg = load_config()
    cfg.trust = [str(root_pem)]
    cfg.save()
    assert cli.main(["sign", str(form_pdf), "--field", "Applicant"]) == 0
    capsys.readouterr()
    assert cli.main(["verify", str(form_pdf.with_name("form-signed.pdf"))]) == 0
    assert "ltv:      validation data embedded" in capsys.readouterr().out
    assert cli.main(["certs", "edit", "carol", "--ltv", "no"]) == 0
    assert load_config().certs["carol"].ltv is False
    assert "ltv" not in Config.__dataclass_fields__  # profile option, not global


def test_ltv_with_smime_certificate(ca, form_pdf, tmp_path):
    """digitalSignature without nonRepudiation (as GEANT Personal CA 4
    issues them) must be accepted for LTV signing and validation."""
    ca.revoked.clear()
    data, _cert = ca.issue("Erin S/MIME", PASSPHRASE.encode(), non_repudiation=False)
    p12 = tmp_path / "erin.p12"
    p12.write_bytes(data)
    root_pem = write_pem(ca.root, tmp_path / "root.pem")
    profile = CertProfile(name="erin", path=str(p12), secret="none", ltv=True)
    out = tmp_path / "ltv.pdf"
    sign_with(profile, form_pdf, out, field_name="Applicant", trust=[str(root_pem)])
    [rep] = verify_pdf(out, trust=[root_pem])
    assert (rep.status, rep.ltv) == (VALID, True)
