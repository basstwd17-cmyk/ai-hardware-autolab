"""Where the user put things, and what happens when two tabs disagree.

A graph layout is the one piece of state in this app that the *user* authors.
Everything else is derived from the ledger and can be thrown away and rebuilt;
a position is a judgement about what belongs next to what, and losing it is
losing work. So this file is more careful than its size suggests.

**The conflict policy is reject, then merge.** Two tabs open on the same graph
will both hold a revision, and the second to save has a stale one. The options
were last-write-wins or a refusal, and last-write-wins is wrong here for the
same reason it is wrong everywhere in this project: it discards the other tab's
arrangement without anyone finding out. So a stale revision is a 409 that
*carries the current layout back*, and the client merges its own moved nodes
over that copy and retries. Positions are independent per node, so the merge is
well defined -- per node, the later write wins; the file as a whole is never
clobbered. The user sees neither a lost drag nor an error.

**The write is atomic.** A temporary file in the same directory, then
``os.replace``, which is ``MoveFileEx`` with ``REPLACE_EXISTING`` on Windows.
Writing in place would mean a process killed mid-write leaves a truncated JSON
file, and the next start would find the layout not merely stale but unreadable.

**A file it cannot interpret is not overwritten.** An unknown ``version``, or
JSON that is not a layout, makes the read return an empty layout *and say so*.
The alternative -- treating an unreadable file as absent and writing over it --
destroys whatever a future version of this app wrote there.
"""

from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = 1

DEFAULT_LAYOUT_FILE = Path(__file__).resolve().parents[1] / "state" / "graph-layout.json"

# Bounds. Every one of these is a refusal rather than a truncation, because a
# layout silently missing the node the user just moved is worse than a save
# that says why it did not happen.
MAX_POSITIONS = 5000
MAX_ID_LENGTH = 256
MAX_COORDINATE = 1_000_000.0


class LayoutError(ValueError):
    """A layout this store will not write, with the reason."""


class LayoutConflict(Exception):
    """A save against a revision that is no longer current."""

    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__(
            f"the layout has moved on: it is at revision {current['revision']}. "
            "Merge your moved nodes onto this copy and save again."
        )
        self.current = current


@dataclass
class Position:
    x: float
    y: float

    def describe(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y}


@dataclass
class LayoutStore:
    """The layout file, read fresh and written whole."""

    path: Path = DEFAULT_LAYOUT_FILE
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # -- read -------------------------------------------------------------

    def read(self) -> dict[str, Any]:
        """The layout on disk, or an empty one. Never raises."""
        with self._lock:
            return self._read_locked()

    def _read_locked(self) -> dict[str, Any]:
        if not self.path.is_file():
            return _empty()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            return _empty(unreadable=f"{type(exc).__name__}: {exc}")
        if not isinstance(raw, dict):
            return _empty(unreadable="the layout file is not a JSON object")
        if raw.get("version") != VERSION:
            # Not an error and not something to overwrite: a file written by a
            # version this one does not understand is still somebody's work.
            return _empty(unreadable=f"unknown layout version {raw.get('version')!r}")

        positions: dict[str, dict[str, float]] = {}
        for key, value in (raw.get("positions") or {}).items():
            try:
                point = _point(key, value)
            except LayoutError:
                continue  # one bad row does not lose the rest of the file
            positions[key] = point.describe()
        revision = raw.get("revision")
        return {
            "version": VERSION,
            "revision": revision if isinstance(revision, int) and revision >= 0 else 0,
            "positions": positions,
            "unreadable": None,
        }

    # -- write ------------------------------------------------------------

    def write(self, positions: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        """Replace the layout, if the caller is writing against the current one.

        Raises ``LayoutConflict`` carrying the current layout when it is not.
        """
        checked = _validate(positions)
        with self._lock:
            current = self._read_locked()
            if expected_revision != current["revision"]:
                raise LayoutConflict(current)

            payload = {
                "version": VERSION,
                "revision": current["revision"] + 1,
                "positions": {key: point.describe() for key, point in checked.items()},
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temporary, self.path)
            return {**payload, "unreadable": None}


def _empty(unreadable: str | None = None) -> dict[str, Any]:
    return {"version": VERSION, "revision": 0, "positions": {}, "unreadable": unreadable}


def _validate(positions: Any) -> dict[str, Position]:
    if not isinstance(positions, dict):
        raise LayoutError("positions must be an object of nodeId -> {x, y}")
    if len(positions) > MAX_POSITIONS:
        raise LayoutError(
            f"{len(positions)} positions is more than {MAX_POSITIONS}. This file is a record "
            "of what a person moved by hand; that many is a bug somewhere, not a layout."
        )
    return {key: _point(key, value) for key, value in positions.items()}


def _point(key: Any, value: Any) -> Position:
    if not isinstance(key, str) or not key.strip():
        raise LayoutError(f"a position key must be a non-empty node id, got {key!r}")
    if len(key) > MAX_ID_LENGTH:
        raise LayoutError(f"node id longer than {MAX_ID_LENGTH} characters")
    if not isinstance(value, dict):
        raise LayoutError(f"{key}: a position must be an object with x and y")
    return Position(x=_coordinate(key, value.get("x")), y=_coordinate(key, value.get("y")))


def _coordinate(key: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LayoutError(f"{key}: x and y must be numbers, got {value!r}")
    number = float(value)
    # NaN and infinity round-trip through JSON in some encoders and then make
    # every layout comparison false. They are refused rather than clamped.
    if not math.isfinite(number):
        raise LayoutError(f"{key}: x and y must be finite, got {value!r}")
    if abs(number) > MAX_COORDINATE:
        raise LayoutError(f"{key}: {number} is outside +-{MAX_COORDINATE:.0f}")
    return number
