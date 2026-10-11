import pytest

from slim_pdf import secretstore
from slim_pdf.config import CertProfile
from slim_pdf.secretstore import Cancelled, SecretError, resolve_credentials
from slim_pdf.signing import open_signer

from conftest import PASSPHRASE


def kp_profile(db, **kw):
    kw.setdefault("keepass_entry", "Certificates/FAU S/MIME")
    return CertProfile(name="kp", secret="keepass", keepass_db=str(db), **kw)


def never(prompt):
    pytest.fail(f"unexpected prompt: {prompt}")


# --- prompt / none ---------------------------------------------------------------


def test_none_never_prompts():
    creds = resolve_credentials(CertProfile(name="x", path="a", secret="none"), never)
    assert creds.secret is None and creds.pkcs12_data is None


def test_prompt_wording():
    prompts = []
    resolve_credentials(CertProfile(name="work", path="a"), lambda p: prompts.append(p) or "s")
    resolve_credentials(CertProfile(name="tok", type="pkcs11", pkcs11_lib="/x.so"),
                        lambda p: prompts.append(p) or "1234")
    assert prompts == ["passphrase for certificate 'work'", "PIN for certificate 'tok'"]


def test_prompt_cancel():
    with pytest.raises(Cancelled):
        resolve_credentials(CertProfile(name="x", path="a"), lambda p: None)


def test_empty_answer_is_not_cancel():
    assert resolve_credentials(CertProfile(name="x", path="a"), lambda p: "").secret == ""


# --- keyring ----------------------------------------------------------------------


def test_keyring_roundtrip(mem_keyring):
    p = CertProfile(name="work", path="a", secret="keyring")
    secretstore.keyring_set("work", "pw")
    assert mem_keyring.store == {("slim-pdf", "work"): "pw"}
    assert resolve_credentials(p, never).secret == "pw"
    secretstore.keyring_delete("work")
    secretstore.keyring_delete("work")  # deleting twice is fine
    with pytest.raises(SecretError, match="set-secret work"):
        resolve_credentials(p, never)


def test_keyring_backend_name(mem_keyring):
    assert "MemKR" in secretstore.keyring_backend_name()


# --- KeePass -------------------------------------------------------------------------


def test_keepass_attachment(keepass_db, p12):
    profile = kp_profile(keepass_db, keepass_attachment="alice.p12")
    prompts = []
    creds = resolve_credentials(profile, lambda p: prompts.append(p) or "master")
    assert prompts == ["KeePass master password for vault.kdbx"]
    assert creds.secret == PASSPHRASE
    assert creds.pkcs12_data == p12[0].read_bytes()
    with open_signer(profile, creds) as s:
        assert "Alice" in s.signing_cert.subject.human_friendly


def test_keepass_password_only(keepass_db, p12):
    profile = kp_profile(keepass_db, path=str(p12[0]))
    creds = resolve_credentials(profile, lambda p: "master")
    assert creds.secret == PASSPHRASE and creds.pkcs12_data is None
    with open_signer(profile, creds):
        pass


@pytest.mark.parametrize("entry, secret", [
    ("Certificates/FAU S/MIME", PASSPHRASE),  # title containing '/'
    ("FAU S/MIME", PASSPHRASE),               # bare title
    ("Plain", "plain-secret"),                # entry in the root group
    ("/Plain", "plain-secret"),
])
def test_keepass_entry_lookup(keepass_db, entry, secret):
    creds = resolve_credentials(kp_profile(keepass_db, keepass_entry=entry), lambda p: "master")
    assert creds.secret == secret


def test_keepass_errors(keepass_db, tmp_path):
    with pytest.raises(SecretError, match="wrong master password"):
        resolve_credentials(kp_profile(keepass_db), lambda p: "wrong")
    with pytest.raises(SecretError, match="not found in vault.kdbx"):
        resolve_credentials(kp_profile(keepass_db, keepass_entry="Nope/Missing"),
                            lambda p: "master")
    with pytest.raises(SecretError, match="attachment 'x.p12' not found"):
        resolve_credentials(kp_profile(keepass_db, keepass_attachment="x.p12"),
                            lambda p: "master")
    with pytest.raises(SecretError, match="database not found"):
        resolve_credentials(kp_profile(tmp_path / "missing.kdbx"), lambda p: "master")
    with pytest.raises(Cancelled):
        resolve_credentials(kp_profile(keepass_db), lambda p: None)


def test_keepass_keyfile(tmp_path):
    from pykeepass import create_database

    keyfile = tmp_path / "db.key"
    keyfile.write_bytes(b"0123456789abcdef" * 4)
    db = tmp_path / "k.kdbx"
    kp = create_database(str(db), password=None, keyfile=str(keyfile))
    kp.add_entry(kp.root_group, "Cert", "u", "pin")
    kp.save()

    profile = CertProfile(name="k", path="a", secret="keepass", keepass_db=str(db),
                          keepass_keyfile=str(keyfile), keepass_entry="Cert")
    # key file only: an empty master password means "no password"
    assert resolve_credentials(profile, lambda p: "").secret == "pin"


@pytest.fixture
def no_keyring(monkeypatch):
    """No usable backend, as on a headless Linux box."""
    import keyring
    from keyring.backends import fail

    old = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    yield
    keyring.set_keyring(old)


def test_no_keyring_backend_is_a_clean_error(no_keyring):

    profile = CertProfile(name="k", path="a.p12", secret="keyring")
    with pytest.raises(SecretError, match="no OS keyring available"):
        resolve_credentials(profile, lambda p: None)
    with pytest.raises(SecretError, match="no OS keyring available"):
        secretstore.keyring_set("k", "x")
    with pytest.raises(SecretError, match="no OS keyring available"):
        secretstore.keyring_delete("k")


def test_cli_no_keyring_backend(no_keyring, p12, cfg_path, capsys, monkeypatch):
    from slim_pdf import cli

    assert cli.main(["certs", "add", "k", "--p12", str(p12[0]), "--secret", "keyring"]) == 0
    assert cli.main(["certs", "test", "k"]) == 1
    assert "error: no OS keyring available" in capsys.readouterr().err
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "x")
    assert cli.main(["certs", "set-secret", "k"]) == 1
    assert cli.main(["certs", "remove", "k", "--purge-secret"]) == 1


def test_keyring_delete_missing_is_fine(mem_keyring):

    secretstore.keyring_delete("never-stored")


def test_keepass_bare_title_skips_recycle_bin_and_refuses_ambiguity(keepass_db):
    from pykeepass import PyKeePass

    kp = PyKeePass(str(keepass_db), password="master")
    old = kp.add_entry(kp.root_group, "Signing", "alice", "old-secret")
    kp.trash_entry(old)  # deleted entries go to the recycle bin
    kp.add_entry(kp.find_groups(name="Certificates", first=True), "Signing", "alice", "new-secret")
    kp.save()
    creds = resolve_credentials(kp_profile(keepass_db, keepass_entry="Signing"), lambda p: "master")
    assert creds.secret == "new-secret"

    kp = PyKeePass(str(keepass_db), password="master")
    kp.add_entry(kp.add_group(kp.root_group, "Other"), "Signing", "bob", "other-secret")
    kp.save()
    with pytest.raises(SecretError, match=r"2 KeePass entries match 'Signing' "
                                          r"\(.*Certificates/Signing.*Other/Signing.*\)"):
        resolve_credentials(kp_profile(keepass_db, keepass_entry="Signing"), lambda p: "master")
    creds = resolve_credentials(kp_profile(keepass_db, keepass_entry="Other/Signing"),
                                lambda p: "master")
    assert creds.secret == "other-secret"


def test_secrets_stored_under_the_old_name(mem_keyring):
    """A passphrase stored before the rename (service "pdfsigner") is used
    until a new one is stored; deleting removes both."""
    mem_keyring.store[("pdfsigner", "work")] = "old secret"
    assert secretstore.keyring_get("work") == "old secret"
    secretstore.keyring_set("work", "new secret")
    assert mem_keyring.store[("slim-pdf", "work")] == "new secret"
    assert secretstore.keyring_get("work") == "new secret"
    secretstore.keyring_delete("work")
    assert secretstore.keyring_get("work") is None
