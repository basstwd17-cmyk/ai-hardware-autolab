"""Common runtime contract below the Lead/Builder/Verifier workflow."""

from __future__ import annotations

from typing import Any, Protocol

from QKD_PROJ.agents.schema import AgentDefinition


class AgentRuntime(Protocol):
    """A replaceable model runtime that emits the common handoff envelopes."""

    name: str

    def invoke(self, agent: AgentDefinition, task: dict[str, Any]) -> dict[str, Any]: ...

    def cancel(self, job_id: str) -> bool: ...

    def close(self) -> None: ...
