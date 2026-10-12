"""One subscription to the daemon, held open, resumed from what it applied.

The office protocol (``orchestrator/wire.py``) is a stream, not a request. A
client connects once, says how far it has got, and is then pushed everything
after that. This module is the client half of that, and it makes three choices
that are worth stating because the alternatives are all quietly wrong.

**Two connections, not one.** The stream connection only reads after its
``subscribe``. Commands go out on their own short-lived connection, because the
Windows named-pipe handle is opened ``r+b`` and writing to it from a second
thread while the first is blocked in ``readline`` is not something the pipe
promises to survive. The daemon gives every connection its own ``WireSession``,
and a session that never subscribes never pushes, so a command connection costs
one round trip and nothing else.

**A gap forces a resubscribe, never a guess.** The contract says one frame per
global cursor and that a forward gap must stop the projector. So an
``event_id`` at or below the cursor is dropped as a duplicate -- replay is
idempotent by design -- and one past ``cursor + 1`` drops the connection and
resubscribes from the last cursor actually applied. The alternative, applying
it anyway, would leave the UI claiming to be current while missing durable
history.

**The cursor advances on apply, not on receipt.** ``on_frame`` runs before the
cursor moves, so a projection that raises is retried at the same cursor on the
next connection rather than being skipped.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ai_hardware_autolab.orchestrator import service as S
from ai_hardware_autolab.orchestrator import wire as W

# Backoff between reconnect attempts. Small, because the daemon is a local
# process the user starts and stops, so a restart should be picked up in about
# the time it takes them to look at the window.
FIRST_RETRY_S = 0.25
MAX_RETRY_S = 5.0

DEFAULT_COMMAND_TIMEOUT_S = 20.0


class DaemonUnavailable(RuntimeError):
    """No daemon answered on the address, or it dropped mid-command."""


class WireGapError(RuntimeError):
    """A forward gap in the global cursor. Replay, never interpolate."""


@dataclass
class SubscriptionStatus:
    """What the web UI is allowed to say about its own connection."""

    address: str
    connected: bool
    cursor: int
    attempts: int
    frames: int
    gaps: int
    last_error: str | None
    profile: str | None = None
    workspace_mode: str | None = None

    def describe(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class DaemonSubscription:
    """A background thread holding protocol 1 open against one daemon.

    ``on_frame`` is called for every frame the daemon sends, in arrival order,
    from the reader thread. It must not block for long and must not call back
    into this object's ``command``.
    """

    address: str | None = None
    on_frame: Callable[[dict[str, Any]], None] | None = None
    on_status: Callable[[SubscriptionStatus], None] | None = None
    # Called with the `after` each session subscribes from, before any
    # frame of it arrives. `after == 0` means the whole history is about to
    # be replayed, which is the only moment a projection may be cleared.
    on_replay: Callable[[int], None] | None = None
    connect_timeout_s: float = 2.0
    command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S

    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _cursor: int = field(default=0, init=False)
    _connected: bool = field(default=False, init=False)
    _attempts: int = field(default=0, init=False)
    _frames: int = field(default=0, init=False)
    _gaps: int = field(default=0, init=False)
    _last_error: str | None = field(default=None, init=False)
    _profile: str | None = field(default=None, init=False)
    _workspace_mode: str | None = field(default=None, init=False)
    _ids: Any = field(default_factory=lambda: itertools.count(1), init=False, repr=False)

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="qkd-webui-wire", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    @property
    def target(self) -> str:
        return self.address or S.DEFAULT_PIPE

    def status(self) -> SubscriptionStatus:
        with self._lock:
            return SubscriptionStatus(
                address=self.target,
                connected=self._connected,
                cursor=self._cursor,
                attempts=self._attempts,
                frames=self._frames,
                gaps=self._gaps,
                last_error=self._last_error,
                profile=self._profile,
                workspace_mode=self._workspace_mode,
            )

    # -- commands ---------------------------------------------------------

    def command(self, frame: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        """Send one office frame on its own connection; return its result.

        A ``commandId`` is generated when the caller has not supplied one,
        because the daemon refuses a command frame without it and answering
        that refusal with a second round trip would be a worse error message.
        """
        if not W.looks_like_wire(frame):
            raise ValueError("a command frame needs a string 'type'")
        payload = dict(frame)
        if payload["type"] != W.SUBSCRIBE and not str(payload.get("commandId", "")).strip():
            payload["commandId"] = f"webui-{next(self._ids)}"
        wanted = payload.get("commandId")
        limit = self.command_timeout_s if timeout is None else timeout
        try:
            with S.Client(self.address, timeout=limit) as client:
                client.send(payload)
                for _ in range(64):
                    answer = client.receive()
                    kind = answer.get("type")
                    if kind == W.ERROR:
                        raise DaemonUnavailable(str(answer.get("error", "daemon refused the frame")))
                    if kind == W.COMMAND_RESULT and answer.get("commandId") == wanted:
                        return answer
        except DaemonUnavailable:
            raise
        except OSError as exc:
            raise DaemonUnavailable(f"no daemon on {self.target}: {exc}") from exc
        except S.ServiceError as exc:
            raise DaemonUnavailable(str(exc)) from exc
        raise DaemonUnavailable("the daemon never answered the command")

    def control(self, request: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        """Send one control-plane request (``status``, ``job``, ``inbox``...).

        Same pipe, different protocol -- the daemon tells them apart by whether
        the frame carries ``type`` or ``command``. Job and approval state is
        read this way rather than rebuilt from the event stream, because the
        ledger is the only thing entitled to say what state a job is in.
        """
        limit = self.command_timeout_s if timeout is None else timeout
        try:
            return S.send(request, self.address, timeout=limit)
        except OSError as exc:
            raise DaemonUnavailable(f"no daemon on {self.target}: {exc}") from exc
        except S.ServiceError as exc:
            raise DaemonUnavailable(str(exc)) from exc

    # -- the reader -------------------------------------------------------

    def _run(self) -> None:
        delay = FIRST_RETRY_S
        while not self._stop.is_set():
            try:
                self._session()
                delay = FIRST_RETRY_S
            except Exception as exc:  # noqa: BLE001 - a dropped daemon is normal
                self._note_error(f"{type(exc).__name__}: {exc}")
                delay = min(delay * 2, MAX_RETRY_S)
            finally:
                self._set_connected(False)
            if self._stop.wait(delay):
                return

    def _session(self) -> None:
        with self._lock:
            self._attempts += 1
            after = self._cursor
        if self.on_replay is not None:
            self.on_replay(after)
        with S.Client(self.address, timeout=self.connect_timeout_s) as client:
            client.send({"type": W.SUBSCRIBE, "protocol": W.PROTOCOL, "after": after})
            while not self._stop.is_set():
                frame = client.receive()
                self._apply(frame)

    def _apply(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == W.ERROR:
            raise DaemonUnavailable(str(frame.get("error", "the daemon refused the subscription")))

        if kind == W.LEDGER_EVENT:
            event = frame.get("event") or {}
            event_id = event.get("event_id")
            if not isinstance(event_id, int):
                raise WireGapError(f"a ledger.event without an integer event_id: {event!r}")
            with self._lock:
                cursor = self._cursor
            if event_id <= cursor:
                return  # replay is idempotent; an older row is not news
            if event_id > cursor + 1:
                with self._lock:
                    self._gaps += 1
                raise WireGapError(
                    f"cursor gap: applied {cursor}, next frame is {event_id}. "
                    "Resubscribing rather than showing a state with a hole in it."
                )

        self._deliver(frame)

        if kind == W.LEDGER_EVENT:
            with self._lock:
                self._cursor = int(frame["event"]["event_id"])
        elif kind == W.READY:
            reported = frame.get("cursor")
            with self._lock:
                if isinstance(reported, int) and reported != self._cursor:
                    # ready.cursor must equal the last event sent. A
                    # disagreement means one of us is wrong about history, and
                    # it is not safe for the UI to pick a winner.
                    self._gaps += 1
                    raise WireGapError(
                        f"ready says cursor {reported}, but {self._cursor} was applied"
                    )
                profile = frame.get("profile")
                workspace_mode = frame.get("workspaceMode")
                self._profile = profile if isinstance(profile, str) else None
                self._workspace_mode = (
                    workspace_mode if isinstance(workspace_mode, str) else None
                )
                self._connected = True
            self._announce()

    def _deliver(self, frame: dict[str, Any]) -> None:
        with self._lock:
            self._frames += 1
        if self.on_frame is not None:
            self.on_frame(frame)

    def _note_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message
        self._announce()

    def _set_connected(self, value: bool) -> None:
        with self._lock:
            changed = self._connected != value
            self._connected = value
        if changed:
            self._announce()

    def _announce(self) -> None:
        if self.on_status is not None:
            self.on_status(self.status())


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0, step: float = 0.02) -> bool:
    """Poll until true or out of time. Used by tests and by startup probes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
