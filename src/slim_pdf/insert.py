"""Add text and images to pages.

The text or image becomes part of the page's content, drawn on top of what
is there, in an incremental update (earlier revisions stay byte-identical,
encryption is kept). Like other page changes this invalidates existing
signatures (pyHanko accepts neither new page content nor new annotations
after a signature), so callers check :func:`pages.signature_names` first.

Text is drawn in Helvetica and must be representable in Windows-1252
(Latin script, €), as in forms.py. Images can be PNG, JPEG and other
formats Pillow reads, or the first page of a PDF (as vector graphics).
Everything is drawn upright as the page is displayed, also on pages with
/Rotate.
"""

from __future__ import annotations

from pathlib import Path

from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.rw_common import find_inherited_value_in_tree

from . import forms
from .i18n import _
from .pages import PageError, _check_pages, _finish, _incremental

Box = tuple[float, float, float, float]
Color = tuple[float, float, float]

PADDING = 2.0  # around text, in points
LEADING = 1.2  # line distance, times the font size
FONT_NAME = "/Helv"


def _lines(text: str) -> list[bytes]:
    raw = forms._encode(text, _("Text"))
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")


def text_size(text: str, size: float) -> tuple[float, float]:
    """(width, height) in points that ``text`` needs unwrapped at ``size``,
    as shown on the page (with padding)."""
    font = forms._Font(FONT_NAME, None, None)
    lines = _lines(text)
    width = max((font.width(line, size) for line in lines), default=0.0)
    return width + 2 * PADDING + 1, len(lines) * size * LEADING + 2 * PADDING


def image_aspect(path: str | Path) -> float:
    """Width / height of an image file as it will be drawn."""
    from pyhanko.pdf_utils.writer import PdfFileWriter

    content = _image_content(path)
    content.set_writer(PdfFileWriter())
    try:
        content.render()  # an imported PDF page knows its size once rendered
    except Exception as e:  # noqa: BLE001 - malformed PDFs raise anything
        raise PageError(_("cannot read the image {path}: {error}").format(
            path=path, error=e)) from None
    return content.box.width / content.box.height


def _image_content(path: str | Path):
    from PIL import Image, UnidentifiedImageError
    from pyhanko.pdf_utils.content import ImportedPdfPage
    from pyhanko.pdf_utils.images import PdfImage

    path = Path(path)
    if path.suffix.lower() == ".pdf":
        if not path.is_file():
            raise PageError(_("no such file: {path}").format(path=path))
        return ImportedPdfPage(str(path), page_ix=0)
    try:
        img = Image.open(path)
        img.load()
    except (OSError, UnidentifiedImageError) as e:
        raise PageError(_("cannot read the image {path}: {error}").format(
            path=path, error=e)) from None
    return PdfImage(img)


# --- drawing on a page -------------------------------------------------------------


def _page(refs, page: int) -> tuple[generic.DictionaryObject, int]:
    [index] = _check_pages([page], len(refs))
    obj = refs[index].get_object()
    rot = int(find_inherited_value_in_tree(obj, "/Rotate", "/Parent") or 0) % 360
    return obj, rot if rot in (0, 90, 180, 270) else 0


def _display_size(box: Box, rot: int) -> tuple[float, float]:
    w, h = abs(box[2] - box[0]), abs(box[3] - box[1])
    return (h, w) if rot in (90, 270) else (w, h)


def _resource_dict(w, parent, key: str) -> generic.DictionaryObject:
    """``parent[key]`` as a dictionary that can be changed: created if
    missing; an indirect one is marked for the update."""
    if key in parent:
        raw = parent.raw_get(key)
        if isinstance(raw, generic.IndirectObject):
            w.mark_update(raw)
        return raw.get_object()
    d = generic.DictionaryObject()
    parent[generic.pdf_name(key)] = d
    return d


def _draw_on_page(w, page, box: Box, xobject_ref) -> None:
    """Paint the Form XObject (sized and turned by forms.appearance) into
    ``box`` (PDF user space), on top of the page's content."""
    if "/Resources" not in page:  # inherited: the page gets its own copy
        inherited = find_inherited_value_in_tree(page, "/Resources", "/Parent")
        page[generic.pdf_name("/Resources")] = generic.DictionaryObject(
            dict(inherited.get_object()) if inherited is not None else {})
    resources = _resource_dict(w, page, "/Resources")
    xobjects = _resource_dict(w, resources, "/XObject")
    n = 1
    while f"/PdfsInsert{n}" in xobjects:
        n += 1
    name = f"/PdfsInsert{n}"
    xobjects[generic.pdf_name(name)] = xobject_ref

    x, y = min(box[0], box[2]), min(box[1], box[3])
    # the old content in q ... Q, so whatever state it leaves doesn't affect ours
    before = w.add_object(generic.StreamObject(stream_data=b"q\n"))
    after = w.add_object(generic.StreamObject(
        stream_data=f"\nQ\nq 1 0 0 1 {forms._num(x)} {forms._num(y)} cm {name} Do Q\n".encode()))
    contents = page.raw_get("/Contents") if "/Contents" in page else None
    old = contents.get_object() if contents is not None else None
    if isinstance(old, generic.ArrayObject):
        parts = [old.raw_get(i) for i in range(len(old))]
    else:
        parts = [contents] if contents is not None else []
    page[generic.pdf_name("/Contents")] = generic.ArrayObject([before, *parts, after])
    w.update_container(page)


def _helvetica(w) -> generic.IndirectObject:
    """A new Helvetica font object (WinAnsi), for text drawn on pages."""
    return w.add_object(generic.DictionaryObject({
        generic.pdf_name("/Type"): generic.pdf_name("/Font"),
        generic.pdf_name("/Subtype"): generic.pdf_name("/Type1"),
        generic.pdf_name("/BaseFont"): generic.pdf_name("/Helvetica"),
        generic.pdf_name("/Encoding"): generic.pdf_name("/WinAnsiEncoding"),
    }))


def add_text(data: bytes, page: int, box: Box, text: str, *, size: float = 11.0,
             color: Color = (0.0, 0.0, 0.0), password: str | None = None) -> bytes:
    """Draw ``text`` in ``box`` (PDF user space) on ``page`` (0-based).
    Lines are wrapped to the box's width as displayed; text beyond its
    height is cut off. Characters outside Windows-1252 raise forms.FormError."""
    if not text.strip():
        raise PageError(_("there is no text to add"))
    if size <= 0:
        raise PageError(_("the font size must be positive"))
    w, refs = _incremental(data, password)
    page_obj, rot = _page(refs, page)
    width, height = _display_size(box, rot)
    font = forms._Font(FONT_NAME, None, None)
    lines = forms._wrap(_lines(text), font, size, max(width - 2 * PADDING, 1))
    rgb = " ".join(forms._num(c) for c in color)
    clip = f"0 0 {forms._num(width)} {forms._num(height)} re W n"
    ops = [f"q {clip} BT {FONT_NAME} {forms._num(size)} Tf {rgb} rg"]
    y = height - PADDING - size * forms.ASCENT
    ops.append(f"{forms._num(PADDING)} {forms._num(y)} Td")
    for i, line in enumerate(lines):
        if i:
            ops.append(f"0 {forms._num(-size * LEADING)} Td")
        ops.append(forms._pdf_string(line).decode("latin-1") + " Tj")
    ops.append("ET Q")
    resources = generic.DictionaryObject({generic.pdf_name("/Font"): generic.DictionaryObject(
        {generic.pdf_name(FONT_NAME): _helvetica(w)})})
    xobj = forms.appearance(w, width, height, rot, "\n".join(ops).encode("latin-1"), resources)
    _draw_on_page(w, page_obj, box, xobj)
    return _finish(w)


def add_image(data: bytes, page: int, box: Box, image: str | Path, *,
              password: str | None = None) -> bytes:
    """Draw the image file ``image`` stretched to ``box`` (PDF user space)
    on ``page`` (0-based)."""
    w, refs = _incremental(data, password)
    page_obj, rot = _page(refs, page)
    width, height = _display_size(box, rot)
    content = _image_content(image)
    content.set_writer(w)
    try:
        img = w.add_object(content.as_form_xobject())
    except Exception as e:  # noqa: BLE001 - malformed PDFs and images raise anything
        raise PageError(_("cannot read the image {path}: {error}").format(
            path=image, error=e)) from None
    sx, sy = width / content.box.width, height / content.box.height
    ops = f"q {forms._num(sx)} 0 0 {forms._num(sy)} 0 0 cm /Img Do Q".encode()
    resources = generic.DictionaryObject({generic.pdf_name("/XObject"): generic.DictionaryObject(
        {generic.pdf_name("/Img"): img})})
    _draw_on_page(w, page_obj, box, forms.appearance(w, width, height, rot, ops, resources))
    return _finish(w)
