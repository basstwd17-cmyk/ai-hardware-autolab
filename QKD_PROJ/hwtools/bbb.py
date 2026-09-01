"""BeagleBone Black tools: deploy the PRU firmware, run reviewed diagnostics.

These are the layer 3 tools for the host side of the rig. They talk to the BBB
over SSH through ``ssh.SshTransport`` and return a ToolResult like every other
tool here. They do not touch the RTASE_V1 board: no JTAG, no CPLD
programming, no DAC data, no analog output. That restriction is not a comment
-- ``contracts/gate9-bbb-host-tooling.yaml`` lists those operations under
``forbidden_operations``, so the same SafetyGate that permits a deploy refuses
a program.

Failure is reported as a stage, not as prose. Every step appends to
``measurements["stages"]`` and the first one that fails is named in
``measurements["failed_stage"]``; a caller can tell "the board was never
reachable" from "the firmware built but the PRU never answered" without
parsing an error string.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from pathlib import Path
from typing import Any, Iterator, Mapping

from .record import RunRecord
from .result import PASS, ToolResult
from .safety import SafetyGate, SafetyViolation
from .ssh import SshError, SshExchange, SshTarget, SshTransport, sha256_bytes

REPO_ROOT = Path(__file__).resolve().parents[2]

SAFE_REMOTE_PATH = re.compile(r"^[A-Za-z0-9_./~-]+$")
HEX_ONLY = re.compile(r"^[0-9a-fA-F]+$")

DEFAULT_COMMAND_TIMEOUT_S = 120.0
DEFAULT_BUILD_TIMEOUT_S = 900.0
DEFAULT_SERVICE_TIMEOUT_S = 180.0
DEFAULT_SCRIPT_TIMEOUT_S = 300.0


class BbbError(RuntimeError):
    """A stage refused or failed. Carries the stage name for the record."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


# -- contract access ---------------------------------------------------------


def _config(gate: SafetyGate) -> dict[str, Any]:
    spec = gate.contracts.test_spec
    config = spec.get("bbb")
    if not isinstance(config, dict):
        raise BbbError(
            "contract",
            f"{gate.contracts.test_spec_path.name} has no 'bbb' section; this tool "
            "needs the BBB host-tooling contract.",
        )
    return config


def _required(config: Mapping[str, Any], *path: str) -> Any:
    node: Any = config
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise BbbError("contract", f"Contract has no field 'bbb.{'.'.join(path)}'")
        node = node[key]
    if node is None or (isinstance(node, str) and not node.strip()):
        raise BbbError("contract", f"Contract field 'bbb.{'.'.join(path)}' is empty")
    return node


def _safe_remote(path: str, what: str) -> str:
    if not SAFE_REMOTE_PATH.fullmatch(path):
        raise BbbError("contract", f"Unsafe {what} in contract: {path!r}")
    return path


# -- stage bookkeeping -------------------------------------------------------


@contextlib.contextmanager
def _stage(result: ToolResult, name: str) -> Iterator[dict[str, Any]]:
    entry: dict[str, Any] = {"stage": name, "ok": False}
    result.measurements.setdefault("stages", []).append(entry)
    started = time.monotonic()
    try:
        yield entry
    except Exception:
        entry["duration_s"] = round(time.monotonic() - started, 3)
        result.measurements["failed_stage"] = name
        raise
    entry["duration_s"] = round(time.monotonic() - started, 3)
    entry["ok"] = True


def _require_rc(exchange: SshExchange, stage: str, what: str) -> SshExchange:
    if exchange.returncode != 0:
        tail = (exchange.stderr or exchange.stdout).strip().splitlines()[-8:]
        raise BbbError(
            stage,
            f"{what} returned {exchange.returncode}: " + " | ".join(tail),
        )
    return exchange


def _record_exchange(entry: dict[str, Any], exchange: SshExchange) -> SshExchange:
    """Keep the full text in the stage, not just a summary.

    The BBB's state cannot be re-derived from files on this machine, so a
    truncated log is a measurement that cannot be checked later.
    """
    entry["returncode"] = exchange.returncode
    entry["stdout"] = exchange.stdout
    entry["stderr"] = exchange.stderr
    return exchange


# -- shared plumbing ---------------------------------------------------------


def _open_transport(
    result: ToolResult,
    transport: SshTransport | None,
    env: Mapping[str, str] | None,
) -> tuple[SshTransport, bool]:
    if transport is not None:
        result.measurements["target"] = transport.target.describe()
        return transport, False
    try:
        target = SshTarget.from_env(env)
    except SshError as exc:
        raise BbbError("connect", str(exc)) from exc
    result.measurements["target"] = target.describe()
    return SshTransport(target), True


def _save_logs(result: ToolResult, record: RunRecord | None, transport: SshTransport) -> None:
    """Copy the transcript into the run directory and hash it."""
    result.commands = transport.commands()
    if record is None:
        return
    transcript = record.dir / f"{result.tool}_{result.action}.transcript.json"
    transcript.write_text(
        json.dumps(transport.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    result.add_artifact(transcript, kind="ssh_transcript", relative_to=record.dir)
    for exchange in transport.transcript:
        if exchange.kind != "exec":
            continue
        for stream, text in (("stdout", exchange.stdout), ("stderr", exchange.stderr)):
            if not text.strip():
                continue
            name = re.sub(r"[^A-Za-z0-9_.-]+", "_", exchange.label)
            path = record.dir / f"{result.action}.{name}.{stream}.log"
            index = 1
            while path.exists():
                index += 1
                path = record.dir / f"{result.action}.{name}.{index}.{stream}.log"
            path.write_text(text, encoding="utf-8")
            result.add_artifact(path, kind=f"bbb_{stream}", relative_to=record.dir)


def _finish(
    result: ToolResult,
    started: float,
    record: RunRecord | None,
    failure: str | None = None,
) -> ToolResult:
    result.duration_s = round(time.monotonic() - started, 3)
    if failure is not None:
        result.fail(failure)
    elif result.verdict is None:
        result.verdict = PASS
    if record is not None:
        record.add(result)
    return result


# -- run_bbb -----------------------------------------------------------------


def run_bbb(
    gate: SafetyGate,
    script: str,
    record: RunRecord | None = None,
    *,
    transport: SshTransport | None = None,
    env: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
    timeout_s: float = DEFAULT_SCRIPT_TIMEOUT_S,
) -> ToolResult:
    """Run one allowlisted script on the BBB and record everything it produced.

    ``script`` is a name from the contract's ``scripts`` map, never a path and
    never inline shell. The named file's content hash must still match the
    reviewed value: a path can be made to point somewhere else between review
    and execution, a hash cannot.
    """
    started = time.monotonic()
    result = ToolResult(tool="bbb", action="run_script")
    result.measurements["script"] = script
    owns_transport = False
    active: SshTransport | None = transport
    try:
        with _stage(result, "safety_gate"):
            gate.require("bbb_ssh_command")

        with _stage(result, "allowlist") as entry:
            allowlist = gate.contracts.test_spec.get("scripts")
            if not isinstance(allowlist, dict) or not allowlist:
                raise BbbError(
                    "allowlist",
                    f"{gate.contracts.test_spec_path.name} defines no script allowlist.",
                )
            entry["allowed"] = sorted(allowlist)
            if script not in allowlist:
                raise BbbError(
                    "allowlist",
                    f"'{script}' is not in the script allowlist of "
                    f"{gate.contracts.test_spec_path.name}. Allowed: "
                    + ", ".join(sorted(allowlist))
                    + ". Add it to the contract and have a human review it first.",
                )
            approved = allowlist[script] or {}
            expected_sha = str(approved.get("sha256") or "").strip().lower()
            if len(expected_sha) != 64:
                raise BbbError(
                    "allowlist", f"Script '{script}' has no full sha256 in the contract."
                )
            entry["effect"] = approved.get("effect")

        with _stage(result, "script_hash") as entry:
            root = Path(repo_root) if repo_root is not None else REPO_ROOT
            path = (root / str(approved["path"])).resolve()
            if not path.is_file():
                raise BbbError("script_hash", f"Allowlisted script is missing: {path}")
            payload = path.read_bytes()
            actual_sha = sha256_bytes(payload)
            entry["path"] = str(path)
            entry["sha256"] = actual_sha
            result.measurements["script_path"] = str(path)
            result.measurements["script_sha256"] = actual_sha
            result.measurements["script_bytes"] = len(payload)
            if actual_sha != expected_sha:
                raise BbbError(
                    "script_hash",
                    f"'{script}' no longer matches the reviewed content: contract "
                    f"{expected_sha}, file {actual_sha}. Re-review the script and "
                    "update the contract before running it.",
                )

        with _stage(result, "connect"):
            active, owns_transport = _open_transport(result, transport, env)
            active.connect()

        expected_rc = int(approved.get("expected_returncode", 0))
        with _stage(result, "execute") as entry:
            text = payload.decode("utf-8", errors="replace")
            exchange = active.run(text, label=script, timeout_s=timeout_s)
            _record_exchange(entry, exchange)
            result.measurements["returncode"] = exchange.returncode
            result.measurements["expected_returncode"] = expected_rc
            result.measurements["stdout"] = exchange.stdout
            result.measurements["stderr"] = exchange.stderr
            result.measurements["stdout_bytes"] = len(exchange.stdout.encode("utf-8"))
            result.measurements["stderr_bytes"] = len(exchange.stderr.encode("utf-8"))
            if exchange.returncode != expected_rc:
                raise BbbError(
                    "execute",
                    f"'{script}' exited {exchange.returncode}, contract expects "
                    f"{expected_rc}.",
                )
    except (BbbError, SshError, SafetyViolation, OSError, KeyError, TypeError, ValueError) as exc:
        if active is not None:
            _save_logs(result, record, active)
            if owns_transport:
                active.close()
        return _finish(result, started, record, failure=str(exc))

    _save_logs(result, record, active)
    if owns_transport and active is not None:
        active.close()
    return _finish(result, started, record)


# -- deploy_pru --------------------------------------------------------------


def deploy_pru(
    gate: SafetyGate,
    record: RunRecord | None = None,
    *,
    transport: SshTransport | None = None,
    env: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
    command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    build_timeout_s: float = DEFAULT_BUILD_TIMEOUT_S,
    service_timeout_s: float = DEFAULT_SERVICE_TIMEOUT_S,
) -> ToolResult:
    """Upload, build, install and restart the PRU firmware, then prove it answers.

    Stages: safety_gate, connect, verify_target, upload, verify_upload, build,
    install, service, verify_protocol, ping. The firmware is restarted through
    ``rtase-pru.service`` rather than by writing remoteproc directly, so the
    boot path and this tool cannot leave the board in two different states.
    """
    started = time.monotonic()
    result = ToolResult(tool="bbb", action="deploy_pru")
    owns_transport = False
    active: SshTransport | None = transport
    try:
        with _stage(result, "safety_gate"):
            gate.require("pru_deploy")
            config = _config(gate)
            root = (Path(repo_root) if repo_root is not None else REPO_ROOT) / str(
                _required(config, "repo_root")
            )
            if not root.is_dir():
                raise BbbError("safety_gate", f"BBB repository root is missing: {root}")
            remote_root = _safe_remote(str(_required(config, "remote_root")), "remote_root")
            firmware = _required(config, "firmware")
            service = _required(config, "service")
            verify = _required(config, "verify")

        with _stage(result, "connect"):
            active, owns_transport = _open_transport(result, transport, env)
            active.connect()

        with _stage(result, "verify_target") as entry:
            expected_model = str(_required(config, "target_model_substring"))
            exchange = _record_exchange(
                entry,
                active.run(
                    "tr -d '\\000' </proc/device-tree/model; echo; uname -r",
                    label="identify",
                    timeout_s=command_timeout_s,
                ),
            )
            _require_rc(exchange, "verify_target", "target identification")
            lines = [line.strip() for line in exchange.stdout.splitlines() if line.strip()]
            model = lines[0] if lines else ""
            kernel = lines[1] if len(lines) > 1 else ""
            entry["model"] = model
            result.measurements["model"] = model
            result.versions["bbb_model"] = model
            result.versions["bbb_kernel"] = kernel
            if expected_model not in model:
                raise BbbError(
                    "verify_target",
                    f"Target is not the expected board: {model!r} does not contain "
                    f"{expected_model!r}. Refusing to deploy.",
                )

        with _stage(result, "upload") as entry:
            uploads = _plan_uploads(config, root, remote_root)
            directories = sorted({str(Path(remote).parent) for _, remote in uploads})
            _require_rc(
                active.run(
                    "mkdir -p " + " ".join(f"'{name}'" for name in directories),
                    label="mkdir",
                    timeout_s=command_timeout_s,
                ),
                "upload",
                "remote mkdir",
            )
            sent: dict[str, str] = {}
            for local, remote in uploads:
                exchange = active.put(local, remote, label=Path(remote).name)
                sent[remote] = str(exchange.sha256)
            entry["file_count"] = len(sent)
            result.measurements["uploaded_sha256"] = sent
            result.measurements["uploaded_file_count"] = len(sent)

        with _stage(result, "verify_upload") as entry:
            listing = " ".join(f"'{remote}'" for remote in sent)
            exchange = _record_exchange(
                entry,
                active.run(f"sha256sum {listing}", label="verify_upload", timeout_s=command_timeout_s),
            )
            _require_rc(exchange, "verify_upload", "remote sha256sum")
            remote_hashes = _parse_sha256sum(exchange.stdout)
            mismatched = sorted(
                remote for remote, digest in sent.items() if remote_hashes.get(remote) != digest
            )
            entry["verified"] = len(sent) - len(mismatched)
            if mismatched:
                raise BbbError(
                    "verify_upload",
                    "Uploaded files differ on the board: " + ", ".join(mismatched[:5]),
                )

        build_dir = f"{remote_root}/{_safe_remote(str(firmware['build_dir']), 'build_dir')}"
        build_output = _safe_remote(str(firmware["build_output"]), "build_output")
        with _stage(result, "build") as entry:
            pru_cgt = _safe_remote(str(firmware["pru_cgt"]), "pru_cgt")
            script = (
                f"cd ~/{build_dir} && "
                "make clean >/dev/null 2>&1; "
                f"PRU_CGT={pru_cgt} make 2>&1; "
                f"test -f {build_output} && sha256sum {build_output}"
            )
            exchange = _record_exchange(
                entry, active.run(script, label="build", timeout_s=build_timeout_s)
            )
            _require_rc(exchange, "build", "PRU firmware build")
            built = _parse_sha256sum(exchange.stdout).get(build_output)
            if not built:
                raise BbbError("build", "Build produced no firmware image to hash.")
            entry["firmware_sha256"] = built
            result.measurements["built_firmware_sha256"] = built

        with _stage(result, "install") as entry:
            installed_as = _safe_remote(str(firmware["installed_as"]), "installed_as")
            exchange = _record_exchange(
                entry,
                active.run(
                    f'cd ~/{build_dir} && echo "$BB_PW" | sudo -S cp {build_output} '
                    f"{installed_as} && sha256sum {installed_as}",
                    label="install",
                    timeout_s=command_timeout_s,
                ),
            )
            _require_rc(exchange, "install", "firmware install")
            installed = _parse_sha256sum(exchange.stdout).get(installed_as)
            entry["installed_sha256"] = installed
            result.measurements["installed_firmware_sha256"] = installed
            result.measurements["installed_firmware_path"] = installed_as
            if installed != built:
                raise BbbError(
                    "install",
                    f"Installed firmware hash {installed} does not match the build {built}.",
                )

        with _stage(result, "service") as entry:
            unit = _safe_remote(str(service["unit"]), "service.unit")
            timeout_active = int(service.get("active_timeout_s", 130))
            exchange = _record_exchange(
                entry,
                active.run(
                    f"systemctl list-unit-files {unit} --no-legend --no-pager | wc -l",
                    label="service_present",
                    timeout_s=command_timeout_s,
                ),
            )
            if exchange.returncode != 0 or _last_line(exchange.stdout) == "0":
                raise BbbError(
                    "service",
                    f"{unit} is not installed on this board. Provision it with "
                    f"{service.get('installed_by', 'host/provision_rtase.py')} first; "
                    "deploy_pru does not provision a board it did not set up.",
                )
            exchange = _record_exchange(
                entry,
                active.run(
                    f'echo "$BB_PW" | sudo -S systemctl restart {unit}; '
                    f"for i in $(seq 1 {timeout_active}); do "
                    f"systemctl is-active --quiet {unit} && break; sleep 1; done; "
                    f"systemctl is-active {unit}",
                    label="service_restart",
                    timeout_s=service_timeout_s,
                ),
            )
            state = _last_line(exchange.stdout)
            entry["service_state"] = state
            result.measurements["service_unit"] = unit
            result.measurements["service_state"] = state
            if state != "active":
                raise BbbError("service", f"{unit} did not become active (state: {state or 'unknown'}).")

        with _stage(result, "verify_protocol") as entry:
            status_command = str(_required(config, "verify", "status_command"))
            exchange = _record_exchange(
                entry,
                active.run(status_command, label="rtase_status", timeout_s=command_timeout_s),
            )
            _require_rc(exchange, "verify_protocol", "rtase status")
            status = _parse_json_object(exchange.stdout)
            if status is None:
                raise BbbError("verify_protocol", "rtase status printed no JSON object.")
            entry["status"] = status
            result.measurements["rtase_status"] = status
            version = str(status.get("version", "")).strip()
            result.versions["pru_firmware_version"] = version
            if status.get("ok") is not True or not version:
                raise BbbError(
                    "verify_protocol",
                    f"PRU firmware did not report a healthy VERSION/STATUS: {status}",
                )

        with _stage(result, "ping") as entry:
            device = _safe_remote(str(verify["rpmsg_device"]), "verify.rpmsg_device")
            request = str(verify["ping_request_hex"]).strip()
            expected_reply = str(verify["ping_reply_hex"]).strip().lower()
            if not HEX_ONLY.fullmatch(request) or not HEX_ONLY.fullmatch(expected_reply):
                raise BbbError("contract", "ping_request_hex/ping_reply_hex must be hex.")
            exchange = _record_exchange(
                entry,
                active.run(_ping_script(device, request), label="ping", timeout_s=command_timeout_s),
            )
            _require_rc(exchange, "ping", "PRU ping")
            reply = _last_line(exchange.stdout).lower()
            entry["reply_hex"] = reply
            result.measurements["ping_reply_hex"] = reply
            result.measurements["expected_ping_reply_hex"] = expected_reply
            if reply != expected_reply:
                raise BbbError(
                    "ping",
                    f"PRU answered {reply or '<nothing>'} to PING, expected {expected_reply}.",
                )
    except (BbbError, SshError, SafetyViolation, OSError, KeyError, TypeError, ValueError) as exc:
        if active is not None:
            _save_logs(result, record, active)
            if owns_transport:
                active.close()
        return _finish(result, started, record, failure=str(exc))

    _save_logs(result, record, active)
    if owns_transport and active is not None:
        active.close()
    return _finish(result, started, record)


# -- helpers -----------------------------------------------------------------


def _plan_uploads(
    config: Mapping[str, Any], root: Path, remote_root: str
) -> list[tuple[Path, str]]:
    """Expand the contract's deploy list into concrete (local, remote) pairs."""
    entries = _required(config, "deploy")
    if not isinstance(entries, list) or not entries:
        raise BbbError("upload", "Contract lists no files to deploy.")
    uploads: list[tuple[Path, str]] = []
    for entry in entries:
        local = (root / str(entry["local"])).resolve()
        remote = _safe_remote(str(entry["remote"]), "deploy.remote")
        if not str(remote).startswith(f"{remote_root}/") and remote != remote_root:
            raise BbbError("upload", f"Deploy target escapes the remote root: {remote}")
        kind = str(entry.get("kind", "file"))
        if kind == "file":
            if not local.is_file():
                raise BbbError("upload", f"Deploy source is missing: {local}")
            uploads.append((local, remote))
        elif kind == "tree":
            if not local.is_dir():
                raise BbbError("upload", f"Deploy source directory is missing: {local}")
            for path in sorted(p for p in local.rglob("*") if p.is_file()):
                # Skip build output and caches. `make clean` deletes gen/ on
                # the board first, so shipping a stale local object can only
                # confuse the record about what was actually built there.
                if set(path.parts) & {"__pycache__", "gen"} or path.suffix == ".pyc":
                    continue
                relative = path.relative_to(local).as_posix()
                uploads.append((path, _safe_remote(f"{remote}/{relative}", "deploy.remote")))
        else:
            raise BbbError("upload", f"Unknown deploy kind: {kind!r}")
    return uploads


def _last_line(text: str) -> str:
    """Last non-empty line, or '' -- a remote command may print nothing at all."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _parse_sha256sum(text: str) -> dict[str, str]:
    """Read ``<hash>  <path>`` lines, ignoring anything else in the output."""
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if match:
            parsed[match.group(2)] = match.group(1).lower()
    return parsed


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Pull the last JSON object out of mixed output (rtase.py prints one)."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _ping_script(device: str, request_hex: str) -> str:
    """One PING exchange against the firmware's RPMsg endpoint.

    firmware/main.c answers 0x01 with [0x01][0x01]. Doing this inline keeps the
    check independent of whatever host CLI happens to be deployed.
    """
    return (
        "python3 - <<'PY'\n"
        "import os\n"
        f"fd = os.open({device!r}, os.O_RDWR)\n"
        "try:\n"
        f"    os.write(fd, bytes.fromhex({request_hex!r}))\n"
        "    print(os.read(fd, 64).hex())\n"
        "finally:\n"
        "    os.close(fd)\n"
        "PY"
    )
