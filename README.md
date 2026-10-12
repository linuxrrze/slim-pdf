# slim-pdf

A slim PDF tool: sign PDFs with a real certificate (PAdES, via
[pyHanko](https://github.com/MatthiasValvekens/pyHanko)), optionally showing
a scanned signature image, check the signatures in them, fill in forms, add
text and images, and rotate, crop, reorder, delete, split and merge pages.
It has a CLI and a Qt GUI that share the same certificate profiles.

*slim-pdf was called pdfsigner until its focus grew beyond signing; see
[Renamed from pdfsigner](#renamed-from-pdfsigner).*

* Sign **prepared signature fields**, or **place a new signature anywhere**
  (click to place, drag to move, drag the corner or press Ctrl+wheel to
  resize; the aspect ratio is kept).
* **Sign multiple times.** Every signature is an incremental update, so
  earlier signatures stay valid. Before asking for any passphrase, slim-pdf
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
* **Fill in forms** (text fields, check boxes, radio buttons, choices) in
  the GUI or with `slim-pdf form`, without breaking earlier signatures.
* **Add text and images** (PNG/JPEG, or PDF as vector graphics) to pages in
  the GUI: drag a box or click where they go.
* **Several documents in tabs;** copy, cut and paste pages between them,
  or drag pages from one to another.
* **Page tools:** rotate, crop and delete pages (CLI and the GUI's page
  menu); split and merge on the command line.
* **Find and copy text** in the GUI: the pages scroll continuously; search
  all of them (`Ctrl+F`), select text (also over several pages) and copy it.
* **Recognize text (OCR)** in scanned pages, so they can be searched and
  copied: with the system's own engine on macOS and Windows, Tesseract on
  Linux; entirely offline (`slim-pdf ocr`, and in the GUI).
* **English and German** (GUI and command line), following the system
  language or `language` in the config.
* **Verify** signatures (`slim-pdf verify`, and a *Signatures* list in the
  GUI): valid, untrusted or invalid (legacy formats: not checked), with what changed after each signature.

## Install

**Windows and macOS:** an installer (`Slim-PDF-<version>-setup.exe`) and
disk images (`Slim-PDF-<version>-macos-<arch>.dmg`) are built for every
release tag and attached to the GitHub release. They contain the GUI and the
`slim-pdf` command, no Python needed. See
[packaging/README.md](packaging/README.md) for details, code signing and
building them yourself.

**From source** (any platform, Python ≥ 3.11):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[all]'          # or pick extras: gui, keyring, keepass, pkcs11, ocr
```

On Windows (PowerShell):

```powershell
py -m venv .venv; .venv\Scripts\Activate.ps1
pip install -e ".[all]"
```

### Platform notes

| | Linux | macOS | Windows |
|---|---|---|---|
| Config file | `~/.config/slim-pdf/` | `~/Library/Application Support/slim-pdf/` | `%LOCALAPPDATA%\slim-pdf\` |
| `keyring` backend | Secret Service / KWallet (KeePassXC possible) | Keychain | Credential Manager |
| KeePass profiles | ✅ | ✅ | ✅ |
| PKCS#11 module (OpenSC) | `opensc-pkcs11.so` | `/Library/OpenSC/lib/opensc-pkcs11.so` | `opensc-pkcs11.dll` |
| PKCS#11 module (YubiKey) | `libykcs11.so` | `libykcs11.dylib` | `libykcs11.dll` |
| Shortcuts | `Ctrl` | `Cmd` | `Ctrl` |
| Text recognition (OCR) | Tesseract: `tesseract-ocr tesseract-ocr-deu` | Apple Vision (built in) | Windows OCR (built in; one OCR language pack per language) |

* Certificates in the **Windows certificate store** or the **macOS
  keychain** can be used directly (`--system-cert`, new; see
  [docs/secret-storage.md](docs/secret-storage.md#6-windows-certificate-store--macos-keychain-type--system)).
* On Windows a PDF can't be overwritten while another program (e.g.
  Acrobat) has it open. slim-pdf then reports this instead of signing.
* Windows and macOS are tested automatically only, not yet by hand (see
  [Known gaps](#known-gaps--next-steps)); feedback is welcome.

## Certificates

```bash
slim-pdf certs add work --p12 ~/certs/fau.p12 --image ~/certs/signature.png \
    --reason "Approval" --location "Erlangen" --secret keyring --default
slim-pdf certs set-secret work          # store the passphrase in the keyring
slim-pdf certs test work                # unlock and print subject/validity
slim-pdf certs list
slim-pdf certs edit work --stamp-text ''    # image only, no text
```

Profiles live in `config.toml` in the config directory above (or
`$SLIM_PDF_CONFIG`, or `--config`). The GUI's *Settings…* dialog edits the same file. Example:

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

## Renamed from pdfsigner

Existing setups keep working:

* The commands are `slim-pdf` and `slim-pdf-gui`; `pdfsigner` and
  `pdfsigner-gui` still work as aliases.
* The configuration moved from `…/pdfsigner/config.toml` to
  `…/slim-pdf/config.toml` (same folders otherwise). On the first start
  without one, the old file is copied over; the old one stays for older
  versions.
* Passphrases stored in the OS keyring under "pdfsigner" are still found;
  new ones are stored under "slim-pdf".
* Environment variables are now `SLIM_PDF_…` (`SLIM_PDF_CONFIG`,
  `SLIM_PDF_LANG`, `SLIM_PDF_PDF_PASSWORD`, …); the old `PDFSIGNER_…` names
  are still read.
* In Python, the package is `slim_pdf` (was `pdfsigner`).

## Documentation

* **[Command line reference](docs/cli.md):** every command with real
  example output, plus scripting recipes and the config file format.
* **[GUI guide](docs/gui.md):** with screenshots.
* **[Secret storage](docs/secret-storage.md):** keyring, KeePassXC and
  KeePass, hardware tokens, with trade-offs.

Quick start:

```bash
slim-pdf fields contract.pdf                         # what can be signed?
slim-pdf sign contract.pdf --field Applicant         # -> contract-signed.pdf
slim-pdf sign letter.pdf --page 2 --at 350,80 --width 160
slim-pdf verify contract-signed.pdf                 # valid / untrusted / invalid
slim-pdf rotate scan.pdf --pages even --angle 180 -i     # page tools
slim-pdf merge -o all.pdf cover.pdf scan.pdf@2-
slim-pdf gui contract.pdf
```

## Tests

```bash
pip install -e '.[dev]'
pytest                       # GUI tests run headless (QT_QPA_PLATFORM=offscreen)
pytest --cov=slim_pdf       # ~650 tests; ~87 % line coverage on Linux (the Windows/macOS store code runs only there)
```

Translations live in `src/slim_pdf/locale/`. After changing user-visible
strings, run `python scripts/i18n.py update`, translate the new entries in
`de/LC_MESSAGES/slim-pdf.po`, then `python scripts/i18n.py compile`;
`test_i18n.py` fails while anything is missing.

| File | Covers |
|---|---|
| `test_signing.py` | New and prepared fields, chains of 2–3 signers (each validated with pyHanko's diff analysis), certification and FieldMDP locks (Include/Exclude/All) and `--force`, encrypted PDFs, timestamps (dummy TSA), stamp variants. **Visual regressions:** stamp upright on all four page rotations; text never overlapping the image. |
| `test_pkcs11_softhsm.py` | Signing with a real PKCS#11 token (SoftHSM 2): token + PKCS#12 chain, wrong PIN, unknown token/key/module, errors while signing; a YubiKey-style token (key selection by label/ID, certificate by the key's ID, PIN before every signature, raw ECDSA); `certs pkcs11` listing. Skipped if SoftHSM isn't installed. |
| `test_tokens.py` | Finding PKCS#11 modules: p11-kit's registry (module paths, `enable-in`/`disable-in`, overriding files, trust modules skipped), the usual places, duplicates, the p11-kit proxy as fallback. |
| `test_chain.py` | Intermediate certificates: found, downloaded via AIA (DER, PEM, PKCS#7) from a local server, cached, embedded when signing; old signatures without the chain validate once it's downloaded. |
| `test_certinfo.py` | Certificate details, stamp text placeholders and their values, template checks. |
| `test_ltv.py` | LTV against a local test CA serving a CRL: data is embedded and verifiable offline with revocation checks *required*; B-LTA with a document timestamp; refuses untrusted, revoked and unreachable cases. |
| `test_validation.py` | Verdicts valid/untrusted/invalid, legacy formats (`adbe.x509.rsa_sha1`) listed as not checked, trust roots from options and config, tampered bytes, disallowed later changes, encrypted PDFs, `verify` exit codes. |
| `test_keepassxc.py` | `keepassxc-cli` source against the real binary (binary-exact attachments via named pipe and temp file, key files, wrong password, missing entry/attachment) and a fake CLI (argument protocol, YubiKey option, no hang when the CLI fails). |
| `test_systemstore.py`, `test_systemstore_native.py` | Windows store / macOS keychain: selection by thumbprint or subject, RSA and ECDSA signing through a fake backend; the native backends against a throwaway store (CI on Windows and macOS only). |
| `test_pages.py` | Page specs; rotate and crop as incremental updates (encryption kept, signatures reported invalid afterwards); crop margins follow the screen on all four rotations; split/merge drop signed fields but keep empty ones; CLI refuses signed input without `--force`. |
| `test_forms.py`, `test_gui_forms.py` | Listing and filling in every field kind (text, multi-line, check box, radio, drop-down, list box, child fields), the drawn appearances, refused values and characters, read-only and signature-locked fields, certified and encrypted documents, signatures staying valid; the GUI's form panel and side panel switching, clicking a field on the page, undo, the warning before a signature would break; `slim-pdf form`. |
| `test_insert.py`, `test_gui_insert.py` | Text and images added upright and in place on all four page rotations, wrapping, vector (PDF) images, encrypted documents, existing content and resources kept; in the GUI: dragging a box, clicking, `Esc`, the text dialog's checks, the warning on signed documents. |
| `test_ocr.py`, `test_gui_ocr.py` | The invisible text layer: words where they are on all four rotations and on cropped pages, pixel-identical rendering, pages with text skipped, progress and cancel, `slim-pdf ocr`, signed documents refused; the real engines (Tesseract, Apple Vision, Windows OCR) wherever CI has them; in the GUI: pdfium only on the GUI thread, cancel, undo, missing engines and languages explained, the settings. |
| `test_gui_text.py` | Selecting text by dragging (also backwards, over lines and on to the next page), `Shift`+click, double-click for a word, `Ctrl+A` (the whole document), the scroll position kept after edits, copying with `Ctrl+C` (pages instead when nothing is selected or the *Pages* panel has the focus), the ribbon and page menu; the find bar: matches across pages, next/previous with wrap-around, starting on the current page, match case, `Enter`/`Esc`, searching again after edits and in another document. |
| `test_gui_pages.py` | GUI page tools: thumbnails, rotating selected pages, cropping by dragging, undo and save, the unsaved-changes prompt, warnings on signed documents, signing an edited document, split and merge dialogs. |
| `test_cli.py` | Every subcommand, exit codes, output format; target errors are reported **before** any passphrase prompt; keyring and encrypted-PDF flows. |
| `test_config.py`, `test_secretstore.py` | Profile validation and TOML round-trip; prompt, keyring and KeePass (attachment, entry paths containing `/`, key files, error cases). |
| `test_gui.py`, `test_gui_coords.py` | Click to place, drag, resize with fixed aspect ratio (handle and Ctrl+wheel), the wheel scrolling and Ctrl+wheel zooming, clamping, page navigation, prepared fields, signing twice, error dialogs, encrypted PDFs, certificate manager. Screen-to-PDF mapping for all rotations. |
| `test_gui_background.py` | Slow work (signing, tokens) off the GUI thread: results and errors, PIN dialogs on the GUI thread, events keep flowing, input to the window ignored meanwhile. |
| `test_docs.py` | Every `slim-pdf …` example in the docs is accepted by the real parser; every command and option is documented, and every documented option and config key exists; links and screenshots exist. |
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
* **Windows and macOS have not been tried by hand yet.** Development and
  manual testing happen on Linux; on Windows and macOS only the automated
  tests run (CI, headless). Not yet seen on a real desktop there: text
  recognition with the system's engines (Apple Vision, Windows OCR), the
  continuous page view, the ribbon and panels with the system's fonts and
  styles, and the installers. **Feedback is welcome**: what works, what
  doesn't, screenshots — please open an
  [issue](https://github.com/linuxrrze/slim-pdf/issues).
