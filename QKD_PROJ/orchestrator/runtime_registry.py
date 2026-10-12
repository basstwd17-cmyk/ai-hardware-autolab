"""Select an agent runtime by role without changing the durable loop contract.

The loop still sees one ``AgentRunner`` callable.  This registry is the thin
adapter that permits Lead, Builder and Verifier to use different Codex models,
Claude adapters or local runtimes later.  Selection is configuration; workflow
state remains in ``JobLedger`` and is never stored here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from QKD_PROJ.agents.schema import AgentDefinition


Runner = Callable[[AgentDefinition, dict[str, Any]], dict[str, Any]]


@dataclass
class RuntimeRegistry:
    default: Runner
    by_agent: Mapping[str, Runner] = field(default_factory=dict)
    _active: dict[str, Runner] = field(default_factory=dict, init=False, repr=False)

    def resolve(self, agent_name: str) -> Runner:
        return self.by_agent.get(agent_name, self.default)

    def __call__(self, agent: AgentDefinition, task: dict[str, Any]) -> dict[str, Any]:
        runner = self.resolve(agent.name)
        job_id = str(task.get("job_id") or "")
        if job_id:
            self._active[job_id] = runner
        try:
            invoke = getattr(runner, "invoke", None)
            return invoke(agent, task) if callable(invoke) else runner(agent, task)
        finally:
            if job_id and self._active.get(job_id) is runner:
                self._active.pop(job_id, None)

    def cancel(self, job_id: str) -> bool:
        runner = self._active.get(job_id)
        cancel = getattr(runner, "cancel", None)
        return bool(callable(cancel) and cancel(job_id))

    def check_resume(self, job_id: str, stopped: dict) -> None:
        check = getattr(self.resolve('builder'), 'check_resume', None)
        if not callable(check):
            raise ValueError('Selected Builder runtime cannot verify deployment for resume')
        check(job_id, stopped)

    def close(self) -> None:
        seen: set[int] = set()
        for runner in (self.default, *self.by_agent.values()):
            identity = id(runner)
            if identity in seen:
                continue
            seen.add(identity)
            close = getattr(runner, "close", None)
            if callable(close):
                close()
