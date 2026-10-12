"""Tesseract, run as a program (its TSV output has a box for every word)."""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import sys
from functools import cached_property
from pathlib import Path

from ..i18n import _
from . import Engine, OcrError, Word

TIMEOUT = 300  # seconds per page

# ISO 639-1 -> Tesseract's language data names; others are passed as they are
CODES = {"de": "deu", "en": "eng", "fr": "fra", "it": "ita", "es": "spa", "nl": "nld",
         "pt": "por", "pl": "pol", "cs": "ces", "da": "dan", "sv": "swe", "fi": "fin",
         "no": "nor", "hu": "hun", "tr": "tur", "ru": "rus", "uk": "ukr"}
ISO = {v: k for k, v in CODES.items()}


def _candidates() -> list[str]:
    """Where installers put it, for when it isn't on the PATH."""
    if sys.platform == "win32":
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")]
        return [os.path.join(r, "Tesseract-OCR", "tesseract.exe") for r in roots if r]
    if sys.platform == "darwin":
        return ["/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract",
                "/opt/local/bin/tesseract"]
    return []


def find_tesseract(configured: str = "") -> str | None:
    if configured:
        path = Path(os.path.expandvars(configured)).expanduser()
        return str(path) if path.is_file() else shutil.which(configured)
    found = shutil.which("tesseract")
    if found:
        return found
    return next((c for c in _candidates() if os.path.isfile(c)), None)


def _run(args: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console window from the GUI
    return subprocess.run(args, input=stdin, capture_output=True, timeout=TIMEOUT,
                          creationflags=flags, check=False)


class TesseractEngine(Engine):
    name = "tesseract"

    def __init__(self, path: str = ""):
        self.configured = path
        self.label = _("Tesseract")

    @cached_property
    def program(self) -> str | None:
        return find_tesseract(self.configured)

    def available(self) -> bool:
        return self.program is not None

    @cached_property
    def _installed(self) -> list[str]:
        if not self.program:
            return []
        try:
            out = _run([self.program, "--list-langs"])
        except (OSError, subprocess.SubprocessError):
            return []
        # first line: 'List of available languages in "/usr/share/tessdata/" (3):'
        lines = out.stdout.decode("utf-8", "replace").splitlines()
        return [line.strip() for line in lines[1:] if line.strip()]

    def languages(self) -> list[str]:
        return [ISO.get(lang, lang) for lang in self._installed if lang != "osd"]

    def language_hint(self, missing: list[str]) -> str:
        if sys.platform == "win32":
            return _("Install the language data with the Tesseract installer.")
        if sys.platform == "darwin":
            return _("Install the language data: brew install tesseract-lang")
        packages = " ".join("tesseract-ocr-" + CODES.get(m, m) for m in missing)
        return _("Install the language data, e.g. sudo apt install {packages}").format(
            packages=packages)

    def recognize(self, image, languages: list[str]) -> list[Word]:
        png = io.BytesIO()
        image.save(png, format="PNG", dpi=(300, 300))
        langs = "+".join(CODES.get(lang, lang) for lang in languages) or "eng"
        try:
            out = _run([self.program, "stdin", "stdout", "-l", langs, "tsv"], png.getvalue())
        except subprocess.TimeoutExpired:
            raise OcrError(_("Tesseract took too long")) from None
        except OSError as e:
            raise OcrError(_("cannot run Tesseract: {error}").format(error=e)) from None
        if out.returncode != 0:
            message = out.stderr.decode("utf-8", "replace").strip().splitlines()
            raise OcrError(_("Tesseract failed: {error}").format(
                error=message[-1] if message else out.returncode))
        return parse_tsv(out.stdout.decode("utf-8", "replace"))


def parse_tsv(text: str) -> list[Word]:
    """Words from Tesseract's TSV output (level 5 rows)."""
    words = []
    lines: dict[tuple[str, str, str, str], int] = {}
    rows = csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    for row in rows:
        if row.get("level") != "5" or not (row.get("text") or "").strip():
            continue
        key = (row["page_num"], row["block_num"], row["par_num"], row["line_num"])
        line = lines.setdefault(key, len(lines))
        left, top = int(row["left"]), int(row["top"])
        words.append(Word(row["text"].strip(),
                          (left, top, left + int(row["width"]), top + int(row["height"])),
                          line, float(row["conf"])))
    return words
