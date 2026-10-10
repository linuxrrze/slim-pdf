import os

import pytest

from pdfsigner.config import (
    DEFAULT_STAMP_TEXT,
    CertProfile,
    Config,
    default_config_path,
    expand,
    load_config,
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
    text = (tmp_path / "c.toml").read_text()
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
    monkeypatch.setenv("PDFSIGNER_CONFIG", str(tmp_path / "x.toml"))
    assert default_config_path() == tmp_path / "x.toml"
    monkeypatch.delenv("PDFSIGNER_CONFIG")
    assert default_config_path().name == "config.toml"
    assert "pdfsigner" in str(default_config_path())


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
