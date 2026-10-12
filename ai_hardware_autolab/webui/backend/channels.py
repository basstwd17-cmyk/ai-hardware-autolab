"""One browser's end of the stream, and what happens when it stops reading.

A tab that is backgrounded, throttled or wedged stops draining its socket. The
frames keep coming, because the daemon does not slow down for a UI. Something
has to give, and there are only three choices:

* **Grow the queue.** The process's memory becomes a function of how long a tab
  stays wedged. Not a bound, a hope.
* **Drop the oldest frame.** Cheap, and it silently breaks the one invariant
  the whole protocol rests on: one frame per global cursor, no gaps. The tab
  goes on rendering, now missing durable history, and nothing says so.
* **Say so, and stop.** Tell the client its view has a hole and end the
  connection, so there is no stream left to misapply.

This module does the third, and the "and stop" is the part that took a
correction. An earlier version sent ``webui.resync_required`` and kept
delivering, on the reasoning that the client could rebase from ``/api/state``
while newer frames arrived, since the protocol's idempotence rule would drop
the overlap. That reasoning is wrong in one direction: **idempotence resolves
duplicates, not omissions.** The frames dropped during the overflow are gone
either way, and a client that goes on applying what arrives after the notice
holds a view with a hole in the middle of it and no way to know.

So the contract is:

    overflow  ->  send webui.resync_required (with the cursor actually applied)
              ->  close the WebSocket
              ->  client re-reads /api/state (or /api/graph)
              ->  client reconnects with after=<that snapshot's cursor>

The server closing is what makes it a contract rather than a request. A client
that ignores the notice and keeps its socket open cannot exist, because the
socket is not open. That rules out the wrong client implementation instead of
documenting it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

# Frames one browser may be behind before its view is declared holed. A job's
# whole life is a few hundred rows, so this is several jobs of slack -- a tab
# that exceeds it is not slow, it is not reading.
DEFAULT_CAPACITY = 512

RESYNC_REQUIRED = "webui.resync_required"

# WebSocket close code. 1008 is "policy violation", which is what the client
# reading too slowly to keep a gapless stream amounts to. It is deliberately
# not 1000: a normal close would let a reconnect loop treat this as routine.
RESYNC_CLOSE_CODE = 1008


@dataclass(eq=False)  # identity, so a channel can live in a set
class ClientChannel:
    """A bounded queue that ends the connection rather than hide a gap."""

    capacity: int = DEFAULT_CAPACITY
    queue: asyncio.Queue = field(default=None, repr=False)  # type: ignore[assignment]
    dropped: int = 0
    holed: bool = False

    def __post_init__(self) -> None:
        if self.queue is None:
            self.queue = asyncio.Queue(maxsize=self.capacity)

    def offer(self, frame: dict[str, Any], cursor: int) -> None:
        """Enqueue one frame. Runs on the event loop thread, never blocks."""
        if self.holed:
            # The notice is already queued and the socket is on its way out.
            # Anything after it would be a frame the client must not apply.
            self.dropped += 1
            return
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            self._declare_hole(cursor)

    def _declare_hole(self, cursor: int) -> None:
        lost = self.queue.qsize()
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self.dropped += lost + 1  # the queued frames, plus the one that did not fit
        self.holed = True
        self.queue.put_nowait(
            {
                "type": RESYNC_REQUIRED,
                "reason": (
                    "this connection fell behind. Frames were dropped, so the stream is no "
                    "longer gapless and this socket is closing. Re-read /api/state and "
                    "reconnect with after=<its cursor>."
                ),
                "cursor": cursor,
                "dropped": self.dropped,
                "capacity": self.capacity,
            }
        )

    def describe(self) -> dict[str, Any]:
        return {
            "capacity": self.capacity,
            "queued": self.queue.qsize(),
            "dropped": self.dropped,
            "holed": self.holed,
        }
