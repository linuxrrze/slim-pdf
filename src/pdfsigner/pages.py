"""Page operations: rotate, crop, split, merge.

* ``rotate`` and ``crop`` are written as incremental updates (like
  signatures), so earlier revisions stay byte-identical and the encryption
  is kept. They still change pages, which invalidates existing signatures;
  callers check :func:`signature_names` first and ask before doing so.
* ``split`` and ``merge`` create new documents (via pypdf). Signatures can't
  be carried over, so signed signature fields are dropped from the output;
  empty signature fields are kept. Encrypted inputs are decrypted; the output
  is not encrypted.

Page numbers are 0-based here; :func:`parse_pages` converts the 1-based
specs used on the command line ("1-3,5,8-", "odd", "even", "all").
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Sequence

from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.rw_common import find_inherited_value_in_tree

from .signing import SigningBlocked, SigningError, _open_reader, _walk_pages

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
            raise PageError(f"invalid page spec {part!r} (use e.g. 1-3,5,8-)") from None
        if not (1 <= start <= end <= page_count):
            raise PageError(f"pages {part} out of range (document has {page_count})")
        result.update(range(start - 1, end))
    return sorted(result)


def page_count(data: bytes, password: str | None = None) -> int:
    reader = _open_reader(data, password)
    return len(list(_walk_pages(reader.root.raw_get("/Pages"))))


def signature_names(data: bytes, password: str | None = None) -> list[str]:
    """Names of the signed signature fields (what an edit would invalidate)."""
    reader = _open_reader(data, password)
    return [s.field_name for s in reader.embedded_signatures]


# --- rotate / crop (incremental) ----------------------------------------------------


def _incremental(data: bytes, password: str | None):
    w = IncrementalPdfFileWriter(io.BytesIO(data), strict=False)
    if w.prev.encrypted:
        if password is None:
            raise SigningError("PDF is encrypted, a password is required")
        w.encrypt(password)
    pages = [ref for ref, _ in _page_refs(w)]
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
        raise PageError("no pages selected")
    bad = [p + 1 for p in pages if not 0 <= p < n]
    if bad:
        raise PageError(f"page {bad[0]} out of range (document has {n})")
    return pages


def rotate(data: bytes, pages: Iterable[int], angle: int, password: str | None = None) -> bytes:
    """Rotate pages clockwise by ``angle`` (a multiple of 90, may be negative)."""
    if angle % 90:
        raise PageError("rotation must be a multiple of 90 degrees")
    w, refs = _incremental(data, password)
    for i in _check_pages(pages, len(refs)):
        page = refs[i].get_object()
        current = int(find_inherited_value_in_tree(page, "/Rotate", "/Parent") or 0)
        page[generic.NameObject("/Rotate")] = generic.NumberObject((current + angle) % 360)
        w.update_container(page)
    return _finish(w)


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
        raise PageError("give exactly one of box, margins or reset")
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
            raise PageError(f"crop leaves nothing of page {i + 1}")
        page[generic.NameObject("/CropBox")] = generic.ArrayObject(
            generic.FloatObject(round(v, 3)) for v in new)
        w.update_container(page)
    return _finish(w)


# --- split / merge (new documents) ----------------------------------------------------


def _reader(data: bytes, password: str | None):
    from pypdf import PdfReader

    r = PdfReader(io.BytesIO(data), strict=False)
    if r.is_encrypted:
        if password is None or not r.decrypt(password):
            raise SigningError("PDF is encrypted, a (correct) password is required")
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
        raise PageError("--every needs a positive number")
    return [list(range(i, min(i + every, n_pages))) for i in range(0, n_pages, every)]


def merge(inputs: Sequence[tuple[bytes, Sequence[int] | None, str | None]]) -> bytes:
    """Concatenate (data, pages or None for all, password) inputs into a new PDF."""
    from pypdf import PdfWriter

    if not inputs:
        raise PageError("nothing to merge")
    writer = PdfWriter()
    for data, pages, password in inputs:
        reader = _reader(data, password)
        sel = list(range(len(reader.pages))) if pages is None else _check_pages(
            pages, len(reader.pages))
        writer.append(reader, pages=sel)
    return _write(writer)
