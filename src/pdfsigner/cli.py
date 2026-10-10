"""Command line interface.

    pdfsigner certs list|show|add|edit|remove|default|set-secret|test|system
    pdfsigner fields FILE
    pdfsigner sign FILE [--field NAME | --page N (--box ... | --at X,Y --width W)]
    pdfsigner verify FILE
    pdfsigner rotate|crop FILE [--pages SPEC] ...
    pdfsigner split FILE [--every N | --part SPEC ...]
    pdfsigner merge -o OUT FILE[@SPEC] ...
    pdfsigner gui [FILE]

Pages are 1-based on the command line; coordinates are PDF points with the
origin at the bottom-left of the (unrotated) page.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from dataclasses import fields as dc_fields
from pathlib import Path

from . import pages, secretstore
from .config import CERT_TYPES, PKCS11_EXAMPLE, SECRET_SOURCES, CertProfile, Config, load_config
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
    sign_pdf,
)


def _ask(prompt: str) -> str | None:
    try:
        return getpass.getpass(f"{prompt}: ")
    except (EOFError, KeyboardInterrupt):
        return None


def _floats(s: str, n: int) -> tuple[float, ...]:
    parts = s.replace(";", ",").split(",")
    if len(parts) != n:
        raise argparse.ArgumentTypeError(f"expected {n} comma-separated numbers")
    return tuple(float(p) for p in parts)


# --- certs -------------------------------------------------------------------

PROFILE_OPTS = {
    # option -> (CertProfile attribute, help)
    "--type": ("type", f"one of {', '.join(CERT_TYPES)}"),
    "--p12": ("path", "path to the .p12/.pfx file"),
    "--pkcs11-lib": ("pkcs11_lib", f"PKCS#11 module, e.g. {PKCS11_EXAMPLE}"),
    "--token-label": ("token_label", "PKCS#11 token label"),
    "--key-label": ("key_label", "PKCS#11 key label"),
    "--cert-label": ("cert_label", "PKCS#11 certificate label (defaults to key label)"),
    "--system-cert": ("system_cert", "Windows/macOS certificate store: thumbprint or part "
                      "of the subject (see 'certs system')"),
    "--image": ("image", "signature image (PNG with transparency recommended)"),
    "--stamp-text": ("stamp_text", "text on the stamp; %%(signer)s and %%(ts)s are replaced; '' for none"),
    "--reason": ("reason", "signature reason"),
    "--location": ("location", "signature location"),
    "--contact": ("contact_info", "contact info"),
    "--timestamp-url": ("timestamp_url", "RFC 3161 timestamp server URL"),
    "--ltv": ("ltv", "embed chain and revocation info for long-term validation: yes/no"),
    "--secret": ("secret", f"where the passphrase/PIN comes from: {', '.join(SECRET_SOURCES)}"),
    "--keepass-db": ("keepass_db", "KeePass .kdbx file"),
    "--keepass-keyfile": ("keepass_keyfile", "KeePass key file"),
    "--keepass-entry": ("keepass_entry", "entry as Group/Title or Title"),
    "--keepass-attachment": ("keepass_attachment", "attachment name holding the .p12"),
    "--keepass-yubikey": ("keepass_yubikey", "YubiKey slot[:serial] for the database (keepassxc only)"),
}


def _add_profile_opts(p: argparse.ArgumentParser) -> None:
    for opt, (attr, help_) in PROFILE_OPTS.items():
        if attr == "ltv":
            p.add_argument(opt, dest=attr, default=None, choices=("yes", "no"), help=help_)
        else:
            p.add_argument(opt, dest=attr, default=None, help=help_)
    p.add_argument("--default", action="store_true", help="make this the default certificate")


def _apply_profile_opts(profile: CertProfile, args) -> CertProfile:
    for attr, _ in PROFILE_OPTS.values():
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
        print(f"no certificates configured ({cfg.path})")
        return 0
    for name, p in cfg.certs.items():
        mark = "*" if name == cfg.default else " "
        src = (p.path or (f"keepass:{p.keepass_attachment}" if p.keepass_attachment else "")
               or p.pkcs11_lib or (f"store:{p.system_cert}" if p.system_cert else ""))
        print(f"{mark} {name:20} {p.type:7} secret={p.secret:8} {src}")
    return 0


def cmd_certs_show(cfg: Config, args) -> int:
    p = cfg.get(args.name)
    for f in dc_fields(p):
        v = getattr(p, f.name)
        if v:
            print(f"{f.name:20} {v!r}")
    return 0


def cmd_certs_add(cfg: Config, args) -> int:
    if args.name in cfg.certs:
        print(f"certificate {args.name!r} exists, use 'certs edit'", file=sys.stderr)
        return 1
    profile = _apply_profile_opts(CertProfile(name=args.name), args)
    cfg.put(profile)
    if args.default:
        cfg.default = profile.name
    print(f"saved to {cfg.save()}")
    return 0


def cmd_certs_edit(cfg: Config, args) -> int:
    profile = _apply_profile_opts(cfg.get(args.name), args)
    cfg.put(profile)
    if args.default:
        cfg.default = profile.name
    print(f"saved to {cfg.save()}")
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
    secret = _ask(f"{'PIN' if p.type == 'pkcs11' else 'Passphrase'} for '{p.name}'")
    if secret is None:
        return 1
    secretstore.keyring_set(p.name, secret)
    print(f"stored in keyring ({secretstore.keyring_backend_name()})")
    if p.secret != "keyring":
        print(f"note: profile uses secret={p.secret!r}; switch with "
              f"'pdfsigner certs edit {p.name} --secret keyring'")
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
            print(f"no certificates with a private key in the {backend.name}")
        for ident in identities:
            print(ident.describe())
    finally:
        backend.close()
    return 0


# --- fields / sign -----------------------------------------------------------


def cmd_fields(cfg: Config, args) -> int:
    info = inspect_pdf(args.file, args.password)
    print(f"{info.page_count} pages", end="")
    if info.certification:
        print(f", certified ({info.certification.name})", end="")
    print()
    if not info.fields:
        print("no signature fields")
    for f in info.fields:
        state = "signed" if f.filled else ("LOCKED" if f.locked else "empty")
        where = (f"page {f.page + 1}" if f.page is not None else "page ?")
        rect = ",".join(f"{v:.0f}" for v in f.rect) if f.rect else "invisible"
        print(f"  {f.name:24} {state:7} {where:8} {rect}")
    return 0


def cmd_sign(cfg: Config, args) -> int:
    profile = cfg.get(args.cert)
    src = Path(args.file)
    dst = src if args.in_place else Path(args.output or src.with_name(f"{src.stem}-signed.pdf"))

    page = box = None
    if args.box or args.at:
        if args.page is None:
            raise SigningError("--box/--at need --page")
        page = args.page - 1
        if args.box:
            box = args.box
        else:
            if args.width is None:
                raise SigningError("--at needs --width")
            if not profile.image_path:
                raise SigningError("--at keeps the image aspect ratio but the profile has no image")
            box = box_from_anchor(*args.at, args.width, image_aspect(profile.image_path))

    # Check the document before asking for any secret.
    if not src.is_file():
        raise SigningError(f"no such file: {src}")
    pdf_password = _ask("PDF password") if inspect_needs_password(src) else None
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
    print(f"signed field {name!r} -> {dst}")
    return 0


def inspect_needs_password(path: Path) -> bool:
    from pyhanko.pdf_utils.reader import PdfFileReader

    with open(path, "rb") as f:
        return PdfFileReader(f, strict=False).encrypted


EXIT_UNTRUSTED, EXIT_INVALID = 4, 5


def cmd_verify(cfg: Config, args) -> int:
    from .validation import INVALID, UNTRUSTED, verify_pdf

    src = Path(args.file)
    if not src.is_file():
        raise SigningError(f"no such file: {src}")
    pdf_password = args.password
    if pdf_password is None and inspect_needs_password(src):
        pdf_password = _ask("PDF password")
    reports = verify_pdf(src, trust=[*cfg.trust, *args.trust],
                         system_trust=not args.no_system_trust, online=args.online,
                         password=pdf_password)
    if not reports:
        raise SigningError("document has no signatures")
    for r in reports:
        print(f"{r.field}: {r.status.upper()}")
        if r.signer:
            print(f"  signer:   {r.signer}")
            print(f"  issuer:   {r.issuer}")
        if r.timestamp:
            print(f"  time:     {r.timestamp:%Y-%m-%d %H:%M:%S %z} (timestamp)")
        elif r.signed_at:
            print(f"  time:     {r.signed_at:%Y-%m-%d %H:%M:%S %z} (signer's clock)")
        if r.coverage:
            print(f"  covers:   {r.coverage}")
        if r.ltv:
            print("  ltv:      validation data embedded")
        for label, value in (("reason", r.reason), ("location", r.location)):
            if value:
                print(f"  {label + ':':9} {value}")
        for problem in r.problems:
            print(f"  problem:  {problem}")
        if args.details and r.details:
            print("\n  " + r.details.strip().replace("\n", "\n  "))
        print()
    statuses = {r.status for r in reports}
    if INVALID in statuses:
        return EXIT_INVALID
    return EXIT_UNTRUSTED if UNTRUSTED in statuses else 0


# --- page tools ----------------------------------------------------------------


def _read_pdf(path: Path, password: str | None) -> tuple[bytes, str | None]:
    if not path.is_file():
        raise SigningError(f"no such file: {path}")
    data = path.read_bytes()
    if password is None and inspect_needs_password(path):
        password = _ask(f"PDF password for {path.name}")
    return data, password


def _guard_signatures(data: bytes, password: str | None, force: bool, what: str,
                      effect: str = "invalidate") -> None:
    names = pages.signature_names(data, password)
    if names and not force:
        raise pages.EditBlocked(f"{what} would {effect} the signature(s) "
                                f"{', '.join(repr(n) for n in names)}")


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
    raise SigningError("--margins takes 1, 2 or 4 values (top,right,bottom,left)")


def cmd_rotate(cfg: Config, args) -> int:
    src = Path(args.file)
    data, pw = _read_pdf(src, args.password)
    selected = pages.parse_pages(args.pages, pages.page_count(data, pw))
    _guard_signatures(data, pw, args.force, "rotating pages")
    dst = _edit_output(src, args, "rotated")
    _atomic_write(dst, pages.rotate(data, selected, args.angle, pw))
    print(f"rotated {len(selected)} page{'s' * (len(selected) != 1)} by {args.angle}° -> {dst}")
    return 0


def cmd_crop(cfg: Config, args) -> int:
    src = Path(args.file)
    data, pw = _read_pdf(src, args.password)
    selected = pages.parse_pages(args.pages, pages.page_count(data, pw))
    _guard_signatures(data, pw, args.force, "cropping pages")
    dst = _edit_output(src, args, "cropped")
    out = pages.crop(data, selected, box=args.box, reset=args.reset, password=pw,
                     margins=_margins(args.margins) if args.margins else None)
    _atomic_write(dst, out)
    print(f"cropped {len(selected)} page{'s' * (len(selected) != 1)} -> {dst}")
    return 0


def cmd_split(cfg: Config, args) -> int:
    src = Path(args.file)
    data, pw = _read_pdf(src, args.password)
    n = pages.page_count(data, pw)
    _guard_signatures(data, pw, args.force, "splitting", "remove")
    if args.part:
        parts = [pages.parse_pages(spec, n) for spec in args.part]
    else:
        parts = pages.chunks(n, args.every)
    pattern = args.output or str(src.with_name(f"{src.stem}-part{{n}}.pdf"))
    if "{n}" not in pattern:
        raise SigningError("-o needs {n} in the name, e.g. part-{n}.pdf")
    for i, part in enumerate(pages.split(data, parts, pw), 1):
        dst = Path(pattern.replace("{n}", str(i)))
        _atomic_write(dst, part)
        print(f"{dst}  ({len(parts[i - 1])} page{'s' * (len(parts[i - 1]) != 1)})")
    return 0


def _merge_input(arg: str) -> tuple[Path, str | None]:
    """FILE or FILE@PAGES (the @ split only applies if FILE exists)."""
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
        _guard_signatures(data, pw, args.force, f"merging {path.name}", "remove")
        sel = None if spec is None else pages.parse_pages(spec, pages.page_count(data, pw))
        total += pages.page_count(data, pw) if sel is None else len(sel)
        inputs.append((data, sel, pw))
    _atomic_write(Path(args.output), pages.merge(inputs))
    print(f"merged {len(inputs)} files ({total} pages) -> {args.output}")
    return 0


def cmd_gui(cfg: Config, args) -> int:
    from .gui.app import main as gui_main

    return gui_main([args.file] if args.file else [], config_path=args.config)


# --- main --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pdfsigner", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="config file (default: $PDFSIGNER_CONFIG "
                    "or ~/.config/pdfsigner/config.toml)")
    ap.add_argument("-v", "--verbose", action="store_true", help="show library warnings")
    sub = ap.add_subparsers(dest="cmd", required=True)

    certs = sub.add_parser("certs", help="manage certificate profiles")
    csub = certs.add_subparsers(dest="certs_cmd", required=True)
    csub.add_parser("list").set_defaults(func=cmd_certs_list)
    p = csub.add_parser("show"); p.add_argument("name"); p.set_defaults(func=cmd_certs_show)
    p = csub.add_parser("add"); p.add_argument("name"); _add_profile_opts(p)
    p.set_defaults(func=cmd_certs_add)
    p = csub.add_parser("edit"); p.add_argument("name"); _add_profile_opts(p)
    p.set_defaults(func=cmd_certs_edit)
    p = csub.add_parser("remove"); p.add_argument("name")
    p.add_argument("--purge-secret", action="store_true", help="also delete it from the keyring")
    p.set_defaults(func=cmd_certs_remove)
    p = csub.add_parser("default"); p.add_argument("name"); p.set_defaults(func=cmd_certs_default)
    p = csub.add_parser("set-secret", help="store passphrase/PIN in the OS keyring")
    p.add_argument("name"); p.set_defaults(func=cmd_certs_set_secret)
    csub.add_parser("system", help="list certificates in the Windows/macOS store"
                    ).set_defaults(func=cmd_certs_system)
    p = csub.add_parser("test", help="unlock the certificate and print its subject")
    p.add_argument("name", nargs="?"); p.set_defaults(func=cmd_certs_test)

    p = sub.add_parser("fields", help="list signature fields")
    p.add_argument("file"); p.add_argument("--password", help="PDF password")
    p.set_defaults(func=cmd_fields)

    p = sub.add_parser("sign", help="sign a PDF")
    p.add_argument("file")
    p.add_argument("-o", "--output", help="output file (default: <name>-signed.pdf)")
    p.add_argument("-i", "--in-place", action="store_true", help="overwrite the input")
    p.add_argument("-c", "--cert", help="certificate profile (default: configured default)")
    p.add_argument("-f", "--field", help="existing field to sign, or name for the new field")
    p.add_argument("-p", "--page", type=int, help="page for a new field (1-based)")
    p.add_argument("--box", type=lambda s: _floats(s, 4), help="x1,y1,x2,y2 in points")
    p.add_argument("--at", type=lambda s: _floats(s, 2),
                   help="x,y of lower-left corner; height follows the image aspect ratio")
    p.add_argument("--width", type=float, help="width in points (with --at)")
    p.add_argument("--no-image", action="store_true")
    p.add_argument("--no-text", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="sign even if this invalidates an existing signature")
    p.set_defaults(func=cmd_sign)

    p = sub.add_parser("verify", help="check the signatures in a PDF")
    p.add_argument("file")
    p.add_argument("--trust", action="append", default=[], metavar="CERT",
                   help="extra trusted root certificate (PEM/DER); repeatable")
    p.add_argument("--no-system-trust", action="store_true",
                   help="trust only --trust and the config's trust list")
    p.add_argument("--online", action="store_true",
                   help="fetch revocation info (OCSP/CRL) from the network")
    p.add_argument("--details", action="store_true", help="print pyHanko's full report")
    p.add_argument("--password", help="PDF password")
    p.set_defaults(func=cmd_verify)

    def edit_opts(p, suffix):
        p.add_argument("file")
        p.add_argument("-o", "--output", help=f"output file (default: <name>-{suffix}.pdf)")
        p.add_argument("-i", "--in-place", action="store_true", help="overwrite the input")
        p.add_argument("--pages", help="pages, e.g. 1-3,5,8- or odd/even (default: all)")
        p.add_argument("--password", help="PDF password")
        p.add_argument("--force", action="store_true",
                       help="go ahead even if this invalidates existing signatures")

    p = sub.add_parser("rotate", help="rotate pages")
    edit_opts(p, "rotated")
    p.add_argument("--angle", type=int, required=True, choices=(90, 180, 270, -90),
                   help="clockwise degrees")
    p.set_defaults(func=cmd_rotate)

    p = sub.add_parser("crop", help="crop pages (sets the visible area)")
    edit_opts(p, "cropped")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--margins", help="points to cut off as seen on screen: ALL, "
                   "VERTICAL,HORIZONTAL or TOP,RIGHT,BOTTOM,LEFT")
    g.add_argument("--box", type=lambda s: _floats(s, 4), help="x1,y1,x2,y2 in PDF points")
    g.add_argument("--reset", action="store_true", help="show the whole page again")
    p.set_defaults(func=cmd_crop)

    p = sub.add_parser("split", help="split into several PDFs")
    p.add_argument("file")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--every", type=int, default=1, help="pages per file (default: 1)")
    g.add_argument("--part", action="append", metavar="SPEC",
                   help="pages of one output file; repeat for more files")
    p.add_argument("-o", "--output", metavar="PATTERN",
                   help="output names with {n} for the part number "
                   "(default: <name>-part{n}.pdf)")
    p.add_argument("--password", help="PDF password")
    p.add_argument("--force", action="store_true", help="split even signed documents "
                   "(the signatures are removed)")
    p.set_defaults(func=cmd_split)

    p = sub.add_parser("merge", help="combine PDFs (or some of their pages)")
    p.add_argument("files", nargs="+", metavar="FILE[@PAGES]")
    p.add_argument("-o", "--output", required=True, help="output file")
    p.add_argument("--force", action="store_true", help="merge even signed documents "
                   "(the signatures are removed)")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("gui", help="start the GUI")
    p.add_argument("file", nargs="?")
    p.set_defaults(func=cmd_gui)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # pyHanko logs expected situations (e.g. self-signed certificates) as warnings.
    logging.basicConfig(level=logging.WARNING if args.verbose else logging.ERROR,
                        format="%(name)s: %(message)s")
    try:
        cfg = load_config(args.config)
        return args.func(cfg, args)
    except Cancelled:
        print("cancelled", file=sys.stderr)
        return 130
    except SigningBlocked as e:
        verb = "continue" if isinstance(e, pages.EditBlocked) else "sign"
        print(f"error: {e} (use --force to {verb} anyway)", file=sys.stderr)
        return 3
    except (SigningError, SecretError, KeyError, ValueError, OSError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else e
        print(f"error: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
