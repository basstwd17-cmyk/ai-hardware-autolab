from __future__ import annotations

from dataclasses import dataclass

from QKD_PROJ.orchestrator.research_budget import (
    RESEARCH_PROGRESS,
    ResearchBudgetPolicy,
    ResearchBudgetState,
    canonical_url,
    normalize_query,
)


@dataclass
class E:
    kind: str
    payload: dict


def test_budget_is_rebuilt_from_append_only_progress() -> None:
    events = [
        E(RESEARCH_PROGRESS, {
            "queries": [" BBB   PRU timing "],
            "queries_used": 2,
            "urls": ["HTTPS://EXAMPLE.COM/a?b=2&a=1#page"],
            "document_ids": ["doc-1"],
            "new_fact_hashes": ["a" * 64],
            "tokens": {"input": 100, "output": 20},
            "elapsed_s": 2.5,
        }),
        E(RESEARCH_PROGRESS, {
            "queries": ["bbb pru timing"],
            "queries_used": 1,
            "urls": ["https://example.com/a?a=1&b=2"],
            "document_ids": ["doc-1"],
            "new_fact_hashes": ["a" * 64],
            "tokens": {"input": 50, "output": 10},
            "elapsed_s": 1.5,
        }),
    ]
    state = ResearchBudgetState.from_events(events)
    policy = ResearchBudgetPolicy(soft_queries_per_turn=4, hard_queries_per_job=5)
    assert state.queries_used == 3
    assert state.turn_limit(policy) == 2
    assert state.stagnant_rounds == 1
    assert state.input_tokens == 150
    assert state.elapsed_s == 4.0
    assert normalize_query(" BBB   PRU timing ") in state.query_keys
    assert canonical_url("https://example.com/a?b=2&a=1#x") in state.urls


def test_new_fact_resets_stagnation() -> None:
    events = [
        E(RESEARCH_PROGRESS, {"new_fact_hashes": ["a" * 64]}),
        E(RESEARCH_PROGRESS, {"new_fact_hashes": ["a" * 64]}),
        E(RESEARCH_PROGRESS, {"new_fact_hashes": ["b" * 64]}),
    ]
    assert ResearchBudgetState.from_events(events).stagnant_rounds == 0


def test_elapsed_time_can_close_the_hard_budget() -> None:
    policy = ResearchBudgetPolicy(hard_time_limit_s=10)
    state = ResearchBudgetState(elapsed_s=10)
    assert state.remaining(policy) == 0
    assert state.turn_limit(policy) == 0
