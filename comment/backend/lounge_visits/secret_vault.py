"""Windows account-bound encryption. Never fall back to plaintext on failure."""
import base64
import ctypes
from ctypes import wintypes
import os

PREFIX = 'dpapi:v1:'


def _crypt(data: bytes, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise RuntimeError('当前凭据保险箱需要 Windows DPAPI')
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError('本机凭据保护失败')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(target.data)


def protect(text):
    return PREFIX + base64.b64encode(_crypt(text.encode('utf-8'))).decode('ascii')


def unprotect(value):
    if not value.startswith(PREFIX):
        return value  # Existing records remain readable; reads never migrate files.
    try:
        return _crypt(base64.b64decode(value[len(PREFIX):], validate=True), True).decode('utf-8')
    except Exception:
        raise RuntimeError('凭据无法由当前 Windows 账户解锁') from None
