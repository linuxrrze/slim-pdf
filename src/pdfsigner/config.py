"""Certificate profiles, persisted as TOML.

The same file is read and written by the CLI (``pdfsigner certs ...``) and the
GUI certificate manager. Location: ``$PDFSIGNER_CONFIG`` or
``<user config dir>/pdfsigner/config.toml``.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import tomli_w
from platformdirs import user_config_dir

CERT_TYPES = ("pkcs12", "pkcs11", "system")

# Where the secret (PKCS#12 passphrase or PKCS#11 PIN) comes from.
#   prompt  - ask interactively every time
#   keyring - OS keyring: Secret Service / KWallet on Linux (KeePassXC can serve
#             this), Keychain on macOS, Credential Manager on Windows
#   keepass - read from a .kdbx database via pykeepass
#   keepassxc - read via KeePassXC's keepassxc-cli (supports YubiKey challenge-response)
#   none    - no secret needed (unencrypted PKCS#12, PIN pad readers)
SECRET_SOURCES = ("prompt", "keyring", "keepass", "keepassxc", "none")
KEEPASS_SOURCES = ("keepass", "keepassxc")

DEFAULT_STAMP_TEXT = "Digitally signed by %(signer)s\n%(ts)s"

# Where OpenSC installs its PKCS#11 module; shown as an example in CLI help and GUI.
PKCS11_EXAMPLE = {
    "win32": r"C:\Program Files\OpenSC Project\OpenSC\pkcs11\opensc-pkcs11.dll",
    "darwin": "/Library/OpenSC/lib/opensc-pkcs11.so",
}.get(sys.platform, "/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so")


def normalize_hex(value: str) -> str:
    """'0x02', '02', 'de:ad be-ef' -> '02', 'deadbeef'."""
    v = value.strip().lower()
    v = v[2:] if v.startswith("0x") else v
    return re.sub(r"[\s:-]", "", v)


@dataclass
class CertProfile:
    name: str
    type: str = "pkcs12"

    # pkcs12: path to the .p12/.pfx file. May be empty when the file is
    # stored as a KeePass attachment instead (see keepass_attachment).
    path: str = ""

    # pkcs11
    pkcs11_lib: str = ""
    token_label: str = ""
    key_label: str = ""
    key_id: str = ""  # CKA_ID in hex, e.g. "02" (YubiKey PIV slot 9c)
    cert_label: str = ""  # empty: the certificate with the key's ID

    # system: Windows certificate store / macOS keychain; SHA-1 thumbprint
    # or part of the subject (see `pdfsigner certs system`)
    system_cert: str = ""

    # Appearance
    image: str = ""
    stamp_text: str = DEFAULT_STAMP_TEXT

    # Signature metadata
    reason: str = ""
    location: str = ""
    contact_info: str = ""
    timestamp_url: str = ""
    # Embed certificate chain and revocation info (PAdES B-LT; B-LTA together
    # with a timestamp server) so the signature stays verifiable after expiry.
    ltv: bool = False

    # Secret handling
    secret: str = "prompt"
    keepass_db: str = ""
    keepass_keyfile: str = ""
    keepass_entry: str = ""  # "Group/Sub/Title" or just "Title"
    keepass_attachment: str = ""  # attachment filename holding the .p12
    keepass_yubikey: str = ""  # keepassxc only: YubiKey "slot[:serial]", e.g. "2"

    def validate(self) -> None:
        if not self.name:
            raise ValueError("certificate profile needs a name")
        if self.type not in CERT_TYPES:
            raise ValueError(f"{self.name}: unknown type {self.type!r}")
        if self.secret not in SECRET_SOURCES:
            raise ValueError(f"{self.name}: unknown secret source {self.secret!r}")
        if self.type == "pkcs12" and not (self.path or self.keepass_attachment):
            raise ValueError(f"{self.name}: pkcs12 needs a path or a KeePass attachment")
        if self.keepass_attachment and self.secret not in KEEPASS_SOURCES:
            raise ValueError(f"{self.name}: keepass_attachment requires secret = "
                             "'keepass' or 'keepassxc'")
        if self.keepass_yubikey and self.secret != "keepassxc":
            raise ValueError(f"{self.name}: keepass_yubikey requires secret = 'keepassxc'")
        if self.type == "system" and not self.system_cert:
            raise ValueError(f"{self.name}: system needs system_cert (thumbprint or subject)")
        if self.type == "pkcs11" and not self.pkcs11_lib:
            raise ValueError(f"{self.name}: pkcs11 needs pkcs11_lib")
        if self.key_id:
            try:
                bytes.fromhex(normalize_hex(self.key_id))
            except ValueError:
                raise ValueError(f"{self.name}: key_id must be hex, e.g. 02") from None
        if not isinstance(self.ltv, bool):
            raise ValueError(f"{self.name}: ltv must be true or false")
        if self.secret in KEEPASS_SOURCES and not (self.keepass_db and self.keepass_entry):
            raise ValueError(f"{self.name}: {self.secret} needs keepass_db and keepass_entry")

    @property
    def image_path(self) -> Path | None:
        return expand(self.image) if self.image else None

    def to_dict(self) -> dict:
        d = asdict(self)
        del d["name"]
        # Keep the file readable: only write non-default values.
        defaults = CertProfile(name=self.name)
        return {k: v for k, v in d.items() if v != getattr(defaults, k) or k == "type"}

    @classmethod
    def from_dict(cls, name: str, d: dict) -> CertProfile:
        known = {f.name for f in fields(cls)} - {"name"}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"{name}: unknown keys {sorted(unknown)}")
        return cls(name=name, **d)


@dataclass
class Config:
    certs: dict[str, CertProfile] = field(default_factory=dict)
    default: str = ""
    trust: list[str] = field(default_factory=list)  # extra trust roots for verify
    path: Path | None = None

    def get(self, name: str | None) -> CertProfile:
        name = name or self.default
        if not name:
            if len(self.certs) == 1:
                return next(iter(self.certs.values()))
            raise KeyError("no certificate given and no default configured")
        try:
            return self.certs[name]
        except KeyError:
            raise KeyError(f"unknown certificate {name!r}") from None

    def put(self, profile: CertProfile) -> None:
        profile.validate()
        self.certs[profile.name] = profile
        if not self.default:
            self.default = profile.name

    def remove(self, name: str) -> None:
        del self.certs[name]
        if self.default == name:
            self.default = next(iter(self.certs), "")

    def save(self, path: Path | None = None) -> Path:
        path = path or self.path or default_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if self.default:
            data["default"] = self.default
        if self.trust:
            data["trust"] = list(self.trust)
        data["certs"] = {n: p.to_dict() for n, p in self.certs.items()}
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            tomli_w.dump(data, f)
        os.chmod(tmp, 0o600)  # owner only; on Windows the profile dir is private anyway
        tmp.replace(path)
        self.path = path
        return path


def default_config_path() -> Path:
    env = os.environ.get("PDFSIGNER_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path(user_config_dir("pdfsigner", appauthor=False)) / "config.toml"


def load_config(path: Path | None = None) -> Config:
    path = path or default_config_path()
    if not path.exists():
        return Config(path=path)
    with open(path, "rb") as f:
        data = tomllib.load(f)
    certs = {
        name: CertProfile.from_dict(name, d) for name, d in data.get("certs", {}).items()
    }
    unknown = set(data) - {"default", "trust", "certs"}
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
    return Config(certs=certs, default=data.get("default", ""),
                  trust=list(data.get("trust", [])), path=path)


def expand(p: str) -> Path:
    return Path(os.path.expandvars(p)).expanduser()
