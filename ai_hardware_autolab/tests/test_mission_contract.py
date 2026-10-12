from ai_hardware_autolab.orchestrator.mission_contract import MissionContract, mission_id_for


def test_mission_id_is_stable_for_equivalent_spacing() -> None:
    assert mission_id_for("build   the board") == mission_id_for("build the board")


def test_draft_exposes_scenario_identity_without_replacing_gate_storage() -> None:
    mission = MissionContract.draft("Build RF1 output", "RF1", "offline")
    assert mission.status == "draft"
    assert mission.mission_id.startswith("mission-")
    assert mission.describe()["scenario_sha256"] == mission.scenario_sha256
