"""Recording SCPI transport.

This is the fixed half of instrument control: connect, send, read, hash,
record. It does not know what is being measured. Measurement recipes -- channel
scale, timebase, trigger, how many samples are "enough" -- are written fresh per
experiment and sit on top of this.

Everything sent and received is logged, so a freshly written recipe cannot lose
provenance even if its author forgets to log. That is the whole point of putting
the transport in layer 3 instead of letting each recipe open its own socket.
"""

from __future__ import annotations

import math
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable


class ScpiError(RuntimeError):
    pass


@dataclass
class ScpiExchange:
    at_s: float
    direction: str  # "write" | "query"
    command: str
    response: str | None = None


class ScpiTransport:
    """Line-oriented SCPI over TCP, with a full transcript.

    mock=True answers from `mock_responses` instead of opening a socket, so
    orchestrators and recipes can be developed and tested with no instrument
    on the bench. Unknown queries return "0" rather than raising, and every
    exchange is still recorded.
    """

    def __init__(
        self,
        host: str,
        port: int = 5025,
        timeout_s: float = 5.0,
        mock: bool = False,
        mock_responses: dict[str, str] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.mock = mock
        self.transcript: list[ScpiExchange] = []
        self._mock_responses = dict(mock_responses or {})
        self._t0 = time.monotonic()
        self._socket: socket.socket | None = None
        # Own buffer instead of socket.makefile(): a makefile reader is
        # permanently unusable after one timeout ("cannot read from timed out
        # object"), so a single unanswered query would kill the whole session.
        # An instrument returns nothing for a command it rejects, which makes
        # that a routine event, not an exceptional one.
        self._buf = bytearray()
        if not mock:
            self._socket = socket.create_connection((host, port), timeout=timeout_s)
            self._socket.settimeout(timeout_s)

    # -- transcript ----------------------------------------------------------

    def _record(self, direction: str, command: str, response: str | None = None) -> None:
        self.transcript.append(
            ScpiExchange(
                at_s=round(time.monotonic() - self._t0, 4),
                direction=direction,
                command=command,
                response=response,
            )
        )

    def commands(self) -> list[str]:
        """Exact SCPI lines issued, for ToolResult.commands."""
        return [e.command for e in self.transcript]

    # -- raw reads -----------------------------------------------------------

    def _recv_into_buffer(self) -> None:
        assert self._socket is not None
        chunk = self._socket.recv(65536)
        if not chunk:
            raise ScpiError("Instrument closed the connection")
        self._buf.extend(chunk)

    def _read_line(self) -> bytes:
        while b"\n" not in self._buf:
            self._recv_into_buffer()
        index = self._buf.index(b"\n")
        line = bytes(self._buf[:index])
        del self._buf[: index + 1]
        return line

    def _read_exact(self, count: int) -> bytes:
        while len(self._buf) < count:
            self._recv_into_buffer()
        data = bytes(self._buf[:count])
        del self._buf[:count]
        return data

    def resync(self) -> int:
        """Drop anything still buffered after a failed query.

        A rejected command produces no response, so the next reply can arrive
        out of step with the query that asked for it. Calling this keeps one
        bad command from shifting every later answer by one.
        """
        dropped = len(self._buf)
        self._buf.clear()
        if self._socket is None:
            return dropped
        self._socket.settimeout(0.2)
        try:
            while True:
                chunk = self._socket.recv(65536)
                if not chunk:
                    break
                dropped += len(chunk)
        except (socket.timeout, TimeoutError, OSError):
            pass
        finally:
            self._socket.settimeout(self.timeout_s)
        return dropped

    # -- primitives ----------------------------------------------------------

    def write(self, command: str) -> None:
        self._record("write", command)
        if self.mock:
            return
        assert self._socket is not None
        self._socket.sendall((command + "\n").encode("ascii"))

    def query(self, command: str) -> str:
        if self.mock:
            response = self._mock_responses.get(command, "0")
            self._record("query", command, response)
            return response
        assert self._socket is not None
        self._socket.sendall((command + "\n").encode("ascii"))
        try:
            raw = self._read_line()
        except (socket.timeout, TimeoutError) as exc:
            self._record("query", command, None)
            self.resync()
            raise ScpiError(
                f"No SCPI response for {command} within {self.timeout_s:g}s "
                f"(the instrument most likely rejected the command)"
            ) from exc
        response = raw.decode("ascii", errors="replace").strip()
        self._record("query", command, response)
        return response

    def query_float(self, command: str) -> float:
        value = float(self.query(command))
        # Instruments report "no result yet" as 9.91e37 (IEEE NaN surrogate).
        # Treating that as a real number silently poisons a measurement.
        if not math.isfinite(value) or abs(value) > 1e36:
            raise ScpiError(f"Invalid result for {command}: {value}")
        return value

    def query_binary_block(self, command: str) -> bytes:
        """Read an IEEE 488.2 definite-length block: #<n><length><payload><LF>.

        The terminator handling matters: some firmware sends CRLF, some LF. A
        reader that assumes one strips a byte off the next response and every
        later query is shifted by one.
        """
        if self.mock:
            self._record("query", command, "<binary block, mocked>")
            return b""
        assert self._socket is not None
        self._socket.sendall((command + "\n").encode("ascii"))
        try:
            marker = self._read_exact(1)
            if marker != b"#":
                raise ScpiError(f"Invalid binary block marker for {command}: {marker!r}")
            digits_raw = self._read_exact(1)
            if not digits_raw.isdigit():
                raise ScpiError(f"Invalid binary block length header for {command}")
            byte_count = int(self._read_exact(int(digits_raw)).decode("ascii"))
            payload = self._read_exact(byte_count)
            terminator = self._read_exact(1)
            if terminator not in (b"\n", b"\r"):
                raise ScpiError(f"Invalid binary block terminator for {command}")
            if terminator == b"\r" and self._read_exact(1) != b"\n":
                raise ScpiError(f"Invalid binary block line ending for {command}")
        except (socket.timeout, TimeoutError) as exc:
            self._record("query", command, None)
            self.resync()
            raise ScpiError(f"Incomplete binary block for {command}") from exc
        self._record("query", command, f"<binary block, {byte_count} bytes>")
        return payload

    # -- error queue ---------------------------------------------------------

    def check_errors(self, context: str = "") -> None:
        """Drain the error queue and raise if anything is in it.

        Call after every configuration group. An instrument accepts a malformed
        command silently and keeps running with the previous setting, so without
        this a recipe measures the wrong thing and reports success.
        """
        response = self.query("SYST:ERR:ALL?")
        if not response.startswith("0,"):
            where = f" during {context}" if context else ""
            raise ScpiError(f"SCPI error queue not empty{where}: {response}")

    # -- lifecycle -----------------------------------------------------------

    def wait_until(
        self,
        predicate: Callable[[], bool],
        timeout_s: float,
        poll_s: float = 0.1,
        on_timeout: str = "condition not met",
    ) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(poll_s)
        raise ScpiError(f"Timed out after {timeout_s:g}s: {on_timeout}")

    def close(self) -> None:
        if self.mock:
            return
        if self._socket is not None:
            self._socket.close()

    def __enter__(self) -> "ScpiTransport":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
