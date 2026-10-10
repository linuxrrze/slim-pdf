"""Sign with a key from the operating system's certificate store.

* **Windows:** the current user's personal store ("Cert:\\CurrentUser\\My"),
  through CNG (``NCryptSignHash``). Covers software keys and smartcards with
  a CNG/minidriver, which also asks for the PIN.
* **macOS:** identities in the keychain search list (login keychain) and
  on smartcards (CryptoTokenKit, e.g. a YubiKey's PIV certificates), through
  ``SecKeyCreateSignature``.

The private key never leaves the store: pyHanko hands us the data to sign,
we hash it and ask the OS to sign the digest. A profile selects the
certificate by SHA-1 thumbprint or by part of its subject
(``system_cert``); ``pdfsigner certs system`` lists the candidates.

RSA (PKCS#1 v1.5) and ECDSA keys are supported.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from typing import Any, Protocol

from asn1crypto import algos, x509
from pyhanko.sign import signers
from pyhanko_certvalidator.registry import SimpleCertificateStore

from .signing import SigningError


class StoreCancelled(SigningError):
    """The user dismissed the OS's PIN or access dialog."""


@dataclass
class StoreIdentity:
    cert: x509.Certificate
    handle: Any  # backend-specific
    on_token: bool = False  # smartcard / hardware token (macOS: CryptoTokenKit)

    @property
    def thumbprint(self) -> str:
        return hashlib.sha1(self.cert.dump()).hexdigest().upper()

    @property
    def subject(self) -> str:
        return self.cert.subject.human_friendly

    def describe(self) -> str:
        return (f"{self.thumbprint}  {self.subject}  "
                f"(valid until {self.cert.not_valid_after:%Y-%m-%d})"
                + ("  [smartcard]" if self.on_token else ""))


class Backend(Protocol):
    name: str

    def identities(self) -> list[StoreIdentity]: ...

    def sign_digest(self, ident: StoreIdentity, digest: bytes, digest_algorithm: str,
                    mechanism: str) -> bytes:
        """Signature over ``digest``: PKCS#1 v1.5 bytes for RSA, DER for ECDSA."""

    def close(self) -> None: ...


def get_backend() -> Backend:
    if sys.platform == "win32":
        from ._winstore import WindowsStore

        return WindowsStore()
    if sys.platform == "darwin":
        from ._macstore import MacKeychain

        return MacKeychain()
    raise SigningError("the system certificate store is only supported on Windows and macOS")


def _norm_thumbprint(s: str) -> str:
    return "".join(c for c in s.upper() if c in "0123456789ABCDEF")


def select(identities: list[StoreIdentity], spec: str) -> StoreIdentity:
    """Pick by SHA-1 thumbprint (spaces/colons allowed) or subject substring."""
    if not spec.strip():
        raise SigningError("no certificate selected (system_cert is empty)")
    tp = _norm_thumbprint(spec)
    if len(tp) == 40:
        for ident in identities:
            if ident.thumbprint == tp:
                return ident
    matches = [i for i in identities if spec.lower() in i.subject.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SigningError(f"no certificate with a private key matches {spec!r} "
                           "(see: pdfsigner certs system)")
    listing = "\n  ".join(i.describe() for i in matches)
    raise SigningError(f"{spec!r} matches several certificates; use the thumbprint:\n  {listing}")


def p1363_to_der(raw: bytes) -> bytes:
    """ECDSA signature r||s (as CNG returns it) -> DER, as CMS expects."""
    return algos.DSASignature.from_p1363(raw).dump()


def _placeholder_length(cert: x509.Certificate) -> int:
    """Upper bound on the signature size, for pyHanko's dry run."""
    key = cert.public_key
    if key.algorithm == "rsa":
        return (key.bit_size + 7) // 8
    coord = (key.bit_size + 7) // 8
    return 2 * coord + 9  # DER SEQUENCE of two INTEGERs, with sign bytes


class SystemStoreSigner(signers.Signer):
    def __init__(self, backend: Backend, ident: StoreIdentity):
        super().__init__(signing_cert=ident.cert,
                         cert_registry=SimpleCertificateStore.from_certs([ident.cert]))
        self.backend = backend
        self.ident = ident

    async def async_sign_raw(self, data: bytes, digest_algorithm: str, dry_run=False) -> bytes:
        mechanism = self.get_signature_mechanism_for_digest(digest_algorithm).signature_algo
        if mechanism not in ("rsassa_pkcs1v15", "ecdsa"):
            raise SigningError(f"unsupported signature mechanism {mechanism!r} for the "
                               "system store (RSA PKCS#1 v1.5 and ECDSA are supported)")
        if dry_run:
            return bytes(_placeholder_length(self.ident.cert))
        digest = hashlib.new(digest_algorithm, data).digest()
        return self.backend.sign_digest(self.ident, digest, digest_algorithm, mechanism)
