"""Windows.Media.Ocr (Windows 10+), via the pywinrt packages.

It recognizes one language at a time: the first of the requested ones
(German text with English words in it comes out fine with German). The
languages are those installed in Windows with their OCR component.
"""

from __future__ import annotations

import asyncio
import sys
from functools import cached_property

from ..i18n import _
from . import Engine, OcrError, Word


# the usual Windows language tag, where it isn't xx-XX
REGIONS = {"en": "en-US", "cs": "cs-CZ", "da": "da-DK", "sv": "sv-SE", "uk": "uk-UA",
           "no": "nb-NO"}


def _modules():
    if sys.platform != "win32":
        return None
    try:
        import winrt.windows.foundation  # noqa: F401 - the types of results
        import winrt.windows.foundation.collections  # noqa: F401
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter
    except ImportError:
        return None
    return OcrEngine, Language, SoftwareBitmap, BitmapPixelFormat, DataWriter


class WindowsEngine(Engine):
    name = "windows"

    def __init__(self):
        self.label = _("Windows OCR")

    def available(self) -> bool:
        return _modules() is not None

    @cached_property
    def _tags(self) -> list[str]:
        if not self.available():
            return []
        ocr_engine = _modules()[0]
        return [lang.language_tag for lang in ocr_engine.available_recognizer_languages]

    def languages(self) -> list[str]:
        return list(dict.fromkeys(t.split("-")[0] for t in self._tags))

    def language_hint(self, missing: list[str]) -> str:
        lang = missing[0]
        tag = lang if "-" in lang else REGIONS.get(lang, f"{lang}-{lang.upper()}")
        return _("Add the language in Windows' settings (Time & language → Language & "
                 "region, with \"Optical character recognition\"), or as administrator in "
                 "PowerShell: Add-WindowsCapability -Online -Name "
                 "Language.OCR~~~{tag}~0.0.1.0").format(tag=tag)

    def recognize(self, image, languages: list[str]) -> list[Word]:
        ocr_engine, language, bitmap_cls, pixel_format, data_writer = _modules()
        lang = languages[0] if languages else "en"
        tag = next((t for t in self._tags if t.split("-")[0] == lang), lang)
        engine = ocr_engine.try_create_from_language(language(tag))
        if engine is None:
            raise OcrError(_("Windows OCR can't recognize {language}. {hint}").format(
                language=tag, hint=self.language_hint([lang])))
        limit = ocr_engine.max_image_dimension
        factor = min(1.0, limit / max(image.size))
        if factor < 1:
            image = image.resize((max(1, int(image.width * factor)),
                                  max(1, int(image.height * factor))))
        rgba = image.convert("RGBA")
        writer = data_writer()
        writer.write_bytes(rgba.tobytes())
        bitmap = bitmap_cls.create_copy_from_buffer(
            writer.detach_buffer(), pixel_format.RGBA8, rgba.width, rgba.height)

        async def run():
            return await engine.recognize_async(bitmap)

        result = asyncio.run(run())
        words = []
        for n, line in enumerate(result.lines):
            for word in line.words:
                r = word.bounding_rect
                words.append(Word(word.text, (r.x / factor, r.y / factor,
                                              (r.x + r.width) / factor,
                                              (r.y + r.height) / factor), n))
        return words
