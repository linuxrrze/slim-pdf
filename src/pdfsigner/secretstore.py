"""Resolve the secret (PKCS#12 passphrase / PKCS#11 PIN) for a profile.

Sources (``CertProfile.secret``):

* ``prompt``  - ask via the ``ask`` callback (getpass in the CLI, a dialog in the GUI)
* ``keyring`` - OS keyring (Secret Service / KWallet). KeePassXC can act as the
                Secret Service provider, so this is also the "KeePassXC" route.
* ``keepass`` - open a .kdbx file with pykeepass. The entry's password is the
                passphrase/PIN; optionally an attachment holds the .p12 itself,
                so the key never lies around unencrypted on disk.
* ``none``    - no secret.

See docs/secret-storage.md for the trade-offs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .config import CertProfile, expand

KEYRING_SERVICE = "pdfsigner"

# Called with a human-readable prompt; returns the secret or None if cancelled.
AskFn = Callable[[str], "str | None"]


class Cancelled(Exception):
    pass


class SecretError(Exception):
    pass


@dataclass
class Credentials:
    secret: str | None = None
    pkcs12_data: bytes | None = None


def resolve_credentials(profile: CertProfile, ask: AskFn) -> Credentials:
    what = "PIN" if profile.type == "pkcs11" else "passphrase"
    match profile.secret:
        case "none":
            return Credentials()
        case "prompt":
            return Credentials(secret=_ask(ask, f"{what} for certificate '{profile.name}'"))
        case "keyring":
            secret = keyring_get(profile.name)
            if secret is None:
                raise SecretError(
                    f"no {what} for '{profile.name}' in the keyring "
                    f"(store it with: pdfsigner certs set-secret {profile.name})"
                )
            return Credentials(secret=secret)
        case "keepass":
            return _from_keepass(profile, ask)
        case other:
            raise SecretError(f"unknown secret source {other!r}")


def _ask(ask: AskFn, prompt: str) -> str:
    value = ask(prompt)
    if value is None:
        raise Cancelled()
    return value


# --- keyring -----------------------------------------------------------------


def keyring_get(profile_name: str) -> str | None:
    import keyring

    return keyring.get_password(KEYRING_SERVICE, profile_name)


def keyring_set(profile_name: str, secret: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, profile_name, secret)


def keyring_delete(profile_name: str) -> None:
    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(KEYRING_SERVICE, profile_name)
    except PasswordDeleteError:
        pass


def keyring_backend_name() -> str:
    import keyring

    kr = keyring.get_keyring()
    return f"{type(kr).__module__}.{type(kr).__name__}"


# --- KeePass -----------------------------------------------------------------


def _from_keepass(profile: CertProfile, ask: AskFn) -> Credentials:
    try:
        from pykeepass import PyKeePass
        from pykeepass.exceptions import CredentialsError
    except ImportError as e:
        raise SecretError("KeePass support needs the 'pykeepass' package") from e

    db = expand(profile.keepass_db)
    keyfile = str(expand(profile.keepass_keyfile)) if profile.keepass_keyfile else None
    master = _ask(ask, f"KeePass master password for {db.name}")
    try:
        kp = PyKeePass(str(db), password=master or None, keyfile=keyfile)
    except CredentialsError:
        raise SecretError(f"wrong master password or key file for {db}") from None
    except FileNotFoundError:
        raise SecretError(f"KeePass database not found: {db}") from None

    entry = find_keepass_entry(kp, profile.keepass_entry)
    if entry is None:
        raise SecretError(f"entry {profile.keepass_entry!r} not found in {db.name}")

    creds = Credentials(secret=entry.password or None)
    if profile.keepass_attachment:
        for att in entry.attachments:
            if att.filename == profile.keepass_attachment:
                creds.pkcs12_data = att.data
                break
        else:
            raise SecretError(
                f"attachment {profile.keepass_attachment!r} not found on entry "
                f"{profile.keepass_entry!r}"
            )
    return creds


def find_keepass_entry(kp, spec: str):
    """``spec`` is either ``Group/Sub/Title`` or a bare title.

    Titles may contain ``/`` themselves ("FAU S/MIME"), so every split point
    between group path and title is tried, then the whole spec as a title.
    """
    parts = spec.split("/")
    for i in range(1, len(parts)):
        group = kp.find_groups(path=parts[:i], first=True)
        if group is None:
            break
        entry = kp.find_entries(group=group, title="/".join(parts[i:]),
                                recursive=False, first=True)
        if entry is not None:
            return entry
    return kp.find_entries(title=spec, first=True)
