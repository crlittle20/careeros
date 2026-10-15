"""Secrets live in Windows Credential Manager (generic credentials), never in files, the database or logs."""
from __future__ import annotations

import ctypes
import sys

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
MAX_BLOB = 2560


class CredentialError(RuntimeError):
    pass


class WindowsCredentials:
    def __init__(self):
        from careeros.demo.sandbox import enabled as demo_mode
        if demo_mode():
            raise CredentialError("Demo Mode never reads credentials")
        if sys.platform != "win32":
            raise CredentialError("Windows Credential Manager is only available on Windows")
        from ctypes import wintypes as wt

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR),
                        ("LastWritten", wt.FILETIME), ("CredentialBlobSize", wt.DWORD),
                        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wt.DWORD),
                        ("AttributeCount", wt.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", wt.LPWSTR),
                        ("UserName", wt.LPWSTR)]

        self._struct = CREDENTIAL
        self._api = ctypes.WinDLL("advapi32", use_last_error=True)
        self._api.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        self._api.CredReadW.restype = wt.BOOL
        self._api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wt.DWORD]
        self._api.CredWriteW.restype = wt.BOOL
        self._api.CredDeleteW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD]
        self._api.CredDeleteW.restype = wt.BOOL

    def read(self, target: str) -> str | None:
        entry = self.read_entry(target)
        return entry[1] if entry else None

    def read_entry(self, target: str) -> tuple[str, str] | None:
        """(user name, secret). Credentials saved with `cmdkey /generic:... /user:... /pass` store UTF-16 text."""
        pointer = ctypes.POINTER(self._struct)()
        if not self._api.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            return None
        try:
            blob = ctypes.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize)
            user = pointer.contents.UserName or ""
        finally:
            self._api.CredFree(pointer)
        return user, (blob.decode("utf-16-le") if b"\x00" in blob else blob.decode("utf-8"))

    def write(self, target: str, secret: str, user: str = "CareerOS") -> None:
        data = secret.encode("utf-8")
        if len(data) > MAX_BLOB:
            raise CredentialError("secret too large for Credential Manager")
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        cred = self._struct(Type=CRED_TYPE_GENERIC, TargetName=target, CredentialBlobSize=len(data),
                            CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
                            Persist=CRED_PERSIST_LOCAL_MACHINE, UserName=user)
        if not self._api.CredWriteW(ctypes.byref(cred), 0):
            raise CredentialError(f"could not save credential (error {ctypes.get_last_error()})")

    def delete(self, target: str) -> bool:
        return bool(self._api.CredDeleteW(target, CRED_TYPE_GENERIC, 0))


class MemoryCredentials:
    """For tests."""

    def __init__(self):
        self.items: dict[str, str] = {}
        self.users: dict[str, str] = {}

    def read(self, target: str) -> str | None:
        return self.items.get(target)

    def read_entry(self, target: str) -> tuple[str, str] | None:
        return (self.users.get(target, ""), self.items[target]) if target in self.items else None

    def write(self, target: str, secret: str, user: str = "CareerOS") -> None:
        self.items[target], self.users[target] = secret, user

    def delete(self, target: str) -> bool:
        return self.items.pop(target, None) is not None
