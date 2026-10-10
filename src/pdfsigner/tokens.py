"""What is on a PKCS#11 token: for choosing token label and key ID of a profile.

Certificates are public; private keys are usually only visible after a
login, so :func:`list_tokens` lists them only when given the PIN.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

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
    module: str
    label: str
    manufacturer: str
    model: str
    serial: str
    logged_in: bool
    entries: list[TokenEntry] = field(default_factory=list)
    pin_status: str = ""  # "locked", "final try", "count low" or ""

    def describe(self) -> str:
        text = _("token '{label}' ({manufacturer} {model}, serial {serial})").format(
            label=self.label, manufacturer=self.manufacturer, model=self.model,
            serial=self.serial or "-")
        status = {"locked": _("PIN locked"), "final try": _("one PIN attempt left"),
                  "count low": _("few PIN attempts left")}.get(self.pin_status)
        return f"{text}  [{status}]" if status else text


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


# Usual install locations of PKCS#11 modules for smartcards and tokens.
KNOWN_MODULES = {
    "linux": [
        "/usr/lib/*/opensc-pkcs11.so", "/usr/lib64/opensc-pkcs11.so",
        "/usr/lib/opensc-pkcs11.so",
        "/usr/lib/*/libykcs11.so*", "/usr/lib64/libykcs11.so*", "/usr/local/lib/libykcs11.so*",
        # the distributions' directory for PKCS#11 modules
        "/usr/lib/*/pkcs11/*.so", "/usr/lib64/pkcs11/*.so", "/usr/lib/pkcs11/*.so",
        # vendor software: SafeNet/Thales Authentication Client, Thales IDPrime,
        # Atos CardOS API, and packages that install below /opt
        "/usr/lib/libeTPkcs11.so", "/usr/lib64/libeTPkcs11.so",
        "/usr/lib/libIDPrimePKCS11.so", "/usr/lib64/libIDPrimePKCS11.so",
        "/usr/lib/libcardos11.so", "/usr/lib64/libcardos11.so",
        "/opt/*/lib/*pkcs11*.so", "/opt/*/lib64/*pkcs11*.so",
    ],
    "darwin": [
        "/Library/OpenSC/lib/opensc-pkcs11.so", "/opt/homebrew/lib/opensc-pkcs11.so",
        "/usr/local/lib/opensc-pkcs11.so", "/opt/homebrew/lib/libykcs11.dylib",
        "/usr/local/lib/libykcs11.dylib",
    ],
    "win32": [
        r"C:\Program Files\OpenSC Project\OpenSC\pkcs11\opensc-pkcs11.dll",
        r"C:\Program Files\Yubico\Yubico PIV Tool\bin\libykcs11.dll",
    ],
}
# p11-kit's registry (Linux): packages describe their module in a *.module
# file; a file in a later directory replaces the one of the same name.
# The user's own directory is added by _p11_kit_config_dirs().
P11_KIT_CONFIG = ["/usr/share/p11-kit/modules", "/etc/pkcs11/modules"]
# Where p11-kit looks for a module that is given without a directory.
P11_KIT_MODULE_DIRS = ["/usr/lib/*/pkcs11", "/usr/lib64/pkcs11", "/usr/lib/pkcs11"]
# p11-kit's proxy bundles all registered modules, so it is only offered when
# no module is found directly.
P11_KIT_PROXY = ["/usr/lib/*/p11-kit-proxy.so", "/usr/lib64/p11-kit-proxy.so"]
# Modules without signing keys: p11-kit's CA list, proxy and remote client,
# GNOME Keyring, NSS' built-in roots and software store.
NOT_FOR_SIGNING = {"p11-kit-trust.so", "p11-kit-client.so", "p11-kit-proxy.so",
                   "gnome-keyring-pkcs11.so", "libnssckbi.so", "libsoftokn3.so"}
PROGRAM = "pdfsigner"  # our name in p11-kit's enable-in / disable-in


def _pin_status(flags) -> str:
    from pkcs11 import TokenFlag

    for flag, status in ((TokenFlag.USER_PIN_LOCKED, "locked"),
                         (TokenFlag.USER_PIN_FINAL_TRY, "final try"),
                         (TokenFlag.USER_PIN_COUNT_LOW, "count low")):
        if flags & flag:
            return status
    return ""


def _p11_kit_config_dirs() -> list[str]:
    user = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return [*P11_KIT_CONFIG, os.path.join(user, "pkcs11", "modules")]


def _p11_kit_modules() -> list[str]:
    """Modules registered with p11-kit, unless their entry excludes us."""
    import glob

    files: dict[str, Path] = {}
    for d in _p11_kit_config_dirs():
        try:
            for f in sorted(Path(d).glob("*.module")):
                files[f.name] = f
        except OSError:
            continue
    modules = []
    for name in sorted(files):
        try:
            text = files[name].read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        conf = {}
        for line in text.splitlines():
            key, sep, value = line.partition(":")
            if sep and not key.lstrip().startswith("#"):
                conf[key.strip().lower()] = value.strip()
        module = conf.get("module", "")
        if (not module or conf.get("trust-policy", "").lower() in ("yes", "true")
                or PROGRAM in conf.get("disable-in", "").split()
                or ("enable-in" in conf and PROGRAM not in conf["enable-in"].split())):
            continue
        if os.path.isabs(module):
            modules.append(module)
        else:
            for d in P11_KIT_MODULE_DIRS:
                modules += sorted(glob.glob(os.path.join(d, module)))
    return modules


def find_modules(platform: str | None = None) -> list[str]:
    """Installed PKCS#11 modules, without duplicates and without modules that
    hold no keys: on Linux those registered with p11-kit first, then those in
    the usual places (KNOWN_MODULES)."""
    import glob
    import sys

    def existing(paths):
        return [p for p in paths if Path(p).is_file()]

    platform = platform or ("linux" if sys.platform.startswith("linux") else sys.platform)
    candidates = _p11_kit_modules() if platform == "linux" else []
    for pattern in KNOWN_MODULES.get(platform, []):
        candidates += sorted(glob.glob(pattern))
    found: dict[str, str] = {}
    for path in existing(candidates):
        real = os.path.realpath(path)
        if not {os.path.basename(path), os.path.basename(real)} & NOT_FOR_SIGNING:
            found.setdefault(real, path)
    if not found and platform == "linux":  # the proxy only as a fallback
        for path in existing(p for pattern in P11_KIT_PROXY for p in sorted(glob.glob(pattern))):
            found.setdefault(os.path.realpath(path), path)
    return list(found.values())


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
        info = TokenInfo(module=module, label=_text(t.label),
                         manufacturer=_text(t.manufacturer_id), model=_text(t.model),
                         serial=_text(t.serial), logged_in=False, pin_status=_pin_status(t.flags))
        # a locked PIN can't be used, and a wrong one shouldn't be risked here
        pin = ask_pin(info.label) if ask_pin and info.pin_status != "locked" else None
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
