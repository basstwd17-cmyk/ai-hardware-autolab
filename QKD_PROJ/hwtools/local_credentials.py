"""Endpoint-bound BBB passwords in the current user's Windows Credential Manager.

No plaintext files, registry environment variables, command-line passwords, or
credential enumeration. This is local persistence, not an agent/OS sandbox.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from typing import Any, Mapping


class CredentialError(RuntimeError):
    pass


def credential_target(endpoint: Mapping[str, Any]) -> str:
    if (not isinstance(endpoint.get("host"), str) or not endpoint["host"].strip()
            or not isinstance(endpoint.get("user"), str) or not endpoint["user"].strip()
            or type(endpoint.get("port")) is not int or not 1 <= endpoint["port"] <= 65535):
        raise CredentialError("A concrete host, user and port are required for local credentials.")
    # Windows target names are case-insensitive; hash exact identities so user
    # case and delimiter characters cannot collide with another endpoint.
    identity = json.dumps([endpoint["host"], endpoint["user"], endpoint["port"]], ensure_ascii=True)
    return "RTASE/BBB/v1/" + hashlib.sha256(identity.encode()).hexdigest()


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _api():
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.POINTER(ctypes.POINTER(_Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def read_password(endpoint: Mapping[str, Any]) -> str | None:
    target = credential_target(endpoint)
    if os.name != "nt":
        return None
    api = _api()
    pointer = ctypes.POINTER(_Credential)()
    if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        code = ctypes.get_last_error()
        if code == 1168:  # ERROR_NOT_FOUND; do not mask access-denied/store errors.
            return None
        raise CredentialError(f"Windows credential lookup failed (error {code}); no device contacted.")
    try:
        item = pointer.contents
        if item.UserName != endpoint["user"] or not 0 < item.CredentialBlobSize <= 2560:
            raise CredentialError("Stored BBB credential metadata is invalid; no device contacted.")
        try:
            return ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize).decode("utf-8")
        except UnicodeDecodeError:
            raise CredentialError("Stored BBB credential encoding is invalid; no device contacted.") from None
    finally:
        api.CredFree(pointer)


def store_password(endpoint: Mapping[str, Any], password: str) -> None:
    target = credential_target(endpoint)
    if os.name != "nt":
        raise CredentialError("Local credential storage requires Windows; use BB_PW on other hosts.")
    if not isinstance(password, str) or not password.strip():
        raise CredentialError("An empty password cannot be stored.")
    encoded = password.encode("utf-8")
    if len(encoded) > 2560:
        raise CredentialError("Password exceeds Windows credential storage capacity.")
    blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
    item = _Credential(Type=1, TargetName=target, UserName=endpoint["user"],
                       Comment="RTASE BBB SSH (current user, local computer only)",
                       CredentialBlobSize=len(encoded), CredentialBlob=blob,
                       Persist=2)  # CRED_PERSIST_LOCAL_MACHINE, not enterprise roaming.
    try:
        if not _api().CredWriteW(ctypes.byref(item), 0):
            code = ctypes.get_last_error()
            raise CredentialError(f"Windows credential save failed (error {code}).")
    finally:
        ctypes.memset(blob, 0, len(encoded))


def main() -> int:
    import argparse
    import getpass
    import sys

    parser = argparse.ArgumentParser(description="Store/check local BBB credentials; never prints secrets.")
    parser.add_argument("action", choices=["set", "status"])
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--port", type=int, default=22)
    args = parser.parse_args()
    endpoint = {"host": args.host, "user": args.user, "port": args.port}
    try:
        if args.action == "set":
            if not sys.stdin.isatty():
                raise CredentialError("Use an interactive terminal for hidden password input.")
            store_password(endpoint, getpass.getpass("BBB SSH password (hidden): "))
        print("configured" if read_password(endpoint) else "not configured")
        return 0
    except CredentialError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
