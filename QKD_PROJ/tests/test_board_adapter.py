"""A board profile is the only source of executable argv for another board."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
import pytest

from QKD_PROJ.boards.profiles import BoardProfile
from QKD_PROJ.hwtools.board_adapter import run_profile_stage
from QKD_PROJ.hwtools.record import RunRecord
from QKD_PROJ.hwtools.safety import Contracts, SafetyGate
from QKD_PROJ.mcp_server.protocol import ToolError
from QKD_PROJ.mcp_server.registry import ServerPolicy, _run_board_stage


def _fixture(tmp_path: Path) -> tuple[BoardProfile, SafetyGate, Path]:
    root = tmp_path
    project = root / "firmware"
    project.mkdir()
    board_map = root / "board-map.yaml"
    board_map.write_text(
        "schema_version: 1\nboard: {id: demo-board, revision: r1}\n",
        encoding="utf-8",
    )
    profile_path = root / "profile.yaml"
    profile_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "board_id": "demo-board",
                "revision": "r1",
                "title": "Demo board",
                "aliases": ["demo-board"],
                "board_map": "board-map.yaml",
                "sources": [{
                    "id": "local-source",
                    "title": "Local source",
                    "source_kind": "reviewed-local-project",
                    "location": "firmware",
                }],
                "stages": [
                    {
                        "id": "build-firmware",
                        "phase": "build",
                        "mode": "offline",
                        "agent": "firmware-builder",
                        "tool": "run_board_build",
                        "status": "ready",
                        "requires_campaign": False,
                        "command": [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; Path('built.bin').write_bytes(b'ok')",
                        ],
                        "cwd": "firmware",
                        "artifacts": ["built.bin"],
                    },
                    {
                        "id": "upload-firmware",
                        "phase": "upload",
                        "mode": "hardware",
                        "agent": "board-operator",
                        "tool": "run_board_stage",
                        "status": "ready",
                        "requires_campaign": True,
                        "command": [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; Path('uploaded.txt').write_text('yes')",
                        ],
                        "cwd": "firmware",
                        "artifacts": ["uploaded.txt"],
                    },
                    {
                        "id": "measure-output",
                        "phase": "measure",
                        "mode": "hardware",
                        "agent": "measurement",
                        "tool": "measure_board_scope",
                        "status": "ready",
                        "requires_campaign": True,
                        "command": [],
                        "cwd": None,
                        "artifacts": ["waveform_csv", "screenshot"],
                        "parameters": {
                            "channels": [{
                                "channel": 4,
                                "signal": "RF1",
                                "scale_v_per_div": 0.5,
                                "offset_v": 0.0,
                            }],
                            "timebase_s_per_div": 1e-6,
                            "trigger_channel": 4,
                            "trigger_level_v": 0.2,
                            "trigger_slope": "POS",
                            "timeout_s": 5.0,
                        },
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    profile = BoardProfile.load(profile_path, project_root=root)
    contracts = root / "contracts"
    contracts.mkdir()
    (contracts / "demo.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "test_id": "demo",
                "status": "reviewed_generated_hardware",
                "allowed_operations": [
                    "firmware_build", "board_upload", "oscilloscope_capture"
                ],
                "forbidden_operations": [],
                "board_profile": {
                    "board_id": "demo-board",
                    "revision": "r1",
                    "path": "profile.yaml",
                    "sha256": profile.sha256,
                    "board_map": "board-map.yaml",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    gate = SafetyGate(
        Contracts.load("demo.yaml", contracts, project_root=root),
        allow_hardware=False,
    )
    return profile, gate, project


def test_offline_board_build_runs_only_profile_argv_and_hashes_artifact(tmp_path):
    profile, gate, project = _fixture(tmp_path)
    record = RunRecord("profile-build", gate, runs_dir=tmp_path / "runs")

    result = run_profile_stage(
        profile,
        "build-firmware",
        gate,
        record=record,
        project_root=tmp_path,
    )

    assert result.ok and result.verdict == "PASS"
    assert (project / "built.bin").read_bytes() == b"ok"
    assert result.measurements["profile_sha256"] == profile.sha256
    assert len(result.artifacts) == 1
    assert (record.dir / "built.bin").read_bytes() == b"ok"


def test_hardware_stage_cannot_run_without_the_campaign_gate(tmp_path):
    profile, gate, project = _fixture(tmp_path)

    result = run_profile_stage(
        profile,
        "upload-firmware",
        gate,
        project_root=tmp_path,
    )

    assert not result.ok and result.verdict == "FAIL"
    assert "documents-only mode" in result.error
    assert not (project / "uploaded.txt").exists()


def test_measurement_role_cannot_invoke_an_upload_stage(tmp_path):
    _fixture(tmp_path)
    policy = ServerPolicy(
        allow_hardware=True,
        role="measurement",
        contracts_dir=tmp_path / "contracts",
        runs_dir=tmp_path / "runs",
        project_root=tmp_path,
    )

    with pytest.raises(ToolError, match="belongs to agent 'board-operator'"):
        _run_board_stage(
            policy,
            {"contract": "demo.yaml", "stage_id": "upload-firmware"},
        )
