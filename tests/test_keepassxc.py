"""The keepassxc-cli secret source.

Real-binary tests run when keepassxc-cli is installed (or
PDFSIGNER_KEEPASSXC_CLI points to it); the protocol tests use a fake CLI.
"""

import json
import os
import sys
import threading

import pytest

from pdfsigner import secretstore
from pdfsigner.config import CertProfile
from pdfsigner.secretstore import Cancelled, SecretError, resolve_credentials
from pdfsigner.signing import open_signer

from conftest import PASSPHRASE


def _real_cli():
    """The app's own lookup, so default install locations count too."""
    try:
        return secretstore.keepassxc_cli()
    except SecretError:
        return None


@pytest.fixture
def real_cli():
    if not _real_cli():
        pytest.skip("keepassxc-cli not installed")


def kpxc_profile(db, entry="Certificates/FAU S/MIME", **kw):
    return CertProfile(name="xc", secret="keepassxc", keepass_db=str(db),
                       keepass_entry=entry, **kw)


def answer(value):
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return value

    ask.asked = asked
    return ask


# --- real keepassxc-cli ------------------------------------------------------------------


def test_passphrase_and_attachment(real_cli, keepass_db, p12):
    profile = kpxc_profile(keepass_db, keepass_attachment="alice.p12")
    ask = answer("master")
    creds = resolve_credentials(profile, ask)
    assert creds.secret == PASSPHRASE
    assert creds.pkcs12_data == p12[0].read_bytes()  # binary, byte-exact
    assert ask.asked == ["KeePass master password for vault.kdbx"]
    with open_signer(profile, creds) as signer:
        assert "Alice Example" in signer.signing_cert.subject.human_friendly


def test_attachment_via_temp_file(real_cli, keepass_db, p12, monkeypatch):
    """The Windows code path (no named pipes)."""
    monkeypatch.delattr(os, "mkfifo")
    profile = kpxc_profile(keepass_db, keepass_attachment="alice.p12")
    assert resolve_credentials(profile, answer("master")).pkcs12_data == p12[0].read_bytes()


def test_root_entry_and_leading_slash(real_cli, keepass_db):
    assert resolve_credentials(kpxc_profile(keepass_db, "Plain"), answer("master")).secret \
        == "plain-secret"
    assert resolve_credentials(kpxc_profile(keepass_db, "/Plain"), answer("master")).secret \
        == "plain-secret"


def test_wrong_master_password(real_cli, keepass_db):
    with pytest.raises(SecretError, match="wrong master password"):
        resolve_credentials(kpxc_profile(keepass_db), answer("nope"))


def test_missing_entry(real_cli, keepass_db):
    with pytest.raises(SecretError, match="Could not find entry"):
        resolve_credentials(kpxc_profile(keepass_db, "Certificates/Nope"), answer("master"))


def test_missing_attachment(real_cli, keepass_db):
    profile = kpxc_profile(keepass_db, keepass_attachment="nope.p12")
    with pytest.raises(SecretError, match="nope.p12"):
        resolve_credentials(profile, answer("master"))


def test_key_file(real_cli, tmp_path):
    from pykeepass import create_database

    keyfile = tmp_path / "db.key"
    keyfile.write_bytes(os.urandom(64))
    db = tmp_path / "k.kdbx"
    kp = create_database(str(db), password="master", keyfile=str(keyfile))
    kp.add_entry(kp.root_group, "Sig", "u", "with-keyfile")
    kp.save()
    profile = kpxc_profile(db, "Sig", keepass_keyfile=str(keyfile))
    assert resolve_credentials(profile, answer("master")).secret == "with-keyfile"
    profile.keepass_keyfile = ""
    with pytest.raises(SecretError, match="wrong master password"):
        resolve_credentials(profile, answer("master"))


# --- protocol, with a fake keepassxc-cli ----------------------------------------------------

FAKE = r'''
import json, os, sys
args = sys.argv[1:]
master = sys.stdin.readline().rstrip("\n")
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"args": args, "master": master}) + "\n")
mode = os.environ.get("FAKE_MODE", "ok")
if args[0] == "show":
    if mode == "badpw":
        sys.exit(1)
    sys.stdout.write("s3cret\n")
elif args[0] == "attachment-export":
    if mode == "noattach":
        sys.stderr.write("Could not find attachment with name x.p12.\n")
        sys.exit(1)
    with open(args[-1], "wb") as f:
        f.write(b"\x00\xff\x80binary")
'''


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    script = tmp_path / "fake_kpxc.py"
    script.write_text(FAKE)
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    real_run = secretstore.subprocess.run

    def run(argv, **kw):  # run the fake through the current interpreter (works on Windows)
        if argv[0] == "fake-keepassxc-cli":
            argv = [sys.executable, str(script), *argv[1:]]
        return real_run(argv, **kw)

    monkeypatch.setattr(secretstore.subprocess, "run", run)
    monkeypatch.setenv("PDFSIGNER_KEEPASSXC_CLI", "fake-keepassxc-cli")

    def calls():
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    return calls


def test_protocol(fake_cli, tmp_path):
    db = tmp_path / "v.kdbx"
    db.write_bytes(b"x")
    profile = kpxc_profile(db, "Group/Title", keepass_attachment="x.p12",
                           keepass_keyfile=str(tmp_path / "k.key"), keepass_yubikey="2:123")
    ask = answer("master pw")
    creds = resolve_credentials(profile, ask)
    assert creds.secret == "s3cret"
    assert creds.pkcs12_data == b"\x00\xff\x80binary"
    assert ask.asked == ["KeePass master password for v.kdbx (then touch your YubiKey)"]
    show, export = fake_cli()
    opts = ["-q", "-k", str(tmp_path / "k.key"), "-y", "2:123"]
    assert show["args"] == ["show", *opts, "-a", "Password", str(db), "/Group/Title"]
    assert export["args"][:-1] == ["attachment-export", *opts, str(db), "/Group/Title", "x.p12"]
    assert show["master"] == export["master"] == "master pw"  # asked once, used twice


@pytest.mark.parametrize("mode, match", [("badpw", "wrong master password"),
                                         ("noattach", "Could not find attachment")])
def test_failures_do_not_hang(fake_cli, tmp_path, monkeypatch, mode, match):
    """A CLI that fails without opening the named pipe must not block the reader."""
    monkeypatch.setenv("FAKE_MODE", mode)
    db = tmp_path / "v.kdbx"
    db.write_bytes(b"x")
    profile = kpxc_profile(db, keepass_attachment="x.p12")
    outcome = {}

    def go():
        try:
            resolve_credentials(profile, answer("pw"))
        except SecretError as e:
            outcome["error"] = str(e)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(20)
    assert not t.is_alive(), "hung waiting for keepassxc-cli"
    assert match in outcome["error"]


def test_cancel_and_missing_db(fake_cli, tmp_path):
    db = tmp_path / "v.kdbx"
    with pytest.raises(SecretError, match="not found"):
        resolve_credentials(kpxc_profile(db), answer("pw"))
    db.write_bytes(b"x")
    with pytest.raises(Cancelled):
        resolve_credentials(kpxc_profile(db), answer(None))


def test_cli_not_found(monkeypatch, tmp_path):
    monkeypatch.delenv("PDFSIGNER_KEEPASSXC_CLI", raising=False)
    monkeypatch.setattr(secretstore.shutil, "which", lambda name: None)
    monkeypatch.setattr(secretstore, "KEEPASSXC_CLI_CANDIDATES", {})
    with pytest.raises(SecretError, match="keepassxc-cli not found"):
        secretstore.keepassxc_cli()


def test_profile_validation():
    with pytest.raises(ValueError, match="keepass_yubikey requires"):
        CertProfile(name="x", path="a.p12", keepass_yubikey="2").validate()
    with pytest.raises(ValueError, match="keepassxc needs keepass_db"):
        CertProfile(name="x", path="a.p12", secret="keepassxc").validate()
    CertProfile(name="x", secret="keepassxc", keepass_db="v.kdbx", keepass_entry="E",
                keepass_attachment="a.p12", keepass_yubikey="2").validate()
