"""Translation catalogs: extract, update, compile, check.

    python scripts/i18n.py update    # source -> locale/pdfsigner.pot -> merge into each .po
    python scripts/i18n.py compile   # .po -> .mo (shipped with the package)
    python scripts/i18n.py check     # exit 1 if anything is out of date or untranslated

Strings are found with ``ast``: calls to ``_("...")``, ``N_("...")`` and
``ngettext("...", "...", n)`` with literal arguments. No gettext tools are
needed; the .mo writer below implements the GNU format.
"""

from __future__ import annotations

import ast
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "pdfsigner"
LOCALE = SRC / "locale"
POT = LOCALE / "pdfsigner.pot"
PLURAL_FORMS = {"de": "nplurals=2; plural=(n != 1);"}
PLACEHOLDER = re.compile(r"(?<!\{)\{(\w*)[^{}]*\}(?!\})")


@dataclass
class Entry:
    msgid: str
    plural: str | None = None
    msgstr: list[str] = field(default_factory=lambda: [""])
    refs: list[str] = field(default_factory=list)
    fuzzy: bool = False


# --- extraction -----------------------------------------------------------------------


def extract() -> dict[tuple[str, str | None], Entry]:
    entries: dict[tuple[str, str | None], Entry] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), rel)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            args = node.args
            if node.func.id in ("_", "N_") and args and _literal(args[0]) is not None:
                key = (_literal(args[0]), None)
            elif (node.func.id == "ngettext" and len(args) >= 2
                  and _literal(args[0]) is not None and _literal(args[1]) is not None):
                key = (_literal(args[0]), _literal(args[1]))
            else:
                # _(CONSTANT) translates a string marked with N_() elsewhere;
                # f-strings, concatenation etc. inside _() can't be looked up.
                dynamic = (ast.JoinedStr, ast.BinOp, ast.Call)
                if node.func.id in ("_", "N_", "ngettext") and any(
                        isinstance(a, dynamic) for a in args):
                    sys.exit(f"{rel}:{node.lineno}: {node.func.id}() needs literal strings; "
                             "use _('... {name}').format(name=...)")
                continue
            e = entries.setdefault(key, Entry(*key))
            e.order = min(getattr(e, "order", (rel, node.lineno)), (rel, node.lineno))
            if rel not in e.refs:  # files only: line numbers would churn on every edit
                e.refs.append(rel)
    return entries


def _literal(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


# --- .po reading / writing --------------------------------------------------------------


def _quote(s: str) -> str:
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t")
    if "\n" not in s:
        return f'"{s}"'
    parts = s.split("\n")
    lines = [p + "\\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    return '""\n' + "\n".join(f'"{line}"' for line in lines)


def _unquote(s: str) -> str:
    s = s.strip()
    assert s.startswith('"') and s.endswith('"'), s
    out, i, body = [], 0, s[1:-1]
    while i < len(body):
        c = body[i]
        if c == "\\":
            nxt = body[i + 1]
            out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}[nxt])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def read_po(path: Path) -> tuple[str, dict[tuple[str, str | None], Entry]]:
    """(header msgstr, entries)."""
    entries: dict[tuple[str, str | None], Entry] = {}
    header = ""
    cur: dict = {}
    last = None
    fuzzy = False

    def flush():
        nonlocal cur, fuzzy, header
        if "msgid" in cur:
            if cur["msgid"] == "" and cur.get("plural") is None:
                header = cur["msgstr"].get(0, "")
            else:
                n = max(cur["msgstr"], default=0) + 1
                e = Entry(cur["msgid"], cur.get("plural"),
                          [cur["msgstr"].get(i, "") for i in range(n)], fuzzy=fuzzy)
                entries[(e.msgid, e.plural)] = e
        cur, fuzzy = {}, False

    for raw in path.read_text(encoding="utf-8").splitlines() + [""]:
        line = raw.strip()
        if not line:
            flush()
            last = None
        elif line.startswith("#,") and "fuzzy" in line:
            fuzzy = True
        elif line.startswith("#"):
            continue
        elif line.startswith("msgid_plural "):
            cur["plural"] = _unquote(line[13:])
            last = ("plural",)
        elif line.startswith("msgid "):
            if "msgid" in cur and "msgstr" in cur:
                flush()
            cur["msgid"] = _unquote(line[6:])
            cur["msgstr"] = {}
            last = ("msgid",)
        elif line.startswith("msgstr["):
            idx = int(line[7:line.index("]")])
            cur["msgstr"][idx] = _unquote(line[line.index("]") + 1:])
            last = ("msgstr", idx)
        elif line.startswith("msgstr "):
            cur["msgstr"][0] = _unquote(line[7:])
            last = ("msgstr", 0)
        elif line.startswith('"') and last:
            if last[0] == "msgstr":
                cur["msgstr"][last[1]] += _unquote(line)
            else:
                cur[last[0]] += _unquote(line)
    return header, entries


def write_po(path: Path, header: str, entries: list[Entry], refs: bool = True) -> None:
    out = ['msgid ""', "msgstr " + _quote(header), ""]
    for e in entries:
        if refs:
            out.extend(f"#: {r}" for r in e.refs)
        if e.fuzzy:
            out.append("#, fuzzy")
        if PLACEHOLDER.search(e.msgid) or (e.plural and PLACEHOLDER.search(e.plural)):
            out.append("#, python-brace-format")
        out.append("msgid " + _quote(e.msgid))
        if e.plural is not None:
            out.append("msgid_plural " + _quote(e.plural))
            out.extend(f"msgstr[{i}] " + _quote(s) for i, s in enumerate(e.msgstr))
        else:
            out.append("msgstr " + _quote(e.msgstr[0]))
        out.append("")
    path.write_text("\n".join(out), encoding="utf-8")


def _header(lang: str | None) -> str:
    h = ("Project-Id-Version: pdfsigner\n"
         "MIME-Version: 1.0\n"
         "Content-Type: text/plain; charset=UTF-8\n"
         "Content-Transfer-Encoding: 8bit\n")
    if lang:
        h += f"Language: {lang}\nPlural-Forms: {PLURAL_FORMS[lang]}\n"
    return h


def languages() -> list[str]:
    return sorted(p.parent.parent.name for p in LOCALE.glob("*/LC_MESSAGES/pdfsigner.po"))


def po_path(lang: str) -> Path:
    return LOCALE / lang / "LC_MESSAGES" / "pdfsigner.po"


def updated_catalogs() -> dict[Path, str]:
    """What the .pot and .po files should contain (path -> text)."""
    import tempfile

    src = sorted(extract().values(), key=lambda e: e.order)
    result = {}
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp) / "x.pot"
        write_po(t, _header(None), src)
        result[POT] = t.read_text(encoding="utf-8")
        for lang in languages():
            _, old = read_po(po_path(lang))
            merged = []
            for e in src:
                prev = old.get((e.msgid, e.plural))
                n = 2 if e.plural is not None else 1
                msgstr = (prev.msgstr if prev else [""] * n) + [""] * n
                merged.append(Entry(e.msgid, e.plural, msgstr[:n], e.refs,
                                    prev.fuzzy if prev else False))
            write_po(t, _header(lang), merged)
            result[po_path(lang)] = t.read_text(encoding="utf-8")
    return result


# --- .mo -------------------------------------------------------------------------------


def mo_bytes(po: Path) -> bytes:
    header, entries = read_po(po)
    pairs = [("", header)]
    for e in entries.values():
        if e.fuzzy or not all(e.msgstr):
            continue
        key = e.msgid if e.plural is None else e.msgid + "\0" + e.plural
        pairs.append((key, "\0".join(e.msgstr)))
    pairs.sort(key=lambda p: p[0].encode("utf-8"))
    ids = [k.encode("utf-8") for k, _ in pairs]
    strs = [v.encode("utf-8") for _, v in pairs]
    n = len(pairs)
    keystart = 7 * 4 + 16 * n
    valuestart = keystart + sum(len(i) + 1 for i in ids)
    koffsets, voffsets, kdata, vdata = [], [], b"", b""
    for i, s in zip(ids, strs, strict=True):
        koffsets += [len(i), keystart + len(kdata)]
        voffsets += [len(s), valuestart + len(vdata)]
        kdata += i + b"\0"
        vdata += s + b"\0"
    head = struct.pack("Iiiiiii", 0x950412DE, 0, n, 7 * 4, 7 * 4 + n * 8, 0, 0)
    return (head + struct.pack(f"{2 * n}i", *koffsets) + struct.pack(f"{2 * n}i", *voffsets)
            + kdata + vdata)


def _rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def mo_path(lang: str) -> Path:
    return po_path(lang).with_suffix(".mo")


# --- check -----------------------------------------------------------------------------


def problems() -> list[str]:
    out = []
    for path, text in updated_catalogs().items():
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            out.append(f"{_rel(path)} is out of date: run scripts/i18n.py update")
    for lang in languages():
        _, entries = read_po(po_path(lang))
        for e in entries.values():
            where = f"{lang}: {e.msgid[:60]!r}"
            if e.fuzzy or not all(e.msgstr):
                out.append(f"{where} is not translated")
                continue
            want = sorted(PLACEHOLDER.findall(e.msgid))
            for s in e.msgstr:
                if sorted(PLACEHOLDER.findall(s)) != want:
                    out.append(f"{where}: placeholders differ ({s[:60]!r})")
        if not mo_path(lang).exists() or mo_path(lang).read_bytes() != mo_bytes(po_path(lang)):
            out.append(f"{_rel(mo_path(lang))} is out of date: run scripts/i18n.py compile")
    return out


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "check"
    if cmd == "update":
        for path, text in updated_catalogs().items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.relative_to(ROOT)}")
    elif cmd == "compile":
        for lang in languages():
            mo_path(lang).write_bytes(mo_bytes(po_path(lang)))
            print(f"wrote {mo_path(lang).relative_to(ROOT)}")
    elif cmd == "check":
        found = problems()
        print("\n".join(found) or "translations up to date")
        return 1 if found else 0
    elif cmd == "init":
        lang = argv[1]
        po_path(lang).parent.mkdir(parents=True, exist_ok=True)
        write_po(po_path(lang), _header(lang), [])
        print(f"created {po_path(lang).relative_to(ROOT)}; now run update")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
