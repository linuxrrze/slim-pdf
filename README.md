# pdfsigner

Sign PDFs with a real certificate (PAdES, via [pyHanko](https://github.com/MatthiasValvekens/pyHanko)),
optionally showing a scanned signature image. It has a CLI and a Qt GUI that
share the same certificate profiles.

* Sign **prepared signature fields**, or **place a new signature anywhere**
  (click to place, drag to move, drag the corner or press Ctrl+wheel to
  resize; the aspect ratio is kept).
* **Sign multiple times.** Every signature is an incremental update, so
  earlier signatures stay valid. Before asking for any passphrase, pdfsigner
  checks for fields locked by earlier signatures and for certified documents
  (no changes allowed, or no new visible fields).
* **Multiple certificate profiles:** PKCS#12 files, PKCS#11 tokens, or the
  Windows certificate store / macOS keychain, each
  with its own image, stamp text, reason, location and timestamp server.
* **Secrets** come from a prompt, the OS keyring (including KeePassXC via
  Secret Service), or a KeePass database (directly, or via `keepassxc-cli` for
  YubiKey-protected databases). The `.p12` can live inside KeePass as an
  attachment. See [docs/secret-storage.md](docs/secret-storage.md).
* **Long-term validation** (`ltv = true`): certificate chain and revocation
  info are embedded (PAdES B-LT, or B-LTA with a timestamp server).
* Signature images can be PNG/JPEG or **PDF (vector)**.
* Stamps are drawn upright on rotated pages (`/Rotate`).
* **Page tools:** rotate, crop and delete pages (CLI and the GUI's page
  menu); split and merge on the command line.
* **English and German** (GUI and command line), following the system
  language or `language` in the config.
* **Verify** signatures (`pdfsigner verify`, and a *Signatures* list in the
  GUI): valid, untrusted or invalid, with what changed after each signature.

## Install

**Windows and macOS:** an installer (`PDF-Signer-<version>-setup.exe`) and
disk images (`PDF-Signer-<version>-macos-<arch>.dmg`) are built for every
release tag and attached to the GitHub release. They contain the GUI and the
`pdfsigner` command, no Python needed. See
[packaging/README.md](packaging/README.md) for details, code signing and
building them yourself.

**From source** (any platform, Python ≥ 3.11):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[all]'          # or pick extras: gui, keyring, keepass, pkcs11
```

On Windows (PowerShell):

```powershell
py -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e ".[all]"
```

### Platform notes

| | Linux | macOS | Windows |
|---|---|---|---|
| Config file | `~/.config/pdfsigner/` | `~/Library/Application Support/pdfsigner/` | `%LOCALAPPDATA%\pdfsigner\` |
| `keyring` backend | Secret Service / KWallet (KeePassXC possible) | Keychain | Credential Manager |
| KeePass profiles | ✅ | ✅ | ✅ |
| PKCS#11 module (OpenSC) | `opensc-pkcs11.so` | `/Library/OpenSC/lib/opensc-pkcs11.so` | `opensc-pkcs11.dll` |
| PKCS#11 module (YubiKey) | `libykcs11.so` | `libykcs11.dylib` | `libykcs11.dll` |
| Shortcuts | `Ctrl` | `Cmd` | `Ctrl` |

* Certificates in the **Windows certificate store** or the **macOS
  keychain** can be used directly (`--system-cert`, new; see
  [docs/secret-storage.md](docs/secret-storage.md#6-windows-certificate-store--macos-keychain-type--system)).
* On Windows a PDF can't be overwritten while another program (e.g.
  Acrobat) has it open. pdfsigner then reports this instead of signing.

## Certificates

```bash
pdfsigner certs add work --p12 ~/certs/fau.p12 --image ~/certs/signature.png \
    --reason "Approval" --location "Erlangen" --secret keyring --default
pdfsigner certs set-secret work         # store the passphrase in the keyring
pdfsigner certs test work               # unlock and print subject/validity
pdfsigner certs list
pdfsigner certs edit work --stamp-text ''   # image only, no text
```

Profiles live in `config.toml` in the config directory above (or
`$PDFSIGNER_CONFIG`, or `--config`). The GUI's *Manage…* dialog edits the same file. Example:

```toml
default = "work"

[certs.work]
type = "pkcs12"
path = "~/certs/fau.p12"
image = "~/certs/signature.png"
reason = "Approval"
secret = "keyring"

[certs.vault]
type = "pkcs12"
secret = "keepass"
keepass_db = "~/Passwords.kdbx"
keepass_entry = "Certificates/FAU S/MIME"
keepass_attachment = "fau.p12"
```

The stamp text is a template with placeholders for the certificate (`%(cn)s`,
`%(org)s`, `%(email)s`, `%(issuer)s`, …) and the signing time (`%(ts)s`,
`%(date)s`, `%(time)s`); `text_position` puts it below, above, left or right of
the image. An empty text gives an image-only stamp. See
[the stamp text](docs/cli.md#the-stamp-text).

## Documentation

* **[Command line reference](docs/cli.md):** every command with real
  example output, plus scripting recipes and the config file format.
* **[GUI guide](docs/gui.md):** with screenshots.
* **[Secret storage](docs/secret-storage.md):** keyring, KeePassXC and
  KeePass, hardware tokens, with trade-offs.

Quick start:

```bash
pdfsigner fields contract.pdf                        # what can be signed?
pdfsigner sign contract.pdf --field Applicant        # -> contract-signed.pdf
pdfsigner sign letter.pdf --page 2 --at 350,80 --width 160
pdfsigner verify contract-signed.pdf                # valid / untrusted / invalid
pdfsigner rotate scan.pdf --pages even --angle 180 -i    # page tools
pdfsigner merge -o all.pdf cover.pdf scan.pdf@2-
pdfsigner gui contract.pdf
```

## Tests

```bash
pip install -e '.[dev]'
pytest                       # GUI tests run headless (QT_QPA_PLATFORM=offscreen)
pytest --cov=pdfsigner       # ~390 tests; ~87 % line coverage on Linux (the Windows/macOS store code runs only there)
```

Translations live in `src/pdfsigner/locale/`. After changing user-visible
strings, run `python scripts/i18n.py update`, translate the new entries in
`de/LC_MESSAGES/pdfsigner.po`, then `python scripts/i18n.py compile`;
`test_i18n.py` fails while anything is missing.

| File | Covers |
|---|---|
| `test_signing.py` | New and prepared fields, chains of 2–3 signers (each validated with pyHanko's diff analysis), certification and FieldMDP locks (Include/Exclude/All) and `--force`, encrypted PDFs, timestamps (dummy TSA), stamp variants. **Visual regressions:** stamp upright on all four page rotations; text never overlapping the image. |
| `test_pkcs11_softhsm.py` | Signing with a real PKCS#11 token (SoftHSM 2): token + PKCS#12 chain, wrong PIN, unknown token/key/module, errors while signing; a YubiKey-style token (key selection by label/ID, certificate by the key's ID, PIN before every signature, raw ECDSA); `certs pkcs11` listing. Skipped if SoftHSM isn't installed. |
| `test_tokens.py` | Finding PKCS#11 modules: p11-kit's registry (module paths, `enable-in`/`disable-in`, overriding files, trust modules skipped), the usual places, duplicates, the p11-kit proxy as fallback. |
| `test_ltv.py` | LTV against a local test CA serving a CRL: data is embedded and verifiable offline with revocation checks *required*; B-LTA with a document timestamp; refuses untrusted, revoked and unreachable cases. |
| `test_validation.py` | Verdicts valid/untrusted/invalid, trust roots from options and config, tampered bytes, disallowed later changes, encrypted PDFs, `verify` exit codes. |
| `test_keepassxc.py` | `keepassxc-cli` source against the real binary (binary-exact attachments via named pipe and temp file, key files, wrong password, missing entry/attachment) and a fake CLI (argument protocol, YubiKey option, no hang when the CLI fails). |
| `test_systemstore.py`, `test_systemstore_native.py` | Windows store / macOS keychain: selection by thumbprint or subject, RSA and ECDSA signing through a fake backend; the native backends against a throwaway store (CI on Windows and macOS only). |
| `test_pages.py` | Page specs; rotate and crop as incremental updates (encryption kept, signatures reported invalid afterwards); crop margins follow the screen on all four rotations; split/merge drop signed fields but keep empty ones; CLI refuses signed input without `--force`. |
| `test_gui_pages.py` | GUI page tools: thumbnails, rotating selected pages, cropping by dragging, undo and save, the unsaved-changes prompt, warnings on signed documents, signing an edited document, split and merge dialogs. |
| `test_cli.py` | Every subcommand, exit codes, output format; target errors are reported **before** any passphrase prompt; keyring and encrypted-PDF flows. |
| `test_config.py`, `test_secretstore.py` | Profile validation and TOML round-trip; prompt, keyring and KeePass (attachment, entry paths containing `/`, key files, error cases). |
| `test_gui.py`, `test_gui_coords.py` | Click to place, drag, resize with fixed aspect ratio (handle and Ctrl+wheel), clamping, page navigation, prepared fields, signing twice, error dialogs, encrypted PDFs, certificate manager. Screen-to-PDF mapping for all rotations. |
| `test_gui_background.py` | Slow work (signing, tokens) off the GUI thread: results and errors, PIN dialogs on the GUI thread, events keep flowing, input to the window ignored meanwhile. |
| `test_docs.py` | Every `pdfsigner …` example in the docs is accepted by the real parser; every command and option is documented, and every documented option and config key exists; links and screenshots exist. |
| `test_i18n.py` | Language choice (environment, config, system locale names), the German catalog complete with matching placeholders and an up-to-date `.mo`, the `.mo` writer against Python's gettext, German CLI and GUI output. |

CI (`.github/workflows/tests.yml`) runs the suite on Linux, macOS and
Windows with Python 3.11 and 3.13.

Screenshots are regenerated with
`QT_QPA_PLATFORM=offscreen python scripts/make_screenshots.py`.

## Known gaps / next steps

* **PKCS#11** is tested against SoftHSM (including a token laid out like a
  YubiKey: Yubico's labels, slot IDs, PIN before every signature; RSA, P-256,
  P-384 and Ed25519 keys) and simulated tokens (PIN pad, PIN-lock states),
  not yet with a physical card. See the card overview in
  [secret-storage.md](docs/secret-storage.md#which-cards).
* **Windows certificate store / macOS keychain** signing is tested in CI with
  software keys only, not yet with smartcards. On macOS, smartcard
  identities come from a separate keychain query that has never seen a card.
