"""Signing core on top of pyHanko. Used by both CLI and GUI.

All coordinates are PDF user space (points, origin bottom-left, unrotated
page), pages are 0-based. Converting from screen coordinates is the GUI's job.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import math
import os
import tempfile
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image
from pyhanko import stamp
from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.content import ImportedPdfPage
from pyhanko.pdf_utils.crypt import AuthStatus, PdfKeyNotAvailableError
from pyhanko.pdf_utils.images import PdfImage
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.layout import (
    AxisAlignment,
    BoxConstraints,
    Margins,
    SimpleBoxLayoutRule,
)
from pyhanko.pdf_utils.misc import PdfError
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.pdf_utils.rw_common import find_inherited_value_in_tree
from pyhanko.pdf_utils.text import TextBoxStyle
from pyhanko.sign import fields, signers, timestamps
from pyhanko.sign.fields import MDPPerm
from pyhanko.sign.general import SigningError as PyHankoSigningError
from pyhanko.sign.validation.pdf_embedded import read_certification_data

from .config import CertProfile, expand
from .i18n import _, ngettext
from .secretstore import Credentials

Box = tuple[float, float, float, float]


class SigningError(Exception):
    pass


class SigningBlocked(SigningError):
    """Signing would invalidate an existing signature."""


class PasswordError(SigningError):
    """The PDF is encrypted and the password is missing or wrong."""


class PasswordRequired(PasswordError):
    """The PDF is encrypted and no password was given."""


class WrongPassword(PasswordError):
    """The password given for an encrypted PDF is wrong."""


@dataclass
class SigField:
    name: str
    filled: bool
    page: int | None  # 0-based; None if the widget isn't on any page
    rect: Box | None  # None for invisible fields
    locked: bool = False  # locked by a FieldMDP of an existing signature


@dataclass
class DocInfo:
    page_count: int
    page_rotation: list[int] = field(default_factory=list)  # /Rotate per page
    fields: list[SigField] = field(default_factory=list)
    certification: MDPPerm | None = None
    encrypted: bool = False

    @property
    def empty_fields(self) -> list[SigField]:
        return [f for f in self.fields if not f.filled]

    def field(self, name: str) -> SigField | None:
        return next((f for f in self.fields if f.name == name), None)

    @property
    def frozen(self) -> bool:
        """Certified with 'no changes allowed': no further signature is possible."""
        return self.certification == MDPPerm.NO_CHANGES

    def blocker(self, target: str | None = None, new_visible: bool = False) -> str | None:
        """Why adding a signature (to ``target``, or as a new visible field)
        would break existing ones.

        Unlike :attr:`frozen` this can be overridden (``allow_breaking``).
        """
        f = self.field(target) if target else None
        if f is not None and f.locked:
            return _("field {name!r} is locked by an existing signature").format(name=target)
        if new_visible and self.certification is not None:
            # Validators (pyHanko, ours) reject new visible signature fields
            # after a certification signature, whatever its permissions.
            empty = ", ".join(repr(f.name) for f in self.empty_fields if not f.locked)
            if empty:
                return _("the document is certified, and a new visible signature field "
                         "breaks the certification; sign an existing empty field ({fields}) "
                         "instead").format(fields=empty)
            return _("the document is certified, and a new visible signature field breaks "
                     "the certification; sign invisibly instead")
        return None


def printable(text: str, keep_newlines: bool = False) -> str:
    """Text from a PDF or certificate, made safe to show.

    Control and format characters (escape sequences, carriage returns,
    bidi overrides, extra lines) are shown escaped, e.g. ``\\x1b``, so a
    crafted field name or signer name can't move the terminal cursor or fake
    another line such as "Contract: VALID".
    """
    out = []
    for ch in str(text):
        if (ch == "\n" and keep_newlines) or unicodedata.category(ch) not in (
                "Cc", "Cf", "Zl", "Zp"):
            out.append(ch)
        else:
            out.append(ch.encode("unicode_escape").decode("ascii"))
    return "".join(out)


# --- inspection --------------------------------------------------------------


def _open_reader(data: bytes | io.BufferedIOBase, password: str | None) -> PdfFileReader:
    stream = io.BytesIO(data) if isinstance(data, bytes) else data
    try:
        reader = PdfFileReader(stream, strict=False)
    except PdfError as e:
        raise SigningError(_("cannot read the PDF: {error}").format(error=e)) from None
    if reader.encrypted:
        if password is None:
            raise PasswordRequired(_("PDF is encrypted, a password is required"))
        try:
            # decrypt() returns an AuthResult, which is always truthy
            ok = reader.decrypt(password).status != AuthStatus.FAILED
        except PdfKeyNotAvailableError:
            ok = False
        if not ok:
            raise WrongPassword(_("wrong PDF password"))
    return reader


def _walk_pages(node, rotate: int = 0) -> Iterator[tuple[generic.DictionaryObject, int | None, int]]:
    """(page dict, object number, effective /Rotate) in document order."""
    obj = node.get_object()
    rotate = int(_get(obj, "/Rotate", rotate)) % 360  # /Rotate is inheritable
    if "/Kids" in obj:
        kids = obj["/Kids"]
        for i in range(len(kids)):
            yield from _walk_pages(kids.raw_get(i), rotate)
    else:
        yield obj, _ref_id(node), rotate


def _get(d, key: str, default=None):
    """``d[key]`` or default. Unlike ``d.get`` this resolves references and
    decrypts values in encrypted documents."""
    return d[key] if key in d else default


def _ref_id(obj) -> int | None:
    if isinstance(obj, generic.IndirectObject):
        return obj.idnum
    if isinstance(obj, generic.Reference):
        return obj.idnum
    return None


def _widgets(field_dict: generic.DictionaryObject, field_ref) -> list[tuple]:
    """(widget dict, widget ref) pairs; a field without /Kids is its own widget."""
    kids = _get(field_dict, "/Kids")
    if kids:
        return [(kids.raw_get(i).get_object(), kids.raw_get(i)) for i in range(len(kids))]
    return [(field_dict, field_ref)]


def _locked_field_names(reader: PdfFileReader) -> tuple[set[str], bool]:
    """Names locked by FieldMDP transforms of existing signatures.

    Returns (names, all_locked). For ``/Exclude`` the excluded names are
    returned prefixed with ``!`` and all_locked is set.
    """
    locked: set[str] = set()
    all_locked = False
    for _name, sig_value, _filled in fields.enumerate_sig_fields(reader, filled_status=True):
        for ref in _get(sig_value.get_object(), "/Reference", ()):
            ref = ref.get_object()
            if _get(ref, "/TransformMethod") != "/FieldMDP":
                continue
            params = _get(ref, "/TransformParams", {})
            action = _get(params, "/Action")
            names = {str(n) for n in _get(params, "/Fields", ())}
            if action == "/All":
                all_locked = True
            elif action == "/Include":
                locked |= names
            elif action == "/Exclude":
                # Everything except `names`; encode as negative set.
                locked |= {f"!{n}" for n in names}
                all_locked = True
    return locked, all_locked


def _data(src: str | os.PathLike | bytes) -> bytes:
    return src if isinstance(src, bytes) else Path(src).read_bytes()


def inspect_pdf(path: str | os.PathLike | bytes, password: str | None = None) -> DocInfo:
    """Pages, rotation, signature fields and certification of a PDF (path or bytes)."""
    reader = _open_reader(_data(path), password)

    pages = list(_walk_pages(reader.root.raw_get("/Pages")))
    page_index = {pid: i for i, (_page, pid, _rot) in enumerate(pages) if pid is not None}
    annot_page: dict[int, int] = {}
    for i, (page, _pid, _rot) in enumerate(pages):
        annots = _get(page, "/Annots")
        if annots is None:
            continue
        annots = annots.get_object()
        for j in range(len(annots)):
            rid = _ref_id(annots.raw_get(j))
            if rid is not None:
                annot_page[rid] = i

    locked, all_locked = _locked_field_names(reader)
    excluded = {n[1:] for n in locked if n.startswith("!")}

    info = DocInfo(
        page_count=len(pages),
        page_rotation=[rot for _page, _pid, rot in pages],
        certification=(cd.permission if (cd := read_certification_data(reader)) else None),
        encrypted=reader.encrypted,
    )
    for name, value, field_ref in fields.enumerate_sig_fields(reader):
        field_dict = field_ref.get_object()
        page = rect = None
        for widget, wref in _widgets(field_dict, field_ref):
            r = _get(widget, "/Rect")
            if r is not None:
                x1, y1, x2, y2 = (float(v) for v in r)
                if abs(x2 - x1) > 0 and abs(y2 - y1) > 0:
                    rect = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
            page = annot_page.get(_ref_id(wref))
            if page is None and "/P" in widget:
                page = page_index.get(_ref_id(widget.raw_get("/P")))
            break  # one widget per signature field is the norm
        filled = value is not None
        is_locked = (not filled) and (
            name in locked or (all_locked and name not in excluded)
        )
        info.fields.append(SigField(name, filled, page, rect, is_locked))
    return info


# --- appearance --------------------------------------------------------------


def is_pdf_image(path: str | os.PathLike) -> bool:
    return Path(path).suffix.lower() == ".pdf"


def _pdf_image_size(path: str | os.PathLike) -> tuple[float, float]:
    """Size of page 1 of a PDF used as a vector signature image (its MediaBox,
    which is what pyHanko imports)."""
    try:
        with open(path, "rb") as f:
            reader = PdfFileReader(f, strict=False)
            page = reader.find_page_for_modification(0)[0].get_object()
            x1, y1, x2, y2 = (float(v) for v in
                              find_inherited_value_in_tree(page, "/MediaBox", "/Parent"))
    except (OSError, PdfError, TypeError, ValueError, IndexError) as e:
        raise SigningError(_("cannot read signature PDF {path}: {error}").format(
            path=path, error=e)) from None
    return abs(x2 - x1), abs(y2 - y1)


def image_aspect(path: str | os.PathLike) -> float:
    """width / height of the signature image (raster, or page 1 of a PDF)."""
    if is_pdf_image(path):
        w, h = _pdf_image_size(path)
    else:
        with Image.open(path) as im:
            w, h = im.size
    return w / h


def box_from_anchor(x: float, y: float, width: float, aspect: float) -> Box:
    """Box with lower-left corner (x, y), given width and width/height ratio."""
    return (x, y, x + width, y + width / aspect)


TEXT_SIZE = 6
TEXT_LEADING = 7
TEXT_MARGIN = 2


def build_stamp_style(
    profile: CertProfile | None, *, with_image: bool = True, with_text: bool = True
) -> stamp.BaseStampStyle:
    image = profile.image_path if (profile and with_image) else None
    text = (profile.stamp_text if profile else "") if with_text else ""

    background = None
    if image is not None:
        if not image.exists():
            raise SigningError(_("signature image not found: {path}").format(path=image))
        if is_pdf_image(image):
            _pdf_image_size(image)  # fail early on unreadable files
            background = ImportedPdfPage(str(image), page_ix=0)
        else:
            background = PdfImage(Image.open(image))

    if background is not None and not text:
        return stamp.StaticStampStyle(background=background, border_width=0)

    if background is not None:
        # Small text in a strip at the bottom, image scaled into the space above.
        text_height = TEXT_LEADING * (text.count("\n") + 1) + 2 * TEXT_MARGIN
        return stamp.TextStampStyle(
            stamp_text=text,
            background=background,
            background_opacity=1.0,
            background_layout=SimpleBoxLayoutRule(
                x_align=AxisAlignment.ALIGN_MID,
                y_align=AxisAlignment.ALIGN_MID,
                margins=Margins(bottom=text_height),
            ),
            border_width=0,
            text_box_style=TextBoxStyle(font_size=TEXT_SIZE, leading=TEXT_LEADING),
            inner_content_layout=SimpleBoxLayoutRule(
                x_align=AxisAlignment.ALIGN_MIN,
                y_align=AxisAlignment.ALIGN_MIN,
                margins=Margins(left=TEXT_MARGIN, right=TEXT_MARGIN,
                                top=TEXT_MARGIN, bottom=TEXT_MARGIN),
            ),
        )
    return stamp.TextStampStyle(stamp_text=text or "%(signer)s\n%(ts)s", border_width=1)


def upright_style(style: stamp.BaseStampStyle, rotation: int) -> stamp.BaseStampStyle:
    """Counter-rotate the appearance so it reads upright on a /Rotate'd page.

    pyHanko lays the stamp out in unrotated user space, so on a page with
    /Rotate 90 it would show up sideways. We lay it out in the displayed
    orientation and give the appearance XObject a /Matrix; the viewer maps
    the transformed BBox onto the widget /Rect (ISO 32000 12.5.5).
    """
    rotation %= 360
    if rotation == 0:
        return style
    rad = math.radians(rotation)
    cos, sin = round(math.cos(rad)), round(math.sin(rad))
    matrix = generic.ArrayObject(
        generic.FloatObject(v) for v in (cos, sin, -sin, cos, 0, 0)
    )

    class Upright(type(style)):
        def create_stamp(self, writer, box, text_params):
            if rotation in (90, 270):
                box = BoxConstraints(width=box.height, height=box.width)
            st = super().create_stamp(writer, box, text_params)
            to_xobject = st.as_form_xobject

            def as_form_xobject():
                xobj = to_xobject()
                xobj["/Matrix"] = matrix
                return xobj

            st.as_form_xobject = as_form_xobject
            return st

    return Upright(**{f.name: getattr(style, f.name) for f in dataclasses.fields(style)})


# --- signer ------------------------------------------------------------------


def _p11_attr(obj, attr):
    """An attribute of a token object, or None if the token doesn't have it."""
    from pkcs11.exceptions import PKCS11Error

    try:
        return obj[attr]
    except PKCS11Error:
        return None


def _describe_token_objects(objs) -> str:
    from pkcs11 import Attribute

    found = sorted(((_p11_attr(o, Attribute.ID) or b"", _p11_attr(o, Attribute.LABEL) or "")
                    for o in objs))  # tokens return objects in no particular order
    return ", ".join(_("'{label}' (ID {id})").format(label=label, id=oid.hex()) if oid
                     else f"'{label}'" for oid, label in found) or _("none")


def _find_token_key(session, profile: CertProfile):
    """Find the private key (by label and/or ID; the only one if neither is
    given) and its certificate (by ``cert_label``, else the key's ID, else
    the key's label).

    Returns (key object, asn1crypto certificate).
    Cards label keys and certificates differently (YubiKey: "Private key for
    Digital Signature" / "X.509 Certificate for Digital Signature"), but give
    both the same ID.
    """
    from asn1crypto import x509 as asn1_x509
    from pkcs11 import Attribute, ObjectClass
    from pkcs11.exceptions import PKCS11Error

    from .config import normalize_hex

    want = {Attribute.CLASS: ObjectClass.PRIVATE_KEY}
    if profile.key_label:
        want[Attribute.LABEL] = profile.key_label
    if profile.key_id:
        want[Attribute.ID] = bytes.fromhex(normalize_hex(profile.key_id))
    keys = list(session.get_objects(want))
    if len(keys) != 1:
        available = _describe_token_objects(
            session.get_objects({Attribute.CLASS: ObjectClass.PRIVATE_KEY}))
        if not keys:
            if profile.key_label and profile.key_id:
                msg = _("no private key with label {label!r} and ID {id!r} on the token "
                        "(keys: {keys})")
            elif profile.key_label:
                msg = _("no private key with label {label!r} on the token (keys: {keys})")
            elif profile.key_id:
                msg = _("no private key with ID {id!r} on the token (keys: {keys})")
            else:
                msg = _("no private key on the token (keys: {keys})")
            raise PKCS11Error(msg.format(label=profile.key_label, id=profile.key_id,
                                         keys=available))
        raise PKCS11Error(ngettext(
            "{n} private key matches, set key_label or key_id (keys: {keys})",
            "{n} private keys match, set key_label or key_id (keys: {keys})",
            len(keys)).format(n=len(keys), keys=available))
    key = keys[0]
    key_label = _p11_attr(key, Attribute.LABEL)
    key_id = _p11_attr(key, Attribute.ID)

    def certs_with(attr, value):
        return list(session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE,
                                         attr: value}))

    certs = []
    if profile.cert_label:
        certs = certs_with(Attribute.LABEL, profile.cert_label)
    else:
        if key_id:
            certs = certs_with(Attribute.ID, key_id)
        if not certs and key_label:
            certs = certs_with(Attribute.LABEL, key_label)
    if len(certs) != 1:
        available = _describe_token_objects(
            session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE}))
        n = len(certs)
        if profile.cert_label and not certs:
            msg = _("no certificate with label {label!r} on the token, set cert_label "
                    "(certificates: {certs})")
        elif profile.cert_label:
            msg = ngettext("{n} certificate with label {label!r} on the token, set cert_label "
                           "(certificates: {certs})",
                           "{n} certificates with label {label!r} on the token, set "
                           "cert_label (certificates: {certs})", n)
        elif not certs:
            msg = _("no certificate for key {key} on the token, set cert_label "
                    "(certificates: {certs})")
        else:
            msg = ngettext("{n} certificate for key {key} on the token, set cert_label "
                           "(certificates: {certs})",
                           "{n} certificates for key {key} on the token, set cert_label "
                           "(certificates: {certs})", n)
        raise PKCS11Error(msg.format(n=n, label=profile.cert_label,
                                     key=_describe_token_objects([key]), certs=available))
    return key, asn1_x509.Certificate.load(certs[0][Attribute.VALUE])


def _use_raw_mechanism(session, key) -> bool:
    """True if the token can only sign pre-hashed data (CKM_ECDSA, CKM_RSA_PKCS),
    not hash-and-sign (CKM_ECDSA_SHA256, ...), as with SoftHSM's ECDSA and many
    smartcard modules. pyHanko then hashes in software."""
    from pkcs11 import Attribute, KeyType, Mechanism
    from pkcs11.exceptions import PKCS11Error

    by_type = {
        KeyType.EC: (Mechanism.ECDSA, (Mechanism.ECDSA_SHA256, Mechanism.ECDSA_SHA384,
                                       Mechanism.ECDSA_SHA512)),
        KeyType.RSA: (Mechanism.RSA_PKCS, (Mechanism.SHA256_RSA_PKCS, Mechanism.SHA384_RSA_PKCS,
                                           Mechanism.SHA512_RSA_PKCS)),
    }
    raw, hashed = by_type.get(_p11_attr(key, Attribute.KEY_TYPE), (None, ()))
    if raw is None:
        return False
    try:
        mechanisms = set(session.token.slot.get_mechanisms())
    except PKCS11Error:
        return False
    return raw in mechanisms and not all(m in mechanisms for m in hashed)


@contextlib.contextmanager
def open_signer(profile: CertProfile, creds: Credentials) -> Iterator[signers.Signer]:
    """Yield a pyHanko signer; PKCS#11 sessions are closed afterwards."""
    if profile.type == "pkcs12":
        if creds.pkcs12_data is not None:
            data = creds.pkcs12_data
        else:
            p = expand(profile.path)
            try:
                data = p.read_bytes()
            except OSError as e:
                raise SigningError(_("cannot read {path}: {error}").format(
                    path=p, error=e.strerror)) from None
        passphrase = creds.secret.encode() if creds.secret else None
        try:
            signer = signers.SimpleSigner.load_pkcs12_data(
                data, other_certs=set(), passphrase=passphrase
            )
        except ValueError as e:
            # cryptography raises ValueError for a wrong passphrase.
            raise SigningError(
                _("cannot load PKCS#12 for '{name}' (wrong passphrase?): {error}").format(
                    name=profile.name, error=e)
            ) from None
        yield signer  # outside the try: errors while signing are not load errors
        return

    if profile.type == "pkcs11":
        try:
            from pyhanko.config.pkcs11 import TokenCriteria
            from pyhanko.sign import pkcs11
        except ImportError as e:
            raise SigningError(_("PKCS#11 support needs 'pyhanko[pkcs11]'")) from e
        from pkcs11 import Attribute
        from pkcs11.exceptions import PinIncorrect, PinLenRange, PKCS11Error, UserNotLoggedIn

        def token_error(e: Exception, signing: bool = False) -> SigningError:
            if isinstance(e, (PinIncorrect, PinLenRange)):
                return SigningError(_("wrong PIN for token of '{name}'").format(
                    name=profile.name))
            if signing and isinstance(e, UserNotLoggedIn):
                return SigningError(_("the key of '{name}' needs the PIN for every "
                                      "signature, but none was given").format(name=profile.name))
            msg = str(e) or type(e).__name__
            if signing:
                return SigningError(_("PKCS#11 ({module}): the token refused to sign: "
                                      "{error}").format(module=profile.pkcs11_lib, error=msg))
            return SigningError(_("PKCS#11 ({module}): {error}").format(
                module=profile.pkcs11_lib, error=msg))

        try:
            session = pkcs11.open_pkcs11_session(
                str(expand(profile.pkcs11_lib)),
                token_criteria=TokenCriteria(label=profile.token_label or None),
                user_pin=creds.secret,
            )
        except PKCS11Error as e:
            raise token_error(e) from None
        try:
            try:
                key, cert = _find_token_key(session, profile)
                # Keys like YubiKey PIV slot 9c want the PIN again for every
                # signature (a context-specific login right before C_Sign).
                always_auth = bool(_p11_attr(key, Attribute.ALWAYS_AUTHENTICATE))
                signer = pkcs11.PKCS11Signer(
                    session, signing_cert=cert,
                    key_label=_p11_attr(key, Attribute.LABEL) or None,
                    key_id=_p11_attr(key, Attribute.ID) or None,
                    use_raw_mechanism=_use_raw_mechanism(session, key),
                    base_sign_kwargs={"pin": creds.secret}
                    if always_auth and creds.secret else None)
                signer.signing_cert  # loads the key handle now, not mid-signing
            except PKCS11Error as e:
                raise token_error(e) from None
            try:
                yield signer
            except PKCS11Error as e:  # raised by the token while signing
                raise token_error(e, signing=True) from None
        finally:
            session.close()
        return

    if profile.type == "system":
        from . import systemstore

        backend = systemstore.get_backend()
        try:
            ident = systemstore.select(backend.identities(), profile.system_cert)
            yield systemstore.SystemStoreSigner(backend, ident)
        finally:
            backend.close()
        return

    raise SigningError(_("unknown certificate type {type!r}").format(type=profile.type))


def describe_signer(signer: signers.Signer) -> str:
    cert = signer.signing_cert
    return _("{subject}\n  issuer:  {issuer}\n  valid:   {start} .. {end}").format(
        subject=cert.subject.human_friendly, issuer=cert.issuer.human_friendly,
        start=f"{cert.not_valid_before:%Y-%m-%d}", end=f"{cert.not_valid_after:%Y-%m-%d}")


# --- signing -----------------------------------------------------------------


def unique_field_name(info: DocInfo, base: str = "Signature") -> str:
    taken = {f.name for f in info.fields}
    n = 1
    while f"{base}{n}" in taken:
        n += 1
    return f"{base}{n}"


def check_target(
    info: DocInfo,
    field_name: str | None,
    page: int | None,
    box: Box | None,
    *,
    allow_breaking: bool = False,
) -> None:
    """Raise if the requested target can't (or shouldn't) be signed.

    Cheap, so callers run it before asking for any passphrase.
    """
    if info.frozen:
        raise SigningError(
            _("document is certified with 'no changes allowed'; it cannot be signed again")
        )
    existing = info.field(field_name) if field_name else None
    if existing is not None:
        if box is not None:
            raise SigningError(_("field {name!r} already exists; omit the box").format(
                name=field_name))
        if existing.filled:
            raise SigningError(_("field {name!r} is already signed").format(name=field_name))
    elif box is not None:
        if page is None or not 0 <= page < info.page_count:
            raise SigningError(_("page out of range (document has {count})").format(
                count=info.page_count))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise SigningError(_("box needs x1 < x2 and y1 < y2"))
    new_visible = existing is None and box is not None
    if not allow_breaking and (reason := info.blocker(field_name, new_visible)):
        raise SigningBlocked(_("signing would invalidate an existing signature: "
                               "{reason}").format(reason=reason))


def sign_pdf(
    src: str | os.PathLike | bytes,
    dst: str | os.PathLike,
    signer: signers.Signer,
    profile: CertProfile | None = None,
    *,
    field_name: str | None = None,
    page: int | None = None,
    box: Box | None = None,
    with_image: bool = True,
    with_text: bool = True,
    pdf_password: str | None = None,
    allow_breaking: bool = False,
    trust: Sequence[str] = (),
) -> str:
    """Sign ``src`` (path or bytes) into ``dst`` (may be the same file).
    Returns the field name.

    * ``field_name`` only            -> sign that existing empty field
    * ``page`` + ``box``             -> create a new visible field there
                                        (named ``field_name`` or auto-named)
    * neither                        -> invisible signature in a new field

    Signing is always incremental, so existing signatures stay valid.

    With ``profile.ltv`` the signer's chain is validated online against the
    Mozilla roots plus ``trust``, and the chain and revocation info are
    embedded (PAdES B-LT, or B-LTA when a timestamp server is configured).
    """
    data = _data(src)
    info = inspect_pdf(data, pdf_password)

    check_target(info, field_name, page, box, allow_breaking=allow_breaking)
    existing = info.field(field_name) if field_name else None
    new_spec = None
    if existing is None:
        field_name = field_name or unique_field_name(info)
        if box is not None:
            new_spec = fields.SigFieldSpec(
                field_name, on_page=page, box=tuple(round(v, 2) for v in box)
            )
        else:
            new_spec = fields.SigFieldSpec(field_name)

    timestamper = (
        timestamps.HTTPTimeStamper(profile.timestamp_url)
        if profile and profile.timestamp_url
        else None
    )
    ltv = {}
    if profile and profile.ltv:
        from .validation import validation_context

        ltv = dict(
            embed_validation_info=True,
            use_pades_lta=timestamper is not None,
            validation_context=validation_context(trust, online=True,
                                                  revocation_mode="hard-fail"),
        )
    meta = signers.PdfSignatureMetadata(
        **ltv,
        field_name=field_name,
        reason=(profile.reason or None) if profile else None,
        location=(profile.location or None) if profile else None,
        contact_info=(profile.contact_info or None) if profile else None,
        subfilter=fields.SigSeedSubFilter.PADES,
    )
    visible = box is not None or (existing is not None and existing.rect is not None)
    style = None
    if visible:
        target_page = page if existing is None else existing.page
        rotation = info.page_rotation[target_page] if target_page is not None else 0
        style = upright_style(
            build_stamp_style(profile, with_image=with_image, with_text=with_text), rotation
        )

    pdf_signer = signers.PdfSigner(
        meta, signer, timestamper=timestamper, stamp_style=style, new_field_spec=new_spec
    )
    writer = IncrementalPdfFileWriter(io.BytesIO(data), strict=False)
    bytes_reserved = None
    if writer.prev.encrypted:
        writer.encrypt(pdf_password)
        # pyHanko adds a PDF MAC token (ISO 32004) for encrypted documents that
        # its size estimate misses, so reserve generously instead.
        bytes_reserved = 16384 + (16384 if timestamper else 0)
    out = io.BytesIO()
    try:
        pdf_signer.sign_pdf(writer, output=out, bytes_reserved=bytes_reserved)
    except PyHankoSigningError as e:
        if not ltv:
            raise
        cause = e.__cause__ or e.__context__ or e  # e.g. PathBuildingError, RevokedError
        raise SigningError(
            _("LTV: cannot validate the signer's certificate chain: {error}. LTV needs a "
              "chain to a trusted root (add it with `trust` in the config) and reachable "
              "revocation info (OCSP/CRL)").format(error=cause)) from None

    _atomic_write(Path(dst), out.getvalue())
    return field_name


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            os.replace(tmp, path)
        except PermissionError as e:
            # Windows refuses to replace a file another program has open.
            raise SigningError(_("cannot write {path}: {error} "
                                 "(is it open in another program?)").format(
                path=path, error=e.strerror)) from e
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
