"""Reserved local-model runtime boundary; provider selection comes later."""

from __future__ import annotations

from typing import Any

from ai_hardware_autolab.agents.schema import AgentDefinition


class LocalModelRuntime:
    name = "local"

    def invoke(self, agent: AgentDefinition, task: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("local model runtime is not configured")

    __call__ = invoke

    def cancel(self, job_id: str) -> bool:
        return False

    def close(self) -> None:
        return None
