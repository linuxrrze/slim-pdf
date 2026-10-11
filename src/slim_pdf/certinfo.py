"""What a signing certificate says: details to show, and the values that the
stamp text can use (``%(cn)s``, ``%(org)s``, ``%(date)s``, ...).

Profiles keep the (public) certificate as PEM once it has been read from the
key, so its details can be shown, and the stamp previewed, without a PIN.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from asn1crypto import pem, x509

from .i18n import N_, _

# Placeholders of the stamp text -> description (in this order in the GUI).
STAMP_FIELDS = {
    "signer": N_("Signer's name"),
    "cn": N_("Common name"),
    "email": N_("E-mail address"),
    "org": N_("Organization"),
    "unit": N_("Organizational unit"),
    "title": N_("Title"),
    "country": N_("Country"),
    "issuer": N_("Issuer"),
    "serial": N_("Serial number"),
    "ts": N_("Date and time of signing"),
    "date": N_("Date of signing"),
    "time": N_("Time of signing"),
    "reason": N_("Reason"),
    "location": N_("Location"),
    "contact": N_("Contact"),
}
TS_FORMAT = "%Y-%m-%d %H:%M:%S %Z"  # as pyHanko's default %(ts)s
PERCENT = re.compile(r"%%|%\((\w*)\)s|%\(\w*\)\S?|%")  # what check_template() looks at

# Shown in previews when the profile has no certificate yet.
SAMPLE = {"signer": "Alice Example", "cn": "Alice Example", "email": "alice@example.org",
          "org": "Example University", "unit": "Computer Science", "title": "Dr.",
          "country": "DE", "issuer": "Example CA", "serial": "1A2B3C4D"}


def load(pem_text: str) -> x509.Certificate | None:
    """The certificate stored in a profile, or None (none, or unreadable)."""
    if not pem_text.strip():
        return None
    try:
        _type, _headers, der = pem.unarmor(pem_text.encode("ascii"))
        return x509.Certificate.load(der)
    except (ValueError, TypeError):
        return None


def to_pem(cert: x509.Certificate) -> str:
    return pem.armor("CERTIFICATE", cert.dump()).decode("ascii")


def _attr(name: x509.Name, key: str) -> str:
    value = name.native.get(key, "")
    return ", ".join(value) if isinstance(value, list) else str(value)


def signer_name(cert: x509.Certificate) -> str:
    return _attr(cert.subject, "common_name") or cert.subject.human_friendly


def check_template(text: str) -> None:
    """Raise ValueError unless every % in the stamp text is %% or a known
    %(name)s. (Python would take "100% sure" as a conversion, silently.)"""
    for m in PERCENT.finditer(text):
        name = m.group(1)
        if m.group(0) == "%%":
            continue
        if name is None or not m.group(0).endswith(")s"):
            raise ValueError(_("the stamp text has a stray %: write %% for a percent sign, "
                               "placeholders as %(name)s"))
        if name not in STAMP_FIELDS:
            raise ValueError(_("unknown placeholder %({name})s in the stamp text; known: "
                               "{known}").format(name=name, known=", ".join(STAMP_FIELDS)))


def text_params(cert: x509.Certificate | None, profile=None,
                when: datetime | None = None) -> dict[str, str]:
    """Values for the stamp text. Without a certificate: sample values."""
    when = when or datetime.now().astimezone()
    if cert is None:
        params = dict(SAMPLE)
    else:
        s = cert.subject
        issuer = (_attr(cert.issuer, "common_name") or _attr(cert.issuer, "organization_name")
                  or cert.issuer.human_friendly)
        params = {
            "signer": signer_name(cert), "cn": _attr(s, "common_name"),
            "email": _attr(s, "email_address"), "org": _attr(s, "organization_name"),
            "unit": _attr(s, "organizational_unit_name"), "title": _attr(s, "title"),
            "country": _attr(s, "country_name"), "issuer": issuer,
            "serial": f"{cert.serial_number:X}",
        }
    params.update(ts=when.strftime(TS_FORMAT), date=f"{when:%Y-%m-%d}", time=f"{when:%H:%M}",
                  reason=getattr(profile, "reason", ""),
                  location=getattr(profile, "location", ""),
                  contact=getattr(profile, "contact_info", ""))
    return params


def fill(template: str, params: dict[str, str]) -> str:
    """The stamp text with its placeholders replaced. A line that is empty
    only because its placeholders are (e.g. an unset reason) is dropped."""
    lines = []
    for line in template.split("\n"):
        filled = line % params
        if filled.strip() or "%(" not in line:
            lines.append(filled)
    return "\n".join(lines)


def _key_description(cert: x509.Certificate) -> str:
    key = cert.public_key
    algo = key.algorithm
    if algo == "rsa":
        return f"RSA {key.bit_size}"
    if algo == "ec":
        return f"EC {key.curve[1]}"
    return algo.upper()


def expires_soon(cert: x509.Certificate, days: int = 30) -> bool:
    left = cert.not_valid_after - datetime.now(timezone.utc)
    return left.days < days


def validity(cert: x509.Certificate) -> str:
    now = datetime.now(timezone.utc)
    if now > cert.not_valid_after:
        return _("expired")
    if now < cert.not_valid_before:
        return _("not yet valid")
    left = (cert.not_valid_after - now).days
    return _("valid, {days} days left").format(days=left)


def details(cert: x509.Certificate) -> list[tuple[str, str]]:
    """(label, value) rows describing the certificate."""
    rows = [(_("Subject"), cert.subject.human_friendly),
            (_("Issuer"), cert.issuer.human_friendly),
            (_("Serial number"), f"{cert.serial_number:X}"),
            (_("Valid from"), f"{cert.not_valid_before:%Y-%m-%d %H:%M} UTC"),
            (_("Valid until"), f"{cert.not_valid_after:%Y-%m-%d %H:%M} UTC"),
            (_("Status"), validity(cert)),
            (_("Key"), _key_description(cert))]
    if cert.key_usage_value is not None:
        rows.append((_("Key usage"), ", ".join(sorted(cert.key_usage_value.native))))
    if cert.extended_key_usage_value is not None:
        rows.append((_("Extended key usage"),
                     ", ".join(cert.extended_key_usage_value.native)))
    if emails := cert.subject_alt_name_value:
        rows.append((_("Alternative names"),
                     ", ".join(str(n.native) for n in emails)))
    der = cert.dump()
    rows += [(_("SHA-256 fingerprint"), hashlib.sha256(der).hexdigest().upper()),
             (_("SHA-1 fingerprint"), hashlib.sha1(der).hexdigest().upper())]
    return rows
