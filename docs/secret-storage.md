# Secret storage: where do the key and passphrase come from?

A signing profile needs two things: the **private key** (a `.p12` file or a
token) and the **secret** that unlocks it (PKCS#12 passphrase or PKCS#11 PIN).
This note compares the options that were looked at, and states which ones
pdfsigner implements.

## Summary

| Option | Implemented | Key on disk? | Master password typed into pdfsigner? | Notes |
|---|---|---|---|---|
| `prompt` | ✅ | `.p12` file | – | Default. Nothing is stored. |
| `keyring` (Secret Service / KWallet, macOS Keychain, Windows Credential Manager) | ✅ | `.p12` file | no | On Linux, **KeePassXC can be the Secret Service provider** (see below). |
| `keepass` (pykeepass, `.kdbx`) | ✅ | **no** (`.p12` as attachment) | yes | Works without KeePassXC running. No YubiKey challenge-response. |
| `keepassxc` (`keepassxc-cli` subprocess) | ✅ | **no** (`.p12` as attachment) | yes | Supports YubiKey challenge-response, unlike pykeepass. Needs KeePassXC installed. |
| KeePassXC browser protocol | ❌ (medium effort) | no | no | Native-messaging protocol, built around URL lookups. |
| PKCS#11 token (YubiKey PIV, Nitrokey, smartcard) | ✅ (tested with SoftHSM, not yet with hardware) | **key can't be exported** | – | Strongest option. The PIN can come from any of the sources above. |

## 1. OS keyring (`secret = "keyring"`)

Uses the Python `keyring` library, which picks the platform's store:

* **Linux:** the freedesktop **Secret Service** D-Bus API (GNOME Keyring,
  KWallet ≥ 5.97, KeePassXC) or KWallet directly.
* **macOS:** the login Keychain. macOS may ask once whether Python may
  access the item.
* **Windows:** Credential Manager (*Windows Credentials*, entry
  `pdfsigner`).

```bash
pdfsigner certs edit work --secret keyring
pdfsigner certs set-secret work      # asks once, stores under service "pdfsigner"
```

**KeePassXC as the keyring (Linux only):** in KeePassXC, go to *Settings → Secret Service
Integration*, enable it, and pick a group to expose in the database settings.
`keyring` then reads and writes that group transparently. Things to know:

* GNOME Keyring or KWallet must not claim `org.freedesktop.secrets` at the
  same time.
* KeePassXC can be set to *confirm each access*. Without that, any process in
  your session can read the secret while the database is unlocked. This is
  true of every Secret Service backend.
* Only the passphrase is stored. The `.p12` stays on disk, but it is
  encrypted with that passphrase.

KeePassXC has no Secret Service on Windows or macOS. There, use a `keepass`
or `keepassxc` profile (sections 2 and 3) to keep using your KeePass
database.

## 2. KeePass database directly (`secret = "keepass"`)

pdfsigner opens the `.kdbx` file with `pykeepass`. The entry's **password**
field holds the passphrase or PIN. Optionally, an **attachment** on the same
entry holds the `.p12` itself. Then the key file isn't on disk at all, apart
from inside the encrypted database.

```bash
pdfsigner certs add work --secret keepass \
    --keepass-db ~/Passwords.kdbx \
    --keepass-entry "Certificates/FAU S/MIME" \
    --keepass-attachment fau.p12        # omit --p12 entirely
```

Entry paths are `Group/Sub/Title`. Titles may contain `/` themselves.

Trade-offs:

* You type the KeePass master password into pdfsigner, and the whole database
  is decrypted in its memory.
* KDBX4 with Argon2 takes a moment to open (~0.5–2 s, depending on your KDF
  settings).
* Key files are supported. **YubiKey challenge-response is not supported by
  pykeepass.** If you need it, use the `keepassxc-cli` variant below.
* It works while KeePassXC is closed, and also on machines without KeePassXC.

## 3. `keepassxc-cli` (`secret = "keepassxc"`)

The same as option 2, but the database is opened by KeePassXC's own
command-line tool, so KeePassXC's unlock methods work, in particular
**YubiKey challenge-response**:

```bash
pdfsigner certs add vault-yk --secret keepassxc --keepass-db ~/Passwords.kdbx \
    --keepass-entry "Certificates/FAU S/MIME" --keepass-attachment fau-smime.p12 \
    --keepass-yubikey 2
```

* pdfsigner asks for the master password once and hands it to
  `keepassxc-cli` on stdin (twice: once for the passphrase, once for the
  attachment). With a YubiKey, touch it when it blinks.
* `--keepass-yubikey` takes KeePassXC's `slot[:serial]` syntax, e.g. `2` or
  `2:7370001`. `--keepass-keyfile` works as well.
* Entries are paths from the database root (`Group/Sub/Title`); unlike
  option 2, a bare title only finds entries in the root group.
* The attachment is passed back through a named pipe, so it never touches
  the disk. On Windows, which has no named pipes for this, it goes to a
  private temporary directory that is deleted right away (the `.p12` itself
  is still encrypted with its passphrase). `attachment-export --stdout` isn't
  used because it corrupts binary files.
* `keepassxc-cli` is found on the `PATH`, in the default install location
  (`/Applications/KeePassXC.app/…` on macOS, `C:\Program Files\KeePassXC\`
  on Windows), or via `$PDFSIGNER_KEEPASSXC_CLI`.
* It is slower than option 2 (two process starts, each deriving the database
  key).

## 4. KeePassXC browser protocol (not implemented)

This is the native-messaging protocol that browser extensions and
`git-credential-keepassxc` use. You associate once (a key exchange). After
that, the running, unlocked KeePassXC hands out entries without the master
password, and it can show a confirmation dialog per access. The catch: it
looks up entries **by URL**, so entries would need a URL such as
`pdfsigner://work`, and attachments can't be fetched. Option 1 gives you most
of this with far less code.

## 5. Hardware tokens (`type = "pkcs11"`)

The real answer to "safe storage" is a key that can't be copied. With
OpenSC, a YubiKey (PIV slot 9c, "Digital Signature") or a Nitrokey works
through OpenSC's PKCS#11 module:

| Platform | Module |
|---|---|
| Linux (Debian/Ubuntu) | `/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so` |
| macOS | `/Library/OpenSC/lib/opensc-pkcs11.so` |
| Windows | `C:\Program Files\OpenSC Project\OpenSC\pkcs11\opensc-pkcs11.dll` |


```bash
pdfsigner certs add token --pkcs11-lib /usr/lib/x86_64-linux-gnu/opensc-pkcs11.so \
    --token-label "PIV_II" --key-label "SIGN key" --secret prompt
```

A GÉANT/DFN S/MIME certificate obtained as a `.p12` can be imported into PIV
slot 9c, for example with `ykman piv keys import` and `ykman piv
certificates import`. Then you can delete the `.p12`, or keep it in KeePass
as a backup. The PIN source is independent (`prompt`, `keyring`,
`keepass` or `keepassxc`).

Install with `pip install 'pdfsigner[pkcs11]'`. The test suite signs with
a SoftHSM 2 software token (`tests/test_pkcs11_softhsm.py`); it hasn't been
tried with a physical token yet.

## 6. Windows certificate store / macOS keychain (`type = "system"`)

A certificate installed through the browser, the company's enrolment, or a
smartcard driver lives in the operating system's store. pdfsigner can sign
with it directly; the private key never leaves the store:

* **Windows:** the personal store of the current user (*certmgr.msc →
  Personal*), through CNG. Smartcards with a minidriver work too; Windows
  shows its own PIN dialog.
* **macOS:** identities in the keychain search list (login keychain,
  smartcards via CryptoTokenKit). macOS asks whether pdfsigner (Python) may
  use the key; *Always Allow* stops it asking again.

```console
$ pdfsigner certs system          # one line per certificate: thumbprint, subject, expiry
$ pdfsigner certs add store --system-cert "Alice Example" --image ~/certs/signature.png
```

`system_cert` is either the SHA-1 thumbprint (spaces and colons are
ignored) or part of the subject; if that matches several certificates,
pdfsigner lists them and asks for the thumbprint. There is no `secret`
for these profiles: the operating system asks for PINs and access itself.
RSA (PKCS#1 v1.5) and ECDSA keys are supported.

pyHanko has no backend for these stores, so pdfsigner talks to them directly
(`src/pdfsigner/_winstore.py`, `_macstore.py`, via `ctypes`). This is
**new and only tested in CI** with software keys imported into a throwaway
store (`tests/test_systemstore_native.py`), not yet with smartcards.

If you'd rather not rely on it, export the certificate with its private key
as `.p12` (Windows: *certmgr.msc → Export → Yes, export the private key*;
macOS: Keychain Access → *Export*) and use a PKCS#12 profile.

## General notes

* The config file (see [cli.md](cli.md) for its location; mode 0600 on
  Linux and macOS) never contains secrets. It only says where they come from.
* Python can't reliably wipe strings from memory. The passphrase and the
  `.p12` bytes live only as long as one signing operation, but they aren't
  zeroed.
* Recommendation: `keyring` with KeePassXC as the Secret Service provider
  (with access confirmation on) for convenience. Use `keepass` with an
  attachment if you want the key off the filesystem. Use a PKCS#11 token if
  you want the key to be non-exportable.
