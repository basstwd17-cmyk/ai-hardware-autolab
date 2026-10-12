from pathlib import Path
import shutil
import subprocess

import pytest

from ai_hardware_autolab.orchestrator.source_context import source_context
from ai_hardware_autolab.orchestrator.simple_mission import start_simple_mission
from ai_hardware_autolab.orchestrator.jobs import JobLedger


ROOT = Path(__file__).resolve().parents[1]


def test_context_is_navigation_not_a_hardware_permission():
    resources = {"source_context": {"active_team": "obsolete"}}
    before = dict(resources)
    context = source_context(resources)
    assert resources == before
    assert context["purpose"] == "navigation_only_not_permission_or_proof"
    assert context["active_team"] == "ai_hardware_autolab/agents/simple/"
    assert context["selected_board_paths"] == []


def test_selected_board_paths_never_default_to_rtase():
    context = source_context({"board_development": {"selected": {
        "profile": "ai_hardware_autolab/boards/custom/r2/profile.yaml",
        "board_map": "../../old-board.yaml",
    }}})
    assert context["selected_board_paths"] == ["ai_hardware_autolab/boards/custom/r2/profile.yaml"]
    assert source_context({"board_development": {"status": "onboarding"}})["selected_board_paths"] == []


def test_mission_brief_records_source_navigation(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    job, _ = start_simple_mission(ledger, "test", "offline review")
    brief = ledger.handoffs(job.job_id, "Brief", 0)[0].payload
    assert brief["resources"]["source_context"] == source_context({})


def test_current_entrypoints_and_history_are_distinct():
    for name in ("README", "STATUS"):
        current = (ROOT / f"{name}.md").read_text(encoding="utf-8")
        old = ROOT / "docs" / "history" / f"{name}-before-2026-09-04.md"
        assert old.is_file()
        assert old.name in current or "docs/history/" in current
        assert "현재" in old.read_text(encoding="utf-8").splitlines()[0]


def test_normal_search_hides_history_but_explicit_opt_in_reads_it(tmp_path):
    if not shutil.which("rg"):
        pytest.skip("ripgrep not installed")
    shutil.copy2(ROOT / ".rgignore", tmp_path / ".rgignore")
    for name in ("README.md", "agents/README.md", "agents/old.md", "agents/simple/lead-agent.md",
                 "docs/history/old.md", "docs/2026-08-21-old.md",
                 "gate12_pru_dac1/source.v", "gate12_pru_dac1/build/stale.v"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    result = subprocess.run(["rg", "--files", "."], cwd=tmp_path,
                            check=True, capture_output=True, text=True)
    files = result.stdout.replace("\\", "/")
    assert "README.md" in files and "source.v" in files
    assert "agents/README.md" in files and "agents/simple/lead-agent.md" in files
    assert "old.md" not in files and "stale.v" not in files
    result = subprocess.run(["rg", "--no-ignore", "--files", "docs/history"],
                            cwd=tmp_path, check=True, capture_output=True, text=True)
    assert "old.md" in result.stdout
