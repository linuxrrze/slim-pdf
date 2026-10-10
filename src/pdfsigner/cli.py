"""Command line interface.

    pdfsigner certs list|show|add|edit|remove|default|set-secret|test
    pdfsigner fields FILE
    pdfsigner sign FILE [--field NAME | --page N (--box ... | --at X,Y --width W)]
    pdfsigner gui [FILE]

Pages are 1-based on the command line; coordinates are PDF points with the
origin at the bottom-left of the (unrotated) page.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from dataclasses import fields as dc_fields
from pathlib import Path

from . import secretstore
from .config import CERT_TYPES, PKCS11_EXAMPLE, SECRET_SOURCES, CertProfile, Config, load_config
from .secretstore import Cancelled, SecretError, resolve_credentials
from .signing import (
    SigningBlocked,
    SigningError,
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
    "--image": ("image", "signature image (PNG with transparency recommended)"),
    "--stamp-text": ("stamp_text", "text on the stamp; %%(signer)s and %%(ts)s are replaced; '' for none"),
    "--reason": ("reason", "signature reason"),
    "--location": ("location", "signature location"),
    "--contact": ("contact_info", "contact info"),
    "--timestamp-url": ("timestamp_url", "RFC 3161 timestamp server URL"),
    "--secret": ("secret", f"where the passphrase/PIN comes from: {', '.join(SECRET_SOURCES)}"),
    "--keepass-db": ("keepass_db", "KeePass .kdbx file"),
    "--keepass-keyfile": ("keepass_keyfile", "KeePass key file"),
    "--keepass-entry": ("keepass_entry", "entry as Group/Title or Title"),
    "--keepass-attachment": ("keepass_attachment", "attachment name holding the .p12"),
}


def _add_profile_opts(p: argparse.ArgumentParser) -> None:
    for opt, (attr, help_) in PROFILE_OPTS.items():
        p.add_argument(opt, dest=attr, default=None, help=help_)
    p.add_argument("--default", action="store_true", help="make this the default certificate")


def _apply_profile_opts(profile: CertProfile, args) -> CertProfile:
    for attr, _ in PROFILE_OPTS.values():
        value = getattr(args, attr)
        if value is not None:
            setattr(profile, attr, value.replace("\\n", "\n") if attr == "stamp_text" else value)
    if args.pkcs11_lib and args.type is None:
        profile.type = "pkcs11"
    return profile


def cmd_certs_list(cfg: Config, args) -> int:
    if not cfg.certs:
        print(f"no certificates configured ({cfg.path})")
        return 0
    for name, p in cfg.certs.items():
        mark = "*" if name == cfg.default else " "
        src = p.path or (f"keepass:{p.keepass_attachment}" if p.keepass_attachment else p.pkcs11_lib)
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
            pdf_password=pdf_password, allow_breaking=args.force,
        )
    print(f"signed field {name!r} -> {dst}")
    return 0


def inspect_needs_password(path: Path) -> bool:
    from pyhanko.pdf_utils.reader import PdfFileReader

    with open(path, "rb") as f:
        return PdfFileReader(f, strict=False).encrypted


def cmd_gui(cfg: Config, args) -> int:
    from .gui.app import main as gui_main

    return gui_main([args.file] if args.file else [], config_path=args.config)


# --- main --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pdfsigner", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="config file (default: $PDFSIGNER_CONFIG "
                    "or ~/.config/pdfsigner/config.toml)")
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

    p = sub.add_parser("gui", help="start the GUI")
    p.add_argument("file", nargs="?")
    p.set_defaults(func=cmd_gui)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        return args.func(cfg, args)
    except Cancelled:
        print("cancelled", file=sys.stderr)
        return 130
    except SigningBlocked as e:
        print(f"error: {e} (use --force to sign anyway)", file=sys.stderr)
        return 3
    except (SigningError, SecretError, KeyError, ValueError, OSError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else e
        print(f"error: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
