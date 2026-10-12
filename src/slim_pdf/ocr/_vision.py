"""Apple Vision (macOS 10.15+), via pyobjc-framework-Vision."""

from __future__ import annotations

import io
import sys
from functools import cached_property

from ..i18n import _
from . import Engine, OcrError, Word


def _modules():
    if sys.platform != "darwin":
        return None
    try:
        import Foundation
        import objc
        import Vision
    except ImportError:
        return None
    return objc, Foundation, Vision


def _iso(tag: str) -> str:
    """'de-DE' -> 'de'; Chinese keeps its script ('zh-Hans')."""
    return tag if tag.startswith("zh") else tag.split("-")[0]


class VisionEngine(Engine):
    name = "vision"

    def __init__(self):
        self.label = _("Apple Vision")

    def available(self) -> bool:
        return _modules() is not None

    def _request(self):
        _objc, _foundation, vision = _modules()
        request = vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(True)
        return request

    @cached_property
    def _tags(self) -> list[str]:
        if not self.available():
            return []
        tags, _error = self._request().supportedRecognitionLanguagesAndReturnError_(None)
        return list(tags or [])

    def languages(self) -> list[str]:
        return list(dict.fromkeys(_iso(t) for t in self._tags))

    def language_hint(self, missing: list[str]) -> str:
        return _("A newer macOS recognizes more languages.")

    def recognize(self, image, languages: list[str]) -> list[Word]:
        objc, foundation, vision = _modules()
        png = io.BytesIO()
        image.save(png, format="PNG")
        raw = png.getvalue()
        width, height = image.size
        with objc.autorelease_pool():
            request = self._request()
            tags = [next(t for t in self._tags if _iso(t) == lang) for lang in languages]
            request.setRecognitionLanguages_(tags)
            handler = vision.VNImageRequestHandler.alloc().initWithData_options_(
                foundation.NSData.dataWithBytes_length_(raw, len(raw)), None)
            ok, error = handler.performRequests_error_([request], None)
            if not ok:
                raise OcrError(_("Apple Vision failed: {error}").format(
                    error=error.localizedDescription() if error else "?"))
            words = []
            for line, observation in enumerate(request.results() or []):
                candidates = observation.topCandidates_(1)
                if not candidates:
                    continue
                candidate = candidates[0]
                text = str(candidate.string())
                for start, token in _tokens(text):
                    # NSString ranges count UTF-16 units
                    loc = len(text[:start].encode("utf-16-le")) // 2
                    length = len(token.encode("utf-16-le")) // 2
                    box, _error = candidate.boundingBoxForRange_error_((loc, length), None)
                    if box is None:
                        continue
                    r = box.boundingBox()  # normalized, origin bottom left
                    words.append(Word(token, (
                        r.origin.x * width, (1 - r.origin.y - r.size.height) * height,
                        (r.origin.x + r.size.width) * width, (1 - r.origin.y) * height),
                        line, float(candidate.confidence()) * 100))
            return words


def _tokens(text: str) -> list[tuple[int, str]]:
    """(start, word) for the whitespace-separated words of ``text``."""
    out, i = [], 0
    for token in text.split():
        i = text.index(token, i)
        out.append((i, token))
        i += len(token)
    return out
