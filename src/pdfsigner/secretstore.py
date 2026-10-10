"""Resolve the secret (PKCS#12 passphrase / PKCS#11 PIN) for a profile.

Sources (``CertProfile.secret``):

* ``prompt``  - ask via the ``ask`` callback (getpass in the CLI, a dialog in the GUI)
* ``keyring`` - OS keyring (Secret Service / KWallet). KeePassXC can act as the
                Secret Service provider, so this is also the "KeePassXC" route.
* ``keepass`` - open a .kdbx file with pykeepass. The entry's password is the
                passphrase/PIN; optionally an attachment holds the .p12 itself,
                so the key never lies around unencrypted on disk.
* ``keepassxc`` - same, but through KeePassXC's ``keepassxc-cli``. Slower,
                but supports YubiKey challenge-response on the database.
* ``none``    - no secret.

See docs/secret-storage.md for the trade-offs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

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
    if profile.type == "system":
        return Credentials()  # Windows/macOS ask for PINs themselves
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
        case "keepassxc":
            return _from_keepassxc_cli(profile, ask)
        case other:
            raise SecretError(f"unknown secret source {other!r}")


def _ask(ask: AskFn, prompt: str) -> str:
    value = ask(prompt)
    if value is None:
        raise Cancelled()
    return value


# --- keyring -----------------------------------------------------------------


def _keyring():
    try:
        import keyring
    except ImportError as e:
        raise SecretError("keyring support needs the 'keyring' package") from e
    return keyring


def _keyring_call(fn, *args):
    """Run a keyring call; backend problems become SecretErrors, not tracebacks."""
    from keyring.errors import KeyringError, NoKeyringError

    try:
        return fn(*args)
    except NoKeyringError:
        raise SecretError(
            "no OS keyring available (on Linux: start a Secret Service provider such as "
            "GNOME Keyring, KWallet or KeePassXC, or use another secret source)") from None
    except KeyringError as e:
        raise SecretError(f"keyring: {e}") from None


def keyring_get(profile_name: str) -> str | None:
    return _keyring_call(_keyring().get_password, KEYRING_SERVICE, profile_name)


def keyring_set(profile_name: str, secret: str) -> None:
    _keyring_call(_keyring().set_password, KEYRING_SERVICE, profile_name, secret)


def keyring_delete(profile_name: str) -> None:
    from keyring.errors import PasswordDeleteError

    def delete(*args):
        try:
            _keyring().delete_password(*args)
        except PasswordDeleteError:
            pass  # nothing stored: fine

    _keyring_call(delete, KEYRING_SERVICE, profile_name)


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


# --- keepassxc-cli -------------------------------------------------------------

KEEPASSXC_CLI_CANDIDATES = {
    "darwin": ["/Applications/KeePassXC.app/Contents/MacOS/keepassxc-cli"],
    "win32": [r"C:\Program Files\KeePassXC\keepassxc-cli.exe"],
}
CLI_TIMEOUT = 120  # seconds; leaves time to touch a YubiKey


def keepassxc_cli() -> str:
    """Path of keepassxc-cli: $PDFSIGNER_KEEPASSXC_CLI, PATH, or the default install."""
    env = os.environ.get("PDFSIGNER_KEEPASSXC_CLI")
    if env:
        return env
    found = shutil.which("keepassxc-cli")
    if found:
        return found
    for p in KEEPASSXC_CLI_CANDIDATES.get(sys.platform, []):
        if Path(p).exists():
            return p
    raise SecretError("keepassxc-cli not found (install KeePassXC, or set "
                      "PDFSIGNER_KEEPASSXC_CLI to its path)")


def _cli_error(db: Path, stderr: str) -> SecretError:
    msg = stderr.strip().splitlines()
    # With -q a wrong password produces no message at all.
    if not msg or "Invalid credentials" in stderr:
        return SecretError(f"wrong master password, key file or YubiKey for {db}")
    return SecretError(f"keepassxc-cli: {msg[-1]}")


def _from_keepassxc_cli(profile: CertProfile, ask: AskFn) -> Credentials:
    cli = keepassxc_cli()
    db = expand(profile.keepass_db)
    if not db.is_file():
        raise SecretError(f"KeePass database not found: {db}")
    opts = ["-q"]
    if profile.keepass_keyfile:
        opts += ["-k", str(expand(profile.keepass_keyfile))]
    if profile.keepass_yubikey:
        opts += ["-y", profile.keepass_yubikey]
    entry = profile.keepass_entry
    entry = entry if entry.startswith("/") else "/" + entry
    hint = " (then touch your YubiKey)" if profile.keepass_yubikey else ""
    master = _ask(ask, f"KeePass master password for {db.name}{hint}")
    stdin = (master + "\n").encode()

    r = subprocess.run([cli, "show", *opts, "-a", "Password", str(db), entry],
                       input=stdin, capture_output=True, timeout=CLI_TIMEOUT)
    if r.returncode != 0:
        raise _cli_error(db, r.stderr.decode(errors="replace"))
    # keepassxc-cli prints the attribute followed by a newline
    creds = Credentials(secret=r.stdout.decode().removesuffix("\n").removesuffix("\r") or None)

    if profile.keepass_attachment:
        creds.pkcs12_data = _export_attachment(
            cli, opts, db, entry, profile.keepass_attachment, stdin)
    return creds


def _export_attachment(cli, opts, db, entry, name, stdin) -> bytes:
    """Export an attachment byte-exactly.

    ``attachment-export --stdout`` writes the data as text and mangles binary
    files, so it goes to a named pipe instead (never touching the disk), or,
    on Windows, to a file in a private temporary directory.
    """
    with tempfile.TemporaryDirectory(prefix="pdfsigner-") as tmp:
        target = Path(tmp) / "attachment"
        argv = [cli, "attachment-export", *opts, str(db), entry, name, str(target)]
        if not hasattr(os, "mkfifo"):
            r = subprocess.run(argv, input=stdin, capture_output=True, timeout=CLI_TIMEOUT)
            if r.returncode != 0:
                raise _cli_error(db, r.stderr.decode(errors="replace"))
            return target.read_bytes()

        os.mkfifo(target, 0o600)
        result: dict = {}

        def reader():
            with open(target, "rb") as f:  # blocks until keepassxc-cli opens it
                result["data"] = f.read()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        r = subprocess.run(argv, input=stdin, capture_output=True, timeout=CLI_TIMEOUT)
        if t.is_alive():
            t.join(0.5)
        if t.is_alive():  # the writer never opened the pipe: release the reader
            os.close(os.open(target, os.O_WRONLY | os.O_NONBLOCK))
            t.join(5)
        if r.returncode != 0:
            raise _cli_error(db, r.stderr.decode(errors="replace"))
        return result.get("data", b"")
