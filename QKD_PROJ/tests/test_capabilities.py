"""The existing manual bring-up is reusable only while its evidence still agrees."""

from __future__ import annotations

import hashlib
import json

import pytest

from QKD_PROJ.capabilities.catalog import CapabilityCatalog, CapabilityCatalogError
from QKD_PROJ.capabilities.extension import derive_extension_plan
from QKD_PROJ.research.mission import MissionPlan


def _write_catalog(root, digest: str) -> None:
    path = root / "QKD_PROJ" / "capabilities"
    path.mkdir(parents=True)
    (path / "verified-assets.yaml").write_text(
        f"""schema_version: 1
capabilities:
  - id: verified-build
    title: Verified build
    summary: A source file tied to a successful build.
    mode: offline
    agents: [design-builder]
    skills: [cpld-diamond-build]
    operations: [diamond_build]
    contracts: [build.yaml]
    runs: [run-pass]
    assets:
      - path: QKD_PROJ/source.v
        sha256: {digest}
""",
        encoding="utf-8",
    )


def _root(tmp_path):
    source = tmp_path / "QKD_PROJ" / "source.v"
    source.parent.mkdir()
    source.write_text("module verified; endmodule\n", encoding="utf-8")
    contract = tmp_path / "QKD_PROJ" / "contracts"
    contract.mkdir()
    (contract / "build.yaml").write_text("test_id: build\n", encoding="utf-8")
    manifest = tmp_path / "QKD_PROJ" / "runs" / "run-pass"
    manifest.mkdir(parents=True)
    (manifest / "manifest.json").write_text(
        json.dumps({"verdict": "PASS", "contract": {"test_spec": "build.yaml"}}),
        encoding="utf-8",
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    _write_catalog(tmp_path, digest)
    return source


def test_catalog_admits_only_the_hashed_pass_backed_asset(tmp_path):
    _root(tmp_path)
    catalog = CapabilityCatalog.load_default(tmp_path)
    assert [row["id"] for row in catalog.for_mode("offline")] == ["verified-build"]
    assert catalog.select(["verified-build"], mode="offline")[0].operations == ("diamond_build",)
    assert catalog.for_planning("offline") == [{
        **catalog.for_mode("offline")[0],
        "available_now": True,
        "requires_hardware_campaign": False,
    }]


def test_catalog_refuses_source_drift_before_an_agent_can_select_it(tmp_path):
    source = _root(tmp_path)
    source.write_text("module changed; endmodule\n", encoding="utf-8")
    with pytest.raises(CapabilityCatalogError, match="asset drifted"):
        CapabilityCatalog.load_default(tmp_path)


def test_extension_plan_expands_only_selected_catalog_records():
    plan = MissionPlan.parse({
        "kind": "MissionPlan", "agent": "mission-architect", "scenario_sha256": "a" * 64,
        "mission_type": "improve", "search_policy": "refresh", "summary": "reuse it",
        "baseline_contract": "baseline.yaml", "baseline_claim_ids": [],
        "questions": [{"id": "q", "question": "check", "why": "change", "required_source_types": ["vendor"]}],
        "completion_criteria": ["checked"], "search_budget": 1, "required_materials": ["source"],
        "execution_agents": ["design-builder"], "execution_skills": ["cpld-diamond-build"],
        "capability_ids": ["verified-build"], "contract_action": "amend",
    }, available_agents={"design-builder"}, available_skills={"cpld-diamond-build"},
       available_capability_ids={"verified-build"}, known_claim_ids=set(),
       selected_contract="baseline.yaml", required_search_policy="refresh")
    extension = derive_extension_plan(plan, {
        "execution_mode": "offline", "capability_catalog_sha256": "b" * 64,
        "available_capabilities": [{"id": "verified-build", "agents": ["design-builder"],
                                    "skills": ["cpld-diamond-build"], "operations": ["diamond_build"]}],
    })
    assert extension.describe() == {
        "scenario_sha256": "a" * 64, "catalog_sha256": "b" * 64, "execution_mode": "offline",
        "capability_ids": ["verified-build"], "agents": ["design-builder"],
        "skills": ["cpld-diamond-build"], "operations": ["diamond_build"],
        "deferred_hardware_capability_ids": [], "deferred_agents": [],
        "deferred_skills": [], "deferred_operations": [],
        "status": "reuse_verified_capabilities", "requires_campaign": False,
    }


def test_offline_extension_stages_only_registered_hardware_for_a_campaign():
    plan = MissionPlan.parse({
        "kind": "MissionPlan", "agent": "mission-architect", "scenario_sha256": "a" * 64,
        "mission_type": "new", "search_policy": "refresh", "summary": "prepare the bench",
        "baseline_contract": None, "baseline_claim_ids": [],
        "questions": [{"id": "q", "question": "check", "why": "change", "required_source_types": ["vendor"]}],
        "completion_criteria": ["checked"], "search_budget": 1, "required_materials": ["source"],
        "execution_agents": ["design-builder"], "execution_skills": ["cpld-diamond-build"],
        "capability_ids": ["verified-build"],
        "deferred_hardware_capability_ids": ["verified-scope"],
        "contract_action": "new",
    }, available_agents={"design-builder"}, available_skills={"cpld-diamond-build"},
       available_capability_ids={"verified-build"},
       available_hardware_capability_ids={"verified-scope"}, known_claim_ids=set(),
       required_search_policy="refresh")
    extension = derive_extension_plan(plan, {
        "execution_mode": "offline", "capability_catalog_sha256": "b" * 64,
        "available_capabilities": [{"id": "verified-build", "agents": ["design-builder"],
                                    "skills": ["cpld-diamond-build"], "operations": ["diamond_build"]}],
        "deferred_hardware_capabilities": [{"id": "verified-scope", "mode": "hardware",
                                             "available_now": False, "agents": ["measurement"],
                                             "skills": ["scope-measurement"],
                                             "operations": ["oscilloscope_capture"]}],
    }).describe()
    assert extension["status"] == "offline_design_then_hardware_campaign"
    assert extension["requires_campaign"] is True
    assert extension["deferred_agents"] == ["measurement"]
    assert extension["deferred_operations"] == ["oscilloscope_capture"]
