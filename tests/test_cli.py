import os

import pytest
from pyhanko.sign import fields

from pdfsigner import cli
from pdfsigner.config import load_config
from pdfsigner.signing import inspect_pdf

from conftest import PASSPHRASE, make_encrypted, validate_all


def run(*argv):
    return cli.main([str(a) for a in argv])


@pytest.fixture
def prompts(monkeypatch):
    """Answers prompts with the passphrase and records them."""
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return PASSPHRASE

    monkeypatch.setattr(cli, "_ask", ask)
    return asked


@pytest.fixture
def work(cfg_path, p12, sig_image):
    assert run("certs", "add", "work", "--p12", p12[0], "--image", sig_image,
               "--reason", "Approval") == 0
    return load_config().certs["work"]


# --- certs ------------------------------------------------------------------------


def test_certs_lifecycle(cfg_path, p12, sig_image, capsys):
    assert run("certs", "list") == 0
    assert "no certificates configured" in capsys.readouterr().out

    assert run("certs", "add", "work", "--p12", p12[0], "--image", sig_image,
               "--stamp-text", "Signed\\n%(ts)s", "--reason", "Approval") == 0
    assert run("certs", "add", "token", "--pkcs11-lib", "/usr/lib/opensc-pkcs11.so",
               "--token-label", "PIV", "--key-label", "SIGN key") == 0
    cfg = load_config()
    assert cfg.default == "work"
    assert cfg.certs["work"].stamp_text == "Signed\n%(ts)s"
    assert cfg.certs["token"].type == "pkcs11"

    capsys.readouterr()
    assert run("certs", "list") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("* work") and "pkcs12" in out[0]
    assert out[1].startswith("  token") and "pkcs11" in out[1]

    assert run("certs", "default", "token") == 0
    assert run("certs", "edit", "work", "--secret", "keyring", "--default") == 0
    cfg = load_config()
    assert cfg.default == "work" and cfg.certs["work"].secret == "keyring"
    assert cfg.certs["work"].reason == "Approval"  # untouched by edit

    assert run("certs", "remove", "token") == 0
    assert list(load_config().certs) == ["work"]
    if os.name != "nt":  # no POSIX permissions on Windows
        assert oct(cfg_path.stat().st_mode & 0o777) == "0o600"


def test_certs_show(work, capsys):
    assert run("certs", "show", "work") == 0
    out = capsys.readouterr().out
    assert "reason               'Approval'" in out
    assert "keepass_db" not in out  # empty values hidden


@pytest.mark.parametrize("argv, message", [
    (["certs", "add", "broken"], "needs a path"),
    (["certs", "add", "x", "--p12", "a.p12", "--secret", "keepass"], "keepass_db"),
    (["certs", "add", "x", "--p12", "a.p12", "--type", "gpg"], "unknown type"),
    (["certs", "edit", "missing", "--reason", "x"], "unknown certificate 'missing'"),
    (["certs", "default", "missing"], "unknown certificate"),
    (["certs", "remove", "missing"], "unknown certificate"),
])
def test_certs_errors(cfg_path, capsys, argv, message):
    assert run(*argv) == 1
    assert message in capsys.readouterr().err


def test_add_existing_refused(work, capsys):
    assert run("certs", "add", "work", "--p12", "x.p12") == 1
    assert "exists" in capsys.readouterr().err


def test_set_secret_and_purge(work, mem_keyring, prompts, capsys):
    assert run("certs", "set-secret", "work") == 0
    assert mem_keyring.store[("pdfsigner", "work")] == PASSPHRASE
    assert "switch with" in capsys.readouterr().out  # profile still uses prompt
    assert run("certs", "remove", "work", "--purge-secret") == 0
    assert mem_keyring.store == {}


def test_certs_test(work, prompts, capsys):
    assert run("certs", "test", "work") == 0
    assert "Alice Example" in capsys.readouterr().out
    assert prompts == ["passphrase for certificate 'work'"]


def test_cancelled_prompt(work, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_ask", lambda prompt: None)
    assert run("certs", "test") == 130
    assert "cancelled" in capsys.readouterr().err


# --- fields ---------------------------------------------------------------------------


def test_fields_output(form_pdf, cfg_path, capsys):
    assert run("fields", form_pdf) == 0
    assert capsys.readouterr().out.splitlines() == [
        "2 pages",
        "  Applicant                empty   page 2   50,100,250,170",
        "  Supervisor               empty   page 2   320,100,520,170",
    ]


def test_fields_no_fields(blank_pdf, cfg_path, capsys):
    assert run("fields", blank_pdf) == 0
    assert "no signature fields" in capsys.readouterr().out


def test_fields_encrypted(form_pdf, tmp_path, cfg_path, capsys):
    enc = make_encrypted(form_pdf, tmp_path / "enc.pdf", "pw")
    assert run("fields", enc) == 1
    assert "password is required" in capsys.readouterr().err
    assert run("fields", enc, "--password", "pw") == 0


# --- sign ------------------------------------------------------------------------------


def test_sign_field_default_output(work, form_pdf, prompts, p12, capsys):
    assert run("sign", form_pdf, "--field", "Applicant") == 0
    out = form_pdf.with_name("form-signed.pdf")
    assert f"signed field 'Applicant' -> {out}" in capsys.readouterr().out
    assert inspect_pdf(out).field("Applicant").filled
    assert not inspect_pdf(form_pdf).field("Applicant").filled  # input untouched
    [(_, st)] = validate_all(out, p12[1])
    assert st.bottom_line


def test_sign_at_keeps_aspect(work, blank_pdf, tmp_path, prompts):
    out = tmp_path / "out.pdf"
    assert run("sign", blank_pdf, "-o", out, "--page", "2", "--at", "100,100",
               "--width", "150") == 0
    f = inspect_pdf(out).field("Signature1")
    assert f.page == 1
    assert f.rect == pytest.approx((100, 100, 250, 150), abs=0.01)


def test_sign_box_and_named_field(work, blank_pdf, tmp_path, prompts):
    out = tmp_path / "out.pdf"
    assert run("sign", blank_pdf, "-o", out, "-p", "1", "--box", "10,20,110,70",
               "-f", "Approval") == 0
    f = inspect_pdf(out).field("Approval")
    assert f.page == 0 and f.rect == pytest.approx((10, 20, 110, 70), abs=0.01)


def test_sign_in_place_twice_with_two_profiles(work, cfg_path, form_pdf, p12, p12_bob,
                                               prompts):
    assert run("certs", "add", "bob", "--p12", p12_bob[0], "--stamp-text",
               "Approved by %(signer)s") == 0
    assert run("sign", form_pdf, "-i", "--field", "Applicant") == 0
    assert run("sign", form_pdf, "-i", "-c", "bob", "--field", "Supervisor") == 0
    assert run("sign", form_pdf, "-i") == 0  # invisible third signature
    assert prompts == ["passphrase for certificate 'work'",
                       "passphrase for certificate 'bob'",
                       "passphrase for certificate 'work'"]
    results = validate_all(form_pdf, p12[1], p12_bob[1])
    assert [n for n, _ in results] == ["Applicant", "Supervisor", "Signature1"]
    assert all(st.bottom_line for _, st in results)


def test_sign_with_keyring_never_prompts(work, cfg_path, form_pdf, mem_keyring,
                                         monkeypatch, tmp_path):
    run("certs", "edit", "work", "--secret", "keyring")
    mem_keyring.store[("pdfsigner", "work")] = PASSPHRASE
    monkeypatch.setattr(cli, "_ask", lambda p: pytest.fail("should not prompt"))
    assert run("sign", form_pdf, "-o", tmp_path / "o.pdf", "--field", "Applicant") == 0


def test_sign_no_image_no_text(work, blank_pdf, tmp_path, prompts):
    assert run("sign", blank_pdf, "-o", tmp_path / "a.pdf", "-p", "1", "--box",
               "10,10,160,60", "--no-image") == 0
    assert run("sign", blank_pdf, "-o", tmp_path / "b.pdf", "-p", "1", "--box",
               "10,10,160,60", "--no-text") == 0


@pytest.mark.parametrize("extra, message", [
    (["--field", "Applicant"], "already signed"),
    (["--at", "1,1", "--width", "10"], "--box/--at need --page"),
    (["--page", "9", "--at", "1,1", "--width", "10"], "page out of range (document has 2)"),
    (["--page", "1", "--at", "1,1"], "--at needs --width"),
    (["--page", "1", "--box", "50,50,10,10"], "x1 < x2"),
    (["--field", "Applicant", "--page", "1", "--box", "1,1,9,9"], "already exists"),
    (["--page", "1"], "--page needs --box or --at"),
    (["--page", "1", "--box", "1,1,90,90", "--width", "50"], "--width only applies to --at"),
])
def test_sign_errors_before_prompt(work, form_pdf, tmp_path, monkeypatch, capsys,
                                   extra, message):
    signed = tmp_path / "signed.pdf"
    monkeypatch.setattr(cli, "_ask", lambda p: PASSPHRASE)
    assert run("sign", form_pdf, "-o", signed, "--field", "Applicant") == 0
    capsys.readouterr()

    monkeypatch.setattr(cli, "_ask", lambda p: pytest.fail(f"prompted: {p}"))
    assert run("sign", signed, "-o", tmp_path / "x.pdf", *extra) == 1
    assert message in capsys.readouterr().err


def test_sign_at_without_image(cfg_path, p12, blank_pdf, capsys):
    run("certs", "add", "plain", "--p12", p12[0])
    assert run("sign", blank_pdf, "--page", "1", "--at", "1,1", "--width", "50") == 1
    assert "has no image" in capsys.readouterr().err


def test_sign_missing_file_before_prompt(work, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_ask", lambda p: pytest.fail("prompted"))
    assert run("sign", "nonexistent.pdf") == 1
    assert "no such file" in capsys.readouterr().err


def test_sign_wrong_passphrase(work, form_pdf, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_ask", lambda p: "wrong")
    assert run("sign", form_pdf, "--field", "Applicant") == 1
    assert "wrong passphrase" in capsys.readouterr().err
    assert not form_pdf.with_name("form-signed.pdf").exists()


def test_sign_locked_field_exit_code_and_force(work, blank_pdf, tmp_path, prompts, capsys):
    from conftest import add_sig_field

    lock = fields.FieldMDPSpec(fields.FieldMDPAction.INCLUDE, fields=["Second"])
    src = add_sig_field(blank_pdf, tmp_path / "a.pdf", "First", 0, (10, 10, 100, 50),
                        field_mdp_spec=lock)
    src = add_sig_field(src, tmp_path / "b.pdf", "Second", 0, (110, 10, 200, 50))
    assert run("sign", src, "-i", "--field", "First") == 0
    capsys.readouterr()
    n = len(prompts)
    assert run("sign", src, "-i", "--field", "Second") == 3
    assert "use --force" in capsys.readouterr().err
    assert len(prompts) == n  # refused before prompting
    assert run("sign", src, "-i", "--field", "Second", "--force") == 0


def test_sign_encrypted_pdf(work, form_pdf, tmp_path, monkeypatch, p12):
    enc = make_encrypted(form_pdf, tmp_path / "enc.pdf", "docpw")
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return "docpw" if prompt == "PDF password" else PASSPHRASE

    monkeypatch.setattr(cli, "_ask", ask)
    assert run("sign", enc, "-i", "--field", "Applicant") == 0
    assert asked == ["PDF password", "passphrase for certificate 'work'"]
    assert inspect_pdf(enc, "docpw").field("Applicant").filled


def test_explicit_config_option(tmp_path, p12, monkeypatch, capsys):
    monkeypatch.delenv("PDFSIGNER_CONFIG", raising=False)
    cfg = tmp_path / "custom.toml"
    assert run("--config", cfg, "certs", "add", "w", "--p12", p12[0]) == 0
    assert cfg.exists()
    assert run("--config", cfg, "certs", "list") == 0
    assert "w " in capsys.readouterr().out


def test_help_lists_commands(capsys):
    with pytest.raises(SystemExit) as e:
        run("--help")
    assert e.value.code == 0
    out = capsys.readouterr().out
    for cmd in ("certs", "fields", "sign", "gui"):
        assert cmd in out


@pytest.mark.parametrize("content", [b"not a pdf", b"%PDF-1.7\ngarbage\n", "truncated"])
@pytest.mark.parametrize("cmd", [["fields"], ["verify"], ["rotate", "-i", "--angle", "90"],
                                 ["split", "--every", "1"], ["merge", "-o", "m.pdf"]])
def test_malformed_pdf_is_an_error_not_a_traceback(cfg_path, blank_pdf, tmp_path, capsys,
                                                    content, cmd):
    bad = tmp_path / "bad.pdf"
    if content == "truncated":
        data = blank_pdf.read_bytes()
        content = data[: len(data) // 2]
    bad.write_bytes(content)
    argv = [*cmd, str(bad)]
    if cmd[0] == "split":
        argv += ["-o", str(tmp_path / "p{n}.pdf")]
    assert run(*argv) == 1
    assert capsys.readouterr().err.startswith("error: cannot read the PDF: ")


def test_sign_box_and_at_exclusive(work, blank_pdf, capsys):
    with pytest.raises(SystemExit):
        run("sign", blank_pdf, "--page", "1", "--box", "1,1,90,90", "--at", "1,1")
    assert "not allowed with argument" in capsys.readouterr().err
