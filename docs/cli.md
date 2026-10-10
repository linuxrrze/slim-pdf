# Command line reference

```
pdfsigner [--config FILE] [--verbose] <command> ...

  certs list | show | add | edit | remove | default | set-secret | test | system | pkcs11
  fields FILE
  sign FILE [target] [options]
  verify FILE [--trust CERT]
  rotate FILE --angle 90|180|270|-90 [--pages SPEC]
  crop FILE (--margins M | --box X1,Y1,X2,Y2 | --reset) [--pages SPEC]
  split FILE [--every N | --part SPEC ...]
  merge -o OUT FILE[@SPEC] ...
  gui [FILE]
```

`-v`/`--verbose` shows warnings from pyHanko that are hidden by default (for
example about self-signed certificates).

* **Pages** are 1-based.
* **Coordinates** are PDF points (1/72 inch), with the origin at the
  bottom-left of the page as stored in the file (before `/Rotate`). An A4
  page is 595 × 842 pt. `pdfsigner fields` prints the rectangles of existing
  fields, which is a quick way to orient yourself.
* **Config file:** `--config FILE`, else `$PDFSIGNER_CONFIG`, else the
  platform default:
  * Linux: `~/.config/pdfsigner/config.toml`
  * macOS: `~/Library/Application Support/pdfsigner/config.toml`
  * Windows: `%LOCALAPPDATA%\pdfsigner\config.toml`
* **Language:** English or German. `$PDFSIGNER_LANG` (`en`, `de`), else
  `language` in the config file, else the system language; everything else
  falls back to English. The examples below show English output.
* **Windows:** the examples use bash syntax. In PowerShell, continue lines with
  `` ` `` instead of `\`, and quote with `"…"`.
* **Paths** in profiles and in `trust` may use `~` and environment variables:
  `$VAR` on all platforms, `%VAR%` on Windows only.
* **Exit codes:** `0` OK, `1` error, `3` refused because it would invalidate
  an existing signature (overridable with `--force`; also for page tools), `4`/`5` signatures
  untrusted/invalid (`verify`), `130` cancelled at a prompt.

All outputs below come from real runs. Paths are shortened to `~`.

---

## Certificate profiles: `pdfsigner certs`

A *profile* says where the key is (a PKCS#12 file, a KeePass attachment, a
PKCS#11 token, or the Windows certificate store / macOS keychain), where its
passphrase or PIN comes from, and how the visible signature looks. The first profile you add becomes the default.

### Add a PKCS#12 certificate

```console
$ pdfsigner certs add work --p12 ~/certs/fau-smime.p12 --image ~/certs/signature.png \
      --reason "Expense claim" --location Erlangen
saved to ~/.config/pdfsigner/config.toml

$ pdfsigner certs add supervisor --p12 ~/certs/supervisor.p12 --image ~/certs/supervisor.png \
      --stamp-text 'Approved by %(signer)s\n%(ts)s'
saved to ~/.config/pdfsigner/config.toml
```

| Option | Meaning |
|---|---|
| `--type TYPE` | `pkcs12` (default), `pkcs11` or `system`. Rarely needed: `--pkcs11-lib` implies `pkcs11`, `--system-cert` implies `system`. |
| `--p12 FILE` | PKCS#12 file (`.p12`/`.pfx`). `~` and `$VARS` are expanded when used. |
| `--image FILE` | Signature image: PNG/JPEG (a transparent background works best) or a PDF, whose first page is embedded as vector graphics (see below). |
| `--stamp-text TEXT` | Text below the image. `%(signer)s` = certificate CN, `%(ts)s` = signing time, `\n` = line break. `''` = image only. Default: `Digitally signed by %(signer)s\n%(ts)s`. |
| `--reason`, `--location`, `--contact` | Stored in the signature; shown by PDF viewers. |
| `--timestamp-url URL` | RFC 3161 timestamp server, e.g. `http://timestamp.digicert.com`. |
| `--ltv yes\|no` | Embed the certificate chain and revocation info (LTV), see below. |
| `--secret SOURCE` | `prompt` (default), `keyring`, `keepass`, `keepassxc` or `none`. See [secret-storage.md](secret-storage.md). |
| `--default` | Make this the default profile. |

### List, inspect and test

```console
$ pdfsigner certs list
* work                 pkcs12  secret=prompt   ~/certs/fau-smime.p12
  supervisor           pkcs12  secret=prompt   ~/certs/supervisor.p12

$ pdfsigner certs show work
name                 'work'
type                 'pkcs12'
path                 '~/certs/fau-smime.p12'
image                '~/certs/signature.png'
stamp_text           'Digitally signed by %(signer)s\n%(ts)s'
reason               'Expense claim'
location             'Erlangen'
secret               'prompt'

$ pdfsigner certs test work
passphrase for certificate 'work':
Common Name: Alice Example
  issuer:  Common Name: Alice Example
  valid:   2026-10-08 .. 2026-11-08
```

`*` marks the default profile. `certs test` unlocks the key exactly like
signing would. Use it to check a new profile or a stored passphrase.

### Change, rename the default, remove

```console
$ pdfsigner certs edit work --stamp-text ''            # image only from now on
$ pdfsigner certs edit work --timestamp-url http://timestamp.digicert.com
$ pdfsigner certs default supervisor
$ pdfsigner certs remove supervisor --purge-secret     # also delete it from the keyring
```

`edit` takes the same options as `add` and changes only the ones you pass.

### Vector signature images

A scanned PNG gets blurry when the stamp is large or the document is
printed. A PDF signature stays sharp: pdfsigner embeds page 1 of the PDF as
vector graphics, and its page size determines the aspect ratio. To turn a
scan into one, trace it (Inkscape: *Path → Trace Bitmap*), set the page to
the drawing (*Edit → Resize Page to Selection*) and save as PDF. An existing
SVG only needs the last step.

```console
$ pdfsigner certs edit work --image ~/certs/signature.pdf
```

### Long-term validation (LTV)

A plain signature can only be checked as long as the certificate's
revocation status can be looked up, which in practice ends when the
certificate expires. With `--ltv yes`, pdfsigner validates the signer's
certificate chain while signing, fetches the revocation info (OCSP/CRL) and
embeds it in the document. Together with a timestamp server this gives
PAdES B-LTA; without one, B-LT.

```console
$ pdfsigner certs edit work --ltv yes --timestamp-url http://timestamp.digicert.com
$ pdfsigner sign contract.pdf --field Buyer
passphrase for certificate 'work':
signed field 'Buyer' -> contract-signed.pdf
$ pdfsigner verify contract-signed.pdf
Buyer: VALID
  ...
  ltv:      validation data embedded
```

Signing with LTV needs network access, and the chain must end in a trusted
root: the Mozilla list (which contains the usual public CAs such as HARICA,
Sectigo or T-TeleSec) or a root you add with `trust = [...]` in the config
file. If the chain can't be validated, or the certificate is revoked,
signing fails instead of producing a signature without LTV data:

```console
$ pdfsigner sign contract.pdf --field Buyer
passphrase for certificate 'work':
error: LTV: cannot validate the signer's certificate chain: Unable to build a validation path for the certificate "Common Name: Carol LTV" - no issuer matching "Common Name: Test Root CA" was found. LTV needs a chain to a trusted root (add it with `trust` in the config) and reachable revocation info (OCSP/CRL)
```

### Store the passphrase in the OS keyring

```console
$ pdfsigner certs edit work --secret keyring
$ pdfsigner certs set-secret work
Passphrase for 'work':
stored in keyring (keyring.backends.SecretService.Keyring)
```

On Linux with KeePassXC's *Secret Service integration* enabled, this entry
ends up in your KeePassXC database. On macOS it goes to the Keychain
(`keyring.backends.macOS.Keyring`), on Windows to the Credential Manager
(`keyring.backends.Windows.WinVaultKeyring`).

### Keep the whole certificate in KeePass

The entry's password is the passphrase, and the `.p12` is an attachment on
the same entry. No key file stays on disk:

```console
$ pdfsigner certs add vault --secret keepass --keepass-db ~/Passwords.kdbx \
      --keepass-entry "Certificates/FAU S/MIME" --keepass-attachment fau-smime.p12 \
      --image ~/certs/signature.png
$ pdfsigner certs test vault
KeePass master password for Passwords.kdbx:
Common Name: Alice Example
  ...
```

`--keepass-keyfile FILE` adds a key file. Entry paths are `Group/Sub/Title`.
A title may itself contain `/`.

If the database is protected with a YubiKey, use `--secret keepassxc`
instead: the same options, but the database is opened by KeePassXC's
`keepassxc-cli` (KeePassXC must be installed). `--keepass-yubikey SLOT`
selects the YubiKey slot (`slot[:serial]`):

```console
$ pdfsigner certs add vault-yk --secret keepassxc --keepass-db ~/Passwords.kdbx \
      --keepass-entry "Certificates/FAU S/MIME" --keepass-attachment fau-smime.p12 \
      --keepass-yubikey 2
$ pdfsigner certs test vault-yk
```

The prompt then reads *KeePass master password for Passwords.kdbx (then
touch your YubiKey)*.

See [secret-storage.md](secret-storage.md#3-keepassxc-cli-secret--keepassxc) for details.

### Windows certificate store / macOS keychain

```console
$ pdfsigner certs system
$ pdfsigner certs add store --system-cert "Alice Example" --image ~/certs/signature.png
```

`certs system` lists the certificates with a private key in the current
user's store (Windows) or keychain (macOS) as *thumbprint, subject, expiry*.
`--system-cert` takes the thumbprint or part of the subject and sets the type
to `system`. The operating system asks for PINs itself, so these profiles
have no `--secret`. Details in
[secret-storage.md](secret-storage.md#6-windows-certificate-store--macos-keychain-type--system).

### Smartcard / token (PKCS#11)

```console
$ pdfsigner certs add yubikey --pkcs11-lib /usr/lib/x86_64-linux-gnu/libykcs11.so \
      --key-id 02 --image ~/certs/signature.png
```

`--pkcs11-lib` sets the type to `pkcs11` automatically. The key is chosen by
`--key-label` and/or `--key-id` (hex; on a YubiKey or other PIV card `01`,
`02`, `03`, `04` are slots 9a, 9c, 9d, 9e); with neither, the token must
hold exactly one key. The certificate is the one with the key's ID (if
there is none, the one with the key's label), so `--cert-label` is only
needed for tokens that pair them differently.
`--token-label` picks a token when several are plugged in.

To see what is on a token, and which ID to use:

```console
$ pdfsigner certs pkcs11 /usr/lib/x86_64-linux-gnu/libykcs11.so --login
PIN for token 'YubiKey PIV #12345678':
token 'YubiKey PIV #12345678' (… manufacturer, model, serial …)
  ID 01    key 'Private key for PIV Authentication' (EC)  certificate 'X.509 Certificate for PIV Authentication': Common Name: Alice Example, expires 2027-03-31
  ID 02    key 'Private key for Digital Signature' (EC, PIN for every signature)  certificate 'X.509 Certificate for Digital Signature': Common Name: Alice Example, expires 2027-03-31

profile: pdfsigner certs add NAME --pkcs11-lib /usr/lib/x86_64-linux-gnu/libykcs11.so --key-id ID
```

(This example shows the layout Yubico's module uses, reproduced with a
software token; the token line is shortened.)

Without `--login` only the certificates are listed; most tokens show private
keys only after the PIN. `--token-label` limits the list to one token.

| Module | Linux (Debian/Ubuntu) | macOS | Windows |
|---|---|---|---|
| Yubico (YubiKey) | `/usr/lib/x86_64-linux-gnu/libykcs11.so` | `/opt/homebrew/lib/libykcs11.dylib` | `C:\Program Files\Yubico\Yubico PIV Tool\bin\libykcs11.dll` |
| OpenSC (most cards) | `/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so` | `/Library/OpenSC/lib/opensc-pkcs11.so` | `C:\Program Files\OpenSC Project\OpenSC\pkcs11\opensc-pkcs11.dll` |

The paths are the usual install locations; they differ with the package
source. Requires `pip install 'pdfsigner[pkcs11]'`. More in
[secret-storage.md](secret-storage.md#5-hardware-tokens-type--pkcs11).

---

## Inspect a document: `pdfsigner fields`

```console
$ pdfsigner fields expense-claim.pdf
2 pages
  Applicant                empty   page 1   60,216,260,282
  Supervisor               empty   page 1   335,216,535,282
```

Each row shows the field name, its state, the page and the rectangle
(`x1,y1,x2,y2`). States:

* `empty`: can be signed.
* `signed`: already signed.
* `LOCKED`: an earlier signature locked this field, so signing it would
  invalidate that signature.

Certified documents show it in the header, e.g. `2 pages, certified
(FILL_FORMS)`. Use `--password` for encrypted PDFs.

---

## Sign: `pdfsigner sign`

```
pdfsigner sign FILE [-c PROFILE] [-o OUT | -i]
               [--field NAME | --page N (--box X1,Y1,X2,Y2 | --at X,Y --width W)]
               [--no-image] [--no-text] [--force]
```

`-c`/`--cert` picks the profile (default: the configured default). The
output defaults to `FILE-signed.pdf`. `-o`/`--output` picks another name, and
`-i`/`--in-place` overwrites the input (written atomically). Signing is
always incremental, so earlier signatures stay valid.

### 1. Sign a prepared field

```console
$ pdfsigner sign expense-claim.pdf --field Applicant
passphrase for certificate 'work':
signed field 'Applicant' -> expense-claim-signed.pdf
```

The stamp fills the field's rectangle. The image keeps its aspect ratio and
is centred above the text.

### 2. A second signer, in place

```console
$ pdfsigner sign expense-claim-signed.pdf -i -c supervisor --field Supervisor
passphrase for certificate 'supervisor':
signed field 'Supervisor' -> expense-claim-signed.pdf
```

### 3. Place a new signature anywhere

Give the lower-left corner and a width. The height follows the image's
aspect ratio:

```console
$ pdfsigner sign expense-claim-signed.pdf -i --page 2 --at 60,600 --width 150
passphrase for certificate 'work':
signed field 'Signature1' -> expense-claim-signed.pdf
```

Or give an exact box. The image is fitted into it without distortion:

```console
$ pdfsigner sign letter.pdf --page 1 --box 350,80,510,133
```

`--field NAME` together with `--page`/`--box` names the new field (default:
`Signature1`, `Signature2`, …).

### 4. Invisible signature

Without a target you get an invisible signature. It is still a full
cryptographic signature, just without a stamp:

```console
$ pdfsigner sign expense-claim-signed.pdf -o final.pdf
passphrase for certificate 'work':
signed field 'Signature2' -> final.pdf

$ pdfsigner fields final.pdf
2 pages
  Applicant                signed  page 1   60,216,260,282
  Supervisor               signed  page 1   335,216,535,282
  Signature1               signed  page 2   60,600,210,650
  Signature2               signed  page 1   invisible
```

### Appearance switches

```console
$ pdfsigner sign contract.pdf --field Buyer --no-text     # image only
$ pdfsigner sign contract.pdf --field Buyer --no-image    # text only, with a thin border
```

A stamp always shows something: with `--no-text` (or `stamp_text = ""`) but
no image in the profile, it falls back to the signer's name and time.

### Errors and safety checks

The target is checked **before** you're asked for a passphrase:

```console
$ pdfsigner sign final.pdf --field Applicant
error: field 'Applicant' is already signed

$ pdfsigner sign expense-claim.pdf --at 60,600 --width 150
error: --box/--at need --page

$ pdfsigner sign expense-claim.pdf --page 5 --at 60,600 --width 150
error: page out of range (document has 2)

$ pdfsigner sign approved.pdf --field Second
error: signing would invalidate an existing signature: field 'Second' is locked by an existing signature (use --force to sign anyway)

$ pdfsigner sign certified.pdf --field Approval
error: document is certified with 'no changes allowed'; it cannot be signed again
```

**Locked fields:** an earlier signature can lock fields (FieldMDP). Signing
one of them exits with code `3`. `--force` signs anyway and knowingly breaks
the earlier signature.

**"No changes" certification:** a document certified this way can't get
another signature at all, so `--force` doesn't help here.

**Other certifications** (form filling allowed) accept further signatures in
existing empty fields and invisible ones. A new *visible* field (`--box`,
`--at`) would break the certification, so it exits with code `3` and names
the empty fields; `--force` signs anyway.

On Windows a file can't be replaced while another program has it open. If
the output (or the input, with `-i`) is open in Acrobat, close it first:

```console
$ pdfsigner sign contract.pdf -i --field Buyer
passphrase for certificate 'work':
error: cannot write contract.pdf: Permission denied (is it open in another program?)
```

A wrong passphrase:

```console
$ pdfsigner sign expense-claim.pdf --field Applicant -o x.pdf
passphrase for certificate 'work':
error: cannot load PKCS#12 for 'work' (wrong passphrase?): Could not load key material from PKCS#12 data
```

---

## Check signatures: `pdfsigner verify`

```console
$ pdfsigner verify final.pdf
Applicant: UNTRUSTED
  signer:   Common Name: Alice Example
  issuer:   Common Name: Alice Example
  time:     2026-10-09 21:48:16 +0200 (signer's clock)
  covers:   revision 3 of 6; later changes: form filling and signatures
  reason:   Expense claim
  location: Erlangen
  problem:  the certificate doesn't chain to a trusted root

Supervisor: UNTRUSTED
  ...
Signature2: UNTRUSTED
  ...
  covers:   entire document
```

Each signature gets one verdict:

| Verdict | Meaning | Exit code |
|---|---|---|
| `VALID` | Intact, the certificate chains to a trusted root, and later changes are allowed ones (form filling, more signatures, validation data). | `0` |
| `UNTRUSTED` | Everything checks out except trust: self-signed certificate, or a CA that isn't in the trust list. | `4` |
| `INVALID` | The signed content was modified, the document was changed in a way the signature doesn't allow (e.g. pages rotated after signing), or the certificate is revoked. | `5` |

The exit code is the worst verdict. A document without signatures is an
error (`1`).

**Trust:** by default, pdfsigner trusts the Mozilla root list (the same list
on every platform, shipped with the `certifi` package). Add your own roots
with `--trust CERT` (PEM or DER, repeatable) or permanently in the config
file (`trust = ["~/certs/my-ca.pem"]`). `--no-system-trust` trusts only
those.

```console
$ pdfsigner verify final.pdf --trust ~/certs/alice.pem --trust ~/certs/supervisor-ca.pem
Applicant: VALID
  ...
```

* `--online` fetches revocation information (OCSP/CRL) from the network.
  Without it, only revocation data embedded in the document (LTV) is used,
  and a missing revocation check is not an error. `ltv:` in the output
  means the document carries such data.
* `--details` appends pyHanko's full report for each signature.
* `--password` for encrypted PDFs (otherwise you are asked).

---

## Page tools: rotate, crop, split, merge

**Page specs** (`--pages`, `--part`, `FILE@…`) are 1-based: `3`, `1-3`,
`5-` (to the end), `-2` (from the start), lists like `1-3,5,8-`, and `odd`
or `even`. Default: all pages.

**Signatures:** any page change invalidates existing signatures, so these
commands refuse signed documents (exit code `3`) unless you add `--force`:

```console
$ pdfsigner rotate final.pdf -i --angle 90
error: rotating pages would invalidate the signature(s) 'Applicant', 'Supervisor', 'Signature1', 'Signature2' (use --force to continue anyway)
```

Edit first, sign afterwards.

### Rotate

```console
$ pdfsigner rotate expense-claim.pdf --pages 2 --angle 90
rotated 1 page by 90° -> expense-claim-rotated.pdf
```

`--angle` is clockwise and adds to the current rotation (`-90` turns back).
Like `sign`, rotate and crop take `-o`/`--output` or `-i`/`--in-place`
(default output: `<name>-rotated.pdf` / `<name>-cropped.pdf`) and
`--password` for encrypted PDFs. Both are saved as an incremental update,
so encryption is kept and earlier revisions stay untouched.

### Crop

```console
$ pdfsigner crop expense-claim.pdf --margins 40,30
cropped 2 pages -> expense-claim-cropped.pdf
$ pdfsigner crop expense-claim-cropped.pdf -i --reset
cropped 2 pages -> expense-claim-cropped.pdf
```

* `--margins` cuts off points **as seen on screen**: one value for all
  sides, `VERTICAL,HORIZONTAL`, or `TOP,RIGHT,BOTTOM,LEFT`. "Top" is the
  top of the displayed page, also on rotated pages. Margins are taken off
  the current visible area, so repeating the command crops further.
* `--box X1,Y1,X2,Y2` sets the visible area in PDF points (same coordinates
  as `sign --box`).
* `--reset` shows the whole page again. Cropping only hides content (it sets
  the PDF's `/CropBox`); nothing is deleted.

### Split

```console
$ pdfsigner split expense-claim.pdf
expense-claim-part1.pdf  (1 page)
expense-claim-part2.pdf  (1 page)
$ pdfsigner split expense-claim.pdf --part 1 --part 2 -o claim-{n}.pdf
claim-1.pdf  (1 page)
claim-2.pdf  (1 page)
```

`--every N` puts N pages in each file (default 1). `--part SPEC` defines one
output file; repeat it for more, in any order and overlapping if you like.
`-o` is a name pattern with `{n}` for the part number.

### Merge

```console
$ pdfsigner merge -o both.pdf expense-claim.pdf@1 expense-claim-part2.pdf expense-claim.pdf
merged 3 files (4 pages) -> both.pdf
```

Files are combined in the order given; `FILE@SPEC` takes only some pages.
You are asked for the password of each encrypted input.

Split and merge create **new documents**: the output isn't encrypted, and
signatures can't be carried over, so signed signature fields are removed
(with `--force`; otherwise these commands refuse signed input). Empty
signature fields are kept and can still be signed.

---

## GUI: `pdfsigner gui`

```console
$ pdfsigner gui expense-claim.pdf
```

Starts the graphical interface (same as `pdfsigner-gui`); see
[gui.md](gui.md).

---

## Scripting examples

Sign every PDF in a folder in the same spot (bottom right of page 1):

```bash
for f in invoices/*.pdf; do
  pdfsigner sign "$f" -c work --page 1 --at 400,40 --width 140 -o "signed/$(basename "$f")"
done
```

With `--secret prompt` this asks once per file. For batches, use
`--secret keyring`, or a KeePass profile.

Sign the first empty field, whatever it's called:

```bash
field=$(pdfsigner fields form.pdf | awk '$2 == "empty" {print $1; exit}')
pdfsigner sign form.pdf --field "$field"
```

React to the verdict of `verify`:

```bash
pdfsigner verify incoming.pdf --trust ~/certs/company-ca.pem
case $? in
  0) echo "all signatures valid" ;;
  4) echo "signed, but by someone you don't trust" ;;
  *) echo "invalid signature or error" ;;
esac
```

Use a separate config, e.g. for a shared machine or CI:

```bash
PDFSIGNER_CONFIG=./signing.toml pdfsigner certs list
pdfsigner --config ./signing.toml sign report.pdf
```

---

## Config file format

Both `pdfsigner certs` and the GUI's *Manage…* dialog write this file. You
can also edit it by hand:

```toml
default = "work"
trust = ["~/certs/supervisor-ca.pem"]   # extra roots for verify (optional)
language = "de"                          # "en" or "de"; omit for the system language

[certs.work]
type = "pkcs12"
path = "~/certs/fau-smime.p12"
image = "~/certs/signature.png"
reason = "Expense claim"
location = "Erlangen"
ltv = true

[certs.supervisor]
type = "pkcs12"
path = "~/certs/supervisor.p12"
image = "~/certs/supervisor.png"
stamp_text = "Approved by %(signer)s\n%(ts)s"

[certs.vault]
type = "pkcs12"
secret = "keepass"
keepass_db = "~/Passwords.kdbx"
keepass_entry = "Certificates/FAU S/MIME"
keepass_attachment = "fau-smime.p12"

[certs.yubikey]
type = "pkcs11"
pkcs11_lib = "/usr/lib/x86_64-linux-gnu/libykcs11.so"
key_id = "02"
```

All keys: `type`, `path`, `pkcs11_lib`, `token_label`, `key_label`,
`key_id`, `cert_label`, `system_cert`, `image`, `stamp_text`, `reason`, `location`, `contact_info`,
`timestamp_url`, `ltv`, `secret`, `keepass_db`, `keepass_keyfile`, `keepass_entry`,
`keepass_attachment`, `keepass_yubikey`. Unknown keys are rejected, so typos don't go
unnoticed. The file is written with mode `0600` (Linux and macOS; on
Windows it inherits the permissions of your profile folder) and **never contains
secrets**.
