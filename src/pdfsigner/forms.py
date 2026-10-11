"""Form fields (AcroForm): list them and fill them in.

:func:`fill` writes an incremental update, as signing does: earlier
revisions stay byte-identical, the encryption is kept, and signatures stay
valid unless they lock the fields being filled (those are refused, as are
read-only fields and documents certified as "no changes allowed").

Viewers show a field's appearance stream, not its value, so filling also
draws new appearances: text in the field's font (from its /DA) when that is
a simple WinAnsi font, otherwise in Helvetica; check boxes and radio
buttons switch to the appearance states the document already has. Text
must be representable in Windows-1252 (Latin script, €, typographic
quotes); other characters are refused with an error.

XFA forms are not supported; their AcroForm fallback fields, if any, are.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.sign.fields import MDPPerm
from pyhanko.sign.validation.pdf_embedded import read_certification_data

from .i18n import _
from .signing import (
    PasswordRequired,
    SigningError,
    _get,
    _locked_field_names,
    _open_reader,
    _walk_pages,
)

Box = tuple[float, float, float, float]

TEXT, CHECKBOX, RADIO, CHOICE = "text", "checkbox", "radio", "choice"

# field flags (/Ff), PDF 32000-1 tables 221, 226, 228, 230
READ_ONLY = 1 << 0
MULTILINE = 1 << 12
PASSWORD = 1 << 13
RADIO_FLAG = 1 << 15
PUSHBUTTON = 1 << 16
COMBO = 1 << 17
EDIT = 1 << 18
MULTI_SELECT = 1 << 21
COMB = 1 << 24

INHERITABLE = ("/FT", "/Ff", "/V", "/DA", "/Q", "/Opt", "/MaxLen")


class FormError(SigningError):
    pass


@dataclass
class Widget:
    page: int | None  # 0-based; None if the widget isn't on any page
    rect: Box
    state: str = ""  # check box / radio button: the widget's "on" state


@dataclass
class FormField:
    name: str  # fully qualified ("parent.child")
    kind: str  # TEXT, CHECKBOX, RADIO or CHOICE
    value: str | bool  # text, the selected choice or radio state; a check box: bool
    label: str = ""  # what the form calls it for people (/TU), if anything
    options: list[tuple[str, str]] = field(default_factory=list)  # (value, label)
    read_only: bool = False
    locked: bool = False  # locked by an existing signature (FieldMDP)
    multiline: bool = False
    combo: bool = False  # a choice field: drop-down (else a list box)
    editable: bool = False  # a drop-down that accepts other text too
    max_len: int | None = None
    widgets: list[Widget] = field(default_factory=list)

    @property
    def fillable(self) -> bool:
        return not (self.read_only or self.locked)

    @property
    def page(self) -> int | None:
        return next((w.page for w in self.widgets if w.page is not None), None)


# --- reading ------------------------------------------------------------------------


def _text(obj) -> str:
    obj = obj.get_object() if obj is not None else None
    if obj is None:
        return ""
    if isinstance(obj, generic.StreamObject):  # rich text and long values may be streams
        return obj.data.decode("utf-8", "replace")
    if isinstance(obj, generic.NameObject):
        return str(obj)[1:]
    if isinstance(obj, generic.ByteStringObject):
        return obj.original_bytes.decode("latin-1")
    return str(obj)


def _terminal_fields(acro):
    """(name, field dict, attrs, [(widget dict, widget ref)]) for each terminal
    field; attrs holds the inheritable entries as they apply to it."""
    out = []

    def walk(ref, parent: str, inherited: dict) -> None:
        node = ref.get_object()
        t = _text(_get(node, "/T")) if "/T" in node else ""
        name = f"{parent}.{t}" if parent and t else (t or parent)
        attrs = dict(inherited)
        for key in INHERITABLE:
            if key in node:
                attrs[key] = node[key]
        kids = _get(node, "/Kids")
        kids = [kids.raw_get(i) for i in range(len(kids))] if kids else []
        if any("/T" in k.get_object() for k in kids):
            for k in kids:
                if "/T" in k.get_object():
                    walk(k, name, attrs)
            return
        widgets = [(k.get_object(), k) for k in kids] or [(node, ref)]
        out.append((name, node, attrs, widgets))

    fields = _get(acro, "/Fields")
    for i in range(len(fields) if fields else 0):
        walk(fields.raw_get(i), "", {})
    return out


def _kind(attrs) -> str | None:
    ft, ff = _get(attrs, "/FT"), int(_get(attrs, "/Ff", 0))
    if ft == "/Tx":
        return TEXT
    if ft == "/Ch":
        return CHOICE
    if ft == "/Btn" and not ff & PUSHBUTTON:
        return RADIO if ff & RADIO_FLAG else CHECKBOX
    return None  # signatures, push buttons


def _on_state(widget) -> str:
    ap = _get(widget, "/AP")
    normal = _get(ap, "/N") if ap is not None else None
    if isinstance(normal, generic.DictionaryObject):
        for key in normal:
            if key != "/Off":
                return str(key)[1:]
    return ""


def _options(attrs) -> list[tuple[str, str]]:
    opts = _get(attrs, "/Opt")
    out = []
    for o in opts or ():
        o = o.get_object()
        if isinstance(o, generic.ArrayObject) and len(o) >= 2:
            out.append((_text(o[0]), _text(o[1])))
        else:
            out.append((_text(o), _text(o)))
    return out


def _annot_pages(root) -> dict[int, int]:
    pages = {}
    for i, (page, _pid, _rot) in enumerate(_walk_pages(root.raw_get("/Pages"))):
        annots = _get(page, "/Annots")
        annots = annots.get_object() if annots is not None else None
        for j in range(len(annots) if annots else 0):
            ref = annots.raw_get(j)
            if isinstance(ref, generic.IndirectObject):
                pages[ref.idnum] = i
    return pages


def _is_locked(name: str, locked: set[str], all_locked: bool) -> bool:
    if all_locked:
        return f"!{name}" not in locked
    return name in locked


def _read(root, locked=(set(), False)) -> list[tuple[FormField, object, dict, list]]:
    acro = _get(root, "/AcroForm")
    if acro is None:
        return []
    annot_pages = _annot_pages(root)
    out = []
    for name, node, attrs, widgets in _terminal_fields(acro.get_object()):
        kind = _kind(attrs)
        if kind is None:
            continue
        ff = int(_get(attrs, "/Ff", 0))
        v = _get(attrs, "/V")
        f = FormField(name=name, kind=kind, value="", label=_text(_get(node, "/TU")),
                      read_only=bool(ff & READ_ONLY),
                      locked=_is_locked(name, *locked), multiline=bool(ff & MULTILINE),
                      combo=bool(ff & COMBO), editable=bool(ff & EDIT))
        if "/MaxLen" in attrs:
            f.max_len = int(attrs["/MaxLen"])
        for wd, ref in widgets:
            rect = _get(wd, "/Rect")
            box = tuple(float(x) for x in rect) if rect else (0.0, 0.0, 0.0, 0.0)
            box = (min(box[0], box[2]), min(box[1], box[3]),
                   max(box[0], box[2]), max(box[1], box[3]))
            page = annot_pages.get(ref.idnum) if isinstance(ref, generic.IndirectObject) \
                else None
            f.widgets.append(Widget(page, box, _on_state(wd) if kind in (CHECKBOX, RADIO)
                                    else ""))
        if kind == CHECKBOX:
            f.value = v is not None and _text(v) not in ("", "Off")
            f.options = [(w.state, w.state) for w in f.widgets[:1] if w.state]
        elif kind == RADIO:
            f.value = "" if v is None or _text(v) == "Off" else _text(v)
            states = list(dict.fromkeys(w.state for w in f.widgets if w.state))
            f.options = [(s, s) for s in states]
        elif kind == CHOICE:
            f.options = _options(attrs)
            if isinstance(v, generic.ArrayObject):  # a multi-select list box
                v = v[0] if len(v) else None
            f.value = _text(v) if v is not None else ""
        else:
            f.value = _text(v) if v is not None else ""
        out.append((f, node, attrs, widgets))
    return out


def form_fields(data: bytes, password: str | None = None) -> list[FormField]:
    """The fillable kinds of fields (text, check box, radio, choice), in the
    order of the form; signature fields and push buttons are left out."""
    reader = _open_reader(data, password)
    return [f for f, *_rest in _read(reader.root, _locked_field_names(reader))]


# --- appearances ----------------------------------------------------------------------

# Helvetica's glyph widths (1/1000 em) for Windows-1252 bytes 32..255, from
# Adobe's Core 14 AFM files ("Helvetica.afm", Copyright (c) 1985, 1987,
# 1989, 1990, 1997 Adobe Systems Incorporated. All Rights Reserved), which
# may be used, copied and distributed for any purpose and without charge.
HELVETICA_WIDTHS = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667,
    667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722,
    667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500,
    556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278,
    556, 500, 722, 500, 500, 500, 334, 260, 334, 584, 0, 556, 0, 222, 556, 333, 1000, 556,
    556, 333, 1000, 667, 333, 1000, 0, 611, 0, 0, 222, 222, 333, 333, 350, 556, 1000, 333,
    1000, 500, 333, 944, 0, 500, 667, 0, 333, 556, 556, 556, 556, 260, 556, 333, 737, 370,
    556, 584, 333, 737, 333, 400, 584, 333, 333, 333, 556, 537, 278, 333, 333, 365, 556, 834,
    834, 834, 611, 667, 667, 667, 667, 667, 667, 1000, 722, 667, 667, 667, 667, 278, 278,
    278, 278, 722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667,
    611, 556, 556, 556, 556, 556, 556, 889, 500, 556, 556, 556, 556, 278, 278, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 584, 611, 556, 556, 556, 556, 500, 556, 500,
]
ASCENT, DESCENT = 0.718, -0.207  # Helvetica, in em; good enough for other fonts
AUTO_SIZE_MAX = 12.0
MIN_SIZE = 4.0
OWN_FONT = "/PdfsHelv"  # our Helvetica, when the field's font won't do


def _num(x) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".") or "0"


def _pdf_string(raw: bytes) -> bytes:
    out = bytearray(b"(")
    for b in raw:
        if b in b"()\\":
            out += b"\\" + bytes([b])
        elif b < 32 or b > 126:
            out += b"\\%03o" % b
        else:
            out.append(b)
    return bytes(out + b")")


def _color_op(arr, stroke: bool) -> str | None:
    vals = [float(x) for x in arr or ()]
    op = {1: "g", 3: "rg", 4: "k"}.get(len(vals))
    if op is None:
        return None
    return " ".join(map(_num, vals)) + " " + (op.upper() if stroke else op)


class _Font:
    """The font text is drawn in: name in the resources, reference, widths."""

    def __init__(self, name: str, ref, widths: dict[int, float] | None, mono: bool = False):
        self.name, self.ref, self.widths, self.mono = name, ref, widths, mono

    def width(self, raw: bytes, size: float) -> float:
        if self.mono:
            return len(raw) * 0.6 * size
        if self.widths is not None:
            return sum(self.widths.get(b, 500) for b in raw) * size / 1000
        return sum(HELVETICA_WIDTHS[b - 32] if b >= 32 else 0 for b in raw) * size / 1000


def _parse_da(da: str) -> tuple[str | None, float, str]:
    """/DA -> (font name, size, colour operator)."""
    font, size, color = None, 0.0, "0 g"
    if m := re.search(r"(/[^\s/\[\]()<>]+)\s+([-\d.]+)\s+Tf", da):
        font, size = m.group(1), float(m.group(2))
    for m in re.finditer(r"((?:[-\d.]+\s+){1,4})(g|rg|k)\b", da):
        nums = m.group(1).split()
        if len(nums) == {"g": 1, "rg": 3, "k": 4}[m.group(2)]:
            color = " ".join(nums) + " " + m.group(2)
    return font, size, color


class _Painter:
    """Builds appearance streams for one fill, sharing the fallback font."""

    def __init__(self, w: IncrementalPdfFileWriter, acro):
        self.w, self.acro = w, acro
        self._own = None

    def own_font(self) -> _Font:
        if self._own is None:
            ref = self.w.add_object(generic.DictionaryObject({
                generic.pdf_name("/Type"): generic.pdf_name("/Font"),
                generic.pdf_name("/Subtype"): generic.pdf_name("/Type1"),
                generic.pdf_name("/BaseFont"): generic.pdf_name("/Helvetica"),
                generic.pdf_name("/Encoding"): generic.pdf_name("/WinAnsiEncoding"),
            }))
            self._own = _Font(OWN_FONT, ref, None)
        return self._own

    def font(self, name: str | None) -> _Font:
        """The /DA font from the form's resources, if it is a simple font in
        WinAnsiEncoding (what we encode text in); else our Helvetica."""
        dr = _get(self.acro, "/DR")
        fonts = _get(dr, "/Font") if dr is not None else None
        if not name or fonts is None or name not in fonts:
            return self.own_font()
        ref = fonts.raw_get(name)
        f = ref.get_object()
        if _get(f, "/Subtype") not in ("/Type1", "/TrueType") or \
                _get(f, "/Encoding") != "/WinAnsiEncoding":
            return self.own_font()
        base = str(_get(f, "/BaseFont", ""))
        widths = None
        if "/Widths" in f and "/FirstChar" in f:
            first = int(f["/FirstChar"])
            widths = {first + i: float(x) for i, x in enumerate(f["/Widths"])}
        return _Font(name, ref, widths, mono="Courier" in base and widths is None)

    def stream(self, widget, body: bytes, font: _Font | None) -> generic.IndirectObject:
        """A Form XObject for ``widget``: its background and border (/MK, /BS),
        then ``body`` drawn in the unrotated box (see :meth:`box`)."""
        width, height, rot = self.box(widget)
        mk = _get(widget, "/MK") or {}
        bw = self.border_width(widget)
        ops = []
        if bg := _color_op(_get(mk, "/BG"), False):
            ops.append(f"q {bg} 0 0 {_num(width)} {_num(height)} re f Q")
        if bw and (bc := _color_op(_get(mk, "/BC"), True)):
            ops.append(f"q {bc} {_num(bw)} w {_num(bw / 2)} {_num(bw / 2)} "
                       f"{_num(width - bw)} {_num(height - bw)} re S Q")
        content = ("\n".join(ops) + "\n").encode() + body if ops else body
        matrix = {0: [1, 0, 0, 1, 0, 0], 90: [0, 1, -1, 0, height, 0],
                  180: [-1, 0, 0, -1, width, height], 270: [0, -1, 1, 0, 0, width]}[rot]
        resources = generic.DictionaryObject()
        if font is not None:
            resources[generic.pdf_name("/Font")] = generic.DictionaryObject(
                {generic.pdf_name(font.name): font.ref})
        stream = generic.StreamObject({
            generic.pdf_name("/Type"): generic.pdf_name("/XObject"),
            generic.pdf_name("/Subtype"): generic.pdf_name("/Form"),
            generic.pdf_name("/BBox"): generic.ArrayObject(
                generic.FloatObject(v) for v in (0, 0, width, height)),
            generic.pdf_name("/Matrix"): generic.ArrayObject(
                generic.NumberObject(v) if isinstance(v, int) else generic.FloatObject(v)
                for v in matrix),
            generic.pdf_name("/Resources"): resources,
        }, stream_data=content)
        return self.w.add_object(stream)

    @staticmethod
    def box(widget) -> tuple[float, float, int]:
        """(width, height, /MK /R): the box text is laid out in."""
        r = [float(x) for x in _get(widget, "/Rect") or (0, 0, 0, 0)]
        width, height = abs(r[2] - r[0]), abs(r[3] - r[1])
        mk = _get(widget, "/MK") or {}
        rot = int(_get(mk, "/R", 0)) % 360
        if rot not in (0, 90, 180, 270):
            rot = 0
        return (height, width, rot) if rot in (90, 270) else (width, height, rot)

    @staticmethod
    def border_width(widget) -> float:
        mk = _get(widget, "/MK") or {}
        if not _color_op(_get(mk, "/BC"), True):
            return 0.0
        bs = _get(widget, "/BS")
        return float(_get(bs, "/W", 1)) if bs is not None else 1.0


def _encode(text: str, field_name: str) -> bytes:
    try:
        return text.encode("cp1252")
    except UnicodeEncodeError:
        bad = "".join(sorted({c for c in text if not _encodable(c)}))
        raise FormError(_("{field}: the form can't show these characters: {chars}").format(
            field=field_name, chars=bad)) from None


def _encodable(c: str) -> bool:
    try:
        c.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def _wrap(lines: list[bytes], font: _Font, size: float, width: float) -> list[bytes]:
    """Break each line into lines no wider than ``width`` (at spaces when
    possible)."""
    out = []
    for line in lines:
        words = line.split(b" ")
        cur = b""
        for word in words:
            cand = word if not cur else cur + b" " + word
            if font.width(cand, size) <= width or not cur and len(word) <= 1:
                cur = cand
                continue
            if cur:
                out.append(cur)
            cur = word
            while len(cur) > 1 and font.width(cur, size) > width:  # a word too long
                cut = len(cur) - 1
                while cut > 1 and font.width(cur[:cut], size) > width:
                    cut -= 1
                out.append(cur[:cut])
                cur = cur[cut:]
        out.append(cur)
    return out


def _text_body(painter: _Painter, widget, attrs, value: str, f: FormField,
               lines_override: list[tuple[str, bool]] | None = None) -> tuple[bytes, _Font]:
    """The /Tx marked content of a text or choice field's appearance."""
    da = _text(_get(attrs, "/DA") or _get(painter.acro, "/DA")) or "/Helv 0 Tf 0 g"
    font_name, size, color = _parse_da(da)
    font = painter.font(font_name)
    width, height, _rot = painter.box(widget)
    bw = painter.border_width(widget)
    pad = bw + 2
    avail = max(width - 2 * pad, 1)
    q = int(_get(attrs, "/Q", 0))
    if int(_get(attrs, "/Ff", 0)) & PASSWORD:
        value = "*" * len(value)
    clip = (f"{_num(bw)} {_num(bw)} {_num(max(width - 2 * bw, 0))} "
            f"{_num(max(height - 2 * bw, 0))} re W n")
    ops = [f"/Tx BMC q {clip}"]

    if lines_override is not None:  # a list box: the options, the chosen one marked
        size = size or AUTO_SIZE_MAX
        leading = size * 1.15
        y = height - pad - size * ASCENT
        for i, (label, chosen) in enumerate(lines_override):
            if chosen:
                top = height - bw - 1 - i * leading
                ops.append(f"q 0.6 0.75 0.95 rg {_num(bw)} {_num(top - leading)} "
                           f"{_num(width - 2 * bw)} {_num(leading)} re f Q")
            raw = _encode(label, f.name)
            ops.append(f"BT {font.name} {_num(size)} Tf {color} {_num(pad)} {_num(y)} Td "
                       + _pdf_string(raw).decode("latin-1") + " Tj ET")
            y -= leading
        ops.append("Q EMC")
        return "\n".join(ops).encode("latin-1"), font

    raw = _encode(value, f.name)
    comb = int(_get(attrs, "/Ff", 0)) & COMB and f.max_len and not f.multiline
    if f.multiline:
        paragraphs = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
        auto = size == 0
        size = size or AUTO_SIZE_MAX
        while True:
            lines = _wrap(paragraphs, font, size, avail)
            leading = size * 1.15
            if not auto or size <= MIN_SIZE or len(lines) * leading <= height - 2 * pad:
                break
            size = max(MIN_SIZE, size - 0.5)
        y = height - pad - size * ASCENT
        for line in lines:
            x = pad + _align(q, avail, font.width(line, size))
            ops.append(f"BT {font.name} {_num(size)} Tf {color} {_num(x)} {_num(y)} Td "
                       + _pdf_string(line).decode("latin-1") + " Tj ET")
            y -= leading
    else:
        raw = raw.replace(b"\r", b" ").replace(b"\n", b" ")
        if size == 0:  # auto: as high as the box allows, then narrow enough
            size = min(AUTO_SIZE_MAX, max(MIN_SIZE, (height - 2 * bw - 2) / 1.15))
            if not comb and (tw := font.width(raw, size)) > avail:
                size = max(MIN_SIZE, size * avail / tw)
        y = (height - size * (ASCENT - DESCENT)) / 2 - size * DESCENT
        if comb:
            cell = width / f.max_len
            for i, b in enumerate(raw[:f.max_len]):
                ch = bytes([b])
                x = i * cell + (cell - font.width(ch, size)) / 2
                ops.append(f"BT {font.name} {_num(size)} Tf {color} {_num(x)} {_num(y)} Td "
                           + _pdf_string(ch).decode("latin-1") + " Tj ET")
        else:
            x = pad + _align(q, avail, font.width(raw, size))
            ops.append(f"BT {font.name} {_num(size)} Tf {color} {_num(x)} {_num(y)} Td "
                       + _pdf_string(raw).decode("latin-1") + " Tj ET")
    ops.append("Q EMC")
    return "\n".join(ops).encode("latin-1"), font


def _align(q: int, avail: float, text_width: float) -> float:
    if q == 1:
        return (avail - text_width) / 2
    if q == 2:
        return avail - text_width
    return 0.0


# --- filling ----------------------------------------------------------------------------


def fill(data: bytes, values: dict[str, str | bool], password: str | None = None) -> bytes:
    """Set the fields named in ``values`` (text and choices: str, check
    boxes: bool, radio groups: the state name, "" for none) and return the
    updated PDF. Unknown names, read-only or locked fields, values a field
    can't take and characters its font can't show raise :class:`FormError`."""
    w = IncrementalPdfFileWriter(io.BytesIO(data), strict=False)
    if w.prev.encrypted:
        if password is None:
            raise PasswordRequired(_("PDF is encrypted, a password is required"))
        w.encrypt(password)
    reader = _open_reader(data, password)
    cert = read_certification_data(reader)
    if cert is not None and cert.permission == MDPPerm.NO_CHANGES:
        raise FormError(_("the document is certified: no changes are allowed"))

    acro = _get(w.root, "/AcroForm")
    found = {f.name: (f, node, attrs, widgets)
             for f, node, attrs, widgets in _read(w.root, _locked_field_names(reader))}
    for name in values:
        if name not in found:
            raise FormError(_("no form field named {name!r}").format(name=name))
    painter = _Painter(w, acro.get_object()) if acro is not None else None

    for name, value in values.items():
        f, node, attrs, widgets = found[name]
        if f.read_only:
            raise FormError(_("{field} is read-only").format(field=name))
        if f.locked:
            raise FormError(_("{field} is locked by a signature").format(field=name))
        if value == f.value:
            continue
        if f.kind == TEXT:
            text = str(value)
            if f.max_len is not None and len(text) > f.max_len:
                raise FormError(_("{field} takes at most {n} characters").format(
                    field=name, n=f.max_len))
            node[generic.pdf_name("/V")] = generic.TextStringObject(text)
            for wd, _ref in widgets:
                body, font = _text_body(painter, wd, attrs, text, f)
                _set_appearance(w, wd, painter.stream(wd, body, font))
        elif f.kind == CHOICE:
            text = str(value)
            labels = dict(f.options)
            if text and text not in labels and not (f.combo and f.editable):
                raise FormError(_("{field}: {value!r} is not one of its choices").format(
                    field=name, value=text))
            node[generic.pdf_name("/V")] = generic.TextStringObject(text)
            if "/I" in node:
                del node["/I"]
            for wd, _ref in widgets:
                if f.combo:
                    body, font = _text_body(painter, wd, attrs, labels.get(text, text), f)
                else:
                    top = int(_get(node, "/TI", 0))
                    rows = [(label, v == text) for v, label in f.options[top:]]
                    body, font = _text_body(painter, wd, attrs, "", f, rows)
                _set_appearance(w, wd, painter.stream(wd, body, font))
        elif f.kind == CHECKBOX:
            on = f.options[0][0] if f.options else "Yes"
            state = generic.pdf_name("/" + on if value else "/Off")
            node[generic.pdf_name("/V")] = state
            for wd, _ref in widgets:
                _set_state(w, wd, state if not value or _on_state(wd) == on else "/Off")
        else:  # RADIO
            value = str(value)
            if value and value not in dict(f.options):
                raise FormError(_("{field}: {value!r} is not one of its choices").format(
                    field=name, value=value))
            node[generic.pdf_name("/V")] = generic.pdf_name("/" + (value or "Off"))
            for wd, _ref in widgets:
                _set_state(w, wd, "/" + value if value and _on_state(wd) == value else "/Off")
        w.update_container(node)

    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def _set_state(w, widget, state: str) -> None:
    """A check box's or radio button's appearance state; widgets that keep
    theirs are left alone (fewer changed objects for signature checks)."""
    if _get(widget, "/AS") != state:
        widget[generic.pdf_name("/AS")] = generic.pdf_name(state)
        w.update_container(widget)


def _set_appearance(w, widget, stream_ref) -> None:
    widget[generic.pdf_name("/AP")] = generic.DictionaryObject(
        {generic.pdf_name("/N"): stream_ref})
    if "/AS" in widget:
        del widget["/AS"]
    w.update_container(widget)
