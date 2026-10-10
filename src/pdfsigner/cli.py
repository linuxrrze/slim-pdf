"""Command line interface; ``DESCRIPTION`` below is the usage summary shown by --help."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from dataclasses import fields as dc_fields
from pathlib import Path

from pyhanko.pdf_utils.misc import PdfError
from pypdf.errors import PyPdfError

from . import i18n, pages, secretstore
from .config import (
    CERT_TYPES,
    PKCS11_EXAMPLE,
    SECRET_SOURCES,
    CertProfile,
    Config,
    load_config,
    new_profile,
)
from .i18n import N_, _, ngettext
from .secretstore import Cancelled, SecretError, resolve_credentials
from .signing import (
    SigningBlocked,
    SigningError,
    _atomic_write,
    check_target,
    box_from_anchor,
    describe_signer,
    image_aspect,
    inspect_pdf,
    open_signer,
    printable,
    sign_pdf,
)

DESCRIPTION = N_("""Command line interface.

    pdfsigner certs list|show|add|edit|remove|default|set-secret|test|system|pkcs11
    pdfsigner fields FILE
    pdfsigner sign FILE [--field NAME | --page N (--box ... | --at X,Y --width W)]
    pdfsigner verify FILE
    pdfsigner rotate|crop FILE [--pages SPEC] ...
    pdfsigner split FILE [--every N | --part SPEC ...]
    pdfsigner merge -o OUT FILE[@SPEC] ...
    pdfsigner gui [FILE]

Pages are 1-based on the command line; coordinates are PDF points with the
origin at the bottom-left of the (unrotated) page.
""")

# argparse's own texts (usage, section headings, its error messages) go through
# its module-level gettext names, which build_parser() points at our catalog.
# They are listed here, spelled exactly as in argparse, so that scripts/i18n.py
# extracts them into our catalog; this tuple is not used at run time.
ARGPARSE_MESSAGES = (
    N_("usage: "),
    N_("%(heading)s:"),
    N_("positional arguments"),
    N_("options"),
    N_("subcommands"),
    N_("show this help message and exit"),
    N_("%(prog)s: error: %(message)s\n"),
    N_("argument %(argument_name)s: %(message)s"),
    N_("the following arguments are required: %s"),
    N_("one of the arguments %s is required"),
    N_("not allowed with argument %s"),
    N_("unrecognized arguments: %s"),
    N_("ambiguous option: %(option)s could match %(matches)s"),
    N_("ignored explicit argument %r"),
    N_("expected one argument"),
    N_("expected at most one argument"),
    N_("expected at least one argument"),
    ngettext("expected %s argument", "expected %s arguments", 2),
    N_("invalid %(type)s value: %(value)r"),
    N_("invalid choice: %(value)r (choose from %(choices)s)"),
    N_("invalid choice: %(value)r, maybe you meant %(closest)r? (choose from %(choices)s)"),
    N_("unknown parser %(parser_name)r (choices: %(choices)s)"),
    N_("can't open '%(filename)s': %(error)s"),
)


def _ask(prompt: str) -> str | None:
    try:
        return getpass.getpass(_("{prompt}: ").format(prompt=prompt))
    except (EOFError, KeyboardInterrupt):
        return None


def _floats(s: str, n: int) -> tuple[float, ...]:
    parts = s.replace(";", ",").split(",")
    if len(parts) != n:
        raise argparse.ArgumentTypeError(ngettext(
            "expected {n} comma-separated number", "expected {n} comma-separated numbers", n
        ).format(n=n))
    return tuple(float(p) for p in parts)


# --- certs -------------------------------------------------------------------

PROFILE_OPTS = {
    # option -> (CertProfile attribute, help; translated and formatted in _add_profile_opts)
    "--type": ("type", N_("one of {types}")),
    "--p12": ("path", N_("path to the .p12/.pfx file")),
    "--pkcs11-lib": ("pkcs11_lib", N_("PKCS#11 module, e.g. {example}")),
    "--token-label": ("token_label", N_("PKCS#11 token label")),
    "--key-label": ("key_label", N_("PKCS#11 key label")),
    "--key-id": ("key_id", N_("PKCS#11 key ID in hex, e.g. 02 for YubiKey PIV slot 9c")),
    "--cert-label": ("cert_label", N_("PKCS#11 certificate label (default: the certificate "
                                      "with the key's ID)")),
    "--system-cert": ("system_cert", N_("Windows/macOS certificate store: thumbprint or part "
                                        "of the subject (see 'certs system')")),
    "--image": ("image", N_("signature image (PNG with transparency recommended)")),
    "--stamp-text": ("stamp_text", N_("text on the stamp; %%(signer)s and %%(ts)s are "
                                      "replaced; '' for none")),
    "--reason": ("reason", N_("signature reason")),
    "--location": ("location", N_("signature location")),
    "--contact": ("contact_info", N_("contact info")),
    "--timestamp-url": ("timestamp_url", N_("RFC 3161 timestamp server URL")),
    "--ltv": ("ltv", N_("embed chain and revocation info for long-term validation: yes/no")),
    "--secret": ("secret", N_("where the passphrase/PIN comes from: {sources}")),
    "--keepass-db": ("keepass_db", N_("KeePass .kdbx file")),
    "--keepass-keyfile": ("keepass_keyfile", N_("KeePass key file")),
    "--keepass-entry": ("keepass_entry", N_("entry as Group/Title or Title")),
    "--keepass-attachment": ("keepass_attachment", N_("attachment name holding the .p12")),
    "--keepass-yubikey": ("keepass_yubikey",
                          N_("YubiKey slot[:serial] for the database (keepassxc only)")),
}


def _add_profile_opts(p: argparse.ArgumentParser) -> None:
    for opt, (attr, text) in PROFILE_OPTS.items():
        help_ = _(text).format(types=", ".join(CERT_TYPES), example=PKCS11_EXAMPLE,
                               sources=", ".join(SECRET_SOURCES))
        if attr == "ltv":
            p.add_argument(opt, dest=attr, default=None, choices=("yes", "no"), help=help_)
        else:
            p.add_argument(opt, dest=attr, default=None, help=help_)
    p.add_argument("--default", action="store_true",
                   help=_("make this the default certificate"))


def _apply_profile_opts(profile: CertProfile, args) -> CertProfile:
    for attr, _help in PROFILE_OPTS.values():
        value = getattr(args, attr)
        if value is None:
            continue
        if attr == "stamp_text":
            value = value.replace("\\n", "\n")
        elif attr == "ltv":
            value = value == "yes"
        setattr(profile, attr, value)
    if args.type is None:
        if args.pkcs11_lib:
            profile.type = "pkcs11"
        elif args.system_cert:
            profile.type = "system"
    return profile


def cmd_certs_list(cfg: Config, args) -> int:
    if not cfg.certs:
        print(_("no certificates configured ({path})").format(path=cfg.path))
        return 0
    for name, p in cfg.certs.items():
        mark = "*" if name == cfg.default else " "
        src = (p.path or (f"keepass:{p.keepass_attachment}" if p.keepass_attachment else "")
               or p.pkcs11_lib or (f"store:{p.system_cert}" if p.system_cert else ""))
        print(f"{mark} {name:20} {p.type:7} secret={p.secret:9} {src}")
    return 0


def cmd_certs_show(cfg: Config, args) -> int:
    p = cfg.get(args.name)
    for f in dc_fields(p):
        v = getattr(p, f.name)
        if v or f.name in ("ltv", "stamp_text"):  # false / '' are meaningful here
            print(f"{f.name:20} {v!r}")
    return 0


def cmd_certs_add(cfg: Config, args) -> int:
    if args.name in cfg.certs:
        print(_("certificate {name} exists, use 'certs edit'").format(name=repr(args.name)),
              file=sys.stderr)
        return 1
    profile = _apply_profile_opts(new_profile(args.name), args)
    cfg.put(profile)
    if args.default:
        cfg.default = profile.name
    print(_("saved to {path}").format(path=cfg.save()))
    return 0


def cmd_certs_edit(cfg: Config, args) -> int:
    profile = _apply_profile_opts(cfg.get(args.name), args)
    cfg.put(profile)
    if args.default:
        cfg.default = profile.name
    print(_("saved to {path}").format(path=cfg.save()))
    return 0


def cmd_certs_remove(cfg: Config, args) -> int:
    cfg.get(args.name)
    cfg.remove(args.name)
    if args.purge_secret:
        secretstore.keyring_delete(args.name)
    cfg.save()
    return 0


def cmd_certs_default(cfg: Config, args) -> int:
    cfg.get(args.name)
    cfg.default = args.name
    cfg.save()
    return 0


def cmd_certs_set_secret(cfg: Config, args) -> int:
    p = cfg.get(args.name)
    if p.type == "pkcs11":
        secret = _ask(_("PIN for '{name}'").format(name=p.name))
    else:
        secret = _ask(_("Passphrase for '{name}'").format(name=p.name))
    if secret is None:
        return 1
    secretstore.keyring_set(p.name, secret)
    print(_("stored in keyring ({backend})").format(backend=secretstore.keyring_backend_name()))
    if p.secret != "keyring":
        print(_("note: profile uses secret={secret}; switch with "
                "'pdfsigner certs edit {name} --secret keyring'").format(
                    secret=repr(p.secret), name=p.name))
    return 0


def cmd_certs_test(cfg: Config, args) -> int:
    p = cfg.get(args.name)
    with open_signer(p, resolve_credentials(p, _ask)) as signer:
        print(describe_signer(signer))
    return 0


def cmd_certs_system(cfg: Config, args) -> int:
    from . import systemstore

    backend = systemstore.get_backend()
    try:
        identities = backend.identities()
        if not identities:
            print(_("no certificates with a private key in the {store}").format(
                store=backend.name))
        for ident in identities:
            print(ident.describe())
    finally:
        backend.close()
    return 0


def cmd_certs_pkcs11(cfg: Config, args) -> int:
    from . import tokens

    ask = ((lambda label: _ask(_("PIN for token '{label}'").format(label=label)))
           if args.login else None)
    found = tokens.list_tokens(args.module, args.token_label, ask)
    if not found:
        print(_("no token found ({module})").format(module=args.module))
        return 0
    for t in found:
        print(t.describe())
        for e in t.entries:
            print(f"  {e.describe()}")
        if not t.entries:
            print("  " + _("(no objects)"))
        if not t.logged_in:
            print("  " + _("(private keys are usually only listed after logging in: --login)"))
    token = f" --token-label {_quote(found[0].label)}" if len(found) > 1 else ""
    print()
    print(_("profile: pdfsigner certs add NAME --pkcs11-lib {module} --key-id ID{token}").format(
        module=_quote(args.module), token=token))
    return 0


def _quote(s: str) -> str:
    return f'"{s}"' if any(c in s for c in " \\'") else s


# --- fields / sign -----------------------------------------------------------


def cmd_fields(cfg: Config, args) -> int:
    path = Path(args.file)
    info = inspect_pdf(path, _pdf_password(path, args.password, _("PDF password")))
    n = info.page_count
    if info.certification:
        print(ngettext("{n} page, certified ({name})", "{n} pages, certified ({name})", n).format(
            n=n, name=info.certification.name))
    else:
        print(ngettext("{n} page", "{n} pages", n).format(n=n))
    if not info.fields:
        print(_("no signature fields"))
    for f in info.fields:
        state = _("signed") if f.filled else (_("LOCKED") if f.locked else _("empty"))
        where = (_("page {page}").format(page=f.page + 1) if f.page is not None
                 else _("page ?"))
        rect = ",".join(f"{v:.0f}" for v in f.rect) if f.rect else _("invisible")
        print(f"  {printable(f.name):24} {state:7} {where:8} {rect}")
    return 0


def cmd_sign(cfg: Config, args) -> int:
    profile = cfg.get(args.cert)
    src = Path(args.file)
    dst = src if args.in_place else Path(args.output or src.with_name(f"{src.stem}-signed.pdf"))

    page = box = None
    if args.page is not None and not (args.box or args.at):
        raise SigningError(
            _("--page needs --box or --at (an existing field is chosen with --field)"))
    if args.width is not None and not args.at:
        raise SigningError(_("--width only applies to --at"))
    if args.box or args.at:
        if args.page is None:
            raise SigningError(_("--box/--at need --page"))
        page = args.page - 1
        if args.box:
            box = args.box
        else:
            if args.width is None:
                raise SigningError(_("--at needs --width"))
            if not profile.image_path:
                raise SigningError(
                    _("--at keeps the image aspect ratio but the profile has no image"))
            box = box_from_anchor(*args.at, args.width, image_aspect(profile.image_path))

    # Check the document before asking for any secret.
    if not src.is_file():
        raise SigningError(_("no such file: {path}").format(path=src))
    pdf_password = _pdf_password(src, None, _("PDF password"))
    check_target(inspect_pdf(src, pdf_password), args.field, page, box,
                 allow_breaking=args.force)

    creds = resolve_credentials(profile, _ask)
    with open_signer(profile, creds) as signer:
        name = sign_pdf(
            src, dst, signer, profile,
            field_name=args.field, page=page, box=box,
            with_image=not args.no_image, with_text=not args.no_text,
            pdf_password=pdf_password, allow_breaking=args.force, trust=cfg.trust,
        )
    print(_("signed field {name} -> {path}").format(name=repr(name), path=dst))
    return 0


PDF_PASSWORD_ENV = "PDFSIGNER_PDF_PASSWORD"


PASSWORD_HELP = N_("password of an encrypted PDF; '-' reads it from stdin. Without it, "
                   "$PDFSIGNER_PDF_PASSWORD or a prompt is used: a password given here is "
                   "visible to other users (process list) and in the shell history")


def _pdf_password(path: Path, given: str | None, prompt: str) -> str | None:
    """The password for an encrypted PDF: --password (``-`` reads a line from
    stdin), else $PDFSIGNER_PDF_PASSWORD, else a prompt. Prompt and stdin keep
    it out of the process list and shell history."""
    if given == "-":
        return sys.stdin.readline().rstrip("\r\n")
    if given is not None or not path.is_file() or not inspect_needs_password(path):
        return given
    env = os.environ.get(PDF_PASSWORD_ENV)
    return env if env is not None else _ask(prompt)


def inspect_needs_password(path: Path) -> bool:
    from pyhanko.pdf_utils.reader import PdfFileReader

    with open(path, "rb") as f:
        return PdfFileReader(f, strict=False).encrypted


EXIT_UNTRUSTED, EXIT_INVALID = 4, 5


def cmd_verify(cfg: Config, args) -> int:
    from .validation import INVALID, UNTRUSTED, VALID, verify_pdf

    src = Path(args.file)
    if not src.is_file():
        raise SigningError(_("no such file: {path}").format(path=src))
    pdf_password = _pdf_password(src, args.password, _("PDF password"))
    reports = verify_pdf(src, trust=[*cfg.trust, *args.trust],
                         system_trust=not args.no_system_trust, online=args.online,
                         password=pdf_password)
    if not reports:
        raise SigningError(_("document has no signatures"))
    status_text = {VALID: _("VALID"), UNTRUSTED: _("UNTRUSTED"), INVALID: _("INVALID")}
    labels = {"signer": _("signer:"), "issuer": _("issuer:"), "time": _("time:"),
              "covers": _("covers:"), "ltv": _("ltv:"), "reason": _("reason:"),
              "location": _("location:"), "problem": _("problem:")}
    width = max(len(label) for label in labels.values())

    def show(key: str, text: str) -> None:
        print(f"  {labels[key]:{width}} {text}")

    for r in reports:
        status = status_text.get(r.status, r.status.upper())
        print(_("{field}: {status}").format(field=printable(r.field), status=status))
        if r.signer:
            show("signer", printable(r.signer))
            show("issuer", printable(r.issuer))
        if r.timestamp:
            show("time", _("{time} (timestamp)").format(
                time=f"{r.timestamp:%Y-%m-%d %H:%M:%S %z}"))
        elif r.signed_at:
            show("time", _("{time} (signer's clock)").format(
                time=f"{r.signed_at:%Y-%m-%d %H:%M:%S %z}"))
        if r.coverage:
            show("covers", r.coverage)
        if r.ltv:
            show("ltv", _("validation data embedded"))
        for key, value in (("reason", r.reason), ("location", r.location)):
            if value:
                show(key, printable(value))
        for problem in r.problems:
            show("problem", printable(problem))
        if args.details and r.details:
            details = printable(r.details.strip(), keep_newlines=True)
            print("\n  " + details.replace("\n", "\n  "))
        print()
    statuses = {r.status for r in reports}
    if INVALID in statuses:
        return EXIT_INVALID
    return EXIT_UNTRUSTED if UNTRUSTED in statuses else 0


# --- page tools ----------------------------------------------------------------


def _read_pdf(path: Path, password: str | None) -> tuple[bytes, str | None]:
    if not path.is_file():
        raise SigningError(_("no such file: {path}").format(path=path))
    password = _pdf_password(path, password,
                             _("PDF password for {file}").format(file=path.name))
    return path.read_bytes(), password


def _guard_signatures(data: bytes, password: str | None, force: bool, message: str,
                      **kw) -> None:
    """``message``: translated, with ``{names}`` for the signatures (and ``kw``)."""
    names = pages.signature_names(data, password)
    if names and not force:
        raise pages.EditBlocked(message.format(names=", ".join(repr(n) for n in names), **kw))


def _edit_output(src: Path, args, suffix: str) -> Path:
    return src if args.in_place else Path(args.output or src.with_name(f"{src.stem}-{suffix}.pdf"))


def _margins(s: str) -> tuple[float, ...]:
    values = [float(v) for v in s.replace(";", ",").split(",")]
    if len(values) == 1:
        return tuple(values * 4)
    if len(values) == 2:  # vertical, horizontal
        return (values[0], values[1], values[0], values[1])
    if len(values) == 4:
        return tuple(values)
    raise SigningError(_("--margins takes 1, 2 or 4 values (top,right,bottom,left)"))


def cmd_rotate(cfg: Config, args) -> int:
    src = Path(args.file)
    data, pw = _read_pdf(src, args.password)
    selected = pages.parse_pages(args.pages, pages.page_count(data, pw))
    _guard_signatures(data, pw, args.force,
                      _("rotating pages would invalidate the signature(s) {names}"))
    dst = _edit_output(src, args, "rotated")
    _atomic_write(dst, pages.rotate(data, selected, args.angle, pw))
    print(ngettext("rotated {n} page by {angle}° -> {path}",
                   "rotated {n} pages by {angle}° -> {path}", len(selected)).format(
        n=len(selected), angle=args.angle, path=dst))
    return 0


def cmd_crop(cfg: Config, args) -> int:
    src = Path(args.file)
    data, pw = _read_pdf(src, args.password)
    selected = pages.parse_pages(args.pages, pages.page_count(data, pw))
    _guard_signatures(data, pw, args.force,
                      _("cropping pages would invalidate the signature(s) {names}"))
    dst = _edit_output(src, args, "cropped")
    out = pages.crop(data, selected, box=args.box, reset=args.reset, password=pw,
                     margins=_margins(args.margins) if args.margins else None)
    _atomic_write(dst, out)
    print(ngettext("cropped {n} page -> {path}", "cropped {n} pages -> {path}",
                   len(selected)).format(n=len(selected), path=dst))
    return 0


def cmd_split(cfg: Config, args) -> int:
    src = Path(args.file)
    data, pw = _read_pdf(src, args.password)
    n = pages.page_count(data, pw)
    _guard_signatures(data, pw, args.force,
                      _("splitting would remove the signature(s) {names}"))
    if args.part:
        parts = [pages.parse_pages(spec, n) for spec in args.part]
    else:
        parts = pages.chunks(n, args.every)
    pattern = args.output or str(src.with_name(f"{src.stem}-part{{n}}.pdf"))
    if "{n}" not in pattern:
        raise SigningError(_("-o needs {n} in the name, e.g. part-{n}.pdf"))
    targets = [Path(pattern.replace("{n}", str(i))) for i in range(1, len(parts) + 1)]
    if any(t.resolve() == src.resolve() for t in targets):
        raise SigningError(_("a part would overwrite the input file {file}").format(file=src))
    existing = [str(t) for t in targets if t.exists()]
    if existing and not args.overwrite:
        raise SigningError(_("already exist: {files} (use --overwrite to replace them)").format(
            files=", ".join(existing)))
    for i, (dst, part) in enumerate(zip(targets, pages.split(data, parts, pw), strict=True), 1):
        _atomic_write(dst, part)
        count = len(parts[i - 1])
        print(ngettext("{path}  ({n} page)", "{path}  ({n} pages)", count).format(
            path=dst, n=count))
    return 0


def _merge_input(arg: str) -> tuple[Path, str | None]:
    """FILE or FILE@PAGES (split at the last @ unless the whole argument is a file)."""
    if "@" in arg and not Path(arg).exists():
        file, spec = arg.rsplit("@", 1)
        return Path(file), spec
    return Path(arg), None


def cmd_merge(cfg: Config, args) -> int:
    inputs = []
    total = 0
    for arg in args.files:
        path, spec = _merge_input(arg)
        data, pw = _read_pdf(path, None)
        _guard_signatures(data, pw, args.force,
                          _("merging {file} would remove the signature(s) {names}"),
                          file=path.name)
        sel = None if spec is None else pages.parse_pages(spec, pages.page_count(data, pw))
        total += pages.page_count(data, pw) if sel is None else len(sel)
        inputs.append((data, sel, pw))
    _atomic_write(Path(args.output), pages.merge(inputs))
    files = ngettext("{n} file", "{n} files", len(inputs)).format(n=len(inputs))
    page_text = ngettext("{n} page", "{n} pages", total).format(n=total)
    print(_("merged {files} ({pages}) -> {path}").format(
        files=files, pages=page_text, path=args.output))
    return 0


def cmd_gui(cfg: Config, args) -> int:
    try:
        from .gui.app import main as gui_main
    except ImportError as e:
        raise SigningError(_("the GUI needs PySide6: pip install 'pdfsigner[gui]' ({error})")
                           .format(error=e)) from None

    return gui_main([args.file] if args.file else [], config_path=args.config)


# --- main --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # argparse translates its own texts with module-level gettext functions
    # (``_`` and ``ngettext``, looked up at call time): route them through our
    # catalog. The msgids are listed in ARGPARSE_MESSAGES.
    argparse._ = _
    argparse.ngettext = ngettext
    ap = argparse.ArgumentParser(prog="pdfsigner", description=_(DESCRIPTION),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help=_(
        "config file (default: $PDFSIGNER_CONFIG, "
        "else config.toml in the platform's config folder, see docs/cli.md)"))
    ap.add_argument("-v", "--verbose", action="store_true", help=_("show library warnings"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    certs = sub.add_parser("certs", help=_("manage certificate profiles"))
    csub = certs.add_subparsers(dest="certs_cmd", required=True)
    csub.add_parser("list").set_defaults(func=cmd_certs_list)
    p = csub.add_parser("show"); p.add_argument("name"); p.set_defaults(func=cmd_certs_show)
    p = csub.add_parser("add"); p.add_argument("name"); _add_profile_opts(p)
    p.set_defaults(func=cmd_certs_add)
    p = csub.add_parser("edit"); p.add_argument("name"); _add_profile_opts(p)
    p.set_defaults(func=cmd_certs_edit)
    p = csub.add_parser("remove"); p.add_argument("name")
    p.add_argument("--purge-secret", action="store_true",
                   help=_("also delete it from the keyring"))
    p.set_defaults(func=cmd_certs_remove)
    p = csub.add_parser("default"); p.add_argument("name"); p.set_defaults(func=cmd_certs_default)
    p = csub.add_parser("set-secret", help=_("store passphrase/PIN in the OS keyring"))
    p.add_argument("name"); p.set_defaults(func=cmd_certs_set_secret)
    csub.add_parser("system", help=_("list certificates in the Windows/macOS store")
                    ).set_defaults(func=cmd_certs_system)
    p = csub.add_parser("pkcs11", help=_("list the keys and certificates on a PKCS#11 token"))
    p.add_argument("module", help=_("PKCS#11 module, e.g. {example}").format(
        example=PKCS11_EXAMPLE))
    p.add_argument("--token-label", help=_("only this token"))
    p.add_argument("--login", action="store_true",
                   help=_("ask for the PIN to list the private keys too"))
    p.set_defaults(func=cmd_certs_pkcs11)
    p = csub.add_parser("test", help=_("unlock the certificate and print its subject"))
    p.add_argument("name", nargs="?"); p.set_defaults(func=cmd_certs_test)

    p = sub.add_parser("fields", help=_("list signature fields"))
    p.add_argument("file"); p.add_argument("--password", help=_(PASSWORD_HELP))
    p.set_defaults(func=cmd_fields)

    p = sub.add_parser("sign", help=_("sign a PDF"))
    p.add_argument("file")
    p.add_argument("-o", "--output", help=_("output file (default: <name>-signed.pdf)"))
    p.add_argument("-i", "--in-place", action="store_true", help=_("overwrite the input"))
    p.add_argument("-c", "--cert", help=_("certificate profile (default: configured default)"))
    p.add_argument("-f", "--field", help=_("existing field to sign, or name for the new field"))
    p.add_argument("-p", "--page", type=int, help=_("page for a new field (1-based)"))
    where = p.add_mutually_exclusive_group()
    where.add_argument("--box", type=lambda s: _floats(s, 4), help=_("x1,y1,x2,y2 in points"))
    where.add_argument("--at", type=lambda s: _floats(s, 2),
                       help=_("x,y of lower-left corner; height follows the image aspect ratio"))
    p.add_argument("--width", type=float, help=_("width in points (with --at)"))
    p.add_argument("--no-image", action="store_true")
    p.add_argument("--no-text", action="store_true")
    p.add_argument("--force", action="store_true",
                   help=_("sign even if this invalidates an existing signature"))
    p.set_defaults(func=cmd_sign)

    p = sub.add_parser("verify", help=_("check the signatures in a PDF"))
    p.add_argument("file")
    p.add_argument("--trust", action="append", default=[], metavar="CERT",
                   help=_("extra trusted root certificate (PEM/DER); repeatable"))
    p.add_argument("--no-system-trust", action="store_true",
                   help=_("don't trust the built-in Mozilla roots, only --trust and the "
                          "config's trust list"))
    p.add_argument("--online", action="store_true",
                   help=_("fetch revocation info (OCSP/CRL) from the network"))
    p.add_argument("--details", action="store_true", help=_("print pyHanko's full report"))
    p.add_argument("--password", help=_(PASSWORD_HELP))
    p.set_defaults(func=cmd_verify)

    def edit_opts(p, suffix):
        p.add_argument("file")
        p.add_argument("-o", "--output",
                       help=_("output file (default: <name>-{suffix}.pdf)").format(suffix=suffix))
        p.add_argument("-i", "--in-place", action="store_true", help=_("overwrite the input"))
        p.add_argument("--pages", help=_("pages, e.g. 1-3,5,8- or odd/even (default: all)"))
        p.add_argument("--password", help=_(PASSWORD_HELP))
        p.add_argument("--force", action="store_true",
                       help=_("go ahead even if this invalidates existing signatures"))

    p = sub.add_parser("rotate", help=_("rotate pages"))
    edit_opts(p, "rotated")
    p.add_argument("--angle", type=int, required=True, choices=(90, 180, 270, -90),
                   help=_("clockwise degrees"))
    p.set_defaults(func=cmd_rotate)

    p = sub.add_parser("crop", help=_("crop pages (sets the visible area)"))
    edit_opts(p, "cropped")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--margins", help=_("points to cut off as seen on screen: ALL, "
                                       "VERTICAL,HORIZONTAL or TOP,RIGHT,BOTTOM,LEFT"))
    g.add_argument("--box", type=lambda s: _floats(s, 4), help=_("x1,y1,x2,y2 in PDF points"))
    g.add_argument("--reset", action="store_true", help=_("show the whole page again"))
    p.set_defaults(func=cmd_crop)

    p = sub.add_parser("split", help=_("split into several PDFs"))
    p.add_argument("file")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--every", type=int, default=1, help=_("pages per file (default: 1)"))
    g.add_argument("--part", action="append", metavar="SPEC",
                   help=_("pages of one output file; repeat for more files"))
    p.add_argument("-o", "--output", metavar="PATTERN",
                   help=_("output names with {n} for the part number "
                          "(default: <name>-part{n}.pdf)"))
    p.add_argument("--password", help=_(PASSWORD_HELP))
    p.add_argument("--overwrite", action="store_true",
                   help=_("replace part files that already exist"))
    p.add_argument("--force", action="store_true", help=_("split even signed documents "
                                                          "(the signatures are removed)"))
    p.set_defaults(func=cmd_split)

    p = sub.add_parser("merge", help=_("combine PDFs (or some of their pages)"))
    p.add_argument("files", nargs="+", metavar="FILE[@PAGES]")
    p.add_argument("-o", "--output", required=True, help=_("output file"))
    p.add_argument("--force", action="store_true", help=_("merge even signed documents "
                                                          "(the signatures are removed)"))
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("gui", help=_("start the GUI"))
    p.add_argument("file", nargs="?")
    p.set_defaults(func=cmd_gui)
    return ap


def _setup_language(argv: list[str] | None) -> None:
    """Before the parser is built, so its help texts are translated too."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", type=Path)
    known, _rest = pre.parse_known_args(argv)
    try:
        configured = load_config(known.config).language
    except Exception:  # reported properly once the command runs
        configured = None
    i18n.setup(configured)


def main(argv: list[str] | None = None) -> int:
    _setup_language(argv)
    args = build_parser().parse_args(argv)
    # pyHanko logs expected situations (e.g. self-signed certificates) as warnings.
    logging.basicConfig(level=logging.WARNING if args.verbose else logging.ERROR,
                        format="%(name)s: %(message)s")
    try:
        cfg = load_config(args.config)
        return args.func(cfg, args)
    except Cancelled:
        print(_("cancelled"), file=sys.stderr)
        return 130
    except SigningBlocked as e:
        if isinstance(e, pages.EditBlocked):
            msg = _("error: {message} (use --force to continue anyway)")
        else:
            msg = _("error: {message} (use --force to sign anyway)")
        print(msg.format(message=e), file=sys.stderr)
        return 3
    except (SigningError, SecretError, KeyError, ValueError, OSError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else e
        print(_("error: {message}").format(message=msg), file=sys.stderr)
        return 1
    except (PdfError, PyPdfError) as e:  # malformed PDF (pyHanko / pypdf)
        print(_("error: cannot read the PDF: {error}").format(error=e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
