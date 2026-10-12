"""The invisible text over a scanned page.

Each recognized word is drawn in Helvetica with text render mode 3
(neither filled nor stroked: invisible), at the size of its line and
stretched horizontally to its box, so viewers select and highlight it where
it is in the scan. Like insert.py this is new page content in an
incremental update, which invalidates existing signatures.

The text is encoded in Windows-1252; other characters become "?".
"""

from __future__ import annotations

from collections import defaultdict

from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.rw_common import find_inherited_value_in_tree

from .. import forms, insert
from ..pages import _finish, _incremental
from . import Word

FONT = "/OcrHelv"
MIN_SIZE = 1.0


def _visible_box(page) -> insert.Box:
    """The crop box (what pdfium renders), else the media box."""
    for key in ("/CropBox", "/MediaBox"):
        box = find_inherited_value_in_tree(page, key, "/Parent")
        if box is not None:
            return tuple(float(v) for v in box.get_object())
    return (0.0, 0.0, 612.0, 792.0)


def _content(words: list[Word], height: float) -> bytes:
    """Text operators for ``words`` (boxes in points, origin top left) in
    a box ``height`` high (origin bottom left)."""
    font = forms._Font(FONT, None, None)
    lines: dict[int, list[Word]] = defaultdict(list)
    for i, word in enumerate(words):
        lines[word.line if word.line >= 0 else -1 - i].append(word)
    ops = ["BT 3 Tr"]
    for line in lines.values():
        top = min(w.box[1] for w in line)
        bottom = max(w.box[3] for w in line)
        size = max((bottom - top) / (forms.ASCENT - forms.DESCENT), MIN_SIZE)
        baseline = height - bottom - forms.DESCENT * size
        line.sort(key=lambda w: w.box[0])
        for i, word in enumerate(line):
            raw = word.text.strip().encode("cp1252", errors="replace")
            natural = font.width(raw, size)
            if not raw or natural <= 0:
                continue
            stretch = 100 * max(word.box[2] - word.box[0], 0.1) / natural
            if i < len(line) - 1:
                raw += b" "  # so text extraction doesn't depend on guessing gaps
            ops.append(f"{FONT} {forms._num(size)} Tf {forms._num(stretch)} Tz "
                       f"1 0 0 1 {forms._num(word.box[0])} {forms._num(baseline)} Tm "
                       + forms._pdf_string(raw).decode("latin-1") + " Tj")
    ops.append("ET")
    return "\n".join(ops).encode("latin-1")


def add_text_layer(data: bytes, words: dict[int, list[Word]], *,
                   password: str | None = None) -> bytes:
    """Add the ``words`` of each page (0-based; boxes in points of the page
    as displayed, as ocr.recognize_pages returns them)."""
    w, refs = _incremental(data, password)
    helv = None
    for index, page_words in sorted(words.items()):
        if not page_words:
            continue
        page, rot = insert._page(refs, index)
        box = _visible_box(page)
        width, height = insert._display_size(box, rot)
        if helv is None:
            helv = insert._helvetica(w)
        resources = generic.DictionaryObject({generic.pdf_name("/Font"): generic.DictionaryObject(
            {generic.pdf_name(FONT): helv})})
        xobj = forms.appearance(w, width, height, rot, _content(page_words, height), resources)
        insert._draw_on_page(w, page, box, xobj)
    return _finish(w)
