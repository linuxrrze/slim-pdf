"""Check the signatures in a PDF.

Each signature gets one of three verdicts:

* ``valid``: intact, cryptographically sound, the signer's certificate chains
  to a trusted root, and later changes are allowed ones.
* ``untrusted``: everything checks out except that the certificate doesn't
  chain to a trusted root (typical for self-signed or unknown CAs).
* ``invalid``: the document was changed in a way the signature doesn't allow,
  or the signature itself is broken or revoked.

Trust comes from Mozilla's root list (via ``certifi``, the same on every
platform) plus any extra roots (``--trust`` or ``trust = [...]`` in the
config). Revocation information is
only fetched when ``online`` is set; otherwise embedded LTV data is used.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import certifi
from asn1crypto import pem, x509
from pyhanko.keys import load_cert_from_pemder
from pyhanko.sign.diff_analysis import ModificationLevel
from pyhanko.sign.validation.errors import SignatureValidationError
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko.sign.validation.dss import DocumentSecurityStore, NoDSSFoundError
from pyhanko.sign.validation.status import SignatureCoverageLevel
from pyhanko_certvalidator import ValidationContext

from .signing import SigningError, _open_reader

log = logging.getLogger(__name__)

VALID, UNTRUSTED, INVALID = "valid", "untrusted", "invalid"

_LATER = {
    ModificationLevel.NONE: "none",
    ModificationLevel.LTA_UPDATES: "validation data and timestamps only",
    ModificationLevel.FORM_FILLING: "form filling and signatures",
    ModificationLevel.ANNOTATIONS: "annotations, form filling and signatures",
    ModificationLevel.OTHER: "changes the signature doesn't allow",
}


@dataclass
class SignatureReport:
    field: str
    status: str  # VALID / UNTRUSTED / INVALID
    signer: str = ""
    issuer: str = ""
    signed_at: datetime | None = None  # as claimed by the signer
    timestamp: datetime | None = None  # from a trusted timestamp, if any
    coverage: str = ""
    reason: str = ""
    location: str = ""
    ltv: bool = False  # document carries validation data (DSS)
    problems: list[str] = field(default_factory=list)
    details: str = ""  # pyHanko's full report

    @property
    def ok(self) -> bool:
        return self.status == VALID


def load_trust_roots(paths) -> list:
    roots = []
    for p in paths:
        try:
            roots.append(load_cert_from_pemder(str(Path(p).expanduser())))
        except (OSError, ValueError) as e:
            raise SigningError(f"cannot load trust root {p}: {e}") from None
    return roots


@functools.cache
def system_trust_roots() -> tuple[x509.Certificate, ...]:
    with open(certifi.where(), "rb") as f:
        return tuple(x509.Certificate.load(der) for _, _, der in pem.unarmor(f.read(), multiple=True))


def validation_context(trust=(), *, system_trust: bool = True, online: bool = False,
                       revocation_mode: str = "soft-fail", dss=None) -> ValidationContext:
    """Validation context; with ``dss``, preloaded with the document's LTV data."""
    roots = load_trust_roots(trust)
    if system_trust:
        roots += system_trust_roots()
    kwargs = dict(trust_roots=roots, allow_fetching=online, revocation_mode=revocation_mode)
    if dss is not None:
        return dss.as_validation_context(kwargs)
    return ValidationContext(**kwargs)


def _str(d, key) -> str:
    try:
        return str(d[key])
    except KeyError:
        return ""


def _coverage(st, revision: int, revisions: int) -> str:
    if st.coverage == SignatureCoverageLevel.ENTIRE_FILE:
        return "entire document"
    if st.coverage == SignatureCoverageLevel.ENTIRE_REVISION:
        later = _LATER.get(st.modification_level, str(st.modification_level))
        return f"revision {revision} of {revisions}; later changes: {later}"
    return "only part of the document"


def report(sig, st, revision: int, revisions: int) -> SignatureReport:
    cert = st.signing_cert
    problems = []
    if not st.intact:
        problems.append("the signed content was modified")
    elif not st.valid:
        problems.append("the signature value doesn't match the certificate")
    if st.revoked:
        problems.append("the signer's certificate is revoked")
    if not st.docmdp_ok or st.modification_level == ModificationLevel.OTHER:
        problems.append("the document was changed in a way this signature doesn't allow")
    if st.coverage not in (SignatureCoverageLevel.ENTIRE_FILE,
                           SignatureCoverageLevel.ENTIRE_REVISION):
        problems.append("the signature doesn't cover a complete revision")

    if problems:
        status = INVALID
    elif st.bottom_line:
        status = VALID
    else:
        status = UNTRUSTED
        problems.append("the certificate doesn't chain to a trusted root")

    ts = st.timestamp_validity
    return SignatureReport(
        field=sig.field_name,
        status=status,
        signer=cert.subject.human_friendly,
        issuer=cert.issuer.human_friendly,
        signed_at=st.signer_reported_dt,
        timestamp=ts.timestamp if ts is not None and ts.valid else None,
        coverage=_coverage(st, revision, revisions),
        reason=_str(sig.sig_object, "/Reason"),
        location=_str(sig.sig_object, "/Location"),
        problems=problems,
        details=st.pretty_print_details(),
    )


def verify_pdf(path: Path | str | bytes, *, trust=(), system_trust: bool = True,
               online: bool = False, password: str | None = None) -> list[SignatureReport]:
    """Validate every signature in the document, oldest first."""
    data = path if isinstance(path, bytes) else Path(path).read_bytes()
    reader = _open_reader(data, password)
    sigs = reader.embedded_regular_signatures  # document timestamps aren't signers
    try:
        dss = DocumentSecurityStore.read_dss(reader)
    except NoDSSFoundError:
        dss = None
    revisions = reader.xrefs.total_revisions
    results = []
    for sig in sigs:
        vc = validation_context(trust, system_trust=system_trust, online=online, dss=dss)
        try:
            st = validate_pdf_signature(sig, vc)
        except SignatureValidationError as e:
            results.append(SignatureReport(field=sig.field_name, status=INVALID,
                                           problems=[str(e)]))
            continue
        revision = sig.signed_revision + 1
        rep = report(sig, st, revision, revisions)
        rep.ltv = dss is not None
        results.append(rep)
    return results
