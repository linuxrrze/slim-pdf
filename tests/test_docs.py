"""Keep the documentation honest: examples must parse, links must resolve."""

import re
import shlex
from pathlib import Path

import pytest

from pdfsigner import cli
from pdfsigner.config import CertProfile

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]


def command_lines(path: Path):
    """`pdfsigner ...` invocations from fenced code blocks (with continuations)."""
    text = path.read_text()
    for block in re.findall(r"```(?:console|bash)\n(.*?)```", text, re.S):
        lines = block.replace("\\\n", " ").splitlines()
        for line in lines:
            line = line.strip()
            if line.startswith("$ "):
                line = line[2:]
            # strip shell context around the call, e.g. "PDFSIGNER_CONFIG=x pdfsigner ..."
            m = re.search(r"(?:^|\s)pdfsigner (.*)", line)
            if not m or line.startswith("#") or "pdfsigner [" in line:
                continue
            cmd = m.group(1).split("  #")[0].split(" | ")[0].strip()
            if "$(" in cmd or "<" in cmd:  # placeholders / shell-generated args
                cmd = re.sub(r'"\$\([^)]*\)"|\$\([^)]*\)|"?\$\w+"?', "X", cmd)
            yield cmd


ALL = [(p.name, c) for p in DOCS for c in command_lines(p)]


def test_examples_found():
    assert len(ALL) > 30


@pytest.mark.parametrize("doc, cmd", ALL, ids=[f"{d}: {c[:50]}" for d, c in ALL])
def test_example_parses(doc, cmd):
    """Every documented command line is accepted by the real argument parser."""
    try:
        args = cli.build_parser().parse_args(shlex.split(cmd))
    except SystemExit as e:
        pytest.fail(f"{doc}: `pdfsigner {cmd}` rejected (exit {e.code})")
    assert callable(args.func)


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_links_and_images_exist(doc):
    for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", doc.read_text()):
        if target.startswith(("http://", "https://")):
            continue
        assert (doc.parent / target).exists(), f"{doc.name}: broken link {target}"


def test_gui_doc_has_screenshots():
    text = (ROOT / "docs" / "gui.md").read_text()
    images = re.findall(r"!\[[^\]]*\]\((images/[^)]+)\)", text)
    assert len(images) >= 6
    for img in images:
        assert (ROOT / "docs" / img).stat().st_size > 2000


def test_documented_config_keys_match_code():
    text = (ROOT / "docs" / "cli.md").read_text()
    m = re.search(r"All keys: (.*?)\. Unknown keys", text, re.S)
    documented = set(re.findall(r"`(\w+)`", m.group(1)))
    actual = set(CertProfile.__dataclass_fields__) - {"name"}
    assert documented == actual


def _parser_options() -> tuple[set[str], set[str]]:
    """(all --options, all subcommand paths like 'certs add') of the CLI."""
    opts, commands = set(), set()
    stack = [((), cli.build_parser())]
    while stack:
        path, p = stack.pop()
        for a in p._actions:
            opts.update(o for o in a.option_strings if o.startswith("--"))
            if a.__class__.__name__ == "_SubParsersAction":
                for name, child in a.choices.items():
                    commands.add(" ".join((*path, name)))
                    stack.append(((*path, name), child))
    opts.discard("--help")
    return opts, commands


def test_everything_in_the_parser_is_documented():
    text = (ROOT / "docs" / "cli.md").read_text()
    opts, commands = _parser_options()
    undocumented = {o for o in opts if not re.search(rf"(?<![\w-]){o}(?![\w-])", text)}
    assert not undocumented
    missing = {c for c in commands if f"pdfsigner {c}" not in text}
    assert not missing


def test_documented_options_exist():
    """Every --option mentioned in cli.md exists in the parser."""
    parser_opts, _ = _parser_options()
    documented = set(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]+)", (ROOT / "docs" / "cli.md").read_text()))
    assert documented <= parser_opts, documented - parser_opts
