"""Reserved Claude Code runtime boundary; no credentials or CLI assumptions yet."""

from __future__ import annotations

from typing import Any

from ai_hardware_autolab.agents.schema import AgentDefinition


class ClaudeCodeRuntime:
    name = "claude-code"

    def invoke(self, agent: AgentDefinition, task: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("Claude Code runtime is not configured")

    __call__ = invoke

    def cancel(self, job_id: str) -> bool:
        return False

    def close(self) -> None:
        return None
