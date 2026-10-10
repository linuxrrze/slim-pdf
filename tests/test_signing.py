import io
import sys
import types
from pathlib import Path

import pytest
from pyhanko import stamp
from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign import fields, signers

from pdfsigner import signing
from pdfsigner.config import CertProfile
from pdfsigner.gui.document import PdfDocument
from pdfsigner.secretstore import Credentials
from pdfsigner.signing import (
    SigningBlocked,
    SigningError,
    box_from_anchor,
    build_stamp_style,
    check_target,
    image_aspect,
    inspect_pdf,
    open_signer,
    sign_pdf,
    unique_field_name,
)

from pki import make_tsa
from conftest import (
    PASSPHRASE,
    add_sig_field,
    asn1_cert,
    blue_bbox,
    draw_signature_image,
    green_bbox,
    make_encrypted,
    make_pdf,
    make_vector_signature,
    render_page,
    text_bbox,
    validate_all,
)


def signer_for(profile, secret=PASSPHRASE):
    return open_signer(profile, Credentials(secret=secret))


def certify(src, dst, profile, perm):
    with signer_for(profile) as s:
        w = IncrementalPdfFileWriter(io.BytesIO(src.read_bytes()))
        meta = signers.PdfSignatureMetadata(field_name="Author", certify=True,
                                            docmdp_permissions=perm)
        with open(dst, "wb") as f:
            signers.PdfSigner(meta, s).sign_pdf(w, output=f)
    return dst


# --- basic signing -------------------------------------------------------------


def test_new_visible_field(profile, blank_pdf, p12, tmp_path):
    out = tmp_path / "out.pdf"
    aspect = image_aspect(profile.image)
    assert aspect == pytest.approx(3.0)
    box = box_from_anchor(300, 80, 180, aspect)
    with signer_for(profile) as s:
        name = sign_pdf(blank_pdf, out, s, profile, page=1, box=box)
    assert name == "Signature1"

    f = inspect_pdf(out).field("Signature1")
    assert f.filled and f.page == 1
    assert f.rect == pytest.approx((300, 80, 480, 140), abs=0.01)

    [(_, st)] = validate_all(out, p12[1])
    assert st.intact and st.valid and st.bottom_line


def test_signature_metadata(profile, blank_pdf, tmp_path):
    profile.location, profile.contact_info = "Erlangen", "alice@example.org"
    out = tmp_path / "out.pdf"
    with signer_for(profile) as s:
        sign_pdf(blank_pdf, out, s, profile, page=0, box=(10, 10, 160, 60))
    with open(out, "rb") as f:
        [es] = PdfFileReader(f).embedded_signatures
        d = es.sig_object
        assert d["/Reason"] == "Test"
        assert d["/Location"] == "Erlangen"
        assert d["/ContactInfo"] == "alice@example.org"
        assert d["/SubFilter"] == "/ETSI.CAdES.detached"  # PAdES


def test_named_new_field(profile, blank_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    with signer_for(profile) as s:
        assert sign_pdf(blank_pdf, out, s, profile, field_name="Approval",
                        page=0, box=(10, 10, 160, 60)) == "Approval"
    assert inspect_pdf(out).field("Approval").filled


def test_invisible_signature(profile, blank_pdf, p12, tmp_path):
    out = tmp_path / "out.pdf"
    with signer_for(profile) as s:
        sign_pdf(blank_pdf, out, s, profile)
    f = inspect_pdf(out).field("Signature1")
    assert f.filled and f.rect is None
    [(_, st)] = validate_all(out, p12[1])
    assert st.bottom_line


def test_in_place_leaves_no_temp_files(profile, blank_pdf, tmp_path):
    before = set(tmp_path.iterdir())
    with signer_for(profile) as s:
        sign_pdf(blank_pdf, blank_pdf, s, profile)
    assert set(tmp_path.iterdir()) == before
    assert inspect_pdf(blank_pdf).field("Signature1").filled



def test_locked_output_gives_clear_error(profile, blank_pdf, tmp_path, monkeypatch):
    """Windows refuses os.replace onto a file open elsewhere (e.g. in Acrobat)."""
    import pdfsigner.signing as signing

    def locked(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(signing.os, "replace", locked)
    original = blank_pdf.read_bytes()
    before = set(tmp_path.iterdir())
    with signer_for(profile) as s, pytest.raises(SigningError, match="open in another program"):
        sign_pdf(blank_pdf, blank_pdf, s, profile)
    assert blank_pdf.read_bytes() == original
    assert set(tmp_path.iterdir()) == before  # temp file cleaned up

# --- multiple signatures ---------------------------------------------------------


def test_prepared_field_then_second_signer(profile, form_pdf, p12, p12_bob, tmp_path):
    info = inspect_pdf(form_pdf)
    assert [(f.name, f.filled, f.page) for f in info.fields] == [
        ("Applicant", False, 1), ("Supervisor", False, 1)]

    step1 = tmp_path / "step1.pdf"
    with signer_for(profile) as s:
        sign_pdf(form_pdf, step1, s, profile, field_name="Applicant")

    bob = CertProfile(name="bob", path=str(p12_bob[0]), secret="none",
                      stamp_text="Approved by %(signer)s")
    with signer_for(bob) as s:
        sign_pdf(step1, step1, s, bob, field_name="Supervisor")

    assert all(f.filled for f in inspect_pdf(step1).fields)
    results = validate_all(step1, p12[1], p12_bob[1])
    assert [n for n, _ in results] == ["Applicant", "Supervisor"]
    for _, st in results:
        # the earlier signature only sees allowed (signing) changes
        assert st.intact and st.valid and st.bottom_line, st.pretty_print_details()


def test_three_signatures_mixed(profile, form_pdf, p12, tmp_path):
    out = tmp_path / "out.pdf"
    with signer_for(profile) as s:
        sign_pdf(form_pdf, out, s, profile, field_name="Applicant")
        sign_pdf(out, out, s, profile, page=0, box=(50, 50, 200, 100))
        sign_pdf(out, out, s, profile)  # invisible
    results = validate_all(out, p12[1])
    assert [n for n, _ in results] == ["Applicant", "Signature1", "Signature2"]
    assert all(st.bottom_line for _, st in results)


def test_unique_names_continue_after_existing(profile, blank_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    with signer_for(profile) as s:
        for _ in range(3):
            sign_pdf(blank_pdf if not out.exists() else out, out, s, profile)
    names = [f.name for f in inspect_pdf(out).fields]
    assert names == ["Signature1", "Signature2", "Signature3"]
    assert unique_field_name(inspect_pdf(out)) == "Signature4"


# --- target checks -----------------------------------------------------------------


@pytest.mark.parametrize("kwargs, message", [
    (dict(field_name="Applicant", page=1, box=(0, 0, 10, 10)), "already exists"),
    (dict(page=2, box=(0, 0, 10, 10)), "page out of range"),
    (dict(page=-1, box=(0, 0, 10, 10)), "page out of range"),
    (dict(page=None, box=(0, 0, 10, 10)), "page out of range"),
    (dict(page=0, box=(50, 50, 10, 60)), "x1 < x2"),
    (dict(page=0, box=(10, 60, 50, 50)), "x1 < x2"),
])
def test_check_target_rejects(form_pdf, kwargs, message):
    info = inspect_pdf(form_pdf)
    kwargs.setdefault("field_name", None)
    with pytest.raises(SigningError, match=message):
        check_target(info, **kwargs)


def test_refuses_to_resign_field(profile, form_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    with signer_for(profile) as s:
        sign_pdf(form_pdf, out, s, profile, field_name="Applicant")
        with pytest.raises(SigningError, match="already signed"):
            sign_pdf(out, out, s, profile, field_name="Applicant")


# --- certification and locks ----------------------------------------------------------


def test_no_changes_certification_cannot_be_signed(profile, form_pdf, tmp_path):
    certified = certify(form_pdf, tmp_path / "cert.pdf", profile, fields.MDPPerm.NO_CHANGES)
    info = inspect_pdf(certified)
    assert info.certification == fields.MDPPerm.NO_CHANGES and info.frozen
    with signer_for(profile) as s:
        for kwargs in ({}, {"field_name": "Applicant"}, {"allow_breaking": True}):
            with pytest.raises(SigningError, match="cannot be signed again"):
                sign_pdf(certified, tmp_path / "x.pdf", s, profile, **kwargs)
    assert not (tmp_path / "x.pdf").exists()


def test_signing_errors_are_not_reported_as_load_errors(profile):
    """Regression: a ValueError inside the with-block was blamed on the passphrase."""
    with pytest.raises(ValueError, match="boom"):
        with signer_for(profile):
            raise ValueError("boom")


def test_certified_fill_forms_allows_signing(profile, form_pdf, p12, tmp_path):
    certified = certify(form_pdf, tmp_path / "cert.pdf", profile, fields.MDPPerm.FILL_FORMS)
    assert inspect_pdf(certified).blocker("Applicant") is None
    out = tmp_path / "out.pdf"
    with signer_for(profile) as s:
        sign_pdf(certified, out, s, profile, field_name="Applicant")
    assert all(st.bottom_line for _, st in validate_all(out, p12[1]))


def test_certified_new_visible_field_is_blocked(profile, form_pdf, p12, tmp_path):
    """A new visible field breaks any certification (pyHanko's diff policy);
    an invisible signature doesn't."""
    certified = certify(form_pdf, tmp_path / "cert.pdf", profile, fields.MDPPerm.FILL_FORMS)
    info = inspect_pdf(certified)
    reason = info.blocker(None, new_visible=True)
    assert "certified" in reason and "'Applicant'" in reason
    with signer_for(profile) as s:
        with pytest.raises(SigningBlocked, match="breaks the certification"):
            sign_pdf(certified, tmp_path / "x.pdf", s, profile, page=0, box=(50, 50, 200, 100))
        assert not (tmp_path / "x.pdf").exists()
        # with --force it is signed, and the certification is indeed broken
        sign_pdf(certified, tmp_path / "forced.pdf", s, profile, page=0, box=(50, 50, 200, 100),
                 allow_breaking=True)
        sign_pdf(certified, tmp_path / "invisible.pdf", s, profile)
    assert not all(st.bottom_line for _, st in validate_all(tmp_path / "forced.pdf", p12[1]))
    assert all(st.bottom_line for _, st in validate_all(tmp_path / "invisible.pdf", p12[1]))


@pytest.mark.parametrize("action, names, locked", [
    (fields.FieldMDPAction.INCLUDE, ["Second"], {"Second"}),
    (fields.FieldMDPAction.EXCLUDE, ["Second"], {"Third"}),
    (fields.FieldMDPAction.ALL, None, {"Second", "Third"}),
])
def test_field_locks(profile, blank_pdf, tmp_path, action, names, locked):
    src = add_sig_field(blank_pdf, tmp_path / "a.pdf", "First", 0, (10, 10, 100, 50),
                        field_mdp_spec=fields.FieldMDPSpec(action, fields=names))
    src = add_sig_field(src, tmp_path / "b.pdf", "Second", 0, (110, 10, 200, 50))
    src = add_sig_field(src, tmp_path / "c.pdf", "Third", 0, (210, 10, 300, 50))
    out = tmp_path / "out.pdf"
    with signer_for(profile) as s:
        sign_pdf(src, out, s, profile, field_name="First")
        info = inspect_pdf(out)
        assert {f.name for f in info.fields if f.locked} == locked
        for name in locked:
            with pytest.raises(SigningBlocked, match="locked"):
                sign_pdf(out, tmp_path / "x.pdf", s, profile, field_name=name)


def test_force_signing_locked_field_breaks_earlier_signature(profile, blank_pdf, p12, tmp_path):
    lock = fields.FieldMDPSpec(fields.FieldMDPAction.INCLUDE, fields=["Second"])
    src = add_sig_field(blank_pdf, tmp_path / "a.pdf", "First", 0, (10, 10, 100, 50),
                        field_mdp_spec=lock)
    src = add_sig_field(src, tmp_path / "b.pdf", "Second", 0, (110, 10, 200, 50))
    out = tmp_path / "out.pdf"
    with signer_for(profile) as s:
        sign_pdf(src, out, s, profile, field_name="First")
        sign_pdf(out, out, s, profile, field_name="Second", allow_breaking=True)
    results = dict(validate_all(out, p12[1]))
    assert results["Second"].bottom_line
    assert not results["First"].bottom_line  # as warned


# --- encrypted documents -----------------------------------------------------------------


def test_encrypted_pdf(profile, form_pdf, p12, tmp_path):
    enc = make_encrypted(form_pdf, tmp_path / "enc.pdf", "docpw")
    with pytest.raises(SigningError, match="password is required"):
        inspect_pdf(enc)
    with pytest.raises(SigningError, match="wrong PDF password"):
        inspect_pdf(enc, "nope")
    assert inspect_pdf(enc, "docpw").encrypted

    out = tmp_path / "out.pdf"
    with signer_for(profile) as s:
        sign_pdf(enc, out, s, profile, field_name="Applicant", pdf_password="docpw")
    assert inspect_pdf(out, "docpw").field("Applicant").filled
    [(_, st)] = validate_all(out, p12[1], password="docpw")
    assert st.bottom_line


# --- signer loading ---------------------------------------------------------------------


def test_wrong_passphrase(profile):
    with pytest.raises(SigningError, match="wrong passphrase"):
        with signer_for(profile, secret="nope"):
            pass


def test_missing_p12(tmp_path):
    p = CertProfile(name="x", path=str(tmp_path / "missing.p12"))
    with pytest.raises(SigningError, match="cannot read"):
        with signer_for(p):
            pass


def test_p12_from_bytes_ignores_path(p12):
    p = CertProfile(name="x", path="/does/not/exist.p12")
    with open_signer(p, Credentials(secret=PASSPHRASE, pkcs12_data=p12[0].read_bytes())) as s:
        assert "Alice" in s.signing_cert.subject.human_friendly


class FakeToken:
    """Stands in for a python-pkcs11 Token: records how it was opened."""

    def __init__(self, label, flags):
        self.label, self.flags, self.opened = label, flags, []

    def open(self, **kw):
        self.opened.append(kw)
        return FakeSession()


class FakeSession:
    closed = False

    def close(self):
        FakeSession.closed = True


@pytest.fixture
def fake_token(monkeypatch):
    """A fake PKCS#11 module with the given tokens; the key and certificate
    lookup is stubbed (test_pkcs11_softhsm.py uses real tokens)."""
    import pkcs11
    from pkcs11 import Attribute, TokenFlag

    from pdfsigner import signing

    tokens = []
    calls = {}

    class FakeSigner:
        signing_cert = None

        def __init__(self, session, **kw):
            calls["signer"] = kw

    monkeypatch.setattr(pkcs11, "lib", lambda path: types.SimpleNamespace(
        get_tokens=lambda: list(tokens), path=path))
    monkeypatch.setattr("pyhanko.sign.pkcs11.PKCS11Signer", FakeSigner)
    key = {Attribute.LABEL: "SIGN key", Attribute.ID: b"\x02",
           Attribute.ALWAYS_AUTHENTICATE: False}
    monkeypatch.setattr(signing, "_find_token_key", lambda session, profile: (key, "CERT"))
    monkeypatch.setattr(signing, "_use_raw_mechanism", lambda session, key: True)
    FakeSession.closed = False
    return types.SimpleNamespace(tokens=tokens, calls=calls, key=key, flags=TokenFlag,
                                 PROTECTED_AUTH=pkcs11.PROTECTED_AUTH)


def _tok_profile(**kw):
    return CertProfile(name="tok", type="pkcs11", pkcs11_lib="/lib/opensc.so", key_id="02",
                       **kw)


def test_pkcs11_session_is_opened_and_closed(fake_token):
    from pkcs11 import Attribute

    init = fake_token.flags.TOKEN_INITIALIZED
    card = FakeToken("My Card", init)
    fake_token.tokens[:] = [FakeToken("Other", init), card, FakeToken("", 0)]
    fake_token.key[Attribute.ALWAYS_AUTHENTICATE] = True
    with open_signer(_tok_profile(token_label="My Card"), Credentials(secret="123456")) as s:
        assert not FakeSession.closed
    assert FakeSession.closed
    assert card.opened == [{"user_pin": "123456"}]
    assert fake_token.calls["signer"] == dict(
        signing_cert="CERT", key_label="SIGN key", key_id=b"\x02", use_raw_mechanism=True,
        base_sign_kwargs={"pin": "123456"})


def test_pkcs11_pin_pad_and_no_login(fake_token):
    """No PIN source ('none'): log in on the reader's PIN pad if there is
    one, else open without logging in."""
    flags = fake_token.flags
    pinpad = FakeToken("Card", flags.TOKEN_INITIALIZED | flags.PROTECTED_AUTHENTICATION_PATH)
    fake_token.tokens[:] = [pinpad]
    with open_signer(_tok_profile(secret="none"), Credentials()):
        pass
    assert pinpad.opened == [{"user_pin": fake_token.PROTECTED_AUTH}]
    plain = FakeToken("Card", flags.TOKEN_INITIALIZED)
    fake_token.tokens[:] = [plain]
    with open_signer(_tok_profile(secret="none"), Credentials()):
        pass
    assert plain.opened == [{}]


def test_pkcs11_pin_for_every_signature_needs_a_pin_source(fake_token):
    from pkcs11 import Attribute

    fake_token.tokens[:] = [FakeToken("Card", fake_token.flags.TOKEN_INITIALIZED
                                      | fake_token.flags.PROTECTED_AUTHENTICATION_PATH)]
    fake_token.key[Attribute.ALWAYS_AUTHENTICATE] = True
    with pytest.raises(SigningError, match="PIN for every signature.*secret = prompt"):
        with open_signer(_tok_profile(secret="none"), Credentials()):
            pass
    assert FakeSession.closed


@pytest.mark.parametrize("tokens, label, message", [
    ([], "", "no token found.*card inserted.*gpgconf --kill scdaemon"),
    ([("A", 1), ("B", 1)], "", r"2 tokens found \('A', 'B'\); set token_label"),
    ([("A", 1)], "Z", r"no token labelled 'Z' \(tokens: 'A'\)"),
    ([("A", 0)], "A", "no token found"),  # not initialised: an empty slot
])
def test_pkcs11_token_selection_errors(fake_token, tokens, label, message):
    init = fake_token.flags.TOKEN_INITIALIZED
    fake_token.tokens[:] = [FakeToken(n, init if i else 0) for n, i in tokens]
    with pytest.raises(SigningError, match=message):
        with open_signer(_tok_profile(token_label=label), Credentials(secret="1")):
            pass


def test_unknown_type():
    with pytest.raises(SigningError, match="unknown certificate type"):
        with open_signer(CertProfile(name="x", type="gpg"), Credentials()):
            pass


# --- timestamps -----------------------------------------------------------------------




def test_timestamp_url_is_used(profile, blank_pdf, p12, tmp_path, monkeypatch):
    from pyhanko.sign.timestamps.dummy_client import DummyTimeStamper

    tsa_cert, tsa_key = make_tsa()
    urls = []

    def fake_http(url):
        urls.append(url)
        return DummyTimeStamper(asn1_cert(tsa_cert), tsa_key)

    monkeypatch.setattr(signing.timestamps, "HTTPTimeStamper", fake_http)
    profile.timestamp_url = "http://tsa.example/rfc3161"
    out = tmp_path / "out.pdf"
    with signer_for(profile) as s:
        sign_pdf(blank_pdf, out, s, profile)
    assert urls == ["http://tsa.example/rfc3161"]
    [(_, st)] = validate_all(out, p12[1], tsa_cert)
    assert st.timestamp_validity is not None and st.timestamp_validity.valid


# --- appearance -------------------------------------------------------------------------


def test_stamp_style_variants(profile, tmp_path):
    assert isinstance(build_stamp_style(profile), stamp.TextStampStyle)
    assert build_stamp_style(profile).border_width == 0
    assert isinstance(build_stamp_style(profile, with_text=False), stamp.StaticStampStyle)
    profile.stamp_text = ""
    assert isinstance(build_stamp_style(profile), stamp.StaticStampStyle)
    text_only = build_stamp_style(profile, with_image=False)
    assert isinstance(text_only, stamp.TextStampStyle) and text_only.border_width == 1
    profile.image = str(tmp_path / "missing.png")
    with pytest.raises(SigningError, match="image not found"):
        build_stamp_style(profile)


def _px_rect(doc_path, page, box, scale):
    """PDF box -> pixel rect on the rendered page."""
    doc = PdfDocument(str(doc_path))
    x, y, w, h = doc.rect_to_display(page, box)
    doc.close()
    return tuple(round(v * scale) for v in (x, y, x + w, y + h))


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_stamp_is_upright_on_rotated_pages(profile, tmp_path, rotate):
    """Regression: pyHanko alone draws the stamp sideways on /Rotate'd pages."""
    src = make_pdf(tmp_path / "r.pdf", pages=1, rotate=rotate)
    doc = PdfDocument(str(src))
    # A 3:1 box as the user sees it on screen.
    box = doc.rect_to_pdf(0, 100, 100, 240, 80)
    doc.close()
    profile.stamp_text = ""
    out = tmp_path / "out.pdf"
    with signer_for(profile) as s:
        sign_pdf(src, out, s, profile, page=0, box=box)

    scale = 2
    img = render_page(out, 0, scale)
    field_px = (200, 200, 680, 360)  # display (100,100) .. (340,180) at scale 2
    ink = blue_bbox(img)
    assert ink is not None
    w, h = ink[2] - ink[0], ink[3] - ink[1]
    assert w > 2 * h, f"stamp not upright: ink {w}x{h}"
    assert field_px[0] - 2 <= ink[0] and ink[2] <= field_px[2] + 2
    assert field_px[1] - 2 <= ink[1] and ink[3] <= field_px[3] + 2


def test_prepared_field_on_rotated_page_is_upright(profile, tmp_path):
    src = make_pdf(tmp_path / "r.pdf", pages=1, rotate=90)
    # field drawn tall in user space = wide on screen
    src = add_sig_field(src, tmp_path / "f.pdf", "Sig", 0, (100, 100, 170, 310))
    profile.stamp_text = ""
    out = tmp_path / "out.pdf"
    with signer_for(profile) as s:
        sign_pdf(src, out, s, profile, field_name="Sig")
    ink = blue_bbox(render_page(out, 0, 2))
    assert ink[2] - ink[0] > 2 * (ink[3] - ink[1])


def test_text_sits_below_image(profile, blank_pdf, tmp_path):
    """Regression: the stamp text used to overlap the signature image."""
    profile.image = str(draw_signature_image(tmp_path / "green.png", color=(20, 150, 40)))
    out = tmp_path / "out.pdf"
    box = (100, 500, 340, 580)  # 240 x 80 pt
    with signer_for(profile) as s:
        sign_pdf(blank_pdf, out, s, profile, page=0, box=box)
    scale = 3
    img = render_page(out, 0, scale)
    field = _px_rect(out, 0, box, scale)
    image_ink = green_bbox(img, field)
    text = text_bbox(img, field)
    assert image_ink and text
    assert image_ink[3] <= text[1], f"image {image_ink} overlaps text {text}"
    # text is left-aligned at the bottom, image centred above it
    assert text[0] - field[0] < 15 * scale
    image_centre = (image_ink[0] + image_ink[2]) / 2
    assert abs(image_centre - (field[0] + field[2]) / 2) < 15 * scale



# --- vector (PDF) signature images ---------------------------------------------------------


def test_pdf_signature_image_aspect(tmp_path):
    vec = make_vector_signature(tmp_path / "sig.pdf", 200, 50)
    assert image_aspect(vec) == pytest.approx(4.0)


@pytest.mark.parametrize("with_text", [False, True])
def test_pdf_signature_image_is_drawn(profile, p12, blank_pdf, tmp_path, with_text):
    profile.image = str(make_vector_signature(tmp_path / "sig.pdf", 200, 50))
    out = tmp_path / "out.pdf"
    box = box_from_anchor(100, 500, 240, image_aspect(profile.image))
    with signer_for(profile) as s:
        sign_pdf(blank_pdf, out, s, profile, page=0, box=box, with_text=with_text)
    scale = 3
    img = render_page(out, 0, scale)
    field = _px_rect(out, 0, box, scale)
    ink = green_bbox(img, field)
    assert ink is not None, "vector signature not drawn"
    # spans most of the field width (it's scaled, not a raster thumbnail)
    assert ink[2] - ink[0] > 0.6 * (field[2] - field[0])
    assert green_bbox(img) == ink  # nothing outside the field
    if with_text:
        text = text_bbox(img, field)
        assert text and ink[3] <= text[1]
    [(_, st)] = validate_all(out, p12[1])
    assert st.bottom_line


def test_pdf_signature_image_is_vector(profile, blank_pdf, tmp_path):
    """The stamp contains the path itself, not an image XObject."""
    profile.image = str(make_vector_signature(tmp_path / "sig.pdf", 200, 50))
    profile.stamp_text = ""
    out = tmp_path / "out.pdf"
    with signer_for(profile) as s:
        sign_pdf(blank_pdf, out, s, profile, page=0, box=(100, 500, 300, 550))
    data = out.read_bytes()
    assert b"/Subtype /Image" not in data and b"/Subtype/Image" not in data
    with open(out, "rb") as fh:
        r = PdfFileReader(fh)
        sig_field = r.root["/AcroForm"]["/Fields"][0].get_object()
        ap = sig_field["/AP"]["/N"]
        xobjs = ap["/Resources"]["/XObject"]
        assert any(x.get_object()["/Subtype"] == "/Form" for x in xobjs.values())


def test_unreadable_pdf_signature_image(profile, blank_pdf, tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    profile.image = str(bad)
    with signer_for(profile) as s, pytest.raises(SigningError, match="cannot read signature PDF"):
        sign_pdf(blank_pdf, tmp_path / "out.pdf", s, profile, page=0, box=(10, 10, 100, 40))

# --- inspection details -------------------------------------------------------------------


def test_inherited_rotate(tmp_path):
    src = make_pdf(tmp_path / "a.pdf", pages=2)
    w = IncrementalPdfFileWriter(io.BytesIO(src.read_bytes()))
    pages = w.root["/Pages"]
    pages["/Rotate"] = generic.NumberObject(270)
    w.update_container(pages)
    kids = pages["/Kids"]
    for i in range(len(kids)):  # make sure the pages themselves don't override it
        kid = kids[i].get_object() if hasattr(kids[i], "get_object") else kids[i]
        if "/Rotate" in kid:
            del kid["/Rotate"]
            w.update_container(kid)
    out = tmp_path / "b.pdf"
    with open(out, "wb") as f:
        w.write(f)
    assert inspect_pdf(out).page_rotation == [270, 270]


def test_field_page_found_via_annots(form_pdf):
    info = inspect_pdf(form_pdf)
    assert info.page_count == 2
    assert [f.page for f in info.fields] == [1, 1]
    assert info.field("Supervisor").rect == (320, 100, 520, 170)
    assert info.empty_fields == info.fields
    assert info.field("nope") is None
