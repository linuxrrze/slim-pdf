# Slim PDF – notes for Claude Code

PAdES signing tool grown into a small PDF editor: CLI `slim-pdf` and Qt GUI
`slim-pdf-gui` (old names `pdfsigner`, `pdfsigner-gui` still work). Python
package `slim_pdf` under `src/`. User docs: `README.md`, `docs/cli.md`,
`docs/gui.md`, `docs/secret-storage.md`.

## Layout
- Signing and verification: `signing.py`, `validation.py`, `chain.py`, `osroots.py`
- Keys: `config.py` (profiles), `secretstore.py` (prompt/keyring/KeePass/keepassxc-cli),
  `tokens.py` (PKCS#11), `systemstore.py` + `_winstore.py`/`_macstore.py`
- Editing: `forms.py`, `insert.py`, `pages.py` (rotate/crop/split/merge/encrypt), `ocr/`
- `cli.py`, `i18n.py`; GUI in `gui/` (`app.py` is the main window and ribbon actions)
- `packaging/`: PyInstaller spec, release requirements (hash-locked), installers,
  `third_party_licenses.py`

## Commands
```bash
pip install -e ".[dev]"
QT_QPA_PLATFORM=offscreen pytest -q            # full suite; start with the touched test files
ruff check src tests scripts packaging
python scripts/i18n.py update|compile|check    # after changing any _() string
QT_QPA_PLATFORM=offscreen python scripts/make_screenshots.py   # docs/images
```
After regenerating screenshots, commit only the ones that really changed: the
certificate-dialog and password images (`gui-certs*.png`, `gui-password.png`)
differ on every run, so `git checkout` them unless those dialogs changed.

## Rules the code and tests rely on
- Every change to a PDF is an **incremental update**, so earlier signatures stay
  valid. Operations that must rewrite the file (split, merge, encrypt) remove
  signed fields: the GUI asks first (`_confirm_breaking`), the CLI exits with
  code 3 unless `--force`.
- Check signature/certification locks **before** asking for any passphrase or PIN.
- pdfium (pypdfium2) is not thread-safe: call it only on the GUI thread; background
  work goes through `gui/background.run`.
- Every user-visible string goes through `_()`/`ngettext`, with a German
  translation in `src/slim_pdf/locale/de/` (`.po` and compiled `.mo`);
  `test_i18n.py` fails otherwise.
- `test_docs.py`: every CLI option must be documented in `docs/cli.md`, examples
  in console/bash blocks must parse, links and images must exist.
- New GUI test files that import fixtures from `test_gui` need an `F811` entry
  in `[tool.ruff.lint.per-file-ignores]`. New test files get a row in the
  README's test table.
- License is GPL-3.0-or-later (pykeepass is GPL). Check the license of any new
  dependency; the installers' `THIRD-PARTY-LICENSES.txt` is generated at build time.
- The CLI `split` and `merge` commands stay (the GUI dialogs for them were removed).

## Git
- One commit per task, author `Marcel Ritter <marcel@linux-ng.de>`. Stage explicit
  paths (no `git add -a`/`-A`).
- Never commit `TODO.md`, `Sample Document.pdf` or screenshots in the repo root:
  they are the user's untracked files.
- Push only when the user asks. Never store a token in git config or a remote URL.
- CI (`.github/workflows/tests.yml`) runs Linux, macOS and Windows × Python 3.11/3.13;
  a change is done when all six jobs are green.
