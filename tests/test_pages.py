"""Page operations (pdfsigner.pages) and their CLI commands."""

import io

import pypdfium2 as pdfium
import pytest
from pyhanko.pdf_utils.reader import PdfFileReader

from pdfsigner import cli, pages
from pdfsigner.pages import PageError, parse_pages
from pdfsigner.signing import PasswordRequired, SigningError, inspect_pdf
from pdfsigner.validation import INVALID, VALID, verify_pdf

from conftest import make_encrypted, make_pdf, sign_with, write_pem


def info(data, password=None):
    """[(display width, height, rotation)] per page, via pdfium."""
    pdf = pdfium.PdfDocument(data, password=password)
    try:
        return [(round(p.get_width()), round(p.get_height()), p.get_rotation()) for p in pdf]
    finally:
        pdf.close()


def numbered_pdf(path, n=5):
    """n pages; page i (1-based) is i*10 pt wide, so pages can be told apart."""
    doc = pdfium.PdfDocument.new()
    for i in range(1, n + 1):
        doc.new_page(100 + i * 10, 200)
    buf = io.BytesIO()
    doc.save(buf)
    path.write_bytes(buf.getvalue())
    return path


def widths(data):
    return [w for w, _, _ in info(data)]


# --- page specs --------------------------------------------------------------------


@pytest.mark.parametrize("spec, expected", [
    (None, [0, 1, 2, 3, 4]), ("all", [0, 1, 2, 3, 4]), ("1", [0]), ("2-3", [1, 2]),
    ("4-", [3, 4]), ("-2", [0, 1]), ("1,3,5", [0, 2, 4]), ("odd", [0, 2, 4]),
    ("even", [1, 3]), ("1-2, 2-3", [0, 1, 2]),
])
def test_parse_pages(spec, expected):
    assert parse_pages(spec, 5) == expected


@pytest.mark.parametrize("spec", ["0", "6", "3-2", "x", "1-x"])
def test_parse_pages_errors(spec):
    with pytest.raises(PageError):
        parse_pages(spec, 5)


# --- rotate ----------------------------------------------------------------------------


def test_rotate_is_incremental_and_relative(blank_pdf):
    data = blank_pdf.read_bytes()
    out = pages.rotate(data, [1], 90)
    assert out.startswith(data)  # earlier revision untouched
    assert [r for _, _, r in info(out)] == [0, 90]
    out = pages.rotate(out, [0, 1], -90)
    assert [r for _, _, r in info(out)] == [270, 0]
    assert info(out)[0][:2] == (842, 595)  # displayed landscape


def test_rotate_errors(blank_pdf):
    data = blank_pdf.read_bytes()
    with pytest.raises(PageError, match="multiple of 90"):
        pages.rotate(data, [0], 45)
    with pytest.raises(PageError, match="out of range"):
        pages.rotate(data, [5], 90)
    with pytest.raises(PageError, match="no pages"):
        pages.rotate(data, [], 90)


def test_rotate_encrypted_stays_encrypted(blank_pdf, tmp_path):
    enc = make_encrypted(blank_pdf, tmp_path / "e.pdf", "pw").read_bytes()
    out = pages.rotate(enc, [0], 180, password="pw")
    assert PdfFileReader(io.BytesIO(out)).encrypted
    assert info(out, "pw")[0][2] == 180
    with pytest.raises(SigningError, match="password"):
        pages.rotate(enc, [0], 90)


def test_rotate_invalidates_signature(profile, p12, form_pdf, tmp_path):
    signed = tmp_path / "s.pdf"
    sign_with(profile, form_pdf, signed, field_name="Applicant")
    data = signed.read_bytes()
    assert pages.signature_names(data) == ["Applicant"]
    out = pages.rotate(data, [0], 90)
    root = write_pem(p12[1], tmp_path / "a.pem")
    assert verify_pdf(data, trust=[root])[0].status == VALID
    assert verify_pdf(out, trust=[root])[0].status == INVALID


# --- crop ---------------------------------------------------------------------------------


def test_crop_box_and_reset(blank_pdf):
    data = blank_pdf.read_bytes()
    out = pages.crop(data, [0], box=(50, 100, 545, 742))
    assert info(out)[0][:2] == (495, 642)
    assert info(out)[1][:2] == (595, 842)
    out = pages.crop(out, [0], box=(-10, -10, 9999, 9999))  # clamped to the MediaBox
    assert info(out)[0][:2] == (595, 842)
    out = pages.crop(pages.crop(data, [0], box=(0, 0, 100, 100)), [0], reset=True)
    assert info(out)[0][:2] == (595, 842)


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_crop_margins_follow_the_screen(tmp_path, rotate):
    """Cutting 'top' removes what the viewer shows at the top, for any /Rotate."""
    src = make_pdf(tmp_path / "r.pdf", pages=1, rotate=rotate)
    out = pages.crop(src.read_bytes(), [0], margins=(100, 0, 0, 0))  # top only
    w0, h0, _ = info(src.read_bytes())[0]
    w1, h1, _ = info(out)[0]
    assert (w1, h1) == (w0, h0 - 100)
    # what is now at the top-left was 100 pt further down before
    from pdfsigner.gui.document import PdfDocument

    cropped, original = PdfDocument(out), PdfDocument(src.read_bytes())
    assert cropped.to_pdf(0, 10, 10) == pytest.approx(original.to_pdf(0, 10, 110), abs=0.5)


def test_crop_errors(blank_pdf):
    data = blank_pdf.read_bytes()
    with pytest.raises(PageError, match="exactly one"):
        pages.crop(data, [0])
    with pytest.raises(PageError, match="exactly one"):
        pages.crop(data, [0], box=(0, 0, 10, 10), reset=True)
    with pytest.raises(PageError, match="leaves nothing"):
        pages.crop(data, [0], margins=(500, 0, 500, 0))


# --- split / merge -----------------------------------------------------------------------


def test_split_every_and_parts(tmp_path):
    data = numbered_pdf(tmp_path / "n.pdf").read_bytes()
    parts = pages.split(data, pages.chunks(5, 2))
    assert [widths(p) for p in parts] == [[110, 120], [130, 140], [150]]
    [part] = pages.split(data, [parse_pages("odd", 5)])
    assert widths(part) == [110, 130, 150]
    with pytest.raises(PageError):
        pages.chunks(5, 0)


def test_merge_with_page_selection(tmp_path, blank_pdf):
    a = numbered_pdf(tmp_path / "a.pdf", 3).read_bytes()
    b = blank_pdf.read_bytes()
    out = pages.merge([(a, [2, 0], None), (b, None, None)])
    assert widths(out) == [130, 110, 595, 595]


def test_merge_encrypted_input(blank_pdf, tmp_path):
    enc = make_encrypted(blank_pdf, tmp_path / "e.pdf", "pw").read_bytes()
    out = pages.merge([(enc, None, "pw")])
    assert not PdfFileReader(io.BytesIO(out)).encrypted
    with pytest.raises(SigningError, match="password"):
        pages.merge([(enc, None, "wrong")])


def test_split_drops_signed_fields_keeps_empty(profile, form_pdf, tmp_path):
    signed = tmp_path / "s.pdf"
    sign_with(profile, form_pdf, signed, field_name="Applicant")
    [part] = pages.split(signed.read_bytes(), [[1]])  # page 2 holds both fields
    p = tmp_path / "part.pdf"
    p.write_bytes(part)
    fields = inspect_pdf(p).fields
    assert [(f.name, f.filled) for f in fields] == [("Supervisor", False)]
    assert pages.signature_names(part) == []
    # and the empty field can still be signed
    sign_with(profile, p, tmp_path / "again.pdf", field_name="Supervisor")


# --- CLI ------------------------------------------------------------------------------------


def run(*argv):
    return cli.main([str(a) for a in argv])


def test_cli_rotate(blank_pdf, cfg_path, capsys):
    assert run("rotate", blank_pdf, "--angle", "90", "--pages", "2") == 0
    out = blank_pdf.with_name("blank-rotated.pdf")
    assert "rotated 1 page" in capsys.readouterr().out
    assert [r for _, _, r in info(out.read_bytes())] == [0, 90]
    assert run("rotate", out, "-i", "--angle", "-90") == 0
    assert [r for _, _, r in info(out.read_bytes())] == [270, 0]
    with pytest.raises(SystemExit):
        run("rotate", blank_pdf, "--angle", "45")  # argparse choices


def test_cli_crop(blank_pdf, cfg_path):
    assert run("crop", blank_pdf, "--margins", "20") == 0
    out = blank_pdf.with_name("blank-cropped.pdf")
    assert info(out.read_bytes())[0][:2] == (555, 802)
    assert run("crop", out, "-i", "--margins", "0,0,100,0", "--pages", "1") == 0
    assert info(out.read_bytes())[0][:2] == (555, 702)
    assert run("crop", out, "-i", "--reset") == 0
    assert info(out.read_bytes())[0][:2] == (595, 842)
    assert run("crop", out, "--box", "0,0,100,100", "-o", out.with_name("c.pdf")) == 0
    assert info(out.with_name("c.pdf").read_bytes())[0][:2] == (100, 100)
    assert run("crop", out, "--margins", "1,2,3") == 1  # 1, 2 or 4 values


def test_cli_split_and_merge(tmp_path, cfg_path, capsys):
    src = numbered_pdf(tmp_path / "n.pdf")
    assert run("split", src) == 0  # default: every page
    files = sorted(tmp_path.glob("n-part*.pdf"))
    assert [f.name for f in files] == [f"n-part{i}.pdf" for i in range(1, 6)]
    assert run("split", src, "--part", "1-2", "--part", "odd", "-o", tmp_path / "x{n}.pdf") == 0
    assert widths((tmp_path / "x1.pdf").read_bytes()) == [110, 120]
    assert widths((tmp_path / "x2.pdf").read_bytes()) == [110, 130, 150]
    assert run("split", src, "--every", "3", "-o", tmp_path / "e-{n}.pdf") == 0
    assert widths((tmp_path / "e-2.pdf").read_bytes()) == [140, 150]
    assert run("split", src, "-o", tmp_path / "nopattern.pdf") == 1

    out = tmp_path / "m.pdf"
    assert run("merge", "-o", out, tmp_path / "x2.pdf", f"{src}@4-5", tmp_path / "x1.pdf") == 0
    assert widths(out.read_bytes()) == [110, 130, 150, 140, 150, 110, 120]
    assert "merged 3 files (7 pages)" in capsys.readouterr().out
    assert run("merge", "-o", out, tmp_path / "missing.pdf") == 1


def test_cli_refuses_signed_without_force(profile, form_pdf, tmp_path, cfg_path, capsys):
    signed = tmp_path / "s.pdf"
    sign_with(profile, form_pdf, signed, field_name="Applicant")
    before = signed.read_bytes()
    for argv in (("rotate", signed, "-i", "--angle", "90"),
                 ("crop", signed, "-i", "--margins", "10"),
                 ("split", signed),
                 ("merge", "-o", tmp_path / "m.pdf", signed)):
        assert run(*argv) == 3, argv
        err = capsys.readouterr().err
        assert "'Applicant'" in err and err.rstrip().endswith("(use --force to continue anyway)")
        assert ("remove" if argv[0] in ("split", "merge") else "invalidate") in err
    assert signed.read_bytes() == before
    assert run("rotate", signed, "-i", "--angle", "90", "--force") == 0


def test_cli_encrypted_prompts(blank_pdf, tmp_path, cfg_path, monkeypatch):
    enc = make_encrypted(blank_pdf, tmp_path / "e.pdf", "pw")
    monkeypatch.setattr(cli, "_ask", lambda prompt: "pw")
    assert run("rotate", enc, "-i", "--angle", "90") == 0
    assert info(enc.read_bytes(), "pw")[0][2] == 90
    assert run("rotate", enc, "-i", "--angle", "90", "--password", "pw") == 0
    assert run("merge", "-o", tmp_path / "m.pdf", enc, blank_pdf) == 0
    assert len(widths((tmp_path / "m.pdf").read_bytes())) == 4


def test_cli_split_does_not_overwrite(tmp_path, cfg_path, capsys):
    src = numbered_pdf(tmp_path / "n.pdf")
    keep = tmp_path / "n-part2.pdf"
    keep.write_bytes(b"precious")
    assert run("split", src) == 1
    err = capsys.readouterr().err
    assert str(keep) in err and "--overwrite" in err
    assert keep.read_bytes() == b"precious" and not (tmp_path / "n-part1.pdf").exists()
    assert run("split", src, "--overwrite") == 0
    assert widths(keep.read_bytes()) == [120]
    # never onto the input itself, not even with --overwrite
    assert run("split", src, "--part", "1", "-o", tmp_path / "{n}.pdf") == 0
    one = tmp_path / "1.pdf"
    assert run("split", one, "-o", tmp_path / "{n}.pdf", "--overwrite") == 1
    assert "overwrite the input file" in capsys.readouterr().err


# --- delete ------------------------------------------------------------------------------


def test_delete_pages(tmp_path):
    data = numbered_pdf(tmp_path / "n.pdf").read_bytes()
    out = pages.delete(data, [1, 3])  # pages 2 and 4
    assert widths(out) == [110, 130, 150]
    assert out.startswith(data)  # incremental: the original revision is untouched
    assert widths(pages.delete(out, [0])) == [130, 150]
    with pytest.raises(PageError, match="can't lose all its pages"):
        pages.delete(data, range(5))
    with pytest.raises(PageError, match="out of range"):
        pages.delete(data, [7])


def test_delete_from_a_nested_page_tree(tmp_path):
    """Real files often have /Pages -> /Pages -> /Page trees; the counts of
    every node on the way up must shrink."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject

    w = PdfWriter()
    for i in range(1, 5):
        w.add_blank_page(100 + i * 10, 200)
    root_pages = w._root_object["/Pages"].get_object()
    kids = list(root_pages["/Kids"])
    inner = DictionaryObject({NameObject("/Type"): NameObject("/Pages"),
                              NameObject("/Kids"): ArrayObject(kids[2:]),
                              NameObject("/Count"): NumberObject(2),
                              NameObject("/Parent"): w._root_object.raw_get("/Pages")})
    inner_ref = w._add_object(inner)
    for k in kids[2:]:
        k.get_object()[NameObject("/Parent")] = inner_ref
    root_pages[NameObject("/Kids")] = ArrayObject([*kids[:2], inner_ref])
    buf = io.BytesIO()
    w.write(buf)
    assert widths(buf.getvalue()) == [110, 120, 130, 140]
    out = pages.delete(buf.getvalue(), [2])  # inside the inner node
    assert widths(out) == [110, 120, 140]
    assert len(PdfReader(io.BytesIO(out)).pages) == 3


def test_delete_keeps_encryption_and_drops_fields_of_deleted_pages(form_pdf, tmp_path):
    from conftest import make_encrypted

    enc = make_encrypted(form_pdf, tmp_path / "enc.pdf", "pw").read_bytes()
    out = pages.delete(enc, [1], "pw")  # page 2 holds both signature fields
    assert info(out, "pw") == [(595, 842, 0)]
    with pytest.raises(PasswordRequired):
        pages.page_count(out)
    assert inspect_pdf(out, "pw").fields == []
    kept = pages.delete(form_pdf.read_bytes(), [0])  # the fields' page survives
    assert [f.name for f in inspect_pdf(kept).fields] == ["Applicant", "Supervisor"]
    assert {f.page for f in inspect_pdf(kept).fields} == {0}


def test_delete_from_signed_document_is_detected(profile, form_pdf, tmp_path):
    from pdfsigner.validation import INVALID, verify_pdf

    signed = tmp_path / "s.pdf"
    sign_with(profile, form_pdf, signed, field_name="Applicant")
    assert pages.signature_names(signed.read_bytes()) == ["Applicant"]
    out = pages.delete(signed.read_bytes(), [0])
    [r] = verify_pdf(out)
    assert r.status == INVALID


def test_cli_delete(tmp_path, cfg_path, profile, form_pdf, capsys):
    src = numbered_pdf(tmp_path / "n.pdf")
    assert run("delete", src, "--pages", "2,4") == 0
    assert widths((tmp_path / "n-deleted.pdf").read_bytes()) == [110, 130, 150]
    assert "deleted 2 pages" in capsys.readouterr().out
    assert run("delete", src, "--pages", "1-5") == 1
    assert "can't lose all its pages" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run("delete", src)  # --pages is required
    signed = tmp_path / "s.pdf"
    sign_with(profile, form_pdf, signed, field_name="Applicant")
    assert run("delete", signed, "--pages", "1", "-i") == 3
    assert "'Applicant'" in capsys.readouterr().err
