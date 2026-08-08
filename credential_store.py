"""Store the API key in Windows Credential Manager, never in state.json."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os

from provider_env import api_key as _provider_api_key
from provider_env import explicit_api_key as _explicit_provider_api_key


TARGET = "DeskOrbAgent/OpenAIAPIKey"
LEGACY_TARGET = "CodexOverlay/OpenAIAPIKey"
PROVIDER_TARGETS = {
    "openai": TARGET,
    "responses": TARGET,
    "openai-compatible": "DeskOrbAgent/OpenAICompatibleAPIKey",
    "deepseek": "DeskOrbAgent/DeepSeekAPIKey",
    "qwen": "DeskOrbAgent/QwenAPIKey",
    "dashscope": "DeskOrbAgent/QwenAPIKey",
}


def _credential_target(provider: str | None = None) -> str:
    name = str(provider or "").strip().lower().replace("_", "-")
    return PROVIDER_TARGETS.get(name, TARGET)


def _environment_api_key(provider: str | None = None) -> str:
    name = str(provider or "").strip().lower().replace("_", "-")
    provider_keys = {
        "openai": ("OPENAI_API_KEY",),
        "responses": ("OPENAI_API_KEY",),
        "openai-compatible": ("OPENAI_API_KEY",),
        "deepseek": ("DEEPSEEK_API_KEY",),
        "qwen": ("QWEN_API_KEY", "DASHSCOPE_API_KEY"),
        "dashscope": ("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
    }
    keys = provider_keys.get(name, (
        "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "QWEN_API_KEY",
    ))
    return next((os.environ.get(key, "").strip() for key in keys
                 if os.environ.get(key, "").strip()), "")


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


def get_api_key(provider: str | None = None) -> str:
    provider_name = str(provider or "").strip().lower()
    env_key = _environment_api_key(provider_name or None)
    if env_key:
        return env_key
    if os.name != "nt":
        return _provider_api_key(provider_name or None)
    pointer = ctypes.POINTER(CREDENTIALW)()
    target = _credential_target(provider_name or None)
    found = bool(_advapi32.CredReadW(target, 1, 0, ctypes.byref(pointer)))
    if not found and provider_name not in {"deepseek", "qwen", "dashscope"}:
        found = bool(_advapi32.CredReadW(LEGACY_TARGET if target == TARGET else TARGET,
                                         1, 0, ctypes.byref(pointer)))
    if not found:
        return _provider_api_key(provider_name or None)
    try:
        cred = pointer.contents
        if not cred.CredentialBlob or not cred.CredentialBlobSize:
            return _provider_api_key(provider_name or None)
        raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return raw.decode("utf-16-le").strip("\x00").strip() or _provider_api_key(provider_name or None)
    finally:
        _advapi32.CredFree(pointer)


def get_explicit_provider_api_key(provider: str | None = None) -> str:
    value = _explicit_provider_api_key(provider)
    if value or os.name != "nt":
        return value
    pointer = ctypes.POINTER(CREDENTIALW)()
    if not _advapi32.CredReadW(_credential_target(provider), 1, 0, ctypes.byref(pointer)):
        return ""
    try:
        cred = pointer.contents
        if not cred.CredentialBlob or not cred.CredentialBlobSize:
            return ""
        raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return raw.decode("utf-16-le").strip("\x00").strip()
    finally:
        _advapi32.CredFree(pointer)


def set_api_key(value: str, provider: str | None = None) -> None:
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
    cred.TargetName = _credential_target(provider)
    cred.CredentialBlobSize = len(raw)
    cred.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_byte))
    cred.Persist = 2                 # CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = "DeskOrb Agent"
    if not _advapi32.CredWriteW(ctypes.byref(cred), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def delete_api_key(provider: str | None = None) -> None:
    if os.name != "nt":
        return
    target = _credential_target(provider)
    if not _advapi32.CredDeleteW(target, 1, 0):
        error = ctypes.get_last_error()
    if error != 1168:            # ERROR_NOT_FOUND
            raise ctypes.WinError(error)
    if target == TARGET and not _advapi32.CredDeleteW(LEGACY_TARGET, 1, 0):
        error = ctypes.get_last_error()
        if error != 1168:
            raise ctypes.WinError(error)


def has_api_key(provider: str | None = None) -> bool:
    return bool(get_api_key(provider))
