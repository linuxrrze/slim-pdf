# Installers

`.github/workflows/release.yml` builds them on GitHub's runners for every
tag `v*` (or manually via *Run workflow*) and attaches them to the release:

| File | Platform |
|---|---|
| `PDF-Signer-<version>-setup.exe` | Windows 10/11, x64. Installs per user (no admin rights); "all users" can be chosen. Optional: `pdfsigner` on the PATH, "Open with" entry for PDFs, desktop icon. |
| `PDF-Signer-<version>-macos-arm64.dmg` | macOS on Apple Silicon |
| `PDF-Signer-<version>-macos-x86_64.dmg` | macOS on Intel. Built with cryptography 48.x, the last release with Intel macOS wheels. |

Each bundle contains the GUI and the `pdfsigner` command line tool with all
optional features (PKCS#11, keyring, KeePass, page tools). On macOS the CLI
is at `/Applications/PDF Signer.app/Contents/MacOS/pdfsigner`; link it into
your PATH if you want it:

```bash
ln -s "/Applications/PDF Signer.app/Contents/MacOS/pdfsigner" /usr/local/bin/pdfsigner
```

Before packaging, the workflow runs `smoke_test.py` against the frozen
binaries (sign, verify, page tools, GUI start), then installs/mounts the
finished installer and runs the CLI from there.

## Building locally

```bash
pip install --require-hashes -r packaging/release-requirements.txt   # or: pip install '.[all]' pyinstaller
pip install --no-deps .
python packaging/make_icons.py
pyinstaller packaging/pdfsigner.spec --noconfirm
python packaging/smoke_test.py "dist/PDF Signer/pdfsigner" "dist/PDF Signer/PDF Signer"
```

Then, on Windows, with [Inno Setup 6](https://jrsoftware.org/isinfo.php):

```powershell
iscc /DAppVersion=0.1.0 packaging\windows\pdfsigner.iss   # -> dist\PDF-Signer-0.1.0-setup.exe
```

On macOS:

```bash
packaging/macos/build_dmg.sh 0.1.0                         # -> dist/PDF-Signer-0.1.0-macos-<arch>.dmg
```

`release-requirements.txt` pins every dependency with hashes, so a release
is built from known packages; the command to update it is at the top of
`release-requirements.in`. The build fails (`pip check`) if the locked set
no longer satisfies `pyproject.toml`.

The same spec also builds a Linux folder (`dist/PDF Signer/`), which is how
the bundling is checked without Windows or a Mac.

## Code signing

Unsigned builds work, but the first start shows a warning:

* **Windows SmartScreen:** *More info → Run anyway*.
* **macOS Gatekeeper:** right-click the app → *Open*, or
  `xattr -dr com.apple.quarantine "/Applications/PDF Signer.app"`.

To sign, create an environment named `release` (*Settings → Environments*),
limit it to tags with *Deployment branches and tags → Selected → tag
pattern `v*`*, optionally add required reviewers, and add these as
**environment secrets** there, not as repository secrets. Only runs for a
`v*` tag use that environment, so a manual run of the workflow on some
branch can't produce a signed binary:

| Secret | Use |
|---|---|
| `WINDOWS_CERT_P12`, `WINDOWS_CERT_PASSWORD` | Code-signing certificate (base64 of the `.pfx`) for `signtool` |
| `MACOS_CERT_P12`, `MACOS_CERT_PASSWORD` | "Developer ID Application" certificate (base64 of the `.p12`) |
| `MACOS_SIGN_IDENTITY` | Its name, e.g. `Developer ID Application: Marcel Ritter (TEAMID)` |
| `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD` | Notarization (app-specific password) |

On Windows, Inno Setup signs both executables, the uninstaller and the
setup. The certificate is imported into the runner's certificate store for
that (no key file in the workspace, no password on a command line) and
removed again at the end of the job. On macOS it goes into a temporary
keychain with a random password, which is deleted at the end.

The macOS app is signed with the hardened runtime and the entitlements in
`macos/entitlements.plist` (see the comments there for why each is needed).

A tag must match the version in `pyproject.toml` (`v0.1.0` ↔ `0.1.0`);
otherwise the workflow stops before building.
