"""Board identity and adapter readiness are data, not model guesses."""

from pathlib import Path

from ai_hardware_autolab.boards.profiles import BoardCatalog
from ai_hardware_autolab.orchestrator.control import ControlPlane
from ai_hardware_autolab.orchestrator.jobs import JobLedger
from ai_hardware_autolab.orchestrator.scenario_preflight import ScenarioPreflight
from ai_hardware_autolab.research.models import scenario_sha256


def test_default_catalog_selects_revision_and_exposes_adapter_gaps():
    root = Path(__file__).resolve().parents[2]
    catalog = BoardCatalog.load_default(root)

    stm = catalog.planning_context("Develop 5 ns timing on NUCLEO-G474RE and measure it")
    dpow = catalog.planning_context("Improve the B-G474E-DPOW1 HRTIM firmware")
    rtase = catalog.planning_context("Update the QKD chip RF1 output")

    assert stm["status"] == "profiled"
    assert stm["selected"]["board_id"] == "nucleo-g474re"
    assert stm["selected"]["ready_through"] == "research"
    assert any(
        row["phase"] == "upload" and row["status"] == "adapter_required"
        for row in stm["selected"]["stages"]
    )
    assert dpow["selected"]["board_id"] == "b-g474e-dpow1"
    assert dpow["selected"]["ready_through"] == "research"
    assert rtase["selected"]["board_id"] == "rtase-v1"
    assert rtase["selected"]["ready_through"] == "verify"


def test_an_mcu_name_alone_does_not_guess_which_board_it_is_on():
    root = Path(__file__).resolve().parents[2]
    context = BoardCatalog.load_default(root).planning_context(
        "Develop new timing firmware for STM32G474RE"
    )

    assert context["status"] == "profile_required"
    assert context["selected"] is None


def test_an_unknown_board_enters_onboarding_without_borrowing_an_adapter():
    root = Path(__file__).resolve().parents[2]
    context = BoardCatalog.load_default(root).planning_context(
        "Develop firmware for the ACME-X9 board and capture four scope channels"
    )

    assert context["status"] == "profile_required"
    assert context["selected"] is None
    assert context["required_stages"] == [
        "research", "design", "build", "upload", "measure", "verify"
    ]


def test_a_profile_gap_does_not_route_work_to_a_similarly_named_board_builder(tmp_path):
    root = Path(__file__).resolve().parents[2]
    ledger = JobLedger(tmp_path / "jobs.db")
    control = ControlPlane(
        ledger,
        preflight_engine=ScenarioPreflight(root / "ai_hardware_autolab" / "contracts"),
        board_catalog=BoardCatalog.load_default(root),
    )
    scenario = (
        "On NUCLEO-G474RE, build and upload firmware, then measure the output; "
        "hardware access is requested."
    )

    response = control.legacy_mission(
        "develop another board",
        scenario,
        scenario_sha256(scenario),
    )
    brief = ledger.handoffs(response["job"]["job_id"], "Brief", 0)[0].payload
    resources = brief["resources"]

    assert resources["board_development"]["selected"]["board_id"] == "nucleo-g474re"
    assert resources["available_execution_agents"] == ["builder", "verifier"]
    assert resources["legacy_execution_agents"] == ["reporter", "verifier"]
    assert "codex_workspace" in resources["available_tools"]
    # The catalog must not route to another board, but no longer hides useful
    # primitives. Actual hardware authority is checked at invocation.
    assert resources["development_mode"] == "autonomous-v1"
    assert resources["available_tools"] == control._development_tools("hardware")
    assert "measure_scope" in resources["available_tools"]
