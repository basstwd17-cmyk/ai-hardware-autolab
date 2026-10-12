"""Writing what a runtime did into the job's log.

``CodexAgentRunner`` takes an ``event_sink`` and hands it every JSON-RPC
notification the App Server produces -- command execution, file changes, MCP
tool calls, sub-agent activity. Those are the events a UI draws. They belong in
the ledger so a viewer that reconnects at ``events.event_id`` sees them in order
beside the job's own transitions.

Three decisions shape how.

**The names stay the runtime's.** A notification arrives as ``method``, and it
is recorded as ``runtime.<method>``. Renaming Codex's vocabulary into one of our
own would put a translation table between what happened and what is written,
and the table would be wrong the first time a vendor added a method. The
namespace exists only so a runtime event can never collide with the ledger's own
``job.*``, ``handoff.*`` or ``approval.*`` kinds.

**They are observability, not evidence.** Nothing decides a gate from these.
The evidence chain is ``runs/`` and the handoffs, both written durably by the
tool and the loop. That is what makes it acceptable to drop them under load,
and to exclude the noisiest ones by default.

Be precise about what the buffer does and does not buy, because an earlier
version of this file overclaimed. ``JobLedger.append_event`` is one
``BEGIN IMMEDIATE`` transaction per event with ``synchronous = FULL``, and the
ledger is frozen -- so buffering **does not reduce the number of fsyncs**. It
only moves them out of the runtime's callback, so a slow disk stalls the flush
instead of the App Server read loop. The thing that actually reduces write
volume is the exclusion list below.

A single real Codex turn from the observation spike emitted 258
``item/agentMessage/delta`` notifications out of roughly 600 total. Recording
each as its own committed transaction is several hundred fsyncs for content
that arrives complete in the matching ``item/completed`` anyway, so the delta
streams are excluded unless a caller asks for them.

**The sink has no idea which job it is for.** It is set once on a runner that
is reused across calls, so the context comes from wrapping the runner rather
than from the callback. ``Loop.advance()`` runs one agent at a time, which is
what makes a single current-context slot correct here; a runner driven
concurrently would need one per thread, and this one is not.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Sequence

from ai_hardware_autolab.agents.schema import AgentDefinition
from ai_hardware_autolab.orchestrator import jobs as J
from ai_hardware_autolab.orchestrator.jobs import JobLedger
from ai_hardware_autolab.orchestrator.pagination import iter_events

# Every kind this module writes starts here. One prefix, so a reader can filter
# runtime noise out of a job's history with a single test.
NAMESPACE = "runtime."

# Notification methods are vendor strings; keep them recognisable but refuse
# anything that could turn a kind into something else.
_SAFE_METHOD = re.compile(r"[^A-Za-z0-9_./-]")

# A single notification carrying a whole file diff is not worth keeping intact.
MAX_PAYLOAD_BYTES = 8_192

# Per-token streams. Excluded by default: each one is a separate committed
# transaction, they are the majority of a turn's traffic, and the content
# arrives whole in the completion notification that follows. Pass
# ``exclude=()`` to record them anyway.
#
# These names come from the App Server capture under
# ``agent-platform/spikes/app-server/captures/``, not from a guess. The methods
# are ``item/started`` and ``item/completed``; ``commandExecution`` and
# ``fileChange`` are *item types* inside their payload, which is why the item
# type is lifted out below rather than being treated as a method name.
DEFAULT_EXCLUDED = (
    "item/agentMessage/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded",
    "item/commandExecution/outputDelta",
)

# How many notifications to hold before writing them out mid-turn.
DEFAULT_BUFFER = 64


def kind_for(method: str) -> str:
    cleaned = _SAFE_METHOD.sub("_", method.strip()) or "unknown"
    return NAMESPACE + cleaned


def _clip(value: Any) -> Any:
    """Keep a payload small enough that the log stays readable."""
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) <= MAX_PAYLOAD_BYTES:
        return value
    return {
        "truncated": True,
        "bytes": len(encoded.encode("utf-8")),
        "head": encoded[: MAX_PAYLOAD_BYTES // 2],
    }


@dataclass
class _Context:
    job_id: str | None = None
    agent: str | None = None


@dataclass
class RuntimeEventRecorder:
    """Buffers runtime notifications and writes them into one job's log."""

    ledger: JobLedger
    buffer_size: int = DEFAULT_BUFFER
    # None records every method that is not excluded. A list narrows it further.
    methods: Sequence[str] | None = None
    exclude: Sequence[str] = DEFAULT_EXCLUDED
    _context: _Context = field(default_factory=_Context, init=False, repr=False)
    _pending: list[tuple[str, str, dict[str, Any], str | None]] = field(
        default_factory=list, init=False, repr=False
    )
    dropped: int = field(default=0, init=False)

    # -- the sink --------------------------------------------------------

    def sink(self, message: dict[str, Any]) -> None:
        """The ``event_sink`` callback. Never raises into the runtime.

        A recorder that threw here would turn a logging problem into a failed
        turn, and the loop would block a job because something could not be
        written down.
        """
        try:
            self._offer(message)
        except Exception:  # noqa: BLE001 - observability must not break the run
            self.dropped += 1

    def _offer(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            self.dropped += 1
            return
        method = message.get("method")
        if not isinstance(method, str) or not method.strip():
            self.dropped += 1
            return
        if method in self.exclude:
            return
        if self.methods is not None and method not in self.methods:
            return
        job_id = self._context.job_id
        if job_id is None:
            # Nothing is running, so there is no job this belongs to. Counting
            # it is better than filing it against whichever job ran last.
            self.dropped += 1
            return

        params = message.get("params")
        payload: dict[str, Any] = {"method": method, "params": _clip(params)}
        # `item/started` and `item/completed` say nothing on their own; what
        # kind of item it was lives in the payload. Lifting it is reading the
        # message, not inventing a vocabulary -- a projector should not have to
        # dig for the one field that distinguishes a command from an edit.
        item = params.get("item") if isinstance(params, dict) else None
        if isinstance(item, dict) and isinstance(item.get("type"), str):
            payload["item_type"] = item["type"]
        self._pending.append((job_id, kind_for(method), payload, self._context.agent))
        if len(self._pending) >= self.buffer_size:
            self.flush()

    # -- writing out -----------------------------------------------------

    def flush(self) -> int:
        """Write what is buffered, one transaction each. Returns how many landed.

        Not a batch. The frozen ledger commits per event, so this is a loop of
        commits -- what it buys is that they happen here rather than inside the
        runtime's read callback.
        """
        if not self._pending:
            return 0
        pending, self._pending = self._pending, []
        written = 0
        terminal: dict[str, bool] = {}
        for job_id, kind, payload, agent in pending:
            try:
                if job_id not in terminal:
                    terminal[job_id] = self.ledger.job(job_id).state == J.DONE
                if terminal[job_id]:
                    # A turn interrupt may emit a short protocol tail after
                    # ``control.cancel``. Keeping it would make the activity
                    # feed look as though a cancelled job continued working.
                    self.dropped += 1
                    continue
                self.ledger.append_event(job_id, kind, payload, agent)
                written += 1
            except Exception:  # noqa: BLE001 - a lost notification is not a lost run
                self.dropped += 1
        return written

    @contextmanager
    def recording(self, job_id: str, agent: str | None = None) -> Iterator[None]:
        """Attribute everything the runtime says to this job, then flush."""
        previous = self._context
        self._context = _Context(job_id=job_id, agent=agent)
        try:
            yield
        finally:
            self.flush()
            self._context = previous

    # -- wrapping a runner -----------------------------------------------

    def wrap(self, runner: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        """An ``AgentRunner`` that records what the runtime did while it ran.

        The job id comes out of the task the loop built, which is the only
        place it exists -- the sink itself is handed raw protocol frames with
        no idea what they are for.
        """

        def recording_runner(agent: AgentDefinition, task: dict[str, Any]) -> dict[str, Any]:
            job_id = str(task.get("job_id") or "")
            if not job_id:
                return runner(agent, task)
            with self.recording(job_id, agent.name):
                return runner(agent, task)

        check = getattr(runner, 'check_resume', None)
        if callable(check):
            recording_runner.check_resume = check
        return recording_runner


def runtime_events(ledger: JobLedger, job_id: str, since: int = 0) -> list[dict[str, Any]]:
    """Just the runtime's own events for one job, in order."""
    return [
        e.describe()
        for e in iter_events(ledger, job_id, since=since)
        if e.kind.startswith(NAMESPACE)
    ]
