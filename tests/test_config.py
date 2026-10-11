import os

import pytest

from slim_pdf.config import (
    DEFAULT_STAMP_TEXT,
    CertProfile,
    Config,
    default_config_path,
    expand,
    load_config,
    normalize_hex,
    supported_cert_types,
)


def test_roundtrip(tmp_path):
    path = tmp_path / "sub" / "config.toml"
    cfg = Config(path=path)
    cfg.put(CertProfile(name="work", path="~/a.p12", image="~/sig.png", reason="R",
                        stamp_text="line1\nline2"))
    cfg.put(CertProfile(name="tok", type="pkcs11", pkcs11_lib="/x.so", key_label="k"))
    cfg.save()
    if os.name != "nt":  # no POSIX permissions on Windows
        assert oct(path.stat().st_mode & 0o777) == "0o600"

    again = load_config(path)
    assert again.default == "work"
    assert again.certs == cfg.certs
    assert again.certs["work"].stamp_text == "line1\nline2"


def test_only_non_defaults_are_written(tmp_path):
    cfg = Config(path=tmp_path / "c.toml")
    cfg.put(CertProfile(name="w", path="a.p12"))
    cfg.save()
    text = (tmp_path / "c.toml").read_text(encoding="utf-8")
    assert 'type = "pkcs12"' in text  # always written
    assert "stamp_text" not in text and "secret" not in text


def test_missing_file_gives_empty_config(tmp_path):
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg.certs == {} and cfg.default == ""


def test_unknown_key_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[certs.w]\ntype = "pkcs12"\npath = "a"\npasswrod = "x"\n')
    with pytest.raises(ValueError, match="unknown keys.*passwrod"):
        load_config(p)


@pytest.mark.parametrize("kwargs, message", [
    (dict(name=""), "needs a name"),
    (dict(name="x", type="gpg"), "unknown type"),
    (dict(name="x", path="a", secret="vault"), "unknown secret source"),
    (dict(name="x"), "needs a path"),
    (dict(name="x", keepass_attachment="a.p12"), "requires secret = 'keepass'"),
    (dict(name="x", type="pkcs11"), "needs pkcs11_lib"),
    (dict(name="x", type="pkcs11", pkcs11_lib="/x.so", key_id="9c-slot"), "key_id must be hex"),
    (dict(name="x", path="a", secret="keepass"), "needs keepass_db and keepass_entry"),
])
def test_validation(kwargs, message):
    with pytest.raises(ValueError, match=message):
        CertProfile(**kwargs).validate()


def test_valid_variants():
    CertProfile(name="a", path="x.p12").validate()
    CertProfile(name="b", secret="keepass", keepass_db="d.kdbx", keepass_entry="E",
                keepass_attachment="x.p12").validate()
    CertProfile(name="c", type="pkcs11", pkcs11_lib="/x.so", secret="keyring").validate()
    CertProfile(name="d", type="pkcs11", pkcs11_lib="/x.so", key_id="0x02").validate()


def test_get_and_default():
    cfg = Config()
    with pytest.raises(KeyError, match="no certificate given"):
        cfg.get(None)
    cfg.put(CertProfile(name="a", path="a"))
    assert cfg.get(None).name == "a"  # first one becomes default
    cfg.put(CertProfile(name="b", path="b"))
    assert cfg.default == "a"
    assert cfg.get("b").name == "b"
    with pytest.raises(KeyError, match="unknown certificate 'c'"):
        cfg.get("c")


def test_single_profile_without_default_is_used():
    cfg = Config(certs={"only": CertProfile(name="only", path="x")})
    assert cfg.get(None).name == "only"


def test_remove_moves_default():
    cfg = Config()
    cfg.put(CertProfile(name="a", path="a"))
    cfg.put(CertProfile(name="b", path="b"))
    cfg.remove("a")
    assert cfg.default == "b"
    cfg.remove("b")
    assert cfg.default == "" and cfg.certs == {}


def test_put_validates():
    with pytest.raises(ValueError):
        Config().put(CertProfile(name="x"))


def test_config_path_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SLIM_PDF_CONFIG", str(tmp_path / "x.toml"))
    assert default_config_path() == tmp_path / "x.toml"
    monkeypatch.delenv("SLIM_PDF_CONFIG")
    assert default_config_path().name == "config.toml"
    assert "slim-pdf" in str(default_config_path())


def test_expand(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    monkeypatch.setenv("CERTS", str(tmp_path / "certs"))
    assert expand("~/a.p12") == tmp_path / "a.p12"
    assert expand("$CERTS/a.p12") == tmp_path / "certs" / "a.p12"
    assert CertProfile(name="x", image="~/s.png").image_path == tmp_path / "s.png"
    assert CertProfile(name="x").image_path is None


def test_default_stamp_text():
    assert "%(signer)s" in DEFAULT_STAMP_TEXT and "%(ts)s" in DEFAULT_STAMP_TEXT


@pytest.mark.parametrize("value, expected", [
    ("02", "02"), ("0x02", "02"), (" 0A ", "0a"), ("de:ad be-ef", "deadbeef")])
def test_normalize_hex(value, expected):
    assert normalize_hex(value) == expected


@pytest.mark.parametrize("toml, message", [
    ('[certs.x]\npath = "a.p12"\nsecret = "foo"\n', "unknown secret source"),
    ('[certs.x]\npath = "a.p12"\nltv = "yes"\n', "ltv must be true or false"),
    ('[certs.x]\npath = "a.p12"\nreason = 5\n', "reason must be a string"),
    ('[certs.x]\npath = true\n', "path must be a string"),
    ('default = "y"\n[certs.x]\npath = "a.p12"\n', "default 'y' is not a certificate profile"),
    ('trust = "root.pem"\n', "trust must be a list"),
    ('[certs.x\n', "Expected"),  # broken TOML
])
def test_hand_edited_config_is_validated(tmp_path, toml, message):
    p = tmp_path / "c.toml"
    p.write_text(toml, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_config(p)


def test_supported_cert_types():
    assert supported_cert_types("linux") == ("pkcs12", "pkcs11")
    assert supported_cert_types("win32") == supported_cert_types("darwin") == (
        "pkcs12", "pkcs11", "system")


def test_stamp_settings_are_checked():
    from slim_pdf.config import CertProfile

    CertProfile(name="p", path="x.p12", stamp_text="%(cn)s\n%(date)s",
                text_position="right").validate()
    with pytest.raises(ValueError, match="text_position must be one of"):
        CertProfile(name="p", path="x.p12", text_position="middle").validate()
    with pytest.raises(ValueError, match=r"p: unknown placeholder %\(nam\)s"):
        CertProfile(name="p", path="x.p12", stamp_text="%(nam)s").validate()


def test_certificate_is_saved_and_loaded(tmp_path):
    from slim_pdf.config import CertProfile, Config, load_config

    pem_text = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    cfg = Config(path=tmp_path / "c.toml")
    cfg.put(CertProfile(name="p", path="x.p12", certificate=pem_text, text_position="left"))
    cfg.save()
    again = load_config(tmp_path / "c.toml").certs["p"]
    assert (again.certificate, again.text_position) == (pem_text, "left")


# --- the rename from pdfsigner ------------------------------------------------------


@pytest.fixture
def config_dirs(tmp_path, monkeypatch):
    """The platform's config folder is tmp_path; no config from the environment."""
    from slim_pdf import config

    for name in ("SLIM_PDF_CONFIG", "PDFSIGNER_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "user_config_dir",
                        lambda name, appauthor=False: str(tmp_path / name))
    return tmp_path


def test_old_config_is_taken_over(config_dirs):
    old = config_dirs / "pdfsigner" / "config.toml"
    Config(certs={"work": CertProfile(name="work", path="w.p12")}, default="work",
           path=old).save()
    cfg = load_config()
    assert cfg.path == config_dirs / "slim-pdf" / "config.toml" and cfg.path.is_file()
    assert list(cfg.certs) == ["work"]
    assert old.is_file()  # copied, not moved
    cfg.default = ""
    cfg.save()
    assert load_config().default == ""  # the new file is used from now on


def test_no_old_config(config_dirs):
    cfg = load_config()
    assert cfg.certs == {} and not cfg.path.exists()


def test_old_environment_variables(config_dirs, monkeypatch, tmp_path):
    from slim_pdf import environ

    monkeypatch.setenv("PDFSIGNER_CONFIG", str(tmp_path / "old-env.toml"))
    assert default_config_path() == tmp_path / "old-env.toml"
    monkeypatch.setenv("SLIM_PDF_CONFIG", str(tmp_path / "new-env.toml"))
    assert default_config_path() == tmp_path / "new-env.toml"  # the new name wins
    monkeypatch.setenv("SLIM_PDF_LANG", "")
    monkeypatch.setenv("PDFSIGNER_LANG", "de")
    assert environ("LANG") == "de"
