"""slim-pdf: sign PDFs with real certificates, check signatures, fill in
forms, add text and images, and edit pages. (Formerly "pdfsigner".)"""

from __future__ import annotations

import os

OLD_NAME = "pdfsigner"  # settings made under the old name are still found


def environ(name: str) -> str | None:
    """``$SLIM_PDF_<name>``, or the old ``$PDFSIGNER_<name>``; empty counts
    as unset."""
    return os.environ.get(f"SLIM_PDF_{name}") or os.environ.get(f"PDFSIGNER_{name}") or None
