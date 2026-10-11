"""Filling in form fields (forms.py)."""

import pytest
from conftest import (
    add_form_fields,
    add_sig_field,
    make_encrypted,
    make_pdf,
    render_page,
    sign_with,
    text_bbox,
    validate_all,
)
from pyhanko.sign import fields
from test_signing import certify

from slim_pdf import forms, validation
from slim_pdf.signing import PasswordRequired


def by_name(data, password=None):
    return {f.name: f for f in forms.form_fields(data, password)}


def test_lists_the_fields(fill_pdf):
    found = by_name(fill_pdf.read_bytes())
    assert list(found) == ["name", "address", "agree", "size", "country", "colour", "fixed",
                           "person.first"]
    kinds = {n: f.kind for n, f in found.items()}
    assert kinds == {"name": "text", "address": "text", "agree": "checkbox", "size": "radio",
                     "country": "choice", "colour": "choice", "fixed": "text",
                     "person.first": "text"}
    assert found["address"].multiline and not found["name"].multiline
    assert found["name"].label == "Your name" and found["address"].label == ""
    assert found["agree"].value is False and found["agree"].options == [("Yes", "Yes")]
    assert found["size"].options == [("S", "S"), ("L", "L")] and found["size"].value == ""
    assert found["country"].combo and found["country"].options == [("de", "Germany"),
                                                                   ("fr", "France")]
    assert not found["colour"].combo
    assert found["fixed"].value == "constant" and not found["fixed"].fillable
    assert found["name"].page == 0 and found["name"].widgets[0].rect == (50, 760, 300, 780)
    assert len(found["size"].widgets) == 2


def test_fill_every_kind(fill_pdf):
    data = fill_pdf.read_bytes()
    values = {"name": "Jürgen Müller – 100 €", "address": "Street 1\n12345 Town",
              "agree": True, "size": "L", "country": "fr", "colour": "green",
              "person.first": "Ann"}
    out = forms.fill(data, values)
    assert out.startswith(data)  # an incremental update
    found = by_name(out)
    assert {n: found[n].value for n in values} == values
    assert found["fixed"].value == "constant"
    # and back: unchecked, no radio button
    again = by_name(forms.fill(out, {"agree": False, "size": ""}))
    assert again["agree"].value is False and again["size"].value == ""


def test_filled_text_is_drawn(fill_pdf, tmp_path):
    """The new appearance shows the text where the field is."""
    out = tmp_path / "out.pdf"
    out.write_bytes(forms.fill(fill_pdf.read_bytes(), {"name": "Hello"}))
    page = render_page(out, scale=1)
    assert text_bbox(page, (50, 842 - 780, 300, 842 - 760)) is not None
    assert text_bbox(render_page(fill_pdf, scale=1), (52, 842 - 778, 298, 842 - 762)) is None


def test_appearance_streams(fill_pdf):
    import io

    from pyhanko.pdf_utils.reader import PdfFileReader

    out = forms.fill(fill_pdf.read_bytes(), {"name": "a(b)\\ €", "agree": True, "size": "S"})
    r = PdfFileReader(io.BytesIO(out))
    form = {str(f.get_object()["/T"]): f.get_object() for f in r.root["/AcroForm"]["/Fields"]}
    stream = form["name"]["/AP"]["/N"]
    assert b"/Helv 12 Tf" in stream.data  # the field's font, auto size capped
    assert rb"(a\(b\)\\ \200) Tj" in stream.data  # escaped, Windows-1252
    assert form["agree"]["/AS"] == "/Yes" and form["agree"]["/V"] == "/Yes"
    s, big = [k.get_object() for k in form["size"]["/Kids"]]
    assert (s["/AS"], big["/AS"], form["size"]["/V"]) == ("/S", "/Off", "/S")


@pytest.mark.parametrize("values, message", [
    ({"nope": "x"}, "no form field named 'nope'"),
    ({"fixed": "x"}, "fixed is read-only"),
    ({"country": "it"}, "not one of its choices"),
    ({"size": "M"}, "not one of its choices"),
    ({"name": "漢字"}, "can't show these characters: 字漢|can't show these characters: 漢字"),
])
def test_refused_values(fill_pdf, values, message):
    with pytest.raises(forms.FormError, match=message):
        forms.fill(fill_pdf.read_bytes(), values)


def test_signatures_stay_valid(fill_pdf, profile, p12, tmp_path):
    """Filling in fields after signing is an allowed change. (Not for radio
    buttons: pyHanko doesn't accept changes to a field's separate widgets,
    so the GUI checks the signatures before filling.)"""
    with_sig = add_sig_field(fill_pdf, tmp_path / "sig.pdf", "Signer", 0, (300, 100, 500, 150))
    signed = tmp_path / "signed.pdf"
    sign_with(profile, with_sig, signed, field_name="Signer")
    out = tmp_path / "filled.pdf"
    out.write_bytes(forms.fill(signed.read_bytes(), {
        "name": "Ann", "address": "x\ny", "agree": True, "country": "de",
        "colour": "blue"}))
    [(_name, status)] = validate_all(out, p12[1])
    assert status.bottom_line
    [report] = validation.verify_pdf(out.read_bytes(), trust=[])
    assert report.status in (validation.VALID, validation.UNTRUSTED) and report.changed_later


def test_locked_fields_are_refused(fill_pdf, profile, tmp_path):
    lock = fields.FieldMDPSpec(fields.FieldMDPAction.INCLUDE, ["name"])
    with_sig = add_sig_field(fill_pdf, tmp_path / "sig.pdf", "Signer", 0, (300, 100, 500, 150),
                             field_mdp_spec=lock)
    signed = tmp_path / "signed.pdf"
    sign_with(profile, with_sig, signed, field_name="Signer")
    found = by_name(signed.read_bytes())
    assert found["name"].locked and not found["name"].fillable
    assert not found["address"].locked
    with pytest.raises(forms.FormError, match="name is locked by a signature"):
        forms.fill(signed.read_bytes(), {"name": "Ann"})


def test_no_changes_certification_is_refused(fill_pdf, profile, tmp_path):
    certified = certify(fill_pdf, tmp_path / "cert.pdf", profile, fields.MDPPerm.NO_CHANGES)
    with pytest.raises(forms.FormError, match="certified"):
        forms.fill(certified.read_bytes(), {"name": "Ann"})


def test_encrypted(fill_pdf, tmp_path):
    enc = make_encrypted(fill_pdf, tmp_path / "enc.pdf", "pw")
    with pytest.raises(PasswordRequired):
        forms.fill(enc.read_bytes(), {"name": "Ann"})
    out = forms.fill(enc.read_bytes(), {"name": "Zebrafish"}, password="pw")
    assert b"Zebrafish" not in out  # still encrypted
    assert by_name(out, "pw")["name"].value == "Zebrafish"


def test_no_form(blank_pdf):
    assert forms.form_fields(blank_pdf.read_bytes()) == []
    with pytest.raises(forms.FormError, match="no form field"):
        forms.fill(blank_pdf.read_bytes(), {"x": "y"})


def test_fields_on_a_later_page(tmp_path):
    pdf = add_form_fields(make_pdf(tmp_path / "b.pdf", pages=3), tmp_path / "f.pdf", page=2)
    assert {f.page for f in forms.form_fields(pdf.read_bytes())} == {2}


def test_auto_size_shrinks_long_text(fill_pdf):
    """Single-line text with font size 0 shrinks to the field's width."""
    import io

    from pyhanko.pdf_utils.reader import PdfFileReader

    out = forms.fill(fill_pdf.read_bytes(), {"name": "x" * 120})
    r = PdfFileReader(io.BytesIO(out))
    name = r.root["/AcroForm"]["/Fields"][0].get_object()
    data = name["/AP"]["/N"].data
    size = float(data.split(b" Tf")[0].split()[-1])
    assert 4 <= size < 6  # 120 x at 500/1000 em must fit 244 pt
