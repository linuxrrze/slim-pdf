import datetime
import io
import math
import os
import random
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from PIL import Image, ImageDraw
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.sign import fields

from slim_pdf.config import CertProfile

PASSPHRASE = "secret"

# GUI tests run headless.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def make_p12(cn: str, passphrase: str = PASSPHRASE) -> tuple[bytes, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(True, True, False, False, False, True, False, False, False),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    data = pkcs12.serialize_key_and_certificates(
        cn.encode(), key, cert, None,
        serialization.BestAvailableEncryption(passphrase.encode()),
    )
    return data, cert


@pytest.fixture(autouse=True)
def no_downloads(tmp_path_factory, monkeypatch):
    """Intermediate certificates are cached in a temporary folder, and never
    downloaded from the internet; tests of chain.py serve them locally."""
    from slim_pdf import chain

    monkeypatch.setenv("SLIM_PDF_CACHE_DIR", str(tmp_path_factory.mktemp("cache")))

    def offline(url):
        raise OSError(f"tests don't download ({url})")

    monkeypatch.setattr(chain, "download", offline)


@pytest.fixture(autouse=True)
def english(monkeypatch):
    """Tests check English messages, whatever the machine's locale; tests of
    the German catalog switch explicitly."""
    from slim_pdf import i18n

    monkeypatch.setenv("SLIM_PDF_LANG", "en")
    i18n.set_language("en")
    yield
    i18n.set_language("en")


@pytest.fixture
def p12(tmp_path) -> tuple[Path, x509.Certificate]:
    data, cert = make_p12("Alice Example")
    path = tmp_path / "alice.p12"
    path.write_bytes(data)
    return path, cert


@pytest.fixture
def p12_bob(tmp_path) -> tuple[Path, x509.Certificate]:
    data, cert = make_p12("Bob Example")
    path = tmp_path / "bob.p12"
    path.write_bytes(data)
    return path, cert


def draw_signature_image(path: Path, seed: int = 0, color=(20, 40, 150)) -> Path:
    """Transparent 300x100 (3:1) PNG with a handwriting-like stroke."""
    rnd = random.Random(seed)
    k = 4  # supersample for smooth edges
    w, h = 300 * k, 100 * k
    # A slanted capital, then a run of uneven small loops trailing upwards.
    amps = [rnd.uniform(0.5, 1.0) for _ in range(5)]
    pts = []
    for i in range(400):  # capital: tall narrow loop
        t = i / 399 * 2 * math.pi
        pts.append((40 + 18 * math.sin(t) + 10 * (1 - math.cos(t / 2)), 70 - 50 * (1 - math.cos(t)) / 2))
    for i in range(1200):
        t = i / 1199
        a = amps[min(int(t * len(amps)), len(amps) - 1)]
        x = 62 + t * 200 + 9 * math.sin(t * 2 * math.pi * 5)
        y = 62 - 20 * a * (1 - math.cos(t * 2 * math.pi * 5)) / 2 - 14 * t
        pts.append((x, y))
    pts = [(x * k, y * k) for x, y in pts]
    underline = [(120 * k, 84 * k), (200 * k, 80 * k), (285 * k, 72 * k)]
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(im).line(pts, fill=(*color, 255), width=3 * k, joint="curve")
    ImageDraw.Draw(im).line(underline, fill=(*color, 255), width=2 * k, joint="curve")
    im.resize((300, 100), Image.LANCZOS).save(path)
    return path


@pytest.fixture
def sig_image(tmp_path) -> Path:
    return draw_signature_image(tmp_path / "sig.png")


@pytest.fixture
def profile(p12, sig_image) -> CertProfile:
    return CertProfile(name="alice", path=str(p12[0]), image=str(sig_image),
                       secret="none", reason="Test")


def make_pdf(path: Path, pages: int = 2, rotate: int = 0, size=(595, 842)) -> Path:
    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        page = doc.new_page(*size)
        if rotate:
            page.set_rotation(rotate)
    buf = io.BytesIO()
    doc.save(buf)
    path.write_bytes(buf.getvalue())
    return path


@pytest.fixture
def blank_pdf(tmp_path) -> Path:
    return make_pdf(tmp_path / "blank.pdf")


@pytest.fixture
def form_pdf(tmp_path) -> Path:
    """PDF with two prepared, empty signature fields on page 2."""
    src = make_pdf(tmp_path / "base.pdf")
    w = IncrementalPdfFileWriter(io.BytesIO(src.read_bytes()))
    fields.append_signature_field(w, fields.SigFieldSpec("Applicant", on_page=1,
                                                         box=(50, 100, 250, 170)))
    fields.append_signature_field(w, fields.SigFieldSpec("Supervisor", on_page=1,
                                                         box=(320, 100, 520, 170)))
    out = tmp_path / "form.pdf"
    with open(out, "wb") as f:
        w.write(f)
    return out


def add_sig_field(src: Path, dst: Path, name: str, page: int, box, **kw) -> Path:
    w = IncrementalPdfFileWriter(io.BytesIO(src.read_bytes()))
    fields.append_signature_field(w, fields.SigFieldSpec(name, on_page=page, box=box, **kw))
    buf = io.BytesIO()
    w.write(buf)
    dst.write_bytes(buf.getvalue())
    return dst


def add_form_fields(src: Path, dst: Path, page: int = 0) -> Path:
    """Fillable fields of every kind on ``page``, as form tools make them:
    name (text), address (multi-line), agree (check box), size (radio: S, L),
    country (drop-down: de/Germany, fr/France), colour (list box), fixed
    (read-only, "constant"), person.first (a child field)."""
    from pyhanko.pdf_utils import generic as g

    n = g.pdf_name
    w = IncrementalPdfFileWriter(io.BytesIO(src.read_bytes()))
    root = w.root
    page_ref = [ref for ref in _page_refs(w)][page]
    page_obj = page_ref.get_object()

    def stream(content: bytes, box=(0, 0, 14, 14)):
        return w.add_object(g.StreamObject({
            n("/Type"): n("/XObject"), n("/Subtype"): n("/Form"),
            n("/BBox"): g.ArrayObject(g.FloatObject(v) for v in box),
            n("/Resources"): g.DictionaryObject()}, stream_data=content))

    def nums(*v):
        return g.ArrayObject(g.FloatObject(x) for x in v)

    def widget(rect, **extra):
        d = g.DictionaryObject({n("/Type"): n("/Annot"), n("/Subtype"): n("/Widget"),
                                n("/Rect"): nums(*rect), n("/F"): g.NumberObject(4),
                                n("/P"): page_ref,
                                n("/MK"): g.DictionaryObject({n("/BC"): nums(0)})})
        d.update({n(k): v for k, v in extra.items()})
        return d

    def check_ap(state):
        return g.DictionaryObject({n("/N"): g.DictionaryObject({
            n("/" + state): stream(b"0 G 0.5 0.5 13 13 re S 0 g 3 3 8 8 re f"),
            n("/Off"): stream(b"0 G 0.5 0.5 13 13 re S")})})

    annots = []
    top = []

    def add(d, is_top=True):
        ref = w.add_object(d)
        if "/Subtype" in d:
            annots.append(ref)
        if is_top:
            top.append(ref)
        return ref

    text = g.TextStringObject
    add(widget((50, 760, 300, 780), **{"/FT": n("/Tx"), "/T": text("name"),
                                       "/TU": text("Your name")}))
    add(widget((50, 680, 300, 750), **{"/FT": n("/Tx"), "/T": text("address"),
                                       "/Ff": g.NumberObject(1 << 12)}))
    add(widget((50, 650, 64, 664), **{"/FT": n("/Btn"), "/T": text("agree"),
                                      "/V": n("/Off"), "/AS": n("/Off"),
                                      "/AP": check_ap("Yes")}))
    size = g.DictionaryObject({n("/FT"): n("/Btn"), n("/T"): text("size"),
                               n("/TU"): text("T-shirt size"),
                               n("/Ff"): g.NumberObject(1 << 15), n("/V"): n("/Off")})
    size_ref = add(size)
    kids = []
    for state, x in (("S", 50), ("L", 80)):
        kids.append(add(widget((x, 620, x + 14, 634), **{
            "/Parent": size_ref, "/AS": n("/Off"), "/AP": check_ap(state)}), is_top=False))
    size[n("/Kids")] = g.ArrayObject(kids)
    opts = g.ArrayObject([g.ArrayObject([text("de"), text("Germany")]),
                          g.ArrayObject([text("fr"), text("France")])])
    add(widget((50, 580, 200, 600), **{"/FT": n("/Ch"), "/T": text("country"),
                                       "/Ff": g.NumberObject(1 << 17), "/Opt": opts}))
    add(widget((50, 500, 200, 570), **{"/FT": n("/Ch"), "/T": text("colour"),
                                       "/Opt": g.ArrayObject(map(text, ["red", "green",
                                                                        "blue"]))}))
    add(widget((50, 460, 200, 480), **{"/FT": n("/Tx"), "/T": text("fixed"),
                                       "/Ff": g.NumberObject(1), "/V": text("constant")}))
    person = g.DictionaryObject({n("/T"): text("person"), n("/FT"): n("/Tx")})
    person_ref = add(person)
    first = add(widget((50, 420, 200, 440), **{"/T": text("first"), "/Parent": person_ref}),
                is_top=False)
    person[n("/Kids")] = g.ArrayObject([first])

    helv = w.add_object(g.DictionaryObject({
        n("/Type"): n("/Font"), n("/Subtype"): n("/Type1"), n("/BaseFont"): n("/Helvetica"),
        n("/Encoding"): n("/WinAnsiEncoding")}))
    if "/AcroForm" not in root:
        root[n("/AcroForm")] = w.add_object(g.DictionaryObject({n("/Fields"): g.ArrayObject()}))
        w.update_root()
    acro = root["/AcroForm"]
    acro[n("/Fields")] = g.ArrayObject(_raw_items(acro.get("/Fields")) + top)
    acro[n("/DA")] = text("/Helv 0 Tf 0 g")
    acro[n("/DR")] = g.DictionaryObject({n("/Font"): g.DictionaryObject({n("/Helv"): helv})})
    w.update_container(acro)
    page_obj[n("/Annots")] = g.ArrayObject(_raw_items(page_obj.get("/Annots")) + annots)
    w.update_container(page_obj)
    buf = io.BytesIO()
    w.write(buf)
    dst.write_bytes(buf.getvalue())
    return dst


def _raw_items(arr) -> list:
    """The references in a (possibly indirect, possibly missing) array."""
    if arr is None:
        return []
    arr = arr.get_object()
    return [arr.raw_get(i) for i in range(len(arr))]


def _page_refs(w):
    out = []

    def walk(ref):
        node = ref.get_object()
        if "/Kids" in node:
            for i in range(len(node["/Kids"])):
                walk(node["/Kids"].raw_get(i))
        else:
            out.append(ref)

    walk(w.root.raw_get("/Pages"))
    return out


@pytest.fixture
def fill_pdf(tmp_path) -> Path:
    """A one-page PDF with the fields of :func:`add_form_fields`."""
    return add_form_fields(make_pdf(tmp_path / "base.pdf", pages=1), tmp_path / "fill.pdf")


def make_encrypted(src: Path, dst: Path, user_pw: str) -> Path:
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.pdf_utils.writer import copy_into_new_writer

    with open(src, "rb") as f:
        w = copy_into_new_writer(PdfFileReader(f))
    w.encrypt(owner_pass="owner-" + user_pw, user_pass=user_pw)
    with open(dst, "wb") as f:
        w.write(f)
    return dst


# --- validation helpers ---------------------------------------------------------


def asn1_cert(cert: x509.Certificate):
    from asn1crypto import x509 as ax509

    return ax509.Certificate.load(cert.public_bytes(serialization.Encoding.DER))


def validate_all(path: Path, *certs: x509.Certificate, password: str | None = None):
    """[(field name, PdfSignatureStatus)] for every signature in the file."""
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    with open(path, "rb") as f:
        r = PdfFileReader(f)
        if password is not None:
            r.decrypt(password)
        vc = ValidationContext(trust_roots=[asn1_cert(c) for c in certs])
        return [(es.field_name, validate_pdf_signature(es, vc)) for es in r.embedded_signatures]


# --- rendering helpers -----------------------------------------------------------


def render_page(path: Path, page: int = 0, scale: float = 1.0, password: str | None = None):
    """PIL RGB image of a page, as displayed (rotation applied, widgets drawn)."""
    pdf = pdfium.PdfDocument(str(path), password=password)
    pdf.init_forms()
    # no_smoothtext: no subpixel colour fringes that look like "blue ink"
    return pdf[page].render(scale=scale, may_draw_forms=True,
                            no_smoothtext=True).to_pil().convert("RGB")


def _bbox_where(img, pred, region=None):
    if region is not None:
        img = img.crop(region)
    px = img.load()
    w, h = img.size
    pts = [(x, y) for y in range(h) for x in range(w) if pred(px[x, y])]
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    l, t, r, b = min(xs), min(ys), max(xs) + 1, max(ys) + 1
    if region is not None:
        l, t, r, b = l + region[0], t + region[1], r + region[0], b + region[1]
    return (l, t, r, b)


def blue_bbox(img, region=None):
    """Bounding box of the blue signature ink (the test images are drawn in blue)."""
    return _bbox_where(img, lambda p: p[2] > 100 and p[2] - p[0] > 60 and p[2] - p[1] > 40,
                       region)


def green_bbox(img, region=None):
    """Bounding box of green ink. Use green images where stamp text is rendered
    too: pdfium draws widget text with subpixel antialiasing, whose fringes are
    blue/orange but never green."""
    return _bbox_where(img, lambda p: p[1] - p[0] > 60 and p[1] - p[2] > 60, region)


def text_bbox(img, region=None):
    """Bounding box of dark, uncoloured pixels (stamp text)."""
    return _bbox_where(img, lambda p: sum(p) < 300 and max(p) - min(p) < 60, region)


# --- keyring / config --------------------------------------------------------------


class MemoryKeyring:
    """Minimal in-memory keyring backend."""

    priority = 1

    def __init__(self):
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        from keyring.errors import PasswordDeleteError

        if (service, username) not in self.store:
            raise PasswordDeleteError("not found")
        del self.store[(service, username)]


@pytest.fixture
def mem_keyring():
    import keyring
    from keyring.backend import KeyringBackend

    backend = type("MemKR", (MemoryKeyring, KeyringBackend), {})()
    old = keyring.get_keyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(old)


@pytest.fixture
def cfg_path(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "config" / "config.toml"
    monkeypatch.setenv("SLIM_PDF_CONFIG", str(path))
    return path


@pytest.fixture
def keepass_db(tmp_path, p12):
    """KeePass DB (master 'master') with entry Certificates/FAU S/MIME holding the
    passphrase and the .p12 as attachment 'alice.p12'."""
    from pykeepass import create_database

    db = tmp_path / "vault.kdbx"
    kp = create_database(str(db), password="master")
    group = kp.add_group(kp.root_group, "Certificates")
    entry = kp.add_entry(group, "FAU S/MIME", "alice", PASSPHRASE)
    entry.add_attachment(kp.add_binary(p12[0].read_bytes()), "alice.p12")
    kp.add_entry(kp.root_group, "Plain", "bob", "plain-secret")
    kp.save()
    return db


def sign_with(profile: CertProfile, src: Path, dst: Path, **kw) -> str:
    """Sign ``src`` into ``dst`` with a PKCS#12 test profile."""
    from slim_pdf.secretstore import Credentials
    from slim_pdf.signing import open_signer, sign_pdf

    with open_signer(profile, Credentials(secret=PASSPHRASE)) as s:
        return sign_pdf(src, dst, s, profile, **kw)


def write_pem(cert: x509.Certificate, path: Path) -> Path:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path


def make_vector_signature(path: Path, width: float = 200, height: float = 50,
                          rgb: tuple[float, float, float] = (0, 0.6, 0)) -> Path:
    """One-page PDF with a stroked 'signature' path, sized width x height pt."""
    r, g, b = rgb
    content = (f"{r} {g} {b} RG 3 w 1 J "
               f"5 {height * 0.3:.1f} m {width * 0.3:.1f} {height * 0.9:.1f} "
               f"{width * 0.5:.1f} {height * 0.1:.1f} {width - 5} {height * 0.6:.1f} c S").encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
        f"/Resources << >> /Contents 4 0 R >>".encode(),
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.7\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objects) + 1, xref))
    path.write_bytes(out.getvalue())
    return path
