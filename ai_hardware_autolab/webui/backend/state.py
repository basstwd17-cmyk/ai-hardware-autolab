"""What the browser is shown, and where each part of it came from.

The split here is deliberate and it is the same one the VS Code fork makes.

* **Events are streamed.** Every ``ledger.event`` frame is kept in arrival
  order, capped, and handed to subscribers. They are history, so a copy of them
  is safe.
* **Job and approval state is asked for.** It is *not* rebuilt from those
  events. A projection that re-derives "is this job blocked" is a second
  implementation of the ledger's state machine, and the first time the two
  disagree the UI is lying with confidence. So the snapshot comes from the
  control plane's ``status``, which reads the rows.
* **Results and artifacts are streamed and last.** ``job.result`` and
  ``artifact.registered`` are pushed by the daemon after a settling handoff and
  are kept by id. A repeated registration under an id that already points
  somewhere else is refused here too, so the browser never sees an artifact id
  change meaning.

One rule is inherited rather than invented. ``Artifact.registration`` in
``orchestrator/results.py`` says of the frame it builds: *"The frame carrying
the path. Never sent to a browser."* The paths stay on this side. This process
is the local host that resolves ``(jobId, artifactId)`` to a file; what crosses
the WebSocket is the id and the label. ``browser_frame`` is where that is
enforced, and it is the only function the fan-out uses.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from ai_hardware_autolab.orchestrator import wire as W

# How much history to hold for a browser that connects late. A job's whole
# life is a few hundred rows; this is several jobs' worth and still small.
DEFAULT_HISTORY = 5000


@dataclass
class Projection:
    """Everything the HTTP surface can answer from memory."""

    history: int = DEFAULT_HISTORY
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _events: deque = field(default_factory=deque, init=False, repr=False)
    _results: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _artifacts: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _snapshot: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _warnings: list[str] = field(default_factory=list, init=False, repr=False)
    _listeners: list[Callable[[dict[str, Any]], None]] = field(
        default_factory=list, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._events = deque(maxlen=self.history)

    # -- ingest -----------------------------------------------------------

    def apply(self, frame: dict[str, Any]) -> None:
        """Take one daemon frame. Raises nothing the reader cannot survive."""
        kind = frame.get("type")
        if kind == W.LEDGER_EVENT:
            event = frame.get("event") or {}
            with self._lock:
                self._events.append(event)
        elif kind == W.JOB_RESULT:
            result = frame.get("result") or {}
            job_id = str(result.get("job_id") or result.get("jobId") or "")
            if job_id:
                with self._lock:
                    self._results[job_id] = result
        elif kind == W.ARTIFACT_REGISTERED:
            self._register(frame)
        self._fan_out(frame)

    def _register(self, frame: dict[str, Any]) -> None:
        """Key an artifact by ``(jobId, artifactId)``, which is its real name.

        ``orchestrator/results.py`` resolves artifacts by that pair, and
        ``Artifact.artifact_id`` is a *role* -- "report", "verdict" -- not a
        global identifier. Keying on the id alone meant the second job to
        register a "report" was refused as a re-pointing of the first job's,
        which is a correct rule applied to the wrong key.
        """
        artifact_id = _text(frame, "artifactId", "artifact_id")
        job_id = _text(frame, "jobId", "job_id")
        if not artifact_id or not job_id:
            return
        key = (job_id, artifact_id)
        with self._lock:
            known = self._artifacts.get(key)
            if known is None:
                self._artifacts[key] = dict(frame)
                return
            if _path_of(known) == _path_of(frame):
                return  # identical registration: idempotent, so say nothing
            self._warnings.append(
                f"{job_id}/{artifact_id} already points at {_path_of(known)!r}; "
                "refusing to re-point it"
            )

    def reset(self) -> None:
        """Drop everything streamed. Used when a resubscribe replays from 0."""
        with self._lock:
            self._events.clear()
            self._results.clear()
            self._artifacts.clear()

    def set_snapshot(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._snapshot = snapshot

    # -- read -------------------------------------------------------------

    def events(
        self, since: int = 0, job_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                e
                for e in self._events
                if int(e.get("event_id", 0)) > since and (job_id is None or e.get("job_id") == job_id)
            ]
        return rows[-limit:] if limit and len(rows) > limit else rows

    def results(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._results)

    def result(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._results.get(job_id)

    def artifacts(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Every registration by ``(jobId, artifactId)``, paths included.

        Host-side callers only -- see ``browser_frame`` for what may leave.
        """
        with self._lock:
            return dict(self._artifacts)

    def artifact_path(self, job_id: str, artifact_id: str) -> str | None:
        """Resolve a pair to the file it was registered against, or nothing.

        A pair, never an id: an artifact id belonging to another job is not a
        near miss to be helpful about, it is a different artifact.
        """
        with self._lock:
            known = self._artifacts.get((job_id, artifact_id))
        return _path_of(known) if known else None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)

    def warnings(self) -> list[str]:
        with self._lock:
            return list(self._warnings)

    def oldest_event_id(self) -> int:
        with self._lock:
            return int(self._events[0].get("event_id", 0)) if self._events else 0

    # -- fan-out ----------------------------------------------------------

    def listen(self, listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def cancel() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return cancel

    def _fan_out(self, frame: dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        if not listeners:
            return
        safe = browser_frame(frame)
        for listener in listeners:
            try:
                listener(safe)
            except Exception:  # noqa: BLE001 - one dead browser must not stop the stream
                continue


# Fields that name a place on this machine. They exist so the host can open a
# file; a browser has no use for them and no business holding them.
_HOST_ONLY = ("targetPath", "target_path", "projectRoot", "project_root")


def _text(frame: dict[str, Any], *keys: str) -> str:
    """The first of ``keys`` present as a non-empty string.

    Both spellings are accepted because the daemon sends ``jobId`` on the wire
    and snake_case everywhere else, and a projection that understood only one
    would drop frames for a reason nobody could see.
    """
    for key in keys:
        value = frame.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _path_of(frame: dict[str, Any]) -> str | None:
    path = frame.get("targetPath", frame.get("target_path"))
    return str(path) if path else None


def flatten_jobs(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """``status.jobs`` in one shape, without inventing a field.

    The control plane answers with a plain list of ledger rows when no daemon
    is attached, and with ``{scheduler status: [Waiting, ...]}`` when one is.
    Those are different records, and the difference is real -- a ``Waiting``
    knows *why* a job is not moving and a ``Job`` row does not.

    Flattening is safe in exactly one direction: the scheduler status the dict
    is keyed by is already a field on every ``Waiting`` it holds, so unrolling
    the dict copies nothing and loses nothing. Nothing is added to the plain
    rows to make the two look alike; a caller that needs the difference reads
    ``snapshot`` itself, which is passed through untouched.
    """
    jobs = snapshot.get("jobs")
    if isinstance(jobs, dict):
        return [row for rows in jobs.values() for row in rows]
    return list(jobs or [])


def browser_frame(frame: dict[str, Any]) -> dict[str, Any]:
    """The version of a daemon frame that may cross the WebSocket."""
    if frame.get("type") != W.ARTIFACT_REGISTERED:
        return frame
    return {k: v for k, v in frame.items() if k not in _HOST_ONLY}
