# Command line reference

```
slim-pdf [--config FILE] [--verbose] <command> ...

  certs list | show | add | edit | remove | default | set-secret | test | system | pkcs11
  fields FILE
  sign FILE [target] [options]
  verify FILE [--trust CERT]
  rotate FILE --angle 90|180|270|-90 [--pages SPEC]
  crop FILE (--margins M | --box X1,Y1,X2,Y2 | --reset) [--pages SPEC]
  delete FILE --pages SPEC
  split FILE [--every N | --part SPEC ...]
  merge -o OUT FILE[@SPEC] ...
  form FILE [--set NAME=VALUE ...]
  ocr FILE [--pages SPEC] [--lang de,en] | ocr --list-engines
  gui [FILE]
```

`-v`/`--verbose` shows warnings from pyHanko that are hidden by default (for
example about self-signed certificates).

* **Pages** are 1-based.
* **Coordinates** are PDF points (1/72 inch), with the origin at the
  bottom-left of the page as stored in the file (before `/Rotate`). An A4
  page is 595 × 842 pt. `slim-pdf fields` prints the rectangles of existing
  fields, which is a quick way to orient yourself.
* **Config file:** `--config FILE`, else `$SLIM_PDF_CONFIG`, else the
  platform default:
  * Linux: `~/.config/slim-pdf/config.toml`
  * macOS: `~/Library/Application Support/slim-pdf/config.toml`
  * Windows: `%LOCALAPPDATA%\slim-pdf\config.toml`
* **Encrypted PDFs:** you are asked for the password. Scripts can set
  `$SLIM_PDF_PDF_PASSWORD` or pipe it in with `--password -`.
  `--password VALUE` also works, but other users can see it in the process
  list, and it ends up in your shell history.
* **Language:** English or German. `$SLIM_PDF_LANG` (`en`, `de`), else
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

## Certificate profiles: `slim-pdf certs`

A *profile* says where the key is (a PKCS#12 file, a KeePass attachment, a
PKCS#11 token, or the Windows certificate store / macOS keychain), where its
passphrase or PIN comes from, and how the visible signature looks. The first profile you add becomes the default.

### Add a PKCS#12 certificate

```console
$ slim-pdf certs add work --p12 ~/certs/fau-smime.p12 --image ~/certs/signature.png \
      --reason "Expense claim" --location Erlangen
saved to ~/.config/slim-pdf/config.toml

$ slim-pdf certs add supervisor --p12 ~/certs/supervisor.p12 --image ~/certs/supervisor.png \
      --stamp-text 'Approved by %(signer)s\n%(ts)s'
saved to ~/.config/slim-pdf/config.toml
```

| Option | Meaning |
|---|---|
| `--type TYPE` | `pkcs12` (default), `pkcs11` or `system`. Rarely needed: `--pkcs11-lib` implies `pkcs11`, `--system-cert` implies `system`. |
| `--p12 FILE` | PKCS#12 file (`.p12`/`.pfx`). `~` and `$VARS` are expanded when used. |
| `--image FILE` | Signature image: PNG/JPEG (a transparent background works best) or a PDF, whose first page is embedded as vector graphics (see below). |
| `--stamp-text TEXT` | Text on the stamp, with placeholders (see [The stamp text](#the-stamp-text)); `\n` = line break, `''` = image only. Default: `Digitally signed by %(signer)s\n%(ts)s`. |
| `--text-position POS` | Where the text goes, seen from the image: `below` (default), `above`, `left` or `right`. |
| `--reason`, `--location`, `--contact` | Stored in the signature; shown by PDF viewers. |
| `--timestamp-url URL` | RFC 3161 timestamp server, e.g. `http://timestamp.digicert.com`. |
| `--ltv yes\|no` | Embed the certificate chain and revocation info (LTV), see below. |
| `--secret SOURCE` | `prompt` (default), `keyring`, `keepass`, `keepassxc` or `none`. See [secret-storage.md](secret-storage.md). |
| `--default` | Make this the default profile. |

### List, inspect and test

```console
$ slim-pdf certs list
* work                 pkcs12  secret=prompt   ~/certs/fau-smime.p12
  supervisor           pkcs12  secret=prompt   ~/certs/supervisor.p12

$ slim-pdf certs show work
name                 'work'
type                 'pkcs12'
path                 '~/certs/fau-smime.p12'
image                '~/certs/signature.png'
stamp_text           'Digitally signed by %(signer)s\n%(ts)s'
reason               'Expense claim'
location             'Erlangen'
secret               'prompt'

$ slim-pdf certs test work
passphrase for certificate 'work':
Common Name: Alice Example
  issuer:  Common Name: Alice Example
  valid:   2026-10-08 .. 2026-11-08
certificate stored in the profile
```

`*` marks the default profile. `certs test` unlocks the key exactly like
signing would. Use it to check a new profile or a stored passphrase. It
also stores the (public) certificate in the profile, so that `certs show`
lists its details without asking for the PIN:

```console
$ slim-pdf certs show work
…
certificate:
  Subject:               Common Name: Alice Example
  Issuer:                Common Name: Alice Example
  Serial number:         1A2B3C4D
  Valid from:            2026-10-08 09:12 UTC
  Valid until:           2026-11-08 09:12 UTC
  Status:                valid, 28 days left
  Key:                   EC secp256r1
  Key usage:             digital_signature, non_repudiation
  SHA-256 fingerprint:   3F…
  SHA-1 fingerprint:     9A…
```

Signing keeps the stored certificate current (for example after a card was
renewed). The GUI reads it when a profile is created.

### The stamp text

The text is a template. Each `%(name)s` is replaced when signing; a line
whose placeholders are all empty (say, `%(reason)s` without a reason) is
left out. Write `%%` for a percent sign.

| Placeholder | Value |
|---|---|
| `%(signer)s` | The signer's name: the certificate's common name |
| `%(cn)s`, `%(email)s`, `%(org)s`, `%(unit)s`, `%(title)s`, `%(country)s` | Attributes of the certificate's subject |
| `%(issuer)s` | Who issued the certificate (its common name or organization) |
| `%(serial)s` | The certificate's serial number (hex) |
| `%(ts)s` | Date and time of signing, e.g. `2026-10-10 14:27:38 CEST` |
| `%(date)s`, `%(time)s` | Date (`2026-10-10`) and time (`14:27`) of signing |
| `%(reason)s`, `%(location)s`, `%(contact)s` | The profile's reason, location and contact |

```console
$ slim-pdf certs edit work --text-position right \
      --stamp-text '%(cn)s\n%(org)s\n%(date)s %(time)s\nReason: %(reason)s'
```

The stamp scales as a whole: a bigger box gets bigger text, too.

### Change, rename the default, remove

```console
$ slim-pdf certs edit work --stamp-text ''             # image only from now on
$ slim-pdf certs edit work --timestamp-url http://timestamp.digicert.com
$ slim-pdf certs default supervisor
$ slim-pdf certs remove supervisor --purge-secret      # also delete it from the keyring
```

`edit` takes the same options as `add` and changes only the ones you pass.

### Vector signature images

A scanned PNG gets blurry when the stamp is large or the document is
printed. A PDF signature stays sharp: slim-pdf embeds page 1 of the PDF as
vector graphics, and its page size determines the aspect ratio. To turn a
scan into one, trace it (Inkscape: *Path → Trace Bitmap*), set the page to
the drawing (*Edit → Resize Page to Selection*) and save as PDF. An existing
SVG only needs the last step.

```console
$ slim-pdf certs edit work --image ~/certs/signature.pdf
```

### Long-term validation (LTV)

A plain signature can only be checked as long as the certificate's
revocation status can be looked up, which in practice ends when the
certificate expires. With `--ltv yes`, slim-pdf validates the signer's
certificate chain while signing, fetches the revocation info (OCSP/CRL) and
embeds it in the document. Together with a timestamp server this gives
PAdES B-LTA; without one, B-LT.

```console
$ slim-pdf certs edit work --ltv yes --timestamp-url http://timestamp.digicert.com
$ slim-pdf sign contract.pdf --field Buyer
passphrase for certificate 'work':
signed field 'Buyer' -> contract-signed.pdf
$ slim-pdf verify contract-signed.pdf
Buyer: VALID
  ...
  ltv:      validation data embedded
```

Signing with LTV needs network access, and the chain must end in a trusted
root: the Mozilla list (which contains the usual public CAs such as HARICA,
Sectigo or T-TeleSec), the system's CA store, or a root you add with `trust = [...]` in the config
file. If the chain can't be validated, or the certificate is revoked,
signing fails instead of producing a signature without LTV data:

```console
$ slim-pdf sign contract.pdf --field Buyer
passphrase for certificate 'work':
error: LTV: cannot validate the signer's certificate chain: Unable to build a validation path for the certificate "Common Name: Carol LTV" - no issuer matching "Common Name: Test Root CA" was found. LTV needs a chain to a trusted root (add it with `trust` in the config) and reachable revocation info (OCSP/CRL)
```

### Store the passphrase in the OS keyring

```console
$ slim-pdf certs edit work --secret keyring
$ slim-pdf certs set-secret work
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
$ slim-pdf certs add vault --secret keepass --keepass-db ~/Passwords.kdbx \
      --keepass-entry "Certificates/FAU S/MIME" --keepass-attachment fau-smime.p12 \
      --image ~/certs/signature.png
$ slim-pdf certs test vault
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
$ slim-pdf certs add vault-yk --secret keepassxc --keepass-db ~/Passwords.kdbx \
      --keepass-entry "Certificates/FAU S/MIME" --keepass-attachment fau-smime.p12 \
      --keepass-yubikey 2
$ slim-pdf certs test vault-yk
```

The prompt then reads *KeePass master password for Passwords.kdbx (then
touch your YubiKey)*.

See [secret-storage.md](secret-storage.md#3-keepassxc-cli-secret--keepassxc) for details.

### Windows certificate store / macOS keychain

```console
$ slim-pdf certs system
$ slim-pdf certs add store --system-cert "Alice Example" --image ~/certs/signature.png
```

`certs system` lists the certificates with a private key in the current
user's store (Windows) or keychain (macOS) as *thumbprint, subject, expiry*.
`--system-cert` takes the thumbprint or part of the subject and sets the type
to `system`. The operating system asks for PINs itself, so these profiles
have no `--secret`. Details in
[secret-storage.md](secret-storage.md#6-windows-certificate-store--macos-keychain-type--system).

### Smartcard / token (PKCS#11)

```console
$ slim-pdf certs add yubikey --pkcs11-lib /usr/lib/x86_64-linux-gnu/libykcs11.so \
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
$ slim-pdf certs pkcs11 /usr/lib/x86_64-linux-gnu/libykcs11.so --login
PIN for token 'YubiKey PIV #12345678':
token 'YubiKey PIV #12345678' (… manufacturer, model, serial …)
  ID 01    key 'Private key for PIV Authentication' (EC)  certificate 'X.509 Certificate for PIV Authentication': Common Name: Alice Example, expires 2027-03-31
  ID 02    key 'Private key for Digital Signature' (EC, PIN for every signature)  certificate 'X.509 Certificate for Digital Signature': Common Name: Alice Example, expires 2027-03-31

profile: slim-pdf certs add NAME --pkcs11-lib /usr/lib/x86_64-linux-gnu/libykcs11.so --key-id ID
```

(This example shows the layout Yubico's module uses, reproduced with a
software token; the token line is shortened.)

Without `--login` only the certificates are listed; most tokens show private
keys only after the PIN. `--token-label` limits the list to one token.
Without a module, `certs pkcs11` asks every module it finds, and names them
first:

* **Linux:** the modules registered with p11-kit (the `*.module` files in
  `/usr/share/p11-kit/modules`, `/etc/pkcs11/modules` and
  `~/.config/pkcs11/modules`, which most distribution packages install),
  then the usual places: OpenSC, Yubico's ykcs11, the distribution's
  `pkcs11` library directory, SafeNet/Thales (`libeTPkcs11.so`,
  `libIDPrimePKCS11.so`), CardOS (`libcardos11.so`) and `/opt/*/lib*/*pkcs11*.so`.
  Modules without signing keys (p11-kit's CA list, GNOME Keyring, NSS) are
  left out, as are p11-kit entries with `disable-in: slim-pdf` or an
  `enable-in` list without `slim-pdf`. If nothing is found, p11-kit's proxy
  module is tried.
* **macOS, Windows:** OpenSC and Yubico's module in their install locations.

A module in another place can always be given as the argument (or in the
profile). In the GUI, the *Module* field lists the modules found, and
*Show token contents…* asks all of them when the field is empty.

| Module | Linux (Debian/Ubuntu) | macOS | Windows |
|---|---|---|---|
| Yubico (YubiKey) | `/usr/lib/x86_64-linux-gnu/libykcs11.so` | `/opt/homebrew/lib/libykcs11.dylib` | `C:\Program Files\Yubico\Yubico PIV Tool\bin\libykcs11.dll` |
| OpenSC (most cards) | `/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so` | `/Library/OpenSC/lib/opensc-pkcs11.so` | `C:\Program Files\OpenSC Project\OpenSC\pkcs11\opensc-pkcs11.dll` |

The paths are the usual install locations; they differ with the package
source. Requires `pip install 'slim-pdf[pkcs11]'`. More in
[secret-storage.md](secret-storage.md#5-hardware-tokens-type--pkcs11).

---

## Inspect a document: `slim-pdf fields`

```console
$ slim-pdf fields expense-claim.pdf
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

## Sign: `slim-pdf sign`

```
slim-pdf sign FILE [-c PROFILE] [-o OUT | -i]
               [--field NAME | --page N (--box X1,Y1,X2,Y2 | --at X,Y --width W)]
               [--no-image] [--no-text] [--force]
```

`-c`/`--cert` picks the profile (default: the configured default). The
output defaults to `FILE-signed.pdf`. `-o`/`--output` picks another name, and
`-i`/`--in-place` overwrites the input (written atomically). Signing is
always incremental, so earlier signatures stay valid.

### 1. Sign a prepared field

```console
$ slim-pdf sign expense-claim.pdf --field Applicant
passphrase for certificate 'work':
signed field 'Applicant' -> expense-claim-signed.pdf
```

The stamp fills the field's rectangle. The image keeps its aspect ratio and
is centred above the text.

### 2. A second signer, in place

```console
$ slim-pdf sign expense-claim-signed.pdf -i -c supervisor --field Supervisor
passphrase for certificate 'supervisor':
signed field 'Supervisor' -> expense-claim-signed.pdf
```

### 3. Place a new signature anywhere

Give the lower-left corner and a width. The height follows the image's
aspect ratio:

```console
$ slim-pdf sign expense-claim-signed.pdf -i --page 2 --at 60,600 --width 150
passphrase for certificate 'work':
signed field 'Signature1' -> expense-claim-signed.pdf
```

Or give an exact box. The image is fitted into it without distortion:

```console
$ slim-pdf sign letter.pdf --page 1 --box 350,80,510,133
```

`--field NAME` together with `--page`/`--box` names the new field (default:
`Signature1`, `Signature2`, …).

### 4. Invisible signature

Without a target you get an invisible signature. It is still a full
cryptographic signature, just without a stamp:

```console
$ slim-pdf sign expense-claim-signed.pdf -o final.pdf
passphrase for certificate 'work':
signed field 'Signature2' -> final.pdf

$ slim-pdf fields final.pdf
2 pages
  Applicant                signed  page 1   60,216,260,282
  Supervisor               signed  page 1   335,216,535,282
  Signature1               signed  page 2   60,600,210,650
  Signature2               signed  page 1   invisible
```

### Appearance switches

```console
$ slim-pdf sign contract.pdf --field Buyer --no-text      # image only
$ slim-pdf sign contract.pdf --field Buyer --no-image     # text only, with a thin border
```

A stamp always shows something: with `--no-text` (or `stamp_text = ""`) but
no image in the profile, it falls back to the signer's name and time.

### Errors and safety checks

The target is checked **before** you're asked for a passphrase:

```console
$ slim-pdf sign final.pdf --field Applicant
error: field 'Applicant' is already signed

$ slim-pdf sign expense-claim.pdf --at 60,600 --width 150
error: --box/--at need --page

$ slim-pdf sign expense-claim.pdf --page 5 --at 60,600 --width 150
error: page out of range (document has 2)

$ slim-pdf sign approved.pdf --field Second
error: signing would invalidate an existing signature: field 'Second' is locked by an existing signature (use --force to sign anyway)

$ slim-pdf sign certified.pdf --field Approval
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
$ slim-pdf sign contract.pdf -i --field Buyer
passphrase for certificate 'work':
error: cannot write contract.pdf: Permission denied (is it open in another program?)
```

A wrong passphrase:

```console
$ slim-pdf sign expense-claim.pdf --field Applicant -o x.pdf
passphrase for certificate 'work':
error: cannot load PKCS#12 for 'work' (wrong passphrase?): Could not load key material from PKCS#12 data
```

---

## Check signatures: `slim-pdf verify`

```console
$ slim-pdf verify final.pdf
Applicant: UNTRUSTED
  signer:   Common Name: Alice Example
  issuer:   Common Name: Alice Example
  time:     2026-10-09 21:48:16 +0200 (signer's clock)
  covers:   revision 3 of 6; later changes: form filling and signatures
  reason:   Expense claim
  location: Erlangen
  problem:  the certificate is self-signed and not in the trust list

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
| `UNTRUSTED` | Everything that can be checked is intact, but trust is missing: a self-signed certificate or a CA that isn't in the trust list, a certificate that wasn't valid at signing time or isn't meant for signatures, revocation status not known yet, or a timestamp from an authority that isn't trusted. The `problem:` line says which. | `4` |
| `NOT CHECKED` | A signature in a format slim-pdf can't check, such as the legacy `adbe.x509.rsa_sha1` (PKCS#1 with SHA-1, deprecated since PDF 2.0). It is listed with signer, time and coverage, but its validity is unknown. | `4` |
| `INVALID` | The signed content was modified, the document was changed in a way the signature doesn't allow (e.g. pages rotated after signing), the certificate is revoked, a timestamp is broken, or the signature violates the field's requirements (seed values). | `5` |

The exit code is the worst verdict. A document without signatures is an
error (`1`).

**Trust:** by default, slim-pdf trusts the Mozilla root list (the same list
on every platform, shipped with the `certifi` package) and the operating
system's CA store: on Linux the distribution's bundle (e.g.
`/etc/ssl/certs/ca-certificates.crt`, so roots installed with
`update-ca-certificates` / `update-ca-trust` count), the Windows "Trusted Root
Certification Authorities" store, the macOS system keychains, and
`$SSL_CERT_FILE` if set. Add your own roots
with `--trust CERT` (PEM or DER, repeatable) or permanently in the config
file (`trust = ["~/certs/my-ca.pem"]`, or *Settings…* in the GUI). `--no-system-trust` trusts only
those (neither Mozilla's nor the system's roots).

```console
$ slim-pdf verify final.pdf --trust ~/certs/alice.pem --trust ~/certs/supervisor-ca.pem
Applicant: VALID
  ...
```

**Intermediate certificates.** Personal certificates are usually issued by
an intermediate CA, which a root signed: "GEANT Personal CA 4", for example,
by "USERTrust RSA Certification Authority" from Mozilla's list. A signature
can only be trusted if it carries that intermediate, and signatures made with
a token or another program often don't. A missing intermediate is then
downloaded from the address in the signer's certificate (as PDF viewers do),
once: it is kept in a cache (`~/.cache/slim-pdf/intermediates.pem` on Linux).
When signing, slim-pdf puts the intermediates into the signature, so its own
signatures validate everywhere without a download.

* `--no-download` doesn't download missing intermediates (the cache is still
  used); such signatures then show as untrusted.
* `--online` fetches revocation information (OCSP/CRL) from the network.
  Without it, only revocation data embedded in the document (LTV) is used,
  and a missing revocation check is not an error. `ltv:` in the output
  means the document carries such data.
* `--details` appends pyHanko's full report for each signature.
* `--password` for encrypted PDFs (otherwise you are asked).

---

## Page tools: rotate, crop, delete, split, merge

**Page specs** (`--pages`, `--part`, `FILE@…`) are 1-based: `3`, `1-3`,
`5-` (to the end), `-2` (from the start), lists like `1-3,5,8-`, and `odd`
or `even`. Default: all pages.

**Signatures:** any page change invalidates existing signatures, so these
commands refuse signed documents (exit code `3`) unless you add `--force`:

```console
$ slim-pdf rotate final.pdf -i --angle 90
error: rotating pages would invalidate the signature(s) 'Applicant', 'Supervisor', 'Signature1', 'Signature2' (use --force to continue anyway)
```

Edit first, sign afterwards.

### Rotate

```console
$ slim-pdf rotate expense-claim.pdf --pages 2 --angle 90
rotated 1 page by 90° -> expense-claim-rotated.pdf
```

`--angle` is clockwise and adds to the current rotation (`-90` turns back).
Like `sign`, rotate and crop take `-o`/`--output` or `-i`/`--in-place`
(default output: `<name>-rotated.pdf` / `<name>-cropped.pdf`) and
`--password` for encrypted PDFs. Both are saved as an incremental update,
so encryption is kept and earlier revisions stay untouched.

### Crop

```console
$ slim-pdf crop expense-claim.pdf --margins 40,30
cropped 2 pages -> expense-claim-cropped.pdf
$ slim-pdf crop expense-claim-cropped.pdf -i --reset
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

### Delete

```console
$ slim-pdf delete scan.pdf --pages 2,4-5
deleted 3 pages -> scan-deleted.pdf
$ slim-pdf delete scan-deleted.pdf -i --pages 1
deleted 1 page -> scan-deleted.pdf
```

`--pages` is required here, and at least one page must remain. Like rotate
and crop, the change is appended to the file (the encryption is kept);
form fields that were only on the deleted pages are removed with them.

### Split

```console
$ slim-pdf split expense-claim.pdf
expense-claim-part1.pdf  (1 page)
expense-claim-part2.pdf  (1 page)
$ slim-pdf split expense-claim.pdf --part 1 --part 2 -o claim-{n}.pdf
claim-1.pdf  (1 page)
claim-2.pdf  (1 page)
```

`--every N` puts N pages in each file (default 1). `--part SPEC` defines one
output file; repeat it for more, in any order and overlapping if you like.
`-o` is a name pattern with `{n}` for the part number. Existing files are
not replaced unless you add `--overwrite`, and a part is never written over
the input file.

### Merge

```console
$ slim-pdf merge -o both.pdf expense-claim.pdf@1 expense-claim-part2.pdf expense-claim.pdf
merged 3 files (4 pages) -> both.pdf
```

Files are combined in the order given; `FILE@SPEC` takes only some pages.
You are asked for the password of each encrypted input.

Split and merge create **new documents**: the output isn't encrypted, and
signatures can't be carried over, so signed signature fields are removed
(with `--force`; otherwise these commands refuse signed input). Empty
signature fields are kept and can still be signed.

---

## Fill in forms: `slim-pdf form`

Without `--set`, `form` lists the form fields (text, check boxes, radio
buttons, drop-downs and list boxes) with their current values and choices:

```console
$ slim-pdf form application.pdf
  name                     text     page 1             ''
  agree                    checkbox page 1             'no'
  size                     radio    page 1             ''
      choices: S, L
  country                  choice   page 1             ''
      choices: de (Germany), fr (France)
  fixed                    text     page 1   read-only 'constant'
  person.first             text     page 1             ''
```

`--set NAME=VALUE` (repeat it for more fields) fills them in. Check boxes
take `yes`/`no`; radio buttons and choices take the value or its label; `\n`
in a text starts a new line:

```console
$ slim-pdf form application.pdf --set "name=Alice Example" --set agree=yes --set country=France
filled in 3 fields -> application-filled.pdf
```

Like the page tools, `form` takes `-o`/`--output` or `-i`/`--in-place`
(default: `<name>-filled.pdf`) and `--password`. The fields are filled in
as an incremental update with new appearances, so every viewer shows the
values. Text must be in the Windows-1252 character set (Latin script, €);
other characters are refused.

**Signatures:** filling in a form after signing is an allowed change, so
signatures stay valid, unless a signature locks the field (refused) or the
document is certified as "no changes allowed" (refused). pyHanko also
doesn't accept changes to fields whose buttons are separate objects, like
radio buttons; when filling in would invalidate a signature, `form` stops
with exit code `3` unless you add `--force`.

## Recognize text (OCR): `slim-pdf ocr`

Scanned pages are only pictures: you can't search them or copy their text.
`ocr` recognizes the text and adds it **invisibly** over the scan. The page
looks exactly the same, but its text can be searched, selected and copied
(and screen readers can read it). Everything runs on your machine; nothing
is uploaded.

```console
$ slim-pdf ocr scan.pdf
skipped 1 page that already has text (--redo recognizes it anyway)
recognized text on 2 pages -> scan-ocr.pdf
```

Pages that already have text (for example, documents that were not scanned)
are skipped; `--redo` recognizes them anyway. Use it for scans with a few
typed lines added, not for pages that were already recognized: those would get
the text a second time. Like the page tools, `ocr` takes `--pages`,
`-o`/`--output` or `-i`/`--in-place` (default: `<name>-ocr.pdf`) and
`--password`. Adding text invalidates existing signatures, so a signed
document is refused with exit code `3` unless you add `--force`; recognize
text *before* signing.

`--lang` picks the languages (default: the user interface's language and
English, e.g. `de,en`). The engine is the one built into the system, or
Tesseract:

| System | Engine (`--engine`) | Install |
|---|---|---|
| macOS 11+ | Apple Vision (`vision`) | Nothing: part of macOS, and of the app. |
| Windows 10/11 | Windows OCR (`windows`) | Nothing, but each language needs its OCR component: *Settings → Time & language → Language & region*, or as administrator: `Add-WindowsCapability -Online -Name Language.OCR~~~de-DE~0.0.1.0` |
| Linux, or anywhere | Tesseract (`tesseract`) | `sudo apt install tesseract-ocr tesseract-ocr-deu` (Fedora: `tesseract tesseract-langpack-deu`; macOS: `brew install tesseract tesseract-lang`; Windows: the [UB Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki)) |

With pip, the macOS and Windows engines come with `pip install
'slim-pdf[ocr]'` (also part of `[all]`). `--list-engines` shows what works
here, with the default marked `*`:

```console
$ slim-pdf ocr --list-engines
* tesseract  Tesseract
      languages: de, en
$ slim-pdf ocr scan.pdf --lang fr
error: Tesseract can't recognize French. Install the language data, e.g. sudo apt install tesseract-ocr-fra
```

Windows OCR reads one language at a time (the first of `--lang`); German
text with English words in it works fine with `de`. Recognized text is
stored in the Windows-1252 character set (Latin script, €); other characters
become `?`.

## GUI: `slim-pdf gui`

```console
$ slim-pdf gui expense-claim.pdf
```

Starts the graphical interface (same as `slim-pdf-gui`); every file given
opens in a tab of its own. See [gui.md](gui.md).

---

## Scripting examples

Sign every PDF in a folder in the same spot (bottom right of page 1):

```bash
for f in invoices/*.pdf; do
  slim-pdf sign "$f" -c work --page 1 --at 400,40 --width 140 -o "signed/$(basename "$f")"
done
```

With `--secret prompt` this asks once per file. For batches, use
`--secret keyring`, or a KeePass profile.

Sign the first empty field, whatever it's called:

```bash
field=$(slim-pdf fields form.pdf | awk '$2 == "empty" {print $1; exit}')
slim-pdf sign form.pdf --field "$field"
```

React to the verdict of `verify`:

```bash
slim-pdf verify incoming.pdf --trust ~/certs/company-ca.pem
case $? in
  0) echo "all signatures valid" ;;
  4) echo "signed, but by someone you don't trust" ;;
  *) echo "invalid signature or error" ;;
esac
```

Use a separate config, e.g. for a shared machine or CI:

```bash
SLIM_PDF_CONFIG=./signing.toml slim-pdf certs list
slim-pdf --config ./signing.toml sign report.pdf
```

---

## Config file format

`slim-pdf certs` and the GUI's *Settings…* dialog (with its *Certificates* tab)
write this file. You can also edit it by hand:

```toml
default = "work"
trust = ["~/certs/supervisor-ca.pem"]   # extra roots for verify (optional)
language = "de"                          # "en" or "de"; omit for the system language

[ocr]                                    # text recognition (optional)
engine = "tesseract"                     # "vision", "windows" or "tesseract"; omit for the default
languages = ["de", "en"]                 # omit for the user interface's language and English
tesseract = "D:/Tools/Tesseract-OCR/tesseract.exe"   # if it isn't on the PATH

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
`key_id`, `cert_label`, `system_cert`, `image`, `stamp_text`, `text_position`, `reason`,
`location`, `contact_info`, `timestamp_url`, `ltv`, `secret`, `keepass_db`, `keepass_keyfile`,
`keepass_entry`, `keepass_attachment`, `keepass_yubikey`, `certificate`. Unknown keys are
rejected, so typos don't go unnoticed. `certificate` is the profile's public certificate
(PEM), written by `certs test`, the GUI and signing; it only serves to show details. The file is written with mode `0600` (Linux and macOS; on
Windows it inherits the permissions of your profile folder) and **never contains
secrets**.
