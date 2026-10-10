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
| Windows certificate store / macOS keychain (`type = "system"`) | ✅ (tested in CI with software keys) | managed by the OS | – | The OS asks for PINs and access itself; see section 6. |

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

Entry paths are `Group/Sub/Title`. Titles may contain `/` themselves. A
bare title is searched in all groups except the recycle bin; if several
entries have it, pdfsigner lists them and asks for the full path.

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
* `keepassxc-cli` is taken from `$PDFSIGNER_KEEPASSXC_CLI` if set, else from
  the `PATH`, else from the default install location
  (`/Applications/KeePassXC.app/…` on macOS, `C:\Program Files\KeePassXC\`
  on Windows). It is never taken from the current directory, even where
  Windows would normally look there, since it receives the master password.
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

The real answer to "safe storage" is a key that can't be copied: a YubiKey,
a Nitrokey or a smartcard, used through the vendor's PKCS#11 module or
OpenSC's.

| Module | Linux (Debian/Ubuntu) | macOS | Windows |
|---|---|---|---|
| Yubico (`ykcs11`, from yubico-piv-tool) | `/usr/lib/x86_64-linux-gnu/libykcs11.so` | `/opt/homebrew/lib/libykcs11.dylib` | `C:\Program Files\Yubico\Yubico PIV Tool\bin\libykcs11.dll` |
| OpenSC | `/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so` | `/Library/OpenSC/lib/opensc-pkcs11.so` | `C:\Program Files\OpenSC Project\OpenSC\pkcs11\opensc-pkcs11.dll` |

`pdfsigner certs pkcs11` (or *Show token contents…* in the GUI) finds these
modules by itself, on Linux also every module registered with p11-kit and
common vendor modules (see [cli.md](cli.md#smartcard--token-pkcs11)), and
lists tokens, keys, certificates and the PIN state.

### Which cards

| Device | How | Status |
|---|---|---|
| YubiKey 5 (PIV) | ykcs11 or OpenSC; or `type = "system"` on Windows (minidriver) and macOS (built-in) | Expected to work; the token layout (labels, slot IDs, PIN before every signature) is tested with SoftHSM |
| Nitrokey 3 (PIV), Nitrokey HSM 2, other PIV cards | OpenSC | Expected to work |
| National ID and qualified-signature cards (e.g. Belgian eID, Estonian ID, D-Trust) | The issuer's PKCS#11 module | Expected to work; the signature key usually wants the PIN for every signature, so the profile needs a PIN source |
| OpenPGP cards (YubiKey/Nitrokey OpenPGP application) | – | Not suitable: they usually hold no X.509 certificate, and GnuPG keeps the card busy |
| German ID card (nPA) | – | Can't create signatures through PKCS#11 |
| Cloud HSMs, remote signing services | – | Not supported |

Key types tested on a (software) token: RSA 2048 (imported and generated on
the token, also with a PIN before every signature), EC P-256 and P-384,
Ed25519. Tokens that can only sign a precomputed hash (common for cards)
are detected; the hash is then computed by pdfsigner. A card with two PINs
(authentication and signature) appears as two tokens; `token_label`
chooses one.

### Troubleshooting

* **"no token found":** is the card inserted and the reader connected? On
  Linux the `pcscd` service must run. If GnuPG used the card,
  `gpgconf --kill scdaemon` releases it.
* **"N tokens found":** several cards, or one card with two PINs: set
  `token_label` (see `pdfsigner certs pkcs11`).
* **"no private key …":** keys are often only visible after the PIN:
  `pdfsigner certs pkcs11 --login` lists them with their IDs.
* **"the PIN … is locked":** unblock it with the PUK (YubiKey:
  `ykman piv access unblock-pin`; OpenSC: `pkcs15-tool --unblock-pin`).
* **Nothing happens while signing:** the card may be waiting for a touch
  (blinking YubiKey) or the PIN on the reader's keypad.
* **Windows/macOS store:** the PIN dialog comes from the operating system;
  on Windows it opens in front of PDF Signer.

### YubiKey (PIV)

Slot 9c ("Digital Signature") is meant for signing. Both modules give its
key and certificate the ID `02`, but different labels:

| Slot | ID | ykcs11 key / certificate label | OpenSC key / certificate label |
|---|---|---|---|
| 9a | `01` | Private key for PIV Authentication / X.509 Certificate for PIV Authentication | PIV AUTH key / Certificate for PIV Authentication |
| 9c | `02` | Private key for Digital Signature / X.509 Certificate for Digital Signature | SIGN key / Certificate for Digital Signature |

So the simplest profile selects the key by ID; pdfsigner then takes the
certificate with the same ID:

```bash
pdfsigner certs add yubikey --pkcs11-lib /usr/lib/x86_64-linux-gnu/libykcs11.so \
    --key-id 02 --secret prompt
```

Setting up slot 9c with [ykman](https://docs.yubico.com/software/yubikey/tools/ykman/):

```bash
# a new key on the YubiKey, and a certificate request for your CA
ykman piv keys generate --algorithm ECCP256 9c pub.pem
ykman piv certificates request --subject "CN=Alice Example" 9c pub.pem csr.pem
ykman piv certificates import 9c cert.pem        # the certificate you got back

# or: an existing .p12 (e.g. a GÉANT/DFN S/MIME certificate)
ykman piv keys import 9c alice.p12
ykman piv certificates import 9c alice.p12
```

Afterwards you can delete the `.p12`, or keep it in KeePass as a backup.
The PIN source is independent (`prompt`, `keyring`, `keepass` or
`keepassxc`). With a **card reader that has a PIN pad**, use
`secret = "none"`: pdfsigner then logs in through the reader, and you type
the PIN there. That doesn't work for keys that want the PIN before every
signature (slot 9c's default): PKCS#11 libraries can only send that PIN
themselves, so such keys need a PIN source here, or `--pin-policy once`.

Slot 9c asks for the PIN before every signature (the YubiKey's default PIN
policy for it). pdfsigner sends the PIN again for such keys, so you still
enter it once per signing. If the key was created with a touch policy
(`--touch-policy always` or `cached`), touch the YubiKey when it blinks;
without a touch the signature fails after a short timeout.

Cards lock their PIN after a few wrong attempts (a YubiKey after 3).
pdfsigner doesn't try a PIN on a locked card, says how many attempts are
left when the card reports it, and doesn't spend the last attempt on a
stored PIN (keyring, KeePass) that may be out of date: type it once with
`secret = "prompt"` instead. `pdfsigner certs pkcs11` shows the PIN state
next to each token.

If GnuPG uses the same YubiKey (OpenPGP), its `scdaemon` may hold the card
exclusively, and the PKCS#11 module then finds no token: `gpgconf --kill
scdaemon` releases it.

Install with `pip install 'pdfsigner[pkcs11]'`. The test suite signs with
SoftHSM 2 software tokens (`tests/test_pkcs11_softhsm.py`), one of them laid
out like a YubiKey with Yubico's labels; PIN pads and PIN-lock states are
tested with simulated tokens. None of this has been tried with a physical
card yet.

## 6. Windows certificate store / macOS keychain (`type = "system"`)

A certificate installed through the browser, the company's enrolment, or a
smartcard driver lives in the operating system's store. pdfsigner can sign
with it directly; the private key never leaves the store:

* **Windows:** the personal store of the current user (*certmgr.msc →
  Personal*), through CNG. Smartcards with a minidriver work too; Windows
  shows its own PIN dialog.
* **macOS:** identities in the keychain search list (login keychain) and on
  smartcards that macOS supports natively (CryptoTokenKit; this includes a
  YubiKey's PIV certificates). `certs system` marks those `[smartcard]`.
  macOS asks whether pdfsigner (Python) may use a keychain key (*Always
  Allow* stops it asking again), and for the card's PIN.

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
store (`tests/test_systemstore_native.py`), not yet with smartcards. For a
YubiKey or another PIV card, the PKCS#11 profile (section 5) is the tested
route.

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
