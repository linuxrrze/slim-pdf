"""Page operations: rotate, crop, split, merge; encrypting a document.

* ``rotate``, ``crop`` and ``delete`` are written as incremental updates (like
  signatures), so earlier revisions stay byte-identical and the encryption
  is kept. They still change pages, which invalidates existing signatures;
  callers check :func:`signature_names` first and ask before doing so.
* ``split`` and ``merge`` create new documents (via pypdf). Signatures can't
  be carried over, so signed signature fields are dropped from the output;
  empty signature fields are kept. Encrypted inputs are decrypted; the output
  is not encrypted.
* ``encrypt`` writes the document anew as well (encryption covers the whole
  file; earlier revisions can't be encrypted afterwards), so signatures are
  dropped the same way.

Page numbers are 0-based here; :func:`parse_pages` converts the 1-based
specs used on the command line ("1-3,5,8-", "odd", "even", "all").
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Sequence

from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.rw_common import find_inherited_value_in_tree
from pyhanko.sign.fields import enumerate_sig_fields

from .i18n import _
from .signing import (
    PasswordRequired,
    SigningBlocked,
    WrongPassword,
    _open_reader,
    _walk_pages,
)

Box = tuple[float, float, float, float]


class PageError(ValueError):
    pass


class EditBlocked(SigningBlocked):
    """A page operation would invalidate or remove existing signatures."""


def parse_pages(spec: str | None, page_count: int) -> list[int]:
    """'1-3,5,8-' / 'odd' / 'even' / 'all' (1-based) -> sorted 0-based indices."""
    if spec is None or spec.strip().lower() in ("", "all"):
        return list(range(page_count))
    result: set[int] = set()
    for part in spec.replace(" ", "").lower().split(","):
        if part in ("odd", "even"):
            result.update(range(0 if part == "odd" else 1, page_count, 2))
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                start = int(a) if a else 1
                end = int(b) if b else page_count
            else:
                start = end = int(part)
        except ValueError:
            raise PageError(_("invalid page spec {spec!r} (use e.g. 1-3,5,8-)").format(
                spec=part)) from None
        if not (1 <= start <= end <= page_count):
            raise PageError(_("pages {pages} out of range (document has {count})").format(
                pages=part, count=page_count))
        result.update(range(start - 1, end))
    return sorted(result)


def page_count(data: bytes, password: str | None = None) -> int:
    reader = _open_reader(data, password)
    return len(list(_walk_pages(reader.root.raw_get("/Pages"))))


def signature_names(data: bytes, password: str | None = None) -> list[str]:
    """Names of the signed signature fields (what an edit would invalidate),
    including signatures in formats pyHanko can't validate."""
    reader = _open_reader(data, password)
    return [name for name, _value, _ref in enumerate_sig_fields(reader, filled_status=True)]


# --- rotate / crop (incremental) ----------------------------------------------------


def _incremental(data: bytes, password: str | None):
    w = IncrementalPdfFileWriter(io.BytesIO(data), strict=False)
    if w.prev.encrypted:
        if password is None:
            raise PasswordRequired(_("PDF is encrypted, a password is required"))
        w.encrypt(password)
    pages = [ref for ref, _page in _page_refs(w)]
    return w, pages


def _page_refs(w):
    """(IndirectObject, page dict) for every page in document order."""
    out = []

    def walk(ref):
        node = ref.get_object()
        if "/Kids" in node:
            for kid in node["/Kids"]:
                walk(kid)
        else:
            out.append((ref, node))

    walk(w.root.raw_get("/Pages"))
    return out


def _finish(w) -> bytes:
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def _check_pages(pages: Iterable[int], n: int) -> list[int]:
    pages = list(pages)
    if not pages:
        raise PageError(_("no pages selected"))
    bad = [p + 1 for p in pages if not 0 <= p < n]
    if bad:
        raise PageError(_("page {page} out of range (document has {count})").format(
            page=bad[0], count=n))
    return pages


def rotate(data: bytes, pages: Iterable[int], angle: int, password: str | None = None) -> bytes:
    """Rotate pages clockwise by ``angle`` (a multiple of 90, may be negative)."""
    if angle % 90:
        raise PageError(_("rotation must be a multiple of 90 degrees"))
    w, refs = _incremental(data, password)
    for i in _check_pages(pages, len(refs)):
        page = refs[i].get_object()
        current = int(find_inherited_value_in_tree(page, "/Rotate", "/Parent") or 0)
        page[generic.NameObject("/Rotate")] = generic.NumberObject((current + angle) % 360)
        w.update_container(page)
    return _finish(w)


def delete(data: bytes, pages: Iterable[int], password: str | None = None) -> bytes:
    """Remove ``pages`` (incremental update, so the encryption is kept).

    Form fields whose widgets were all on removed pages are removed from the
    form too; fields with widgets elsewhere lose only the removed widgets.
    """
    w, refs = _incremental(data, password)
    targets = sorted(set(_check_pages(pages, len(refs))))
    if len(targets) == len(refs):
        raise PageError(_("a document can't lose all its pages"))
    gone_pages = {_key(refs[i]) for i in targets}
    gone_annots = set()
    for i in targets:
        page = refs[i].get_object()
        for a in _array(page.get("/Annots")):
            if isinstance(a, generic.IndirectObject):
                gone_annots.add(_key(a))
    for i in targets:
        ref = refs[i]
        parent_ref = ref.get_object().raw_get("/Parent")
        parent = parent_ref.get_object()
        parent[generic.NameObject("/Kids")] = generic.ArrayObject(
            k for k in _array(parent["/Kids"]) if _key(k) != _key(ref))
        node = parent
        while node is not None:
            node[generic.NameObject("/Count")] = generic.NumberObject(int(node["/Count"]) - 1)
            w.update_container(node)
            up = node.raw_get("/Parent") if "/Parent" in node else None
            node = up.get_object() if up is not None else None
    _prune_fields(w, gone_pages, gone_annots)
    return _finish(w)


# Page attributes a page may inherit from its /Pages ancestors (ISO 32000-1, 7.7.3.4).
INHERITABLE = ("/Resources", "/MediaBox", "/CropBox", "/Rotate")


def move(data: bytes, pages: Iterable[int], to: int, password: str | None = None) -> bytes:
    """Move ``pages`` (in their document order) so they come before page
    ``to`` of the original numbering (``to`` = page count: to the end).

    Incremental update: the page tree becomes one flat /Kids list. Pages
    that inherited attributes (resources, page size, rotation) from an
    intermediate /Pages node get them copied in, so they look the same.
    """
    w, refs = _incremental(data, password)
    n = len(refs)
    moved = sorted(set(_check_pages(pages, n)))
    if not 0 <= to <= n:
        raise PageError(_("page {page} out of range (document has {count})").format(
            page=to + 1, count=n))
    order = ([i for i in range(to) if i not in moved] + moved
             + [i for i in range(to, n) if i not in moved])
    if order == list(range(n)):
        raise PageError(_("the pages are already in that order"))
    root_ref, root = _flatten(w, refs)
    root[generic.NameObject("/Kids")] = generic.ArrayObject(refs[i] for i in order)
    root[generic.NameObject("/Count")] = generic.NumberObject(n)
    w.update_container(root)
    return _finish(w)


def _flatten(w, refs):
    """Make every page a child of the root /Pages node (copying in what
    they inherited from intermediate nodes); returns (root ref, root)."""
    root_ref = w.root.raw_get("/Pages")
    root = root_ref.get_object()
    for ref in refs:
        page = ref.get_object()
        if _key(page.raw_get("/Parent")) == _key(root_ref):
            continue
        for key in INHERITABLE:
            if key not in page:
                value = find_inherited_value_in_tree(page, key, "/Parent")
                if value is not None:
                    page[generic.NameObject(key)] = value
        page[generic.NameObject("/Parent")] = root_ref
        w.update_container(page)
    return root_ref, root


# What a copied page takes along. Annotations (links, form fields, comments)
# are left behind: they point to other pages and to the document's form.
PAGE_KEYS = ("/Resources", "/MediaBox", "/CropBox", "/Rotate", "/Contents", "/UserUnit",
             "/Group", "/BleedBox", "/TrimBox", "/ArtBox")


def insert(data: bytes, source: bytes, pages: Iterable[int], at: int,
           password: str | None = None, source_password: str | None = None) -> bytes:
    """Copy ``pages`` of the PDF ``source`` into ``data`` before its page
    ``at`` (its page count: at the end), as an incremental update of
    ``data``. Content, resources, size and rotation come along; annotations
    and form fields don't. ``source`` may be ``data`` itself."""
    w, refs = _incremental(data, password)
    n = len(refs)
    if not 0 <= at <= n:
        raise PageError(_("page {page} out of range (document has {count})").format(
            page=at + 1, count=n))
    reader = _open_reader(source, source_password)
    src_pages = [page for page, _pid, _rot in _walk_pages(reader.root.raw_get("/Pages"))]
    selected = _check_pages(pages, len(src_pages))
    copies = generic.ArrayObject()
    for i in selected:
        page = src_pages[i]
        copy = generic.DictionaryObject({generic.NameObject("/Type"):
                                         generic.NameObject("/Page")})
        for key in PAGE_KEYS:
            if key in page:
                copy[generic.NameObject(key)] = page.raw_get(key)
            elif key in INHERITABLE:
                value = find_inherited_value_in_tree(page, key, "/Parent")
                if value is not None:
                    copy[generic.NameObject(key)] = value
        copies.append(copy)
    # in one go, so resources shared by the pages (fonts, images) are copied
    # once (pyHanko's importer: pinned version, see pyproject.toml)
    from pyhanko.pdf_utils.writer import _ObjectImporter

    imported = _ObjectImporter(source=reader, target=w, obj_stream=None,
                               reference_map={}).import_object(copies)
    root_ref, root = _flatten(w, refs)
    new_refs = []
    for copy in imported:
        copy[generic.NameObject("/Parent")] = root_ref
        new_refs.append(w.add_object(copy))
    root[generic.NameObject("/Kids")] = generic.ArrayObject(refs[:at] + new_refs + refs[at:])
    root[generic.NameObject("/Count")] = generic.NumberObject(n + len(new_refs))
    w.update_container(root)
    return _finish(w)


def _array(value) -> list:
    """An array entry as a list, also when it is an indirect (or, in an
    encrypted file, a decryption proxy of an) array; missing -> []."""
    if value is None:
        return []
    return list(value.get_object() if hasattr(value, "get_object") else value)


def _key(ref) -> tuple[int, int]:
    return ref.idnum, ref.generation


def _prune_fields(w, gone_pages: set, gone_annots: set) -> None:
    acro_ref = w.root.raw_get("/AcroForm") if "/AcroForm" in w.root else None
    if acro_ref is None:
        return
    acro = acro_ref.get_object()

    def on_gone_page(ref) -> bool:
        obj = ref.get_object()
        page = obj.raw_get("/P") if "/P" in obj else None
        return _key(ref) in gone_annots or (page is not None and _key(page) in gone_pages)

    def keep(ref) -> bool:
        """False if the field (or widget) only lived on removed pages."""
        if not isinstance(ref, generic.IndirectObject):
            return True
        obj = ref.get_object()
        kids = _array(obj.get("/Kids"))
        if kids:
            remaining = [k for k in kids if keep(k)]
            if len(remaining) != len(kids):
                obj[generic.NameObject("/Kids")] = generic.ArrayObject(remaining)
                w.update_container(obj)
            return bool(remaining)
        return not on_gone_page(ref)

    fields = _array(acro.get("/Fields"))
    remaining = [f for f in fields if keep(f)]
    if len(remaining) != len(fields):
        acro[generic.NameObject("/Fields")] = generic.ArrayObject(remaining)
        w.update_container(acro)


def _box(page, key: str) -> Box:
    value = find_inherited_value_in_tree(page, key, "/Parent")
    if value is None:
        return _box(page, "/MediaBox") if key != "/MediaBox" else (0, 0, 612, 792)
    x1, y1, x2, y2 = (float(v) for v in value)
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


def margins_to_user_space(margins: Sequence[float], rotation: int) -> tuple[float, ...]:
    """(top, right, bottom, left) as seen on screen -> (left, bottom, right, top)
    in unrotated user space, for a page with clockwise /Rotate ``rotation``."""
    top, right, bottom, left = margins
    return {
        0: (left, bottom, right, top),
        90: (top, left, bottom, right),
        180: (right, top, left, bottom),
        270: (bottom, right, top, left),
    }[rotation % 360]


def crop(data: bytes, pages: Iterable[int], *, box: Box | None = None,
         margins: Sequence[float] | None = None, reset: bool = False,
         password: str | None = None) -> bytes:
    """Set the visible area (/CropBox) of pages.

    * ``box``: x1,y1,x2,y2 in PDF user space (as for signing);
    * ``margins``: top,right,bottom,left in points as seen on screen, taken
      off the current visible area;
    * ``reset``: show the whole page again (CropBox = MediaBox).
    """
    if sum(x is not None and x is not False for x in (box, margins, reset or None)) != 1:
        raise PageError(_("give exactly one of box, margins or reset"))
    w, refs = _incremental(data, password)
    for i in _check_pages(pages, len(refs)):
        page = refs[i].get_object()
        media = _box(page, "/MediaBox")
        if reset:
            new = media
        elif box is not None:
            new = box
        else:
            rotation = int(find_inherited_value_in_tree(page, "/Rotate", "/Parent") or 0)
            left, bottom, right, top = margins_to_user_space(margins, rotation)
            x1, y1, x2, y2 = _box(page, "/CropBox")
            new = (x1 + left, y1 + bottom, x2 - right, y2 - top)
        # keep it inside the MediaBox
        new = (max(new[0], media[0]), max(new[1], media[1]),
               min(new[2], media[2]), min(new[3], media[3]))
        if new[2] - new[0] < 1 or new[3] - new[1] < 1:
            raise PageError(_("crop leaves nothing of page {page}").format(page=i + 1))
        page[generic.NameObject("/CropBox")] = generic.ArrayObject(
            generic.FloatObject(round(v, 3)) for v in new)
        w.update_container(page)
    return _finish(w)


# --- split / merge (new documents) ----------------------------------------------------


def _reader(data: bytes, password: str | None):
    from pypdf import PdfReader

    r = PdfReader(io.BytesIO(data), strict=False)
    if r.is_encrypted:
        if password is None:
            raise PasswordRequired(_("PDF is encrypted, a (correct) password is required"))
        if not r.decrypt(password):
            raise WrongPassword(_("PDF is encrypted, a (correct) password is required"))
    return r


def _drop_signed_fields(writer) -> None:
    """Remove signed signature widgets and fields: their signatures can't be
    valid in a new document, and keeping the stamps would look signed."""
    from pypdf.generic import ArrayObject, NameObject

    def is_signed_sig(obj) -> bool:
        while obj is not None:
            if obj.get("/FT") == "/Sig":
                return "/V" in obj
            obj = obj.get("/Parent")
            obj = obj.get_object() if obj is not None else None
        return False

    for page in writer.pages:
        annots = page.get("/Annots")
        if annots is None:
            continue
        keep = ArrayObject(a for a in annots.get_object() if not is_signed_sig(a.get_object()))
        page[NameObject("/Annots")] = keep
    acro = writer._root_object.get("/AcroForm")
    if acro is not None:
        acro = acro.get_object()
        fields = acro.get("/Fields")
        if fields is not None:
            acro[NameObject("/Fields")] = ArrayObject(
                f for f in fields.get_object() if not is_signed_sig(f.get_object()))
        if "/SigFlags" in acro:
            del acro["/SigFlags"]


def _write(writer) -> bytes:
    _drop_signed_fields(writer)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def split(data: bytes, parts: Sequence[Sequence[int]], password: str | None = None) -> list[bytes]:
    """One new document per entry in ``parts`` (each a list of 0-based pages)."""
    from pypdf import PdfWriter

    reader = _reader(data, password)
    n = len(reader.pages)
    out = []
    for part in parts:
        writer = PdfWriter()
        writer.append(reader, pages=_check_pages(part, n))
        out.append(_write(writer))
    return out


def chunks(n_pages: int, every: int) -> list[list[int]]:
    if every < 1:
        raise PageError(_("--every needs a positive number"))
    return [list(range(i, min(i + every, n_pages))) for i in range(0, n_pages, every)]


def merge(inputs: Sequence[tuple[bytes, Sequence[int] | None, str | None]]) -> bytes:
    """Concatenate (data, pages or None for all, password) inputs into a new PDF."""
    from pypdf import PdfWriter

    if not inputs:
        raise PageError(_("nothing to merge"))
    writer = PdfWriter()
    for data, pages, password in inputs:
        reader = _reader(data, password)
        sel = list(range(len(reader.pages))) if pages is None else _check_pages(
            pages, len(reader.pages))
        writer.append(reader, pages=sel)
    return _write(writer)


# --- encrypt (a new document) ----------------------------------------------------------

ENCRYPTION = "AES-256"


def encrypt(data: bytes, new_password: str, password: str | None = None) -> bytes:
    """The document encrypted with ``new_password`` (AES-256), which opens
    it and allows everything. An encrypted document (``password``: its
    current one) gets the new password instead."""
    from pypdf import PdfWriter

    if not new_password:
        raise PageError(_("the new password is empty"))
    writer = PdfWriter(clone_from=_reader(data, password))
    writer.encrypt(user_password=new_password, algorithm=ENCRYPTION)
    return _write(writer)
