"""The process that hosts the daemon: one instance, a timer, and a pipe.

Three jobs, and each one exists because the alternative loses something.

**One instance.** Two daemons on one ledger would both pick the same runnable
job and both call ``advance()`` on it. SQLite would keep the *writes*
consistent -- the handoff uniqueness constraint would refuse the second -- but
both would have already spent a turn on the runtime to find that out. So the
lock is an OS file lock held for the process lifetime, not a pid file: a pid
file outlives the crash that made it stale, and a lock does not.

**A timer, not a spin.** The scheduler already knows how long every waiting job
has to wait, so the sleep is derived from that rather than fixed.

**A pipe, and an ACL on it.** The pipe carries two protocols: the office UI's
persistent subscribe/replay stream (``wire.py``) and the one-shot command API a
CLI uses (``control.py``). Both are newline-delimited JSON, told apart by
whether the frame has a ``type`` or a ``command``.

The security boundary is explicit rather than inherited. An earlier version
created the pipe with a null ``lpSecurityAttributes`` and the docs claimed
"reachable by the user who owns it", which was not something that code
established: the default DACL for a pipe grants the creating user, plus
LocalSystem and administrators, and says nothing at all about remote clients.
Now the pipe is created with a DACL naming this account's user SID and this
session's logon SID and nothing else, with ``PIPE_REJECT_REMOTE_CLIENTS`` so a
share cannot reach it, and with
``FILE_FLAG_FIRST_PIPE_INSTANCE`` on the first instance so another process
cannot have got there first and be collecting approvals under our name.

Nothing here decides anything about a job. Every decision is in the ledger,
which is why stopping this process at any moment is safe.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from QKD_PROJ.knowledge.models import KnowledgeScope
from QKD_PROJ.capabilities.catalog import CapabilityCatalog
from QKD_PROJ.boards.profiles import BoardCatalog
from QKD_PROJ.knowledge.store import KnowledgeStore
from QKD_PROJ.hwtools.result import utc_now
from QKD_PROJ.orchestrator import daemon as D
from QKD_PROJ.orchestrator.campaign import CampaignController
from QKD_PROJ.orchestrator import scenario_preflight as P
from QKD_PROJ.orchestrator import wire as W
from QKD_PROJ.orchestrator.control import ControlPlane
from QKD_PROJ.orchestrator.jobs import JobLedger

IS_WINDOWS = sys.platform == "win32"
DEFAULT_PIPE = r"\\.\pipe\qkd-agent-platform"
DEFAULT_INTERVAL_S = 5.0
MAX_SLEEP_S = 30.0


class ServiceError(RuntimeError):
    """The service will not start, and why."""


# -- single instance ---------------------------------------------------------


class InstanceLock:
    """An OS lock on a file, held for as long as the process runs.

    Deliberately not a pid file. A pid file survives the crash that made it
    stale, so the next start either refuses for no reason or has to guess
    whether a pid belongs to the daemon or to whatever reused the number. A
    lock is released by the kernel when the holder dies.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if IS_WINDOWS:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise ServiceError(
                f"another daemon already holds {self.path}. Two daemons on one ledger would "
                "both pick the same job and both spend a turn discovering it."
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()} {utc_now()}\n".encode("utf-8"))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        with suppress(OSError):
            if IS_WINDOWS:
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        with suppress(OSError):
            self._handle.close()
        self._handle = None

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


# -- one connection ----------------------------------------------------------


class Channel(Protocol):
    """A byte stream to one client."""

    def read_line(self) -> bytes: ...

    def write(self, payload: bytes) -> None: ...

    def close(self) -> None: ...


class Connection:
    """One client: reads frames, answers them, and pushes new events.

    A UI holds this open for as long as the office is on screen, so reading and
    pushing happen on different threads and share one write lock. Without the
    lock two half-frames could interleave on the wire and the office's line
    parser would stop at the first bad line -- which the contract says it must.
    """

    def __init__(
        self,
        channel: Channel,
        ledger: JobLedger,
        control: ControlPlane,
        stopping: threading.Event,
        poll_s: float = W.DEFAULT_POLL_S,
        runs_dir: Path | None = None,
        project_root: Path | None = None,
        runtime_profile: str | None = None,
        workspace_mode: str | None = None,
    ) -> None:
        self.channel = channel
        self.stopping = stopping
        self.poll_s = poll_s
        self._write_lock = threading.Lock()
        self._closed = threading.Event()
        # Kept rather than swallowed. A pusher that fails every cycle inside a
        # bare `except: pass` is a connection that looks alive and delivers
        # nothing, which is exactly the failure a UI cannot diagnose.
        self.push_errors: list[str] = []
        self.session = W.WireSession(
            ledger,
            control,
            self._send,
            poll_s=poll_s,
            runs_dir=runs_dir,
            project_root=project_root,
            runtime_profile=runtime_profile,
            workspace_mode=workspace_mode,
        )
        self.control = control

    def _send(self, frame: dict[str, Any]) -> None:
        payload = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            if self._closed.is_set():
                return
            try:
                self.channel.write(payload)
            except OSError:
                self._closed.set()

    def serve(self) -> None:
        pusher = threading.Thread(target=self._push_loop, name="rtase-push", daemon=True)
        pusher.start()
        try:
            self._read_loop()
        finally:
            self._closed.set()
            pusher.join(timeout=1.0)
            with suppress(OSError):
                self.channel.close()

    def _read_loop(self) -> None:
        while not self.stopping.is_set() and not self._closed.is_set():
            try:
                line = self.channel.read_line()
            except OSError:
                return
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                frame = json.loads(text)
            except json.JSONDecodeError as exc:
                self._send({"ok": False, "error": f"not JSON: {exc}"})
                continue
            if W.looks_like_wire(frame):
                self.session.handle(frame)
            else:
                # The one-shot command API. Kept on the same pipe so a terminal
                # can still poke a daemon the office is attached to.
                self._send(self.control.handle(frame))

    def _push_loop(self) -> None:
        """Tail the ledger for this subscriber until the connection ends."""
        while not self.stopping.is_set() and not self._closed.is_set():
            try:
                self.session.push_new()
            except Exception as exc:  # noqa: BLE001 - must not kill the reader
                self.push_errors.append(f"{type(exc).__name__}: {exc}")
            if self._closed.wait(self.poll_s):
                return


# -- transports --------------------------------------------------------------


class SocketChannel:
    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection
        self._reader = connection.makefile("rb")

    def read_line(self) -> bytes:
        return self._reader.readline()

    def write(self, payload: bytes) -> None:
        self._connection.sendall(payload)

    def close(self) -> None:
        with suppress(OSError):
            self._reader.close()
        with suppress(OSError):
            self._connection.close()


class SocketTransport:
    """Loopback TCP, for platforms without named pipes and for tests.

    Bound to 127.0.0.1 on an ephemeral port. It is the fallback, not the
    default: a port is reachable by anything on the machine that can guess it,
    and unlike the pipe there is no ACL to say otherwise.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._requested = (host, port)
        self._server: socket.socket | None = None
        self._bound = ("", 0)

    def open(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(self._requested)
        server.listen(8)
        server.settimeout(0.25)
        self._server = server
        self._bound = server.getsockname()

    @property
    def address(self) -> str:
        return f"tcp://{self._bound[0]}:{self._bound[1]}"

    def serve(self, make_connection: Callable[[Channel], None], stopping: threading.Event) -> None:
        assert self._server is not None
        while not stopping.is_set():
            try:
                connection, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            make_connection(SocketChannel(connection))

    def close(self) -> None:
        if self._server is not None:
            with suppress(OSError):
                self._server.close()
            self._server = None


class PipeChannel:
    """One connected client on a named pipe.

    Reads are gated by ``PeekNamedPipe`` rather than blocking in ``ReadFile``,
    and that is not an optimisation -- it is the only way this works at all.

    A pipe handle created without ``FILE_FLAG_OVERLAPPED`` is a *synchronous*
    handle, and Windows serialises I/O on one: while a ``ReadFile`` is pending,
    a ``WriteFile`` from another thread queues behind it instead of running. The
    first version had a reader blocked in ``ReadFile`` waiting for the next
    command while the pusher tried to send events on the same handle, so the
    office subscribed, got its replay, and then never received another frame --
    while the identical code over TCP worked, because sockets have no such rule.

    Peeking first means the reader never holds a pending operation, so the
    pusher's writes go out immediately. The cost is a poll interval on the
    command path, which is nothing for a control channel.
    """

    ERROR_BROKEN_PIPE = 109
    ERROR_PIPE_NOT_CONNECTED = 233

    def __init__(
        self,
        kernel32,
        handle,
        buffer_bytes: int,
        should_stop: Callable[[], bool] | None = None,
        poll_s: float = 0.02,
    ) -> None:
        self._k = kernel32
        self._handle = handle
        self._buffer_bytes = buffer_bytes
        self._buffered = bytearray()
        self._should_stop = should_stop or (lambda: False)
        self._poll_s = poll_s

    def _available(self) -> int:
        import ctypes
        from ctypes import wintypes

        total = wintypes.DWORD(0)
        ok = self._k.PeekNamedPipe(
            self._handle, None, 0, None, ctypes.byref(total), None
        )
        if not ok:
            error = ctypes.get_last_error()
            if error in (self.ERROR_BROKEN_PIPE, self.ERROR_PIPE_NOT_CONNECTED):
                raise OSError(error, "the client disconnected")
            raise OSError(error, "PeekNamedPipe failed")
        return total.value

    def read_line(self) -> bytes:
        import ctypes
        from ctypes import wintypes

        while b"\n" not in self._buffered:
            if self._should_stop():
                return b""
            if self._available() == 0:
                time.sleep(self._poll_s)
                continue
            chunk = ctypes.create_string_buffer(self._buffer_bytes)
            read = wintypes.DWORD(0)
            ok = self._k.ReadFile(
                self._handle, chunk, self._buffer_bytes, ctypes.byref(read), None
            )
            if not ok or read.value == 0:
                return bytes(self._buffered) if self._buffered else b""
            self._buffered.extend(chunk.raw[: read.value])
        index = self._buffered.index(b"\n")
        line = bytes(self._buffered[: index + 1])
        del self._buffered[: index + 1]
        return line

    def write(self, payload: bytes) -> None:
        import ctypes
        from ctypes import wintypes

        written = wintypes.DWORD(0)
        ok = self._k.WriteFile(
            self._handle, payload, len(payload), ctypes.byref(written), None
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "WriteFile on the pipe failed")

    def close(self) -> None:
        with suppress(Exception):
            self._k.FlushFileBuffers(self._handle)
        with suppress(Exception):
            self._k.DisconnectNamedPipe(self._handle)
        with suppress(Exception):
            self._k.CloseHandle(self._handle)


def current_user_sid() -> str:
    """The SID of the account this process is running as.

    Every prototype below is declared. The first attempt left
    ``GetCurrentProcess`` at the default ``c_int`` restype, which truncates the
    pointer-sized pseudo-handle, and ``OpenProcessToken`` then failed with
    ERROR_INVALID_HANDLE. It is the same trap as the pipe handle: on 64-bit
    Windows an undeclared ctypes signature silently narrows a pointer.
    """
    import ctypes
    from ctypes import wintypes

    TOKEN_QUERY = 0x0008
    TokenUser = 1
    ERROR_INSUFFICIENT_BUFFER = 122

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise ServiceError(f"OpenProcessToken failed: {ctypes.get_last_error()}")
    try:
        size = wintypes.DWORD(0)
        ctypes.set_last_error(0)
        advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
        error = ctypes.get_last_error()
        if size.value == 0 and error != ERROR_INSUFFICIENT_BUFFER:
            raise ServiceError(f"GetTokenInformation sizing failed: {error}")

        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, TokenUser, buffer, size, ctypes.byref(size)
        ):
            raise ServiceError(f"GetTokenInformation failed: {ctypes.get_last_error()}")

        # TOKEN_USER starts with SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes }
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ServiceError(f"ConvertSidToStringSid failed: {ctypes.get_last_error()}")
        try:
            return str(text.value)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(token)


def current_logon_sid() -> str | None:
    """Return the session logon SID (``S-1-5-5-X-Y``), when present.

    A user SID identifies the account, but Windows can give child processes a
    restricted token.  Named-pipe access must then be granted both by the
    ordinary token SIDs and by a restricting SID.  The logon SID is the
    per-session identity Microsoft recommends for local pipe DACLs, and it is
    the identity shared by the daemon, CLI and web bridge in one sign-in.

    Some service accounts do not carry a logon SID.  Those callers keep the
    existing user-SID ACE; returning ``None`` is therefore an intentional
    fallback rather than an invented broader principal.
    """
    if not IS_WINDOWS:  # pragma: no cover - Windows API
        return None

    import ctypes
    from ctypes import wintypes

    TOKEN_QUERY = 0x0008
    TOKEN_GROUPS_CLASS = 2
    SE_GROUP_LOGON_ID = 0xC0000000

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TOKEN_GROUPS(ctypes.Structure):
        _fields_ = [("GroupCount", wintypes.DWORD), ("Groups", SID_AND_ATTRIBUTES * 1)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise ServiceError(f"OpenProcessToken failed: {ctypes.get_last_error()}")
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token, TOKEN_GROUPS_CLASS, None, 0, ctypes.byref(size)
        )
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token,
            TOKEN_GROUPS_CLASS,
            buffer,
            size,
            ctypes.byref(size),
        ):
            raise ServiceError(f"GetTokenInformation failed: {ctypes.get_last_error()}")

        header = ctypes.cast(buffer, ctypes.POINTER(TOKEN_GROUPS)).contents
        groups = ctypes.cast(
            ctypes.addressof(buffer) + TOKEN_GROUPS.Groups.offset,
            ctypes.POINTER(SID_AND_ATTRIBUTES),
        )
        for index in range(header.GroupCount):
            group = groups[index]
            if group.Attributes & SE_GROUP_LOGON_ID != SE_GROUP_LOGON_ID:
                continue
            text = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(group.Sid, ctypes.byref(text)):
                raise ServiceError(
                    f"ConvertSidToStringSid failed: {ctypes.get_last_error()}"
                )
            try:
                return str(text.value)
            finally:
                kernel32.LocalFree(text)
        return None
    finally:
        kernel32.CloseHandle(token)


class NamedPipeTransport:
    """A Windows named pipe with an explicit DACL, served with ctypes.

    Two ctypes details cost a debugging round and are worth keeping written
    down. ``HANDLE`` is pointer-sized, so without ``restype`` ctypes truncates
    it to a 32-bit int on 64-bit Windows and a good handle looks invalid. And
    ``GetLastError`` is meaningless without ``WinDLL(..., use_last_error=True)``
    -- a failed ``CreateNamedPipe`` reported "error 0" until that was set.

    ``ConnectNamedPipe`` blocks until a client arrives, so stopping the service
    connects to its own pipe once to wake the accept loop.
    """

    PIPE_ACCESS_DUPLEX = 0x00000003
    FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    PIPE_TYPE_BYTE = 0x00000000
    PIPE_READMODE_BYTE = 0x00000000
    PIPE_WAIT = 0x00000000
    PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
    PIPE_UNLIMITED_INSTANCES = 255
    ERROR_PIPE_CONNECTED = 535
    ERROR_ACCESS_DENIED = 5
    ERROR_PIPE_BUSY = 231
    ERROR_INVALID_NAME = 123
    # Give up only after the name has refused this many times in a row.
    MAX_CREATE_FAILURES = 20

    def __init__(self, name: str = DEFAULT_PIPE, buffer_bytes: int = 65536) -> None:
        if not IS_WINDOWS:  # pragma: no cover - guarded by the caller
            raise ServiceError("named pipes are a Windows transport")
        self.name = name
        self.buffer_bytes = buffer_bytes
        self._kernel32 = None
        self._security = None
        self._pending = None
        self.sid: str | None = None
        self.logon_sid: str | None = None
        # Faults the transport survived. Empty is the healthy state; anything
        # here is worth a person's attention even though serving continued.
        self.warnings: list[str] = []

    @property
    def address(self) -> str:
        return self.name

    # -- setup ------------------------------------------------------------

    def open(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self._kernel32
        k.CreateNamedPipeW.restype = wintypes.HANDLE
        k.CreateNamedPipeW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        ]
        for name in ("ConnectNamedPipe", "DisconnectNamedPipe", "CloseHandle", "FlushFileBuffers"):
            fn = getattr(k, name)
            fn.argtypes = [wintypes.HANDLE] + ([ctypes.c_void_p] if name == "ConnectNamedPipe" else [])
            fn.restype = wintypes.BOOL
        for name in ("ReadFile", "WriteFile"):
            fn = getattr(k, name)
            fn.argtypes = [
                wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
            ]
            fn.restype = wintypes.BOOL
        k.PeekNamedPipe.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        k.PeekNamedPipe.restype = wintypes.BOOL
        self._security = self._security_attributes()
        # Claim the first instance here, not on the accept thread. A bad name,
        # a denied ACL, or a pipe another process already owns must fail the
        # start -- the first version discovered it on a background thread, and
        # the service went on to announce itself as running with no transport
        # behind it.
        self._pending = self._create_instance(first=True)

    def _security_attributes(self):
        """A DACL naming only this account and this sign-in session.

        ``D:P`` makes it protected, so nothing is inherited in. One ACE grants
        the account SID generic-all; a second ACE grants the per-session logon
        SID when the token has one.  The latter matters for restricted child
        tokens used by terminal and web bridge processes.  There is no ACE for
        anyone else -- an
        administrator can still take ownership, which is a property of the OS
        rather than something this can prevent, but no other interactive
        account can open the pipe.
        """
        import ctypes
        from ctypes import wintypes

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL),
            ]

        self.sid = current_user_sid()
        self.logon_sid = current_logon_sid()
        principals = [self.sid]
        if self.logon_sid and self.logon_sid not in principals:
            principals.append(self.logon_sid)
        sddl = "D:P" + "".join(f"(A;;GA;;;{principal})" for principal in principals)

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG),
        ]
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

        descriptor = ctypes.c_void_p()
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise ServiceError(
                f"could not build a DACL for {self.name}: error {ctypes.get_last_error()}"
            )

        attributes = SECURITY_ATTRIBUTES()
        attributes.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        attributes.lpSecurityDescriptor = descriptor
        attributes.bInheritHandle = False
        # Held on the instance so the descriptor outlives this call.
        self._descriptor = descriptor
        return attributes

    def _create_instance(self, first: bool = False):
        """One listening instance. `first` is a parameter, never remembered state.

        ``FILE_FLAG_FIRST_PIPE_INSTANCE`` makes creation fail with
        ERROR_ACCESS_DENIED if the name already exists -- which is exactly what
        it is for on the *first* call and exactly what breaks every call after
        it. The flag used to be governed by a mutable ``self._first``, so any
        path that reset or re-read it wrongly would take the whole transport
        down with error 5 on the second instance. An external bridge reported
        precisely that. It could not be reproduced here across eight sequential
        Node reconnects, four concurrent clients and a simultaneous CLI, so the
        state is gone rather than merely audited: with `first` passed in by the
        one caller that means it, there is no value left to get wrong.
        """
        import ctypes

        open_mode = self.PIPE_ACCESS_DUPLEX
        if first:
            open_mode |= self.FILE_FLAG_FIRST_PIPE_INSTANCE

        handle = self._kernel32.CreateNamedPipeW(
            self.name,
            open_mode,
            self.PIPE_TYPE_BYTE
            | self.PIPE_READMODE_BYTE
            | self.PIPE_WAIT
            | self.PIPE_REJECT_REMOTE_CLIENTS,
            self.PIPE_UNLIMITED_INSTANCES,
            self.buffer_bytes,
            self.buffer_bytes,
            0,
            ctypes.byref(self._security),
        )
        if handle and handle != ctypes.c_void_p(-1).value:
            return handle

        error = ctypes.get_last_error()
        if not first and error == self.ERROR_ACCESS_DENIED:
            # Should be unreachable now that `first` is a parameter. If it ever
            # happens anyway, the pipe name is worth more than the diagnosis:
            # note it loudly and keep serving rather than take the transport
            # down and leave every client with ENOENT.
            self.warnings.append(
                "CreateNamedPipe returned ERROR_ACCESS_DENIED for a non-first instance. "
                "Serving continued; this should not be possible and is worth reporting."
            )
            handle = self._kernel32.CreateNamedPipeW(
                self.name,
                self.PIPE_ACCESS_DUPLEX,
                self.PIPE_TYPE_BYTE
                | self.PIPE_READMODE_BYTE
                | self.PIPE_WAIT
                | self.PIPE_REJECT_REMOTE_CLIENTS,
                self.PIPE_UNLIMITED_INSTANCES,
                self.buffer_bytes,
                self.buffer_bytes,
                0,
                None,
            )
            if handle and handle != ctypes.c_void_p(-1).value:
                return handle
            error = ctypes.get_last_error()

        hint = ""
        if first and error in (self.ERROR_ACCESS_DENIED, self.ERROR_PIPE_BUSY):
            hint = " Another process already owns this pipe name."
        elif error == self.ERROR_INVALID_NAME:
            hint = " That is not a valid pipe name; check the leading backslashes."
        raise ServiceError(f"could not create {self.name}: error {error}.{hint}")

    # -- serving ----------------------------------------------------------

    def serve(self, make_connection: Callable[[Channel], None], stopping: threading.Event) -> None:
        """Always keep one instance listening, and never die on a bad one.

        Two properties the first version did not have.

        **No gap.** The next instance is created *before* the current one is
        handed to a connection thread. Between handing off and creating, the
        name had no instance in the listening state, and a client reconnecting
        in that window gets ENOENT -- which is what a bridge retrying after a
        dropped connection does.

        **No fatal creation.** A failure to create an instance is recorded and
        retried with a short backoff. The accept loop dying takes the pipe name
        with it, so every client sees ENOENT forever; a loop that keeps trying
        turns the same fault into a gap in service that heals itself.
        """
        import ctypes

        listening = self._pending
        self._pending = None
        failures = 0

        while not stopping.is_set():
            if listening is None:
                try:
                    listening = self._create_instance()
                    failures = 0
                except ServiceError as exc:
                    failures += 1
                    self.warnings.append(str(exc))
                    if failures >= self.MAX_CREATE_FAILURES:
                        raise
                    if stopping.wait(min(1.0, 0.05 * failures)):
                        return
                    continue

            ctypes.set_last_error(0)
            connected = self._kernel32.ConnectNamedPipe(listening, None)
            if not connected and ctypes.get_last_error() not in (0, self.ERROR_PIPE_CONNECTED):
                self._kernel32.CloseHandle(listening)
                listening = None
                continue
            if stopping.is_set():
                self._kernel32.CloseHandle(listening)
                return

            accepted, listening = listening, None
            try:
                listening = self._create_instance()
            except ServiceError as exc:
                # Serve the client we have; the next loop will try again.
                self.warnings.append(str(exc))
            make_connection(
                PipeChannel(
                    self._kernel32, accepted, self.buffer_bytes, should_stop=stopping.is_set
                )
            )

        if listening is not None:
            self._kernel32.CloseHandle(listening)

    def close(self) -> None:
        """Wake a blocked accept by connecting to our own pipe once."""
        if self._kernel32 is None:
            return
        with suppress(OSError):
            with open(self.name, "r+b", buffering=0):
                pass


def open_transport(pipe_name: str | None = None):
    if IS_WINDOWS:
        return NamedPipeTransport(pipe_name or DEFAULT_PIPE)
    return SocketTransport()


# -- the service -------------------------------------------------------------


@dataclass
class ServiceConfig:
    interval_s: float = DEFAULT_INTERVAL_S
    max_sleep_s: float = MAX_SLEEP_S
    pipe_name: str | None = None
    lock_path: Path | None = None
    install_signal_handlers: bool = True
    poll_s: float = W.DEFAULT_POLL_S
    # Needed to resolve artifacts for `job.result`. Without them the daemon
    # still streams events; it just offers nothing to open.
    runs_dir: Path | None = None
    project_root: Path | None = None
    contracts_dir: Path | None = None
    runtime_profile: str | None = None
    workspace_mode: str | None = None


class Service:
    """Runs the daemon on a timer and answers control requests."""

    def __init__(
        self,
        ledger: JobLedger,
        daemon: D.Daemon,
        *,
        config: ServiceConfig | None = None,
        transport: Any | None = None,
        now: Callable[[], str] = utc_now,
        knowledge_store: KnowledgeStore | None = None,
        knowledge_scope: KnowledgeScope | None = None,
        campaign_controller: CampaignController | None = None,
        capability_catalog: CapabilityCatalog | None = None,
        board_catalog: BoardCatalog | None = None,
        hardware_profile: Any | None = None,
        hardware_connections: Any | None = None,
        hardware_registry: Any | None = None,
        hardware_executor: Any | None = None,
        runtime_runner: Any | None = None,
        project_registry: Any | None = None,
    ) -> None:
        self.ledger = ledger
        self.daemon = daemon
        self.config = config or ServiceConfig()
        self.transport = transport
        self.now = now
        self.stopping = threading.Event()
        self._turn = threading.RLock()
        self.control = ControlPlane(
            ledger,
            daemon,
            on_shutdown=self.stop,
            preflight_engine=P.ScenarioPreflight(self.config.contracts_dir),
            knowledge_store=knowledge_store,
            default_knowledge_scope=knowledge_scope,
            campaign_controller=campaign_controller,
            capability_catalog=capability_catalog,
            board_catalog=board_catalog,
            hardware_profile=hardware_profile,
            hardware_connections=hardware_connections,
            hardware_registry=hardware_registry,
            hardware_executor=hardware_executor,
            hardware_enabled=self.config.runtime_profile == "hardware",
            project_registry=project_registry,
        )
        self.knowledge_store = knowledge_store
        self.knowledge_scope = knowledge_scope
        from QKD_PROJ.knowledge.conversation_ingestor import ConversationKnowledgeIngestor
        self.knowledge_ingestor = (ConversationKnowledgeIngestor(ledger, knowledge_store, knowledge_scope)
                                   if knowledge_store is not None and knowledge_scope is not None else None)
        self.campaign_controller = campaign_controller
        self.runtime_runner = runtime_runner
        self.started_at: str | None = None
        self.ticks = 0
        self._server: threading.Thread | None = None
        self._connections: list[threading.Thread] = []
        self.transport_error: str | None = None
        self._connection_objects: list[Connection] = []
        self.lock: InstanceLock | None = None

    # -- lifecycle -------------------------------------------------------

    def _lock_path(self) -> Path:
        return self.config.lock_path or self.ledger.path.with_suffix(".lock")

    def start(self) -> dict[str, Any]:
        self.lock = InstanceLock(self._lock_path())
        self.lock.acquire()
        self.started_at = self.now()
        recovered = self.daemon.recover()
        campaign = (
            self.campaign_controller.recover()
            if self.campaign_controller is not None
            else None
        )

        if self.transport is None:
            self.transport = open_transport(self.config.pipe_name)
        self.transport.open()
        self._server = threading.Thread(
            target=self._accept_loop,
            name="rtase-accept",
            daemon=True,
        )
        self._server.start()

        if self.config.install_signal_handlers:
            self._install_signals()
        return {
            "started_at": self.started_at,
            "address": self.transport.address,
            "recovered": recovered,
            "campaign": campaign,
            "cursor": self.ledger.latest_event_id(),
            "sid": getattr(self.transport, "sid", None),
            "knowledge_cursor": (
                self.knowledge_store.latest_event_id()
                if self.knowledge_store is not None
                else None
            ),
        }

    def _accept_loop(self) -> None:
        """Serve connections, and stop the service if the transport dies.

        An accept loop that raises and leaves everything else running is a
        daemon that answers nothing while looking healthy. Recording the fault
        and setting the stop flag makes it visible at the one place a caller
        already checks.
        """
        try:
            self.transport.serve(self._spawn_connection, self.stopping)
        except Exception as exc:  # noqa: BLE001 - the transport is gone either way
            self.transport_error = f"{type(exc).__name__}: {exc}"
            self.stopping.set()

    def _spawn_connection(self, channel: Channel) -> None:
        connection = Connection(
            channel,
            self.ledger,
            _LockedControl(self.control, self._turn, self._interrupt_job),
            self.stopping,
            poll_s=self.config.poll_s,
            runs_dir=self.config.runs_dir,
            project_root=self.config.project_root,
            runtime_profile=self.config.runtime_profile,
            workspace_mode=self.config.workspace_mode,
        )
        thread = threading.Thread(target=connection.serve, name="rtase-conn", daemon=True)
        thread.start()
        self._connections.append(thread)
        self._connection_objects.append(connection)

    def _interrupt_job(self, job_id: str) -> bool:
        """Best-effort runtime interruption; the ledger cancellation is authoritative."""
        conversation = getattr(self.daemon.loop, "conversation", None)
        owner = (conversation.runtime if conversation is not None and conversation.owns(job_id)
                 else self.runtime_runner)
        cancel = getattr(owner, "cancel", None)
        if not callable(cancel):
            return False
        try:
            return bool(cancel(job_id))
        except Exception:
            # A broken runtime must not be able to veto the durable stop. The
            # control plane will still close the job and the scheduler will
            # never dispatch another step for it.
            return False

    def _install_signals(self) -> None:
        import signal

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            handler = getattr(signal, name, None)
            if handler is None:
                continue
            with suppress(ValueError, OSError):
                signal.signal(handler, lambda *_: self.stop())

    def stop(self) -> None:
        self.stopping.set()
        if self.transport is not None:
            with suppress(Exception):
                self.transport.close()

    def close(self) -> None:
        self.stop()
        for thread in self._connections:
            thread.join(timeout=2.0)
        self._connections.clear()
        if self._server is not None:
            self._server.join(timeout=2.0)
            self._server = None
        if self.lock is not None:
            self.lock.release()
            self.lock = None

    def __enter__(self) -> "Service":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- the timer -------------------------------------------------------

    def tick_once(self) -> D.Tick:
        with self._turn:
            self.ticks += 1
            if self.knowledge_ingestor is not None:
                self.knowledge_ingestor.sync()
            try:
                return self.daemon.tick(self.now())
            finally:
                if self.knowledge_ingestor is not None:
                    self.knowledge_ingestor.sync()

    def sleep_for(self, tick: D.Tick, now: str | None = None) -> float:
        """How long to wait before looking again, from what the survey said."""
        if tick.advanced is not None:
            return 0.0
        moment = now or self.now()
        due = [w.retry_at for w in tick.waiting if w.retry_at]
        if not due:
            return min(self.config.max_sleep_s, max(self.config.interval_s, 0.0))
        soonest = min(due)
        seconds = (
            datetime.fromisoformat(soonest) - datetime.fromisoformat(moment)
        ).total_seconds()
        return max(0.0, min(self.config.max_sleep_s, seconds))

    def run_forever(self, max_ticks: int | None = None) -> int:
        ran = 0
        while not self.stopping.is_set():
            if max_ticks is not None and ran >= max_ticks:
                break
            tick = self.tick_once()
            ran += 1
            delay = self.sleep_for(tick)
            if delay > 0 and self.stopping.wait(delay):
                break
        return ran


class _LockedControl:
    """Serialises control commands against the tick.

    SQLite keeps the writes consistent on its own; this keeps a cancel from
    landing between a tick's survey and the advance it decided on.
    """

    def __init__(
        self,
        control: ControlPlane,
        lock: threading.RLock,
        interrupt_job: Callable[[str], bool] | None = None,
    ) -> None:
        self._control = control
        self._lock = lock
        self._interrupt_job = interrupt_job or (lambda _job_id: False)

    def __getattr__(self, name: str) -> Any:
        """Lock public control calls added after this proxy was written.

        The wire protocol calls typed methods such as ``mission`` directly.
        Keeping a second, hand-written forwarding list made the first mission
        command fail at runtime even though ``ControlPlane.mission`` existed.
        Resolve future public methods through the same turn lock so extending
        the control plane cannot silently leave the service wrapper behind.
        """

        target = getattr(self._control, name)
        if not callable(target):
            return target

        def locked(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                return target(*args, **kwargs)

        return locked

    def handle(self, request: Any) -> dict[str, Any]:
        # A model turn can take minutes.  Reads of the append-only ledger must
        # not sit behind that turn: otherwise the office keeps receiving the
        # live event stream while its transcript API appears to be frozen.
        # These commands neither mutate the ledger nor grant a capability; the
        # ledger's own transactions give each a consistent read.
        if isinstance(request, dict) and request.get("command") in {
            "ping", "status", "job", "handoffs", "events", "inbox", "models-list", "hardware-status", "hardware-catalog",
        }:
            return self._control.handle(request)
        # Starts a background identity check; the hardware adapter owns its
        # device lock, so UI commands never wait for a model/programming turn.
        if isinstance(request, dict) and request.get("command") in {"hardware-check", "hardware-discover", "hardware-register"}:
            return self._control.handle(request)
        if isinstance(request, dict) and request.get("command") == "cancel":
            job_id = str(request.get("job_id") or "")
            result = self._control.handle(request)
            if result.get("ok"):
                self._interrupt_job(job_id)
            return result
        if isinstance(request, dict) and request.get("command") == "steer":
            conversation = getattr(getattr(self._control.daemon, "loop", None), "conversation", None)
            if conversation is not None and conversation.owns(str(request.get("job_id") or "")):
                return self._control.handle(request)
        with self._lock:
            return self._control.handle(request)

    def approve(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return self._control.approve(*args, **kwargs)

    def start(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return self._control.start(*args, **kwargs)

    def preflight(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return self._control.preflight(*args, **kwargs)

    def steer(self, *args: Any, **kwargs: Any) -> Any:
        job_id = str(args[0] if args else kwargs.get("job_id") or "")
        conversation = getattr(getattr(self._control.daemon, "loop", None), "conversation", None)
        if conversation is not None and conversation.owns(job_id):
            return self._control.steer(*args, **kwargs)
        with self._lock:
            return self._control.steer(*args, **kwargs)

    def cancel(self, *args: Any, **kwargs: Any) -> Any:
        job_id = str(args[0] if args else kwargs.get("job_id") or "")
        # Cancellation is deliberately out-of-band. Waiting for ``_lock``
        # here would make a stop button wait for the very model turn it is
        # supposed to stop.
        result = self._control.cancel(*args, **kwargs)
        self._interrupt_job(job_id)
        return result


# -- clients -----------------------------------------------------------------


def _open_pipe(address: str, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return open(address, "r+b", buffering=0)
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


class Client:
    """A persistent connection, for a UI or a test that needs one."""

    def __init__(self, address: str | None = None, timeout: float = 10.0) -> None:
        self.address = address or DEFAULT_PIPE
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._handle = None
        self._reader = None
        self._buffered = bytearray()

    def __enter__(self) -> "Client":
        if self.address.startswith("tcp://"):
            host, _, port = self.address[len("tcp://") :].rpartition(":")
            self._socket = socket.create_connection((host, int(port)), timeout=self.timeout)
            self._reader = self._socket.makefile("rb")
        else:
            self._handle = _open_pipe(self.address, self.timeout)
        return self

    def __exit__(self, *_: object) -> None:
        for closeable in (self._reader, self._socket, self._handle):
            if closeable is not None:
                with suppress(OSError):
                    closeable.close()

    def send(self, frame: dict[str, Any]) -> None:
        payload = (json.dumps(frame) + "\n").encode("utf-8")
        if self._socket is not None:
            self._socket.sendall(payload)
        else:
            self._handle.write(payload)
            self._handle.flush()

    def receive(self) -> dict[str, Any]:
        if self._reader is not None:
            line = self._reader.readline()
        else:
            while b"\n" not in self._buffered:
                chunk = self._handle.read(4096)
                if not chunk:
                    break
                self._buffered.extend(chunk)
            index = self._buffered.index(b"\n") if b"\n" in self._buffered else len(self._buffered)
            line = bytes(self._buffered[: index + 1])
            del self._buffered[: index + 1]
        if not line:
            raise ServiceError("the daemon closed the connection")
        return json.loads(line.decode("utf-8"))

    def until(self, kind: str, limit: int = 5000) -> list[dict[str, Any]]:
        """Read frames until one of `kind` arrives. Returns everything read."""
        seen: list[dict[str, Any]] = []
        for _ in range(limit):
            frame = self.receive()
            seen.append(frame)
            if frame.get("type") == kind:
                return seen
        raise ServiceError(f"no {kind!r} frame within {limit} frames")


def send(request: dict[str, Any], address: str | None = None, timeout: float = 10.0):
    """Send one control request and read one response."""
    with Client(address, timeout) as client:
        client.send(request)
        return client.receive()
