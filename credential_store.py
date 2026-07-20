"""Store the API key in Windows Credential Manager, never in state.json."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os

from provider_env import api_key as _provider_api_key


TARGET = "DeskOrbAgent/OpenAIAPIKey"
LEGACY_TARGET = "CodexOverlay/OpenAIAPIKey"


if os.name == "nt":
    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
            ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME),
            ("CredentialBlobSize", wt.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
            ("Attributes", ctypes.c_void_p), ("TargetAlias", wt.LPWSTR),
            ("UserName", wt.LPWSTR),
        ]

    _advapi32 = ctypes.WinDLL("Advapi32", use_last_error=True)
    _advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wt.DWORD]
    _advapi32.CredWriteW.restype = wt.BOOL
    _advapi32.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD,
                                    ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    _advapi32.CredReadW.restype = wt.BOOL
    _advapi32.CredDeleteW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD]
    _advapi32.CredDeleteW.restype = wt.BOOL
    _advapi32.CredFree.argtypes = [ctypes.c_void_p]


def get_api_key() -> str:
    env_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if env_key:
        return env_key
    if os.name != "nt":
        return _provider_api_key()
    pointer = ctypes.POINTER(CREDENTIALW)()
    if not _advapi32.CredReadW(TARGET, 1, 0, ctypes.byref(pointer)):
        if not _advapi32.CredReadW(LEGACY_TARGET, 1, 0, ctypes.byref(pointer)):
            return _provider_api_key()
    try:
        cred = pointer.contents
        if not cred.CredentialBlob or not cred.CredentialBlobSize:
            return _provider_api_key()
        raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return raw.decode("utf-16-le").strip("\x00").strip() or _provider_api_key()
    finally:
        _advapi32.CredFree(pointer)


def set_api_key(value: str) -> None:
    value = str(value or "").strip()
    if not value:
        delete_api_key()
        return
    if os.name != "nt":
        raise RuntimeError("Persistent API-key storage currently requires Windows")
    raw = value.encode("utf-16-le")
    blob = (ctypes.c_byte * len(raw)).from_buffer_copy(raw)
    cred = CREDENTIALW()
    cred.Type = 1                    # CRED_TYPE_GENERIC
    cred.TargetName = TARGET
    cred.CredentialBlobSize = len(raw)
    cred.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_byte))
    cred.Persist = 2                 # CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = "DeskOrb Agent"
    if not _advapi32.CredWriteW(ctypes.byref(cred), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def delete_api_key() -> None:
    if os.name != "nt":
        return
    if not _advapi32.CredDeleteW(TARGET, 1, 0):
        error = ctypes.get_last_error()
        if error != 1168:            # ERROR_NOT_FOUND
            raise ctypes.WinError(error)


def has_api_key() -> bool:
    return bool(get_api_key())
