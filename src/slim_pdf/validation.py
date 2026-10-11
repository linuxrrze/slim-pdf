"""Check the signatures in a PDF.

Each signature gets one of these verdicts:

* ``valid``: intact, cryptographically sound, the signer's certificate chains
  to a trusted root, and later changes are allowed ones.
* ``untrusted``: everything checks out except that the certificate doesn't
  chain to a trusted root (typical for self-signed or unknown CAs).
* ``invalid``: the document was changed in a way the signature doesn't allow,
  or the signature itself is broken or revoked (revocation is only known
  from data embedded in the PDF, or with ``online=True``).
* ``unsupported``: a signature format pyHanko can't check, such as the
  legacy ``adbe.x509.rsa_sha1`` (PKCS#1 with SHA-1, deprecated in PDF 2.0).
  It is listed (signer, time, coverage) rather than silently skipped.

Trust comes from Mozilla's root list (via ``certifi``, the same on every
platform) and the operating system's CA store (see osroots.py), plus any
extra roots (``--trust`` or ``trust = [...]`` in the
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
from pyhanko.pdf_utils import generic
from pyhanko.sign.fields import SigSeedSubFilter, enumerate_sig_fields
from pyhanko.sign.diff_analysis import ModificationLevel
from pyhanko.sign.validation.errors import SignatureValidationError
from pyhanko.sign.validation.settings import KeyUsageConstraints
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko.sign.validation.dss import DocumentSecurityStore, NoDSSFoundError
from pyhanko.sign.validation.status import SignatureCoverageLevel
from pyhanko_certvalidator import ValidationContext

from .config import expand
from .i18n import N_, _
from . import chain, osroots
from .signing import SigningError, _open_reader

log = logging.getLogger(__name__)

VALID, UNTRUSTED, INVALID = "valid", "untrusted", "invalid"
# Key usages that make a certificate fit for signing documents: either, as
# Adobe Reader accepts. pyHanko's default is nonRepudiation only, which
# rejects common S/MIME certificates (e.g. GEANT Personal CA 4).
SIGNER_KEY_USAGE = {"digital_signature", "non_repudiation"}
UNSUPPORTED = "unsupported"
SUPPORTED_SUBFILTERS = {s.value for s in SigSeedSubFilter}


class _QuietSkip(logging.Filter):
    """pyHanko logs a traceback for every signature it can't handle; those are
    reported as UNSUPPORTED instead."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("Encountered signature with unsupported")


logging.getLogger("pyhanko.sign.validation.pdf_embedded").addFilter(_QuietSkip())

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
    status: str  # VALID / UNTRUSTED / INVALID / UNSUPPORTED
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
    certificate: x509.Certificate | None = None  # the signer's

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
    """Mozilla's roots and the operating system's, without duplicates."""
    with open(certifi.where(), "rb") as f:
        roots = [x509.Certificate.load(der)
                 for _type, _headers, der in pem.unarmor(f.read(), multiple=True)]
    seen = {c.dump() for c in roots}
    for cert in osroots.os_roots():
        if cert.dump() not in seen:
            seen.add(cert.dump())
            roots.append(cert)
    return tuple(roots)


def trust_roots(trust=(), *, system_trust: bool = True) -> list:
    roots = load_trust_roots(trust)
    if system_trust:
        roots += system_trust_roots()
    return roots


def validation_context(trust=(), *, system_trust: bool = True, online: bool = False,
                       revocation_mode: str = "soft-fail", dss=None,
                       other_certs=()) -> ValidationContext:
    """Validation context; with ``dss``, preloaded with the document's LTV data.
    ``other_certs``: intermediates the signatures may lack (see chain.py)."""
    roots = trust_roots(trust, system_trust=system_trust)
    kwargs = dict(trust_roots=roots, allow_fetching=online, revocation_mode=revocation_mode,
                  other_certs=list(other_certs))
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


def _trust_problem(indic, cert, md_algorithm: str = "") -> str:
    """Why the signer's certificate isn't trusted, from pyHanko's AdES
    sub-indication (and the certificate, where the indication is ambiguous)."""
    name = getattr(indic, "name", "")
    if name in ("CRYPTO_CONSTRAINTS_FAILURE", "CRYPTO_CONSTRAINTS_FAILURE_NO_POE"):
        # e.g. SHA-1, which isn't accepted any more (for signatures, or in
        # the certificate chain)
        if md_algorithm:
            return _("the signature or its certificate chain uses an algorithm that is no "
                     "longer considered secure (digest: {algorithm})").format(
                         algorithm=md_algorithm.upper().replace("SHA", "SHA-"))
        return _("the signature or its certificate chain uses an algorithm that is no "
                 "longer considered secure")
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
        return _no_chain(cert)
    if name in ("TRY_LATER", "REVOCATION_OUT_OF_BOUNDS_NO_POE"):
        return _("the certificate's revocation status is not known yet")
    if name in ("REVOKED_NO_POE", "REVOKED_CA_NO_POE"):
        return _("the certificate (or its CA) is revoked")
    return _no_chain(cert)


def _common_name(name) -> str:
    cn = name.native.get("common_name")  # a list when there are several
    return (", ".join(cn) if isinstance(cn, list) else cn) or name.human_friendly


def _issuer_name(cert) -> str:
    return _common_name(cert.issuer)


def _no_chain(cert) -> str:
    return _("the certificate doesn't chain to a trusted root (issuer {issuer!r} not "
             "trusted, or not found)").format(issuer=_issuer_name(cert))


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
            problems.append(_trust_problem(st.trust_problem_indic, cert,
                                           getattr(st, "md_algorithm", "") or ""))
        for t, what in ((ts, _("the timestamp authority {tsa!r} isn't trusted (issuer "
                                "{issuer!r})")),
                        (content_ts, _("the content timestamp authority {tsa!r} isn't "
                                       "trusted (issuer {issuer!r})"))):
            if t is not None and not t.trusted:
                tsa = t.signing_cert
                problems.append(what.format(
                    tsa=_common_name(tsa.subject),
                    issuer=_issuer_name(tsa)))
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
        certificate=cert,
    )


def _signed_end(sig_object) -> int:
    """Where a signature's byte range ends: older signatures end earlier."""
    try:
        br = sig_object["/ByteRange"]
        return int(br[2]) + int(br[3])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


def _first_cert(sig_object) -> x509.Certificate | None:
    """The signer's certificate of a legacy signature (/Cert: one or a list)."""
    try:
        certs = sig_object["/Cert"]
        der = certs[0] if isinstance(certs, generic.ArrayObject) else certs
        cert = x509.Certificate.load(bytes(der))
        cert.native  # noqa: B018 - parses now: garbage raises here
        return cert
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def unsupported_signatures(reader) -> list[tuple[str, generic.DictionaryObject]]:
    """(field name, signature dictionary) of the signatures in a format
    pyHanko skips."""
    found = []
    for name, value, _ref in enumerate_sig_fields(reader, filled_status=True):
        sig_object = value.get_object()
        subfilter = sig_object.get("/SubFilter")
        if subfilter is not None and str(subfilter) not in SUPPORTED_SUBFILTERS:
            found.append((name, sig_object))
    return found


def unsupported_report(name: str, sig_object, size: int) -> SignatureReport:
    subfilter = str(sig_object.get("/SubFilter")).lstrip("/")
    cert = _first_cert(sig_object)
    try:
        signed_at = generic.parse_pdf_date(str(sig_object["/M"]), strict=False)
    except (KeyError, ValueError, TypeError):
        signed_at = None
    if _signed_end(sig_object) == size:
        coverage = _("entire document")
    else:
        coverage = _("an earlier revision; later changes not checked")
    return SignatureReport(
        field=name, status=UNSUPPORTED,
        signer=cert.subject.human_friendly if cert else _str(sig_object, "/Name"),
        issuer=cert.issuer.human_friendly if cert else "",
        signed_at=signed_at, coverage=coverage,
        reason=_str(sig_object, "/Reason"), location=_str(sig_object, "/Location"),
        problems=[_("the signature uses the format {format}, which slim-pdf cannot check "
                    "(a legacy format, unlike PAdES/PKCS#7)").format(format=subfilter)],
        certificate=cert)


def verify_pdf(path: Path | str | bytes, *, trust=(), system_trust: bool = True,
               online: bool = False, password: str | None = None,
               fetch_issuers: bool = True) -> list[SignatureReport]:
    """Validate every signature in the document, oldest first; those in a
    format that can't be checked are reported as UNSUPPORTED. Intermediate
    certificates a signature lacks are downloaded once (``fetch_issuers``)
    and cached, see chain.py."""
    data = path if isinstance(path, bytes) else Path(path).read_bytes()
    reader = _open_reader(data, password)
    sigs = reader.embedded_regular_signatures  # document timestamps aren't signers
    try:
        dss = DocumentSecurityStore.read_dss(reader)
    except NoDSSFoundError:
        dss = None
    revisions = reader.xrefs.total_revisions
    results = [(_signed_end(obj), unsupported_report(name, obj, len(data)))
               for name, obj in unsupported_signatures(reader)]
    roots = trust_roots(trust, system_trust=system_trust)
    dss_certs = list(dss.load_certs()) if dss is not None else []
    for sig in sigs:
        try:
            known = [*sig.other_embedded_certs, *dss_certs]
            missing = chain.complete_chain(sig.signer_cert, known, roots, fetch=fetch_issuers)
        except (ValueError, KeyError):  # a broken signature: validation reports it
            missing = []
        vc = validation_context(trust, system_trust=system_trust, online=online, dss=dss,
                                other_certs=missing)
        try:
            st = validate_pdf_signature(
                sig, vc, key_usage_settings=KeyUsageConstraints(key_usage=SIGNER_KEY_USAGE))
        except SignatureValidationError as e:
            results.append((_signed_end(sig.sig_object),
                            SignatureReport(field=sig.field_name, status=INVALID,
                                            problems=[str(e)])))
            continue
        revision = sig.signed_revision + 1
        rep = report(sig, st, revision, revisions)
        rep.ltv = dss is not None
        results.append((_signed_end(sig.sig_object), rep))
    results.sort(key=lambda r: r[0])
    return [rep for _end, rep in results]
