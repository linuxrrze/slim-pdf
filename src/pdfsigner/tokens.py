"""What is on a PKCS#11 token: for choosing token label and key ID of a profile.

Certificates are public; private keys are usually only visible after a
login, so :func:`list_tokens` lists them only when given the PIN.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .config import expand
from .i18n import _
from .signing import SigningError, _p11_attr


@dataclass
class TokenEntry:
    """A key and/or certificate, paired by their ID."""

    id: bytes | None
    key_label: str | None = None
    key_type: str | None = None  # "EC", "RSA", ...
    always_auth: bool = False  # PIN before every signature
    cert_label: str | None = None
    subject: str | None = None
    not_after: str | None = None  # YYYY-MM-DD

    @property
    def id_hex(self) -> str:
        return self.id.hex() if self.id else ""

    def describe(self) -> str:
        parts = [_("ID {id}").format(id=f"{self.id_hex or '-':4}")]
        if self.key_label is not None:
            key = (_("key '{label}' ({type}, PIN for every signature)") if self.always_auth
                   else _("key '{label}' ({type})"))
            parts.append(key.format(label=self.key_label, type=self.key_type or "?"))
        if self.cert_label is not None:
            parts.append(_("certificate '{label}': {subject}, expires {date}").format(
                label=self.cert_label, subject=self.subject, date=self.not_after))
        return "  ".join(parts)


@dataclass
class TokenInfo:
    label: str
    manufacturer: str
    model: str
    serial: str
    logged_in: bool
    entries: list[TokenEntry] = field(default_factory=list)

    def describe(self) -> str:
        return _("token '{label}' ({manufacturer} {model}, serial {serial})").format(
            label=self.label, manufacturer=self.manufacturer, model=self.model,
            serial=self.serial or "-")


def _text(value) -> str:
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    return (value or "").strip()


def _read(session, logged_in: bool) -> list[TokenEntry]:
    from asn1crypto import x509
    from pkcs11 import Attribute, ObjectClass

    entries: list[TokenEntry] = []
    by_id: dict[bytes, TokenEntry] = {}

    def entry_for(oid: bytes | None) -> TokenEntry:
        if not oid:
            e = TokenEntry(id=None)
            entries.append(e)
            return e
        if oid not in by_id:
            by_id[oid] = TokenEntry(id=oid)
            entries.append(by_id[oid])
        return by_id[oid]

    if logged_in:
        for key in session.get_objects({Attribute.CLASS: ObjectClass.PRIVATE_KEY}):
            e = entry_for(_p11_attr(key, Attribute.ID))
            e.key_label = _p11_attr(key, Attribute.LABEL) or ""
            kt = _p11_attr(key, Attribute.KEY_TYPE)
            e.key_type = getattr(kt, "name", None)
            e.always_auth = bool(_p11_attr(key, Attribute.ALWAYS_AUTHENTICATE))
    for obj in session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE}):
        e = entry_for(_p11_attr(obj, Attribute.ID))
        if e.cert_label is not None:  # second certificate with this ID
            e = entry_for(None)
            e.id = _p11_attr(obj, Attribute.ID)
        e.cert_label = _p11_attr(obj, Attribute.LABEL) or ""
        try:
            cert = x509.Certificate.load(_p11_attr(obj, Attribute.VALUE) or b"")
            e.subject = cert.subject.human_friendly
            e.not_after = cert["tbs_certificate"]["validity"]["not_after"].native.date().isoformat()
        except ValueError:
            e.subject, e.not_after = _("(unreadable)"), "?"
    entries.sort(key=lambda e: (e.id is None, e.id or b""))
    return entries


def list_tokens(module: str, token_label: str | None = None,
                ask_pin: Callable[[str], str | None] | None = None) -> list[TokenInfo]:
    """Initialised tokens of ``module`` and their keys/certificates.

    ``ask_pin(token label)`` is called per token to log in (keys are listed
    only then); returning None skips the login.
    """
    try:
        import pkcs11
        from pkcs11 import TokenFlag
        from pkcs11.exceptions import PinIncorrect, PinLenRange, PKCS11Error
    except ImportError as e:
        raise SigningError(_("PKCS#11 support needs 'pyhanko[pkcs11]'")) from e

    try:
        lib = pkcs11.lib(str(expand(module)))
        tokens = [t for t in lib.get_tokens(token_label=token_label or None)
                  if t.flags & TokenFlag.TOKEN_INITIALIZED]
    except PKCS11Error as e:
        raise SigningError(_("PKCS#11 ({module}): {error}").format(
            module=module, error=str(e) or type(e).__name__)) from None
    result = []
    for t in tokens:
        info = TokenInfo(label=_text(t.label), manufacturer=_text(t.manufacturer_id),
                         model=_text(t.model), serial=_text(t.serial), logged_in=False)
        pin = ask_pin(info.label) if ask_pin else None
        try:
            with t.open(user_pin=pin) if pin is not None else t.open() as session:
                info.logged_in = pin is not None
                info.entries = _read(session, info.logged_in)
        except (PinIncorrect, PinLenRange):
            raise SigningError(
                _("wrong PIN for token '{label}'").format(label=info.label)) from None
        except PKCS11Error as e:
            raise SigningError(_("PKCS#11 ({module}), token '{label}': {error}").format(
                module=module, label=info.label, error=str(e) or type(e).__name__)) from None
        result.append(info)
    return result
