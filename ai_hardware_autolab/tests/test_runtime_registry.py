from __future__ import annotations

from ai_hardware_autolab.agents.schema import load_simple_team
from ai_hardware_autolab.orchestrator.runtime_registry import RuntimeRegistry


class FakeRunner:
    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[str] = []
        self.closed = 0

    def __call__(self, agent, task):
        self.calls.append(agent.name)
        return {"runtime": self.label, "job": task["job_id"]}

    def close(self) -> None:
        self.closed += 1


def test_registry_routes_one_role_and_defaults_others() -> None:
    agents = {agent.name: agent for agent in load_simple_team()}
    default = FakeRunner("codex")
    lead = FakeRunner("local")
    registry = RuntimeRegistry(default, {"lead-agent": lead})

    assert registry(agents["lead-agent"], {"job_id": "j1"})["runtime"] == "local"
    assert registry(agents["builder"], {"job_id": "j1"})["runtime"] == "codex"
    assert lead.calls == ["lead-agent"]
    assert default.calls == ["builder"]


def test_registry_closes_each_unique_runtime_once() -> None:
    shared = FakeRunner("shared")
    registry = RuntimeRegistry(shared, {"lead-agent": shared, "builder": shared})
    registry.close()
    assert shared.closed == 1
