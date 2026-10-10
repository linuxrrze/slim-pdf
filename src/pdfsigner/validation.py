"""Check the signatures in a PDF.

Each signature gets one of three verdicts:

* ``valid``: intact, cryptographically sound, the signer's certificate chains
  to a trusted root, and later changes are allowed ones.
* ``untrusted``: everything checks out except that the certificate doesn't
  chain to a trusted root (typical for self-signed or unknown CAs).
* ``invalid``: the document was changed in a way the signature doesn't allow,
  or the signature itself is broken or revoked (revocation is only known
  from data embedded in the PDF, or with ``online=True``).

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

from .config import expand
from .i18n import N_, _
from .signing import SigningError, _open_reader

log = logging.getLogger(__name__)

VALID, UNTRUSTED, INVALID = "valid", "untrusted", "invalid"

_LATER = {
    ModificationLevel.NONE: N_("none"),
    ModificationLevel.LTA_UPDATES: N_("validation data and timestamps only"),
    ModificationLevel.FORM_FILLING: N_("form filling and signatures"),
    ModificationLevel.ANNOTATIONS: N_("annotations, form filling and signatures"),
    ModificationLevel.OTHER: N_("changes the signature doesn't allow"),
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
    # allowed changes after this signature that alter what is shown (form
    # values, annotations, further signatures); validation data doesn't count
    changed_later: bool = False
    problems: list[str] = field(default_factory=list)
    details: str = ""  # pyHanko's full report

    @property
    def ok(self) -> bool:
        return self.status == VALID


def load_trust_roots(paths) -> list:
    roots = []
    for p in paths:
        try:
            roots.append(load_cert_from_pemder(str(expand(p))))
        except (OSError, ValueError) as e:
            raise SigningError(_("cannot load trust root {path}: {error}").format(
                path=p, error=e)) from None
    return roots


@functools.cache
def system_trust_roots() -> tuple[x509.Certificate, ...]:
    with open(certifi.where(), "rb") as f:
        return tuple(x509.Certificate.load(der)
                     for _type, _headers, der in pem.unarmor(f.read(), multiple=True))


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
        return _("entire document")
    if st.coverage == SignatureCoverageLevel.ENTIRE_REVISION:
        level = st.modification_level
        later = _(_LATER[level]) if level in _LATER else str(level)
        return _("revision {revision} of {revisions}; later changes: {later}").format(
            revision=revision, revisions=revisions, later=later)
    return _("only part of the document")


def _trust_problem(indic, cert) -> str:
    """Why the signer's certificate isn't trusted, from pyHanko's AdES
    sub-indication (and the certificate, where the indication is ambiguous)."""
    name = getattr(indic, "name", "")
    if name in ("EXPIRED", "NOT_YET_VALID", "OUT_OF_BOUNDS_NO_POE",
                "OUT_OF_BOUNDS_NOT_REVOKED"):
        return _("the certificate was not valid at the time of signing")
    if name in ("CHAIN_CONSTRAINTS_FAILURE", "SIG_CONSTRAINTS_FAILURE"):
        # certvalidator raises the same error for an untrusted self-signed
        # certificate and for a wrong key usage
        if cert.self_signed == "maybe" or cert.self_issued:
            return _("the certificate is self-signed and not in the trust list")
        usage = cert.key_usage_value.native if cert.key_usage_value else None
        if usage is not None and not {"digital_signature", "non_repudiation"} & set(usage):
            return _("the certificate is not meant for signatures (key usage)")
        return _("the certificate doesn't chain to a trusted root")
    if name in ("TRY_LATER", "REVOCATION_OUT_OF_BOUNDS_NO_POE"):
        return _("the certificate's revocation status is not known yet")
    if name in ("REVOKED_NO_POE", "REVOKED_CA_NO_POE"):
        return _("the certificate (or its CA) is revoked")
    return _("the certificate doesn't chain to a trusted root")


def report(sig, st, revision: int, revisions: int) -> SignatureReport:
    cert = st.signing_cert
    problems = []
    if not st.intact:
        problems.append(_("the signed content was modified"))
    elif not st.valid:
        problems.append(_("the signature value doesn't match the certificate"))
    if st.revoked:
        problems.append(_("the signer's certificate is revoked"))
    if not st.docmdp_ok or st.modification_level == ModificationLevel.OTHER:
        problems.append(_("the document was changed in a way this signature doesn't allow"))
    if st.coverage not in (SignatureCoverageLevel.ENTIRE_FILE,
                           SignatureCoverageLevel.ENTIRE_REVISION):
        problems.append(_("the signature doesn't cover a complete revision"))
    ts = st.timestamp_validity
    content_ts = getattr(st, "content_timestamp_validity", None)
    if ts is not None and not (ts.intact and ts.valid):
        problems.append(_("the signature's timestamp is broken"))
    if content_ts is not None and not (content_ts.intact and content_ts.valid):
        problems.append(_("the content timestamp is broken"))
    if getattr(st, "seed_value_constraint_error", None) is not None:
        problems.append(_("the signature doesn't meet the field's requirements: {error}").format(
            error=st.seed_value_constraint_error))

    if problems:
        status = INVALID
    elif st.bottom_line:
        status = VALID
    else:
        # Everything that can be checked is intact; what's missing is trust.
        status = UNTRUSTED
        if not st.trusted:
            problems.append(_trust_problem(st.trust_problem_indic, cert))
        for t, what in ((ts, _("the timestamp authority isn't trusted")),
                        (content_ts, _("the content timestamp authority isn't trusted"))):
            if t is not None and not t.trusted:
                problems.append(what)
        if not problems:
            problems.append(_("the signature could not be fully validated"))

    return SignatureReport(
        field=sig.field_name,
        status=status,
        signer=cert.subject.human_friendly,
        issuer=cert.issuer.human_friendly,
        signed_at=st.signer_reported_dt,
        timestamp=ts.timestamp if ts is not None and ts.trusted else None,
        coverage=_coverage(st, revision, revisions),
        changed_later=st.modification_level in (ModificationLevel.FORM_FILLING,
                                                ModificationLevel.ANNOTATIONS),
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
