"""Text recognition (OCR) for scanned pages.

Pages are rendered with pdfium, an OCR engine finds the words, and
:func:`textlayer.add_text_layer` writes them as invisible text over the
page: it looks the same, but its text can be searched, selected and
copied. Everything runs locally; documents never leave the machine.

The engine is the platform's own where there is one, else Tesseract:

* macOS: Apple Vision (``vision``)
* Windows: Windows.Media.Ocr (``windows``), with the OCR languages
  installed in Windows' language settings
* everywhere: Tesseract (``tesseract``), when installed

Languages are ISO 639-1 codes ("de", "en"); engines map them to their own.
Rendering needs pypdfium2 (the ``ocr`` or ``gui`` extra).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..i18n import N_, _
from ..secretstore import Cancelled
from ..signing import SigningError

DPI = 300
MAX_PIXELS = 8000  # longest side of a rendered page; bigger pages get fewer dpi


class OcrError(SigningError):
    pass


@dataclass(frozen=True)
class Word:
    """A recognized word. ``box`` is (left, top, right, bottom): pixels of
    the image from an engine, points of the page as displayed (origin top
    left) from :func:`recognize_pages`. Words with the same ``line`` (>= 0)
    are on one line."""
    text: str
    box: tuple[float, float, float, float]
    line: int = -1
    confidence: float = 100.0


class Engine:
    """An OCR engine. Subclasses implement the methods below."""
    name = ""
    label = ""

    def available(self) -> bool:
        raise NotImplementedError

    def languages(self) -> list[str]:
        """The languages it can recognize (ISO 639-1 where known)."""
        raise NotImplementedError

    def recognize(self, image, languages: list[str]) -> list[Word]:
        """Words in the PIL ``image``, boxes in its pixels."""
        raise NotImplementedError

    def check_languages(self, languages: list[str]) -> None:
        missing = [lang for lang in languages if lang not in self.languages()]
        if missing:
            raise OcrError(_("{engine} can't recognize {languages}. {hint}").format(
                engine=self.label, languages=", ".join(language_name(m) for m in missing),
                hint=self.language_hint(missing)))

    def language_hint(self, missing: list[str]) -> str:
        """How to add the ``missing`` languages."""
        return ""


ENGINES = ("vision", "windows", "tesseract")
DEFAULT_ORDER = {"darwin": ("vision", "tesseract"),
                 "win32": ("windows", "tesseract")}.get(sys.platform, ("tesseract",))

LANGUAGE_NAMES = {"de": N_("German"), "en": N_("English"), "fr": N_("French"),
                  "it": N_("Italian"), "es": N_("Spanish"), "nl": N_("Dutch"),
                  "pt": N_("Portuguese"), "pl": N_("Polish"), "cs": N_("Czech"),
                  "da": N_("Danish"), "sv": N_("Swedish"), "fi": N_("Finnish"),
                  "no": N_("Norwegian"), "hu": N_("Hungarian"), "tr": N_("Turkish"),
                  "ru": N_("Russian"), "uk": N_("Ukrainian")}


def language_name(code: str) -> str:
    return _(LANGUAGE_NAMES[code]) if code in LANGUAGE_NAMES else code


def default_languages() -> list[str]:
    """The user interface's language, and English."""
    from ..i18n import current
    return list(dict.fromkeys([current(), "en"]))


def get_engine(name: str = "", tesseract: str = "") -> Engine:
    """The engine called ``name`` ("" = the first one available here);
    ``tesseract``: the program's path, if not on the PATH."""
    if name and name not in ENGINES:
        raise OcrError(_("unknown OCR engine {name!r} (known: {engines})").format(
            name=name, engines=", ".join(ENGINES)))
    for candidate in [name] if name else DEFAULT_ORDER:
        engine = make_engine(candidate, tesseract)
        if engine.available() or name:
            if not engine.available():
                raise OcrError(_("{engine} is not available. {hint}").format(
                    engine=engine.label, hint=install_hint(candidate)))
            return engine
    raise OcrError(_("no OCR engine found. {hint}").format(hint=install_hint("tesseract")))


def engines(tesseract: str = "") -> list[Engine]:
    """All engines that work on this machine."""
    return [e for e in (make_engine(n, tesseract) for n in ENGINES) if e.available()]


def make_engine(name: str, tesseract: str) -> Engine:
    if name == "tesseract":
        from ._tesseract import TesseractEngine
        return TesseractEngine(tesseract)
    if name == "vision":
        from ._vision import VisionEngine
        return VisionEngine()
    from ._winocr import WindowsEngine
    return WindowsEngine()


def install_hint(name: str) -> str:
    """What to install for engine ``name`` on this platform."""
    if name in ("vision", "windows"):
        if sys.platform != {"vision": "darwin", "windows": "win32"}[name]:
            return (_("Apple Vision is only available on macOS.") if name == "vision" else
                    _("Windows OCR is only available on Windows."))
        return _("Install its packages: pip install 'slim-pdf[ocr]'")
    if sys.platform == "win32":
        return _("Install Tesseract, e.g. from https://github.com/UB-Mannheim/tesseract/wiki "
                 "(with the German language data).")
    if sys.platform == "darwin":
        return _("Install Tesseract, e.g. with Homebrew: brew install tesseract tesseract-lang")
    return _("Install Tesseract with the German language data, e.g. "
             "sudo apt install tesseract-ocr tesseract-ocr-deu "
             "(Fedora: sudo dnf install tesseract tesseract-langpack-deu).")


# --- recognizing pages ----------------------------------------------------------------


def _pdfium():
    try:
        import pypdfium2
    except ImportError:
        raise OcrError(_("text recognition needs pypdfium2: pip install 'slim-pdf[ocr]'")
                       ) from None
    return pypdfium2


Call = Callable[[Callable[[], object]], object]


def _direct(fn):
    return fn()


def pages_with_text(data: bytes, password: str | None = None, *,
                    pdfium_call: Call = _direct) -> list[int]:
    """Pages (0-based) that already have text."""
    pdfium = _pdfium()

    def find():
        pdf = pdfium.PdfDocument(data, password=password)
        try:
            found = []
            for i in range(len(pdf)):
                page = pdf[i]
                textpage = page.get_textpage()
                if textpage.count_chars() > 0:
                    found.append(i)
                textpage.close()
                page.close()
            return found
        finally:
            pdf.close()

    return pdfium_call(find)


def _render(pdf, index: int, dpi: int):
    """(PIL image, width, height in points) of a page as displayed."""
    page = pdf[index]
    try:
        width, height = page.get_size()
        scale = min(dpi / 72, MAX_PIXELS / max(width, height, 1))
        # page content only: form fields and annotations have their text
        bitmap = page.render(scale=scale, draw_annots=False, grayscale=True)
        image = bitmap.to_pil().copy()  # independent of pdfium's buffer
        bitmap.close()
        return image, width, height
    finally:
        page.close()


def recognize_pages(data: bytes, pages: Iterable[int], engine: Engine, languages: list[str], *,
                    password: str | None = None, dpi: int = DPI,
                    progress: Callable[[int, int], bool | None] | None = None,
                    pdfium_call: Call = _direct) -> dict[int, list[Word]]:
    """Words on each of ``pages`` (0-based), boxes in points of the page as
    displayed. ``progress(done, total)`` is called before each page and
    after the last; returning False cancels (raises Cancelled).

    pdfium may only be used by one thread at a time: when this runs in a
    thread of its own, ``pdfium_call(fn)`` must run ``fn`` on the thread that
    uses pdfium otherwise (the GUI's) and return its result."""
    pages = list(pages)
    engine.check_languages(languages)
    pdfium = _pdfium()
    pdf = pdfium_call(lambda: pdfium.PdfDocument(data, password=password))
    result: dict[int, list[Word]] = {}
    try:
        for done, index in enumerate(pages):
            if progress is not None and progress(done, len(pages)) is False:
                raise Cancelled()
            image, width, height = pdfium_call(lambda index=index: _render(pdf, index, dpi))
            sx, sy = width / image.width, height / image.height
            result[index] = [
                Word(w.text, (w.box[0] * sx, w.box[1] * sy, w.box[2] * sx, w.box[3] * sy),
                     w.line, w.confidence)
                for w in engine.recognize(image, languages) if w.text.strip()]
        if progress is not None and progress(len(pages), len(pages)) is False:
            raise Cancelled()
    finally:
        pdfium_call(pdf.close)
    return result


def recognize(data: bytes, pages: Iterable[int], engine: Engine, languages: list[str], *,
              password: str | None = None, force: bool = False,
              progress: Callable[[int, int], bool | None] | None = None,
              pdfium_call: Call = _direct) -> tuple[bytes | None, list[int], list[int]]:
    """Recognize ``pages`` and add the text layer: (new PDF or None if no
    text was found, pages with text added, pages skipped because they
    already have text; unless ``force``). Progress and pdfium_call as for
    :func:`recognize_pages`."""
    from .textlayer import add_text_layer

    pages = list(pages)
    skipped = [] if force else sorted(set(pages) & set(pages_with_text(
        data, password, pdfium_call=pdfium_call)))
    todo = [p for p in pages if p not in skipped]
    if not todo:
        return None, [], skipped
    words = recognize_pages(data, todo, engine, languages, password=password,
                            progress=progress, pdfium_call=pdfium_call)
    words = {p: ws for p, ws in words.items() if ws}
    if not words:
        return None, [], skipped
    return add_text_layer(data, words, password=password), sorted(words), skipped
