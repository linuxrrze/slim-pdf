"""macOS backend for systemstore: keychain identities via Security.framework (ctypes)."""

from __future__ import annotations

import ctypes
import logging
from ctypes import c_char_p, c_int32, c_long, c_void_p

from asn1crypto import x509

from .i18n import _
from .signing import SigningError
from .systemstore import StoreCancelled, StoreIdentity

log = logging.getLogger(__name__)

cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
sec = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")

errSecSuccess = 0
errSecItemNotFound = -25300
errSecUserCanceled = -128
kCFStringEncodingUTF8 = 0x08000100

cf.CFRelease.argtypes = [c_void_p]
cf.CFDictionaryCreate.argtypes = [c_void_p, ctypes.POINTER(c_void_p), ctypes.POINTER(c_void_p),
                                  c_long, c_void_p, c_void_p]
cf.CFDictionaryCreate.restype = c_void_p
cf.CFArrayGetCount.argtypes = [c_void_p]
cf.CFArrayGetCount.restype = c_long
cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_long]
cf.CFArrayGetValueAtIndex.restype = c_void_p
cf.CFDataCreate.argtypes = [c_void_p, c_char_p, c_long]
cf.CFDataCreate.restype = c_void_p
cf.CFDataGetLength.argtypes = [c_void_p]
cf.CFDataGetLength.restype = c_long
cf.CFDataGetBytePtr.argtypes = [c_void_p]
cf.CFDataGetBytePtr.restype = c_void_p
cf.CFErrorGetCode.argtypes = [c_void_p]
cf.CFErrorGetCode.restype = c_long
cf.CFErrorCopyDescription.argtypes = [c_void_p]
cf.CFErrorCopyDescription.restype = c_void_p
cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, ctypes.c_uint32]
cf.CFStringGetCString.restype = ctypes.c_bool

sec.SecItemCopyMatching.argtypes = [c_void_p, ctypes.POINTER(c_void_p)]
sec.SecItemCopyMatching.restype = c_int32
sec.SecIdentityCopyCertificate.argtypes = [c_void_p, ctypes.POINTER(c_void_p)]
sec.SecIdentityCopyCertificate.restype = c_int32
sec.SecIdentityCopyPrivateKey.argtypes = [c_void_p, ctypes.POINTER(c_void_p)]
sec.SecIdentityCopyPrivateKey.restype = c_int32
sec.SecCertificateCopyData.argtypes = [c_void_p]
sec.SecCertificateCopyData.restype = c_void_p
sec.SecKeyCreateSignature.argtypes = [c_void_p, c_void_p, c_void_p, ctypes.POINTER(c_void_p)]
sec.SecKeyCreateSignature.restype = c_void_p


def _const(lib, name: str) -> int:
    """Value of an exported ``const CFStringRef`` (or other CF object) global."""
    return c_void_p.in_dll(lib, name).value


def _address(lib, name: str) -> int:
    """Address of an exported struct (e.g. the dictionary callbacks)."""
    return ctypes.addressof(ctypes.c_byte.in_dll(lib, name))


SIGN_ALGORITHMS = {
    ("rsassa_pkcs1v15", d): f"kSecKeyAlgorithmRSASignatureDigestPKCS1v15{d.upper()}"
    for d in ("sha1", "sha256", "sha384", "sha512")
} | {
    ("ecdsa", d): f"kSecKeyAlgorithmECDSASignatureDigestX962{d.upper()}"
    for d in ("sha1", "sha256", "sha384", "sha512")
}


def _cfdata_bytes(data: int) -> bytes:
    return ctypes.string_at(cf.CFDataGetBytePtr(data), cf.CFDataGetLength(data))


def _cferror(err: int) -> str:
    desc = cf.CFErrorCopyDescription(err)
    buf = ctypes.create_string_buffer(1024)
    ok = cf.CFStringGetCString(desc, buf, len(buf), kCFStringEncodingUTF8)
    cf.CFRelease(desc)
    return buf.value.decode() if ok else _("unknown error")


class MacKeychain:
    @property
    def name(self) -> str:
        return _("macOS keychain")

    def __init__(self):
        self._arrays: list[int] = []

    def _search(self, token: bool) -> int | None:
        """CFArray of identities, or None. Smartcard identities (CryptoTokenKit)
        are only returned by a query for the token access group."""
        keys = [_const(sec, "kSecClass"), _const(sec, "kSecMatchLimit"),
                _const(sec, "kSecReturnRef")]
        values = [_const(sec, "kSecClassIdentity"), _const(sec, "kSecMatchLimitAll"),
                  _const(cf, "kCFBooleanTrue")]
        if token:
            keys.append(_const(sec, "kSecAttrAccessGroup"))
            values.append(_const(sec, "kSecAttrAccessGroupToken"))
        n = len(keys)
        query = cf.CFDictionaryCreate(None, (c_void_p * n)(*keys), (c_void_p * n)(*values), n,
                                      _address(cf, "kCFTypeDictionaryKeyCallBacks"),
                                      _address(cf, "kCFTypeDictionaryValueCallBacks"))
        result = c_void_p()
        status = sec.SecItemCopyMatching(query, ctypes.byref(result))
        cf.CFRelease(query)
        if status == errSecItemNotFound:
            return None
        if status != errSecSuccess:
            if token:  # e.g. no smartcard support; the keychain itself still works
                log.info("keychain: smartcard search failed (OSStatus %s)", status)
                return None
            raise SigningError(
                _("keychain search failed (OSStatus {status})").format(status=status))
        self._arrays.append(result.value)  # keeps the identities alive until close()
        return result.value

    def identities(self) -> list[StoreIdentity]:
        identities: list[StoreIdentity] = []
        seen: set[bytes] = set()
        for token in (False, True):
            array = self._search(token)
            if array is None:
                continue
            for i in range(cf.CFArrayGetCount(array)):
                ident = cf.CFArrayGetValueAtIndex(array, i)
                certref = c_void_p()
                if sec.SecIdentityCopyCertificate(ident, ctypes.byref(certref)) != errSecSuccess:
                    continue
                data = sec.SecCertificateCopyData(certref)
                der = _cfdata_bytes(data)
                cf.CFRelease(data)
                cf.CFRelease(certref)
                if der in seen:
                    continue
                seen.add(der)
                try:
                    cert = x509.Certificate.load(der)
                    _ = cert.native  # parse fully now
                except ValueError:
                    continue
                identities.append(StoreIdentity(cert=cert, handle=ident, on_token=token))
        return identities

    def sign_digest(self, ident: StoreIdentity, digest: bytes, digest_algorithm: str,
                    mechanism: str) -> bytes:
        try:
            algorithm = _const(sec, SIGN_ALGORITHMS[(mechanism, digest_algorithm)])
        except KeyError:
            raise SigningError(_("unsupported algorithm {mechanism}/{digest}").format(
                mechanism=mechanism, digest=digest_algorithm)) from None
        key = c_void_p()
        status = sec.SecIdentityCopyPrivateKey(ident.handle, ctypes.byref(key))
        if status != errSecSuccess:
            raise SigningError(
                _("cannot access the private key (OSStatus {status})").format(status=status))
        data = cf.CFDataCreate(None, digest, len(digest))
        err = c_void_p()
        try:
            sig = sec.SecKeyCreateSignature(key, algorithm, data, ctypes.byref(err))
            if not sig:
                code = cf.CFErrorGetCode(err) if err.value else 0
                msg = _cferror(err) if err.value else _("unknown error")
                if err.value:
                    cf.CFRelease(err)
                if code == errSecUserCanceled:
                    raise StoreCancelled(_("cancelled in the keychain dialog"))
                raise SigningError(_("keychain signing failed: {error}").format(error=msg))
            out = _cfdata_bytes(sig)  # RSA: PKCS#1 bytes; ECDSA: DER (X9.62)
            cf.CFRelease(sig)
            return out
        finally:
            cf.CFRelease(data)
            cf.CFRelease(key)

    def close(self) -> None:
        for array in self._arrays:
            cf.CFRelease(array)
        self._arrays = []
