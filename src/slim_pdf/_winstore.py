"""Windows backend for systemstore: current user's "My" store via CNG (ctypes)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt

from asn1crypto import x509

from .i18n import _
from .signing import SigningError
from . import systemstore
from .systemstore import StoreCancelled, StoreIdentity, p1363_to_der

crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
ncrypt = ctypes.WinDLL("ncrypt")

CERT_KEY_PROV_INFO_PROP_ID = 2
CRYPT_ACQUIRE_COMPARE_KEY_FLAG = 0x4
CRYPT_ACQUIRE_ONLY_NCRYPT_KEY_FLAG = 0x40000
BCRYPT_PAD_PKCS1 = 0x2
NTE_USER_CANCELLED = 0x80090036
SCARD_W_CANCELLED_BY_USER = 0x8010006E
ERROR_CANCELLED = 0x800704C7
CANCEL_CODES = {NTE_USER_CANCELLED, SCARD_W_CANCELLED_BY_USER, ERROR_CANCELLED}
CNG_HASH_IDS = {"sha1": "SHA1", "sha256": "SHA256", "sha384": "SHA384", "sha512": "SHA512"}


class CERT_CONTEXT(ctypes.Structure):
    _fields_ = [
        ("dwCertEncodingType", wt.DWORD),
        ("pbCertEncoded", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbCertEncoded", wt.DWORD),
        ("pCertInfo", ctypes.c_void_p),
        ("hCertStore", ctypes.c_void_p),
    ]


PCCERT_CONTEXT = ctypes.POINTER(CERT_CONTEXT)


class BCRYPT_PKCS1_PADDING_INFO(ctypes.Structure):
    _fields_ = [("pszAlgId", wt.LPCWSTR)]


crypt32.CertOpenSystemStoreW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
crypt32.CertOpenSystemStoreW.restype = ctypes.c_void_p
crypt32.CertCloseStore.argtypes = [ctypes.c_void_p, wt.DWORD]
crypt32.CertCloseStore.restype = wt.BOOL
crypt32.CertEnumCertificatesInStore.argtypes = [ctypes.c_void_p, PCCERT_CONTEXT]
crypt32.CertEnumCertificatesInStore.restype = PCCERT_CONTEXT
crypt32.CertDuplicateCertificateContext.argtypes = [PCCERT_CONTEXT]
crypt32.CertDuplicateCertificateContext.restype = PCCERT_CONTEXT
crypt32.CertFreeCertificateContext.argtypes = [PCCERT_CONTEXT]
crypt32.CertFreeCertificateContext.restype = wt.BOOL
crypt32.CertGetCertificateContextProperty.argtypes = [
    PCCERT_CONTEXT, wt.DWORD, ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
crypt32.CertGetCertificateContextProperty.restype = wt.BOOL
crypt32.CryptAcquireCertificatePrivateKey.argtypes = [
    PCCERT_CONTEXT, wt.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
    ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.BOOL)]
crypt32.CryptAcquireCertificatePrivateKey.restype = wt.BOOL
ncrypt.NCryptSignHash.argtypes = [
    ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
    ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.DWORD]
ncrypt.NCryptSignHash.restype = ctypes.c_long
ncrypt.NCryptFreeObject.argtypes = [ctypes.c_size_t]
ncrypt.NCryptFreeObject.restype = ctypes.c_long
ncrypt.NCryptSetProperty.argtypes = [
    ctypes.c_size_t, wt.LPCWSTR, ctypes.c_void_p, wt.DWORD, wt.DWORD]
ncrypt.NCryptSetProperty.restype = ctypes.c_long
NCRYPT_WINDOW_HANDLE_PROPERTY = "HWND Handle"


def _message(code: int) -> str:
    # FormatError takes a C int: HRESULTs >= 0x80000000 must be passed as negatives
    return ctypes.FormatError(code - 0x100000000 if code >= 0x80000000 else code).strip()


def _check(status: int, what: str) -> None:
    """``what`` is an already translated failure message with an {error} placeholder."""
    code = status & 0xFFFFFFFF
    if code == 0:
        return
    if code in CANCEL_CODES:
        raise StoreCancelled(_("cancelled in the Windows PIN/consent dialog"))
    raise SigningError(what.format(error=f"{_message(code)} (0x{code:08X})"))


class WindowsStore:
    @property
    def name(self) -> str:
        return _("Windows certificate store (CurrentUser\\My)")

    def __init__(self):
        self._store = crypt32.CertOpenSystemStoreW(None, "MY")
        if not self._store:
            raise SigningError(_("cannot open the certificate store: {error}").format(
                error=_message(ctypes.get_last_error())))
        self._contexts: list = []

    def identities(self) -> list[StoreIdentity]:
        result = []
        ctx = crypt32.CertEnumCertificatesInStore(self._store, None)
        while ctx:
            size = wt.DWORD(0)
            has_key = crypt32.CertGetCertificateContextProperty(
                ctx, CERT_KEY_PROV_INFO_PROP_ID, None, ctypes.byref(size))
            if has_key:
                der = ctypes.string_at(ctx.contents.pbCertEncoded, ctx.contents.cbCertEncoded)
                try:
                    cert = x509.Certificate.load(der)
                    _ = cert.native  # parse fully now; skip what asn1crypto can't read
                except ValueError:
                    cert = None
                if cert is not None:
                    dup = crypt32.CertDuplicateCertificateContext(ctx)
                    self._contexts.append(dup)
                    result.append(StoreIdentity(cert=cert, handle=dup))
            ctx = crypt32.CertEnumCertificatesInStore(self._store, ctx)  # frees ctx
        return result

    def sign_digest(self, ident: StoreIdentity, digest: bytes, digest_algorithm: str,
                    mechanism: str) -> bytes:
        key = ctypes.c_size_t(0)
        spec = wt.DWORD(0)
        must_free = wt.BOOL(False)
        ok = crypt32.CryptAcquireCertificatePrivateKey(
            ident.handle, CRYPT_ACQUIRE_ONLY_NCRYPT_KEY_FLAG | CRYPT_ACQUIRE_COMPARE_KEY_FLAG,
            None, ctypes.byref(key), ctypes.byref(spec), ctypes.byref(must_free))
        if not ok:
            err = ctypes.get_last_error()
            _check(err | 0x80070000 if err and err < 0x10000 else err,
                   _("accessing the private key failed: {error}"))
            raise SigningError(_("cannot access the private key"))
        try:
            if systemstore.parent_window:
                # The smartcard PIN dialog belongs to our window, so it opens
                # in front of it. Best effort: not every key provider takes it.
                hwnd = ctypes.c_void_p(systemstore.parent_window)
                ncrypt.NCryptSetProperty(key, NCRYPT_WINDOW_HANDLE_PROPERTY,
                                         ctypes.byref(hwnd), ctypes.sizeof(hwnd), 0)
            pad, flags = None, 0
            if mechanism == "rsassa_pkcs1v15":
                try:
                    pad = BCRYPT_PKCS1_PADDING_INFO(CNG_HASH_IDS[digest_algorithm])
                except KeyError:
                    raise SigningError(_("unsupported digest {digest}").format(
                        digest=digest_algorithm)) from None
                flags = BCRYPT_PAD_PKCS1
            pad_ptr = ctypes.byref(pad) if pad is not None else None
            hash_buf = (ctypes.c_ubyte * len(digest)).from_buffer_copy(digest)
            size = wt.DWORD(0)
            _check(ncrypt.NCryptSignHash(key, pad_ptr, hash_buf, len(digest), None, 0,
                                         ctypes.byref(size), flags),
                   _("signing failed: {error}"))
            sig = (ctypes.c_ubyte * size.value)()
            _check(ncrypt.NCryptSignHash(key, pad_ptr, hash_buf, len(digest), sig, size,
                                         ctypes.byref(size), flags),
                   _("signing failed: {error}"))
            raw = bytes(sig[: size.value])
        finally:
            if must_free:
                ncrypt.NCryptFreeObject(key)
        return p1363_to_der(raw) if mechanism == "ecdsa" else raw

    def close(self) -> None:
        for ctx in self._contexts:
            crypt32.CertFreeCertificateContext(ctx)
        self._contexts = []
        if self._store:
            crypt32.CertCloseStore(self._store, 0)
            self._store = None
