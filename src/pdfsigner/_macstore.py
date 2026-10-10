"""macOS backend for systemstore: keychain identities via Security.framework (ctypes)."""

from __future__ import annotations

import ctypes
from ctypes import c_char_p, c_int32, c_long, c_void_p

from asn1crypto import x509

from .signing import SigningError
from .systemstore import StoreCancelled, StoreIdentity

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
    return buf.value.decode() if ok else "unknown error"


class MacKeychain:
    name = "macOS keychain"

    def __init__(self):
        self._array = None

    def identities(self) -> list[StoreIdentity]:
        keys = (c_void_p * 3)(_const(sec, "kSecClass"), _const(sec, "kSecMatchLimit"),
                              _const(sec, "kSecReturnRef"))
        values = (c_void_p * 3)(_const(sec, "kSecClassIdentity"),
                                _const(sec, "kSecMatchLimitAll"),
                                _const(cf, "kCFBooleanTrue"))
        query = cf.CFDictionaryCreate(None, keys, values, 3,
                                      _address(cf, "kCFTypeDictionaryKeyCallBacks"),
                                      _address(cf, "kCFTypeDictionaryValueCallBacks"))
        result = c_void_p()
        status = sec.SecItemCopyMatching(query, ctypes.byref(result))
        cf.CFRelease(query)
        if status == errSecItemNotFound:
            return []
        if status != errSecSuccess:
            raise SigningError(f"keychain search failed (OSStatus {status})")
        self._array = result.value  # keeps the identities alive until close()

        identities = []
        for i in range(cf.CFArrayGetCount(self._array)):
            ident = cf.CFArrayGetValueAtIndex(self._array, i)
            certref = c_void_p()
            if sec.SecIdentityCopyCertificate(ident, ctypes.byref(certref)) != errSecSuccess:
                continue
            data = sec.SecCertificateCopyData(certref)
            der = _cfdata_bytes(data)
            cf.CFRelease(data)
            cf.CFRelease(certref)
            try:
                cert = x509.Certificate.load(der)
                _ = cert.native  # parse fully now
            except ValueError:
                continue
            identities.append(StoreIdentity(cert=cert, handle=ident))
        return identities

    def sign_digest(self, ident: StoreIdentity, digest: bytes, digest_algorithm: str,
                    mechanism: str) -> bytes:
        try:
            algorithm = _const(sec, SIGN_ALGORITHMS[(mechanism, digest_algorithm)])
        except KeyError:
            raise SigningError(f"unsupported algorithm {mechanism}/{digest_algorithm}") from None
        key = c_void_p()
        status = sec.SecIdentityCopyPrivateKey(ident.handle, ctypes.byref(key))
        if status != errSecSuccess:
            raise SigningError(f"cannot access the private key (OSStatus {status})")
        data = cf.CFDataCreate(None, digest, len(digest))
        err = c_void_p()
        try:
            sig = sec.SecKeyCreateSignature(key, algorithm, data, ctypes.byref(err))
            if not sig:
                code = cf.CFErrorGetCode(err) if err.value else 0
                msg = _cferror(err) if err.value else "unknown error"
                if err.value:
                    cf.CFRelease(err)
                if code == errSecUserCanceled:
                    raise StoreCancelled("cancelled in the keychain dialog")
                raise SigningError(f"keychain signing failed: {msg}")
            out = _cfdata_bytes(sig)  # RSA: PKCS#1 bytes; ECDSA: DER (X9.62)
            cf.CFRelease(sig)
            return out
        finally:
            cf.CFRelease(data)
            cf.CFRelease(key)

    def close(self) -> None:
        if self._array:
            cf.CFRelease(self._array)
            self._array = None
