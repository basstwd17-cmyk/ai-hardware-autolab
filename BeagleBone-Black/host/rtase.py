#!/usr/bin/env python3
"""RTASE_V1 command-line controller for the BBB PRU firmware.

Run from Windows (SSH to BB_HOST, default 192.168.50.2):
    python host/rtase.py status
    python host/rtase.py demo --pattern ramp --shots 100 --delay-ns 100
    python host/rtase.py demo --pattern random --seed 1234 --shots 100

The provisioner installs this same file on the BBB.  ``--local`` talks directly
to /dev/rpmsg0 and is used automatically by the Windows-side wrapper.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import random
import select
import shlex
import struct
import sys
from pathlib import Path
from typing import Iterable, Sequence


CMD_VERSION = 0x03
CMD_BUFFER_RESET = 0x50
CMD_BUFFER_APPEND = 0x51
CMD_RUN = 0x52
CMD_STATUS = 0x53
MAX_SYMBOLS = 10_000
MAX_APPEND_SYMBOLS = 4 * (496 - 3)
PRU_HZ = 200_000_000
CONTROL_LOCK = "/run/lock/rtase-control.lock"


class RtaseError(RuntimeError):
    pass


@contextmanager
def control_lock():
    """Serialize PRU buffer/run operations with CPLD CODE transactions."""
    import fcntl

    fd = os.open(CONTROL_LOCK, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def validate_reply(payload: bytes, reply: bytes) -> bytes:
    """Validate the common RTASE reply envelope.

    VERSION predates the ACK byte used by the RTASE extension commands and is
    encoded as ``[0x03][ascii...]``.  All other commands use
    ``[command][1][payload...]`` on success.
    """
    if len(reply) < 2:
        raise RtaseError(f"Short PRU reply: {reply.hex()}")
    if reply[0] != payload[0]:
        raise RtaseError(
            f"Reply command mismatch: 0x{reply[0]:02x} != 0x{payload[0]:02x}"
        )
    if payload[0] != CMD_VERSION and reply[1] != 1:
        raise RtaseError(
            f"PRU rejected command 0x{payload[0]:02x}: {reply.hex()}"
        )
    return reply


def pack_symbols(symbols: Iterable[int]) -> bytes:
    values = list(symbols)
    if any(value not in (0, 1, 2, 3) for value in values):
        raise ValueError("Every RTASE symbol must be a two-bit value (0..3).")
    packed = bytearray((len(values) + 3) // 4)
    for index, value in enumerate(values):
        packed[index >> 2] |= value << ((index & 3) * 2)
    return bytes(packed)


def unpack_symbols(packed: bytes, count: int) -> list[int]:
    if count < 0 or len(packed) < (count + 3) // 4:
        raise ValueError("Packed data is shorter than the requested symbol count.")
    return [
        (packed[index >> 2] >> ((index & 3) * 2)) & 3
        for index in range(count)
    ]


def ns_to_cycles(value_ns: int) -> int:
    if value_ns < 0 or value_ns % 5:
        raise ValueError("PRU delay must be a non-negative multiple of 5 ns.")
    cycles = value_ns // 5
    if cycles > 0xFFFF:
        raise ValueError("Selection-to-update delay exceeds 327675 ns.")
    return cycles


def us_to_cycles(value_us: int) -> int:
    if value_us < 0:
        raise ValueError("Inter-shot gap cannot be negative.")
    cycles = value_us * (PRU_HZ // 1_000_000)
    if cycles > 0xFFFFFFFF:
        raise ValueError("Inter-shot gap is too large for the protocol.")
    return cycles


class Rpmsg:
    def __init__(self, device: str = "/dev/rpmsg0") -> None:
        self.device = device

    def request(self, payload: bytes, timeout_s: float = 5.0) -> bytes:
        fd = os.open(self.device, os.O_RDWR)
        try:
            os.write(fd, payload)
            ready, _, _ = select.select([fd], [], [], timeout_s)
            if not ready:
                raise RtaseError(
                    f"Timed out after {timeout_s:.1f} s waiting for PRU reply."
                )
            reply = os.read(fd, 496)
        finally:
            os.close(fd)
        return validate_reply(payload, reply)


def make_sequence(pattern: str, count: int, seed: int) -> list[int]:
    if not 1 <= count <= MAX_SYMBOLS:
        raise ValueError(f"Sequence count must be 1..{MAX_SYMBOLS}.")
    if pattern == "ramp":
        return [index & 3 for index in range(count)]
    rng = random.Random(seed)
    return [rng.randrange(4) for _ in range(count)]


def load_sequence(link: Rpmsg, symbols: Sequence[int]) -> int:
    if not 1 <= len(symbols) <= MAX_SYMBOLS:
        raise ValueError(f"Sequence length must be 1..{MAX_SYMBOLS}.")
    link.request(bytes([CMD_BUFFER_RESET]))
    total = 0
    for start in range(0, len(symbols), MAX_APPEND_SYMBOLS):
        chunk = symbols[start : start + MAX_APPEND_SYMBOLS]
        request = bytes([CMD_BUFFER_APPEND]) + struct.pack("<H", len(chunk)) + pack_symbols(chunk)
        reply = link.request(request)
        if len(reply) != 4:
            raise RtaseError(f"Malformed buffer reply: {reply.hex()}")
        total = struct.unpack_from("<H", reply, 2)[0]
    if total != len(symbols):
        raise RtaseError(f"PRU reports {total} symbols, expected {len(symbols)}.")
    return total


def run_sequence(
    link: Rpmsg,
    *,
    delay_ns: int,
    pulse_ns: int,
    gap_us: int,
    shots: int,
) -> dict[str, int]:
    if not 1 <= shots <= 100_000:
        raise ValueError("shots must be 1..100000.")
    delay_cycles = ns_to_cycles(delay_ns)
    pulse_cycles = ns_to_cycles(pulse_ns)
    if pulse_cycles == 0:
        raise ValueError("UPDATE pulse width must be at least 5 ns.")
    gap_cycles = us_to_cycles(gap_us)
    request = bytes([CMD_RUN]) + struct.pack(
        "<HHII", delay_cycles, pulse_cycles, gap_cycles, shots
    )
    expected_s = shots * (gap_us / 1_000_000.0) + 10.0
    reply = link.request(request, timeout_s=max(10.0, expected_s))
    if len(reply) != 7:
        raise RtaseError(f"Malformed run reply: {reply.hex()}")
    completed = struct.unpack_from("<I", reply, 2)[0]
    return {
        "shots_completed": completed,
        "last_symbol": reply[6],
        "delay_ns": delay_ns,
        "pulse_ns": pulse_ns,
        "gap_us": gap_us,
    }


def direct_command(args: argparse.Namespace) -> dict[str, object]:
    with control_lock():
        link = Rpmsg(args.device)
        if args.command == "status":
            version = link.request(bytes([CMD_VERSION]))[1:].decode("ascii", "replace")
            status = link.request(bytes([CMD_STATUS]))
            if len(status) != 5:
                raise RtaseError(f"Malformed status reply: {status.hex()}")
            return {
                "ok": True,
                "version": version,
                "symbols_loaded": struct.unpack_from("<H", status, 2)[0],
                "outputs_active": bool(status[4]),
                "device": args.device,
            }

        symbols = make_sequence(args.pattern, args.sequence_count, args.seed)
        loaded = load_sequence(link, symbols)
        result: dict[str, object] = {
            "ok": True,
            "pattern": args.pattern,
            "seed": args.seed,
            "symbols_loaded": loaded,
            "first_symbols": symbols[:16],
        }
        if args.command == "load":
            return result
        result.update(
            run_sequence(
                link,
                delay_ns=args.delay_ns,
                pulse_ns=args.pulse_ns,
                gap_us=args.gap_us,
                shots=args.shots,
            )
        )
        return result


def remote_command(args: argparse.Namespace, argv: Sequence[str]) -> int:
    # paramiko is intentionally imported only on the Windows/SSH path; the
    # copy running on the BBB needs only the Python standard library.
    try:
        from provision import connect, run
    except ModuleNotFoundError as exc:
        raise RtaseError(
            "Windows host dependency missing. Run: python -m pip install -r requirements.txt"
        ) from exc
    remote_args = ["--local", "--device", args.device, *argv]
    command = "python3 ~/pru/rtase.py " + " ".join(map(shlex.quote, remote_args))
    client = connect(host=args.host)
    try:
        rc, out, err = run(client, command, timeout=1800)
    finally:
        client.close()
    if out:
        sys.stdout.write(out)
    if err.strip():
        sys.stderr.write(err)
    return rc


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default=os.environ.get("BB_HOST", "192.168.50.2"))
    p.add_argument("--device", default="/dev/rpmsg0")
    p.add_argument("--local", action="store_true", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    for name in ("load", "demo"):
        q = sub.add_parser(name)
        q.add_argument("--pattern", choices=("random", "ramp"), default="random")
        q.add_argument("--seed", type=int, default=1)
        q.add_argument("--sequence-count", type=int, default=256)
        if name == "demo":
            q.add_argument("--shots", type=int, default=100)
            q.add_argument("--delay-ns", type=int, default=100)
            q.add_argument("--pulse-ns", type=int, default=20)
            q.add_argument("--gap-us", type=int, default=100_000)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(raw)
    try:
        if not args.local:
            return remote_command(args, raw)
        print(json.dumps(direct_command(args), indent=2, sort_keys=True))
        return 0
    except (OSError, RtaseError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
