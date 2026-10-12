"""Offline tests for run_bbb() and deploy_pru().

Every test here runs with a fake SSH client. The board is on someone else's
bench during bring-up, and a tool that can only be exercised with hardware
attached cannot be written at all while that is true.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.hwtools import (  # noqa: E402
    Contracts,
    RunRecord,
    SafetyGate,
    SafetyViolation,
    SshTarget,
    SshTransport,
    deploy_pru,
    run_bbb,
)
from ai_hardware_autolab.hwtools.ssh import REDACTED  # noqa: E402

CONTRACT = "gate9-bbb-host-tooling.yaml"
REPO_ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "hunter2-lab-secret"


# -- fake transport ----------------------------------------------------------


class FakeSsh:
    """An SSH connection that answers by pattern and remembers everything."""

    def __init__(self, rules: list[tuple[str, object]] | None = None) -> None:
        self.rules = list(rules or [])
        self.scripts: list[str] = []
        self.uploads: list[tuple[Path, str]] = []
        self.remote_bytes: dict[str, bytes] = {}
        self.closed = False

    def exec_script(self, script: str, timeout_s: float) -> tuple[int, str, str]:
        self.scripts.append(script)
        for needle, reply in self.rules:
            if needle in script:
                return reply(self, script) if callable(reply) else reply
        if "tr -d '\\000' </proc/device-tree/model; echo; uname -r" in script:
            return (0, "TI AM335x BeagleBone Black\n5.10-test\n", "")
        return (0, "", "")

    def put_file(self, local: Path, remote: str) -> None:
        payload = Path(local).read_bytes()
        self.uploads.append((Path(local), remote))
        self.remote_bytes[remote] = payload

    def close(self) -> None:
        self.closed = True

    # helpers used by the rules below
    def sha256sum(self, script: str) -> str:
        lines = []
        for remote in re.findall(r"'([^']+)'", script):
            payload = self.remote_bytes.get(remote, b"")
            lines.append(f"{hashlib.sha256(payload).hexdigest()}  {remote}")
        return "\n".join(lines) + "\n"


def transport_for(fake: FakeSsh, password: str = PASSWORD) -> SshTransport:
    target = SshTarget(host="192.168.50.2", user="xilinx", password=password)
    return SshTransport(target, client=fake)


FIRMWARE_HASH = hashlib.sha256(b"pru-firmware-image").hexdigest()

DEPLOY_RULES: list[tuple[str, object]] = [
    ("/proc/device-tree/model", (0, "TI AM335x BeagleBone Black\n5.10.168-ti-r71\n", "")),
    ("mkdir -p", (0, "", "")),
    ("sha256sum '", lambda fake, script: (0, fake.sha256sum(script), "")),
    (
        "PRU_CGT=",
        (
            0,
            "Built: gen/pru0_pulse_io.out\n"
            f"{FIRMWARE_HASH}  gen/pru0_pulse_io.out\n",
            "",
        ),
    ),
    (
        "sudo -S cp",
        (0, f"{FIRMWARE_HASH}  /lib/firmware/am335x-pru0-rtase.out\n", ""),
    ),
    ("list-unit-files", (0, "1\n", "")),
    ("systemctl restart", (0, "active\n", "")),
    (
        "rtase.py --local status",
        (
            0,
            json.dumps(
                {
                    "ok": True,
                    "version": "rtase-1.4",
                    "symbols_loaded": 0,
                    "outputs_active": False,
                    "device": "/dev/rpmsg0",
                }
            )
            + "\n",
            "",
        ),
    ),
    ("os.open", (0, "0101\n", "")),
]


def rules_with(overrides: dict[str, object]) -> list[tuple[str, object]]:
    return [(needle, overrides.get(needle, reply)) for needle, reply in DEPLOY_RULES]


# -- fixtures ----------------------------------------------------------------


@pytest.fixture
def gate() -> SafetyGate:
    return SafetyGate(Contracts.load(CONTRACT), allow_hardware=True)


@pytest.fixture
def blocked_gate() -> SafetyGate:
    return SafetyGate(Contracts.load(CONTRACT))


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A stand-in for the BeagleBone-Black tree the contract deploys."""
    bbb = tmp_path / "BeagleBone-Black"
    firmware = bbb / "firmware"
    (firmware / "pru-support").mkdir(parents=True)
    (firmware / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (firmware / "Makefile").write_text("all:\n\t@echo Built:\n", encoding="utf-8")
    (firmware / "pru-support" / "pru_cfg.h").write_text("/* vendored */\n", encoding="utf-8")
    (firmware / "gen").mkdir()
    (firmware / "gen" / "stale.object").write_bytes(b"\x00")
    (bbb / "host").mkdir()
    (bbb / "host" / "rtase.py").write_text("# rtase cli\n", encoding="utf-8")
    (bbb / "host" / "rtase_spi.py").write_text("# rtase spi cli\n", encoding="utf-8")
    return tmp_path


def stage_names(result) -> list[str]:
    return [entry["stage"] for entry in result.measurements["stages"]]


# -- contract ----------------------------------------------------------------


def test_contract_forbids_every_board_touching_operation(gate: SafetyGate) -> None:
    for operation in ("jtag_scan", "sram_fast_program", "nonvolatile_flash", "oscilloscope_capture"):
        with pytest.raises(SafetyViolation):
            gate.require(operation)


def test_allowlisted_script_hash_matches_the_file_on_disk(gate: SafetyGate) -> None:
    """If the script is edited, this fails until a human re-reviews the contract."""
    for name, entry in gate.contracts.test_spec["scripts"].items():
        path = REPO_ROOT / entry["path"]
        assert path.is_file(), f"{name} points at a missing file: {path}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"], name


# -- run_bbb -----------------------------------------------------------------


def test_run_bbb_executes_allowlisted_script_and_records_everything(
    gate: SafetyGate, tmp_path: Path
) -> None:
    fake = FakeSsh([("== identity ==", (0, "== identity ==\nBeagleBone Black\n", "warn\n"))])
    record = RunRecord("bbb_run", gate, runs_dir=tmp_path)

    result = run_bbb(gate, "diagnose_rtase", record, transport=transport_for(fake))

    assert result.ok and result.verdict == "PASS"
    assert stage_names(result) == ["safety_gate", "allowlist", "script_hash", "connect", "verify_target", "execute"]
    assert result.measurements["returncode"] == 0
    assert "BeagleBone Black" in result.measurements["stdout"]
    assert result.measurements["stderr"] == "warn\n"
    expected = hashlib.sha256(
        (REPO_ROOT / "BeagleBone-Black/scripts/diagnose_rtase.sh").read_bytes()
    ).hexdigest()
    assert result.measurements["script_sha256"] == expected
    # the script body itself reached the board
    assert "/proc/device-tree/model" in fake.scripts[0]
    assert "== remoteproc ==" in fake.scripts[1]

    kinds = {artifact.kind for artifact in result.artifacts}
    assert {"ssh_transcript", "bbb_stdout", "bbb_stderr"} <= kinds
    manifest = json.loads((record.dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["steps"][0]["measurements"]["script"] == "diagnose_rtase"


def test_run_bbb_refuses_a_script_outside_the_allowlist(gate: SafetyGate) -> None:
    fake = FakeSsh()
    result = run_bbb(gate, "install_reverse_spi_overlay", transport=transport_for(fake))

    assert not result.ok
    assert result.measurements["failed_stage"] == "allowlist"
    assert fake.scripts == []  # nothing was sent to the board


def test_run_bbb_refuses_when_the_script_no_longer_matches_its_reviewed_hash(
    gate: SafetyGate, tmp_path: Path
) -> None:
    edited = tmp_path / "BeagleBone-Black" / "scripts"
    edited.mkdir(parents=True)
    (edited / "diagnose_rtase.sh").write_text("#!/bin/bash\nrm -rf /\n", encoding="utf-8")
    fake = FakeSsh()

    result = run_bbb(
        gate, "diagnose_rtase", transport=transport_for(fake), repo_root=tmp_path
    )

    assert not result.ok
    assert result.measurements["failed_stage"] == "script_hash"
    assert fake.scripts == []


def test_run_bbb_is_blocked_in_documents_only_mode(blocked_gate: SafetyGate) -> None:
    fake = FakeSsh()
    result = run_bbb(blocked_gate, "diagnose_rtase", transport=transport_for(fake))

    assert not result.ok
    assert result.measurements["failed_stage"] == "safety_gate"
    assert "allow_hardware" in (result.error or "")
    assert fake.scripts == []


def test_run_bbb_reports_a_nonzero_exit_as_an_execute_failure(gate: SafetyGate) -> None:
    fake = FakeSsh([("== identity ==", (3, "partial\n", "dmesg: permission denied\n"))])
    result = run_bbb(gate, "diagnose_rtase", transport=transport_for(fake))

    assert not result.ok
    assert result.measurements["failed_stage"] == "execute"
    assert result.measurements["returncode"] == 3
    assert result.measurements["stderr"] == "dmesg: permission denied\n"


def test_missing_connection_environment_fails_at_connect(gate: SafetyGate) -> None:
    result = run_bbb(gate, "diagnose_rtase", env={})

    assert not result.ok
    assert result.measurements["failed_stage"] == "connect"
    assert "BB_PW" in (result.error or "")  # endpoint comes from the selected map


def test_password_never_reaches_the_record(gate: SafetyGate, tmp_path: Path) -> None:
    # A remote script that echoes its environment would otherwise put BB_PW
    # into manifest.json, which is committed evidence.
    fake = FakeSsh([("== identity ==", (0, f"BB_PW={PASSWORD}\n", f"sudo: {PASSWORD}\n"))])
    record = RunRecord("bbb_secret", gate, runs_dir=tmp_path)

    result = run_bbb(gate, "diagnose_rtase", record, transport=transport_for(fake))
    record.finish()

    assert PASSWORD in fake.scripts[0]  # the board really did receive it
    assert PASSWORD not in json.dumps(result.to_dict())
    assert REDACTED in result.measurements["stdout"]
    for name in ("manifest.json", "run_status.json"):
        assert PASSWORD not in (record.dir / name).read_text(encoding="utf-8")
    for path in record.dir.glob("*.log"):
        assert PASSWORD not in path.read_text(encoding="utf-8")


# -- deploy_pru --------------------------------------------------------------


def test_deploy_pru_runs_every_stage_and_pins_what_it_shipped(
    gate: SafetyGate, fake_repo: Path, tmp_path: Path
) -> None:
    fake = FakeSsh(DEPLOY_RULES)
    record = RunRecord("bbb_deploy", gate, runs_dir=tmp_path / "runs")

    result = deploy_pru(gate, record, transport=transport_for(fake), repo_root=fake_repo)

    assert result.ok and result.verdict == "PASS", result.error
    assert stage_names(result) == [
        "safety_gate",
        "connect",
        "verify_target",
        "upload",
        "verify_upload",
        "build",
        "install",
        "service",
        "verify_protocol",
        "ping",
    ]
    remote_paths = {remote for _, remote in fake.uploads}
    assert "pru/rtase.py" in remote_paths
    assert "pru/pru0_pulse_io/main.c" in remote_paths
    assert "pru/pru0_pulse_io/pru-support/pru_cfg.h" in remote_paths
    # build output is not shipped: make clean deletes gen/ on the board anyway,
    # and a stale local object silently becoming the remote one is a trap.
    assert not any(remote.startswith("pru/pru0_pulse_io/gen/") for remote in remote_paths)
    uploaded = result.measurements["uploaded_sha256"]
    local_cli = (fake_repo / "BeagleBone-Black" / "host" / "rtase.py").read_bytes()
    assert uploaded["pru/rtase.py"] == hashlib.sha256(local_cli).hexdigest()
    assert result.measurements["uploaded_file_count"] == len(fake.uploads)
    assert result.measurements["installed_firmware_sha256"] == FIRMWARE_HASH
    assert result.measurements["built_firmware_sha256"] == FIRMWARE_HASH
    assert result.measurements["service_state"] == "active"
    assert result.measurements["ping_reply_hex"] == "0101"
    assert result.measurements["rtase_status"]["ok"] is True
    assert result.versions["pru_firmware_version"] == "rtase-1.4"
    assert result.versions["bbb_kernel"] == "5.10.168-ti-r71"


def test_deploy_pru_refuses_a_board_that_is_not_the_expected_model(
    gate: SafetyGate, fake_repo: Path
) -> None:
    fake = FakeSsh(
        rules_with({"/proc/device-tree/model": (0, "Xilinx Zynq ZC702\n5.15.0\n", "")})
    )
    result = deploy_pru(gate, transport=transport_for(fake), repo_root=fake_repo)

    assert not result.ok
    assert result.measurements["failed_stage"] == "verify_target"
    assert fake.uploads == []  # refused before anything was written


def test_deploy_pru_stops_at_the_stage_that_failed(gate: SafetyGate, fake_repo: Path) -> None:
    fake = FakeSsh(rules_with({"PRU_CGT=": (2, "clpru: error #10008\n", "make: *** [all]\n")}))
    result = deploy_pru(gate, transport=transport_for(fake), repo_root=fake_repo)

    assert not result.ok
    assert result.measurements["failed_stage"] == "build"
    assert stage_names(result)[-1] == "build"
    assert "install" not in stage_names(result)
    assert "#10008" in result.measurements["stages"][-1]["stdout"]


def test_deploy_pru_rejects_an_upload_that_differs_on_the_board(
    gate: SafetyGate, fake_repo: Path
) -> None:
    corrupt = hashlib.sha256(b"not what was sent").hexdigest()
    fake = FakeSsh(
        rules_with(
            {
                "sha256sum '": lambda fake, script: (
                    0,
                    "\n".join(
                        f"{corrupt}  {remote}" for remote in re.findall(r"'([^']+)'", script)
                    )
                    + "\n",
                    "",
                )
            }
        )
    )
    result = deploy_pru(gate, transport=transport_for(fake), repo_root=fake_repo)

    assert not result.ok
    assert result.measurements["failed_stage"] == "verify_upload"


def test_deploy_pru_refuses_to_provision_a_board_without_the_service(
    gate: SafetyGate, fake_repo: Path
) -> None:
    fake = FakeSsh(rules_with({"list-unit-files": (0, "0\n", "")}))
    result = deploy_pru(gate, transport=transport_for(fake), repo_root=fake_repo)

    assert not result.ok
    assert result.measurements["failed_stage"] == "service"
    assert "provision_rtase" in (result.error or "")


def test_deploy_pru_fails_when_the_pru_does_not_answer_ping(
    gate: SafetyGate, fake_repo: Path
) -> None:
    fake = FakeSsh(rules_with({"os.open": (0, "\n", "")}))
    result = deploy_pru(gate, transport=transport_for(fake), repo_root=fake_repo)

    assert not result.ok
    assert result.measurements["failed_stage"] == "ping"
    assert result.measurements["expected_ping_reply_hex"] == "0101"


def test_deploy_pru_is_blocked_in_documents_only_mode(
    blocked_gate: SafetyGate, fake_repo: Path
) -> None:
    fake = FakeSsh(DEPLOY_RULES)
    result = deploy_pru(blocked_gate, transport=transport_for(fake), repo_root=fake_repo)

    assert not result.ok
    assert result.measurements["failed_stage"] == "safety_gate"
    assert fake.scripts == [] and fake.uploads == []
