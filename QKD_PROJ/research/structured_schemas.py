"""Structured-output schemas for discovery handoffs.

The Python parsers remain the final authority for cross-object links and
semantic invariants.  These schemas move field shape into the model turn so a
missing claim value or an object where a question id is required is repaired
before the App Server declares the turn complete.
"""

from __future__ import annotations

from typing import Any


_TEXT: dict[str, Any] = {"type": "string", "minLength": 1}
_ID: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 64,
    # Keep the App Server's structured-output rule identical to the durable
    # Python validator.  A broader wire schema lets a turn appear complete
    # only for the ledger to reject it afterwards (for example
    # ``Diamond-target`` instead of ``diamond-target``).
    "pattern": "^[a-z0-9][a-z0-9_.-]*$",
    "description": "lowercase identifier: letters a-z, digits, dot, underscore, or hyphen",
}
_DIGEST: dict[str, Any] = {
    "type": "string",
    "pattern": "^[0-9a-f]{64}$",
}


def _array(items: dict[str, Any], *, minimum: int = 0, maximum: int = 128) -> dict[str, Any]:
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _scalar_or_scalar_array() -> dict[str, Any]:
    scalar = {
        "anyOf": [
            {"type": "string"},
            {"type": "number"},
            {"type": "boolean"},
            {"type": "null"},
        ]
    }
    return {"anyOf": [*scalar["anyOf"], _array(scalar, maximum=64)]}


def _base(kind: str, agent: str, properties: dict[str, Any]) -> dict[str, Any]:
    return _object(
        {
            "kind": {"type": "string", "enum": [kind]},
            "agent": {"type": "string", "enum": [agent]},
            **properties,
        }
    )


def _strings(value: Any, key: str) -> list[str]:
    if not isinstance(value, dict) or not isinstance(value.get(key), list):
        return []
    return [item for item in value[key] if isinstance(item, str) and item]


def _ids(value: Any, key: str) -> list[str]:
    if not isinstance(value, dict) or not isinstance(value.get(key), list):
        return []
    result: list[str] = []
    for item in value[key]:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
            result.append(item["id"])
    return result


def _enum(values: list[str], fallback: dict[str, Any] = _ID) -> dict[str, Any]:
    unique = list(dict.fromkeys(values))
    return {"type": "string", "enum": unique} if unique else fallback


def _scenario(task: dict[str, Any] | None) -> dict[str, Any]:
    value = task.get("scenario_sha256") if isinstance(task, dict) else None
    return {"type": "string", "enum": [value]} if isinstance(value, str) else _DIGEST


def discovery_handoff_schema(
    kind: str, agent: str, task: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Return a closed semantic schema for a discovery handoff kind."""

    if kind == "MissionPlan":
        resources = task.get("resources") if isinstance(task, dict) else None
        agents = _strings(task, "available_execution_agents") if isinstance(task, dict) else []
        skills = _strings(task, "available_execution_skills") if isinstance(task, dict) else []
        capabilities = _ids(task, "available_capabilities") if isinstance(task, dict) else []
        deferred_hardware = _ids(task, "deferred_hardware_capabilities") if isinstance(task, dict) else []
        context = task.get("knowledge_context") if isinstance(task, dict) else None
        claim_ids = _ids(context, "claims")
        selected = resources.get("selected_contract") if isinstance(resources, dict) else None
        contract_name = selected.get("name") if isinstance(selected, dict) else None
        baseline_schema: dict[str, Any] = (
            {"type": "string", "enum": [contract_name]}
            if isinstance(contract_name, str) and contract_name
            else {"type": "null"}
        )
        required_policy = task.get("search_policy") if isinstance(task, dict) else None
        policy_schema = (
            {"type": "string", "enum": [required_policy]}
            if required_policy in ("refresh", "always")
            else {"type": "string", "enum": ["refresh", "always"]}
        )
        question = _object(
            {
                "id": _ID,
                "question": _TEXT,
                "why": _TEXT,
                "required_source_types": _array(_TEXT, minimum=1, maximum=8),
            }
        )
        return _base(
            kind,
            agent,
            {
                "scenario_sha256": _scenario(task),
                "mission_type": {
                    "type": "string",
                    "enum": ["new", "improve", "fix", "investigate"],
                },
                "search_policy": policy_schema,
                "summary": _TEXT,
                "baseline_contract": baseline_schema,
                "baseline_claim_ids": _array(_enum(claim_ids, _DIGEST), maximum=64),
                "questions": _array(question, minimum=1, maximum=32),
                "completion_criteria": _array(_TEXT, minimum=1, maximum=64),
                "search_budget": {"type": "integer", "minimum": 1, "maximum": 64},
                "required_materials": _array(_TEXT, minimum=1, maximum=64),
                "execution_agents": _array(_enum(agents), minimum=1, maximum=32),
                "execution_skills": _array(_enum(skills), maximum=64),
                "capability_ids": _array(_enum(capabilities), maximum=64),
                "deferred_hardware_capability_ids": _array(
                    _enum(deferred_hardware), maximum=64
                ),
                "contract_action": {
                    "type": "string",
                    "enum": (["reuse", "amend", "new"] if contract_name else ["new"]),
                },
            },
        )

    if kind == "ResearchPlan":
        question = _object(
            {
                "id": _ID,
                "question": _TEXT,
                "why": _TEXT,
                "required_source_types": _array(_TEXT, minimum=1, maximum=8),
            }
        )
        return _base(
            kind,
            agent,
            {
                "scenario_sha256": _scenario(task),
                "questions": _array(question, minimum=1, maximum=16),
                "completion_criteria": _array(_TEXT, minimum=1, maximum=64),
                "search_budget": {"type": "integer", "minimum": 1, "maximum": 64},
            },
        )

    if kind == "SourcePacket":
        plan = task.get("research_plan") if isinstance(task, dict) else None
        question_ids = _ids(plan, "questions")
        question_id = _enum(question_ids)
        query = _object(
            {
                "query": _TEXT,
                "reason": _TEXT,
                "selected_document_ids": _array(_DIGEST),
            }
        )
        document = _object(
            {"document_id": _DIGEST, "title": _TEXT, "source_kind": _ID}
        )
        citation = _object(
            {
                "document_id": _DIGEST,
                "page": {"type": "integer", "minimum": 1},
                "excerpt": _TEXT,
            }
        )
        claim = _object(
            {
                "id": _ID,
                "question_id": question_id,
                "statement": _TEXT,
                "value": _scalar_or_scalar_array(),
                "citations": _array(citation, minimum=1, maximum=16),
            }
        )
        return _base(
            kind,
            agent,
            {
                "scenario_sha256": _scenario(task),
                "queries": _array(query),
                "documents": _array(document),
                "claims": _array(claim),
                "open_questions": _array(question_id, maximum=16),
            },
        )

    if kind == "ChangeAnalysis":
        mission = task.get("mission_plan") if isinstance(task, dict) else None
        packet = task.get("source_packet") if isinstance(task, dict) else None
        baseline = mission.get("baseline_contract") if isinstance(mission, dict) else None
        baseline_schema = (
            {"type": "string", "enum": [baseline]}
            if isinstance(baseline, str) and baseline
            else {"type": "null"}
        )
        finding = _object(
            {
                "id": _ID,
                "area": _ID,
                "status": {
                    "type": "string",
                    "enum": ["unchanged", "changed", "missing", "conflict"],
                },
                "baseline": _TEXT,
                "current": _TEXT,
                "impact": _TEXT,
                "source_claim_ids": _array(_enum(_ids(packet, "claims")), maximum=128),
            }
        )
        return _base(
            kind,
            agent,
            {
                "scenario_sha256": _scenario(task),
                "baseline_contract": baseline_schema,
                "summary": _TEXT,
                "recommended_contract_action": {
                    "type": "string",
                    "enum": (["reuse", "amend", "new"] if baseline else ["new"]),
                },
                "findings": _array(finding, minimum=1, maximum=64),
                "impacted_agents": _array(
                    _enum(_strings(mission, "execution_agents")), maximum=32
                ),
            },
        )

    if kind == "ArchitectureDecision":
        plan = task.get("research_plan") if isinstance(task, dict) else None
        packet = task.get("source_packet") if isinstance(task, dict) else None
        question_id = _enum(_ids(plan, "questions"))
        claim_id = _enum(_ids(packet, "claims"))
        decision = _object(
            {
                "id": _ID,
                "statement": _TEXT,
                "rationale": _TEXT,
                "source_claim_ids": _array(claim_id, maximum=128),
            }
        )
        return _base(
            kind,
            agent,
            {
                "scenario_sha256": _scenario(task),
                "architecture_id": _ID,
                "status": {
                    "type": "string",
                    "enum": ["selected", "needs_research", "not_feasible"],
                },
                "summary": _TEXT,
                "decisions": _array(decision, maximum=64),
                "open_questions": _array(question_id, maximum=16),
            },
        )

    if kind == "ContractDraft":
        decision = task.get("architecture_decision") if isinstance(task, dict) else None
        packet = task.get("source_packet") if isinstance(task, dict) else None
        agents = _strings(task, "available_agents") if isinstance(task, dict) else []
        tools = _strings(task, "available_tools") if isinstance(task, dict) else []
        architecture_id = (
            decision.get("architecture_id") if isinstance(decision, dict) else None
        )
        architecture_schema = (
            {"type": "string", "enum": [architecture_id]}
            if isinstance(architecture_id, str)
            else _ID
        )
        agent_id = _enum(agents)
        tool_id = _enum(tools)
        claim_id = _enum(_ids(packet, "claims"))
        empty_args = _object({})
        expected_io_args = _object(
            {
                "expected_io": _object(
                    {
                        "ports": _array(
                            _object(
                                {
                                    "name": _ID,
                                    "pin": {"type": "integer", "minimum": 1},
                                }
                            ),
                            minimum=1,
                            maximum=64,
                        ),
                        "io_type": _TEXT,
                        "drive_ma": {"type": "integer", "minimum": 1},
                        "slew": _TEXT,
                    }
                )
            }
        )
        tool_args = {
            "anyOf": [
                empty_args,
                expected_io_args,
                _object({"script": _ID}),
                _object({"program_xcf": _TEXT}),
                _object({"params_json": _TEXT}),
            ]
        }
        condition = _object(
            {"field": _TEXT, "expect": _scalar_or_scalar_array(), "source": _TEXT}
        )
        step = _object(
            {
                "id": _ID,
                "agent": agent_id,
                "tool": tool_id,
                "tool_args": tool_args,
                "intent": _TEXT,
                "touches_hardware": {"type": "boolean"},
                "advance_when": _array(condition, minimum=1, maximum=64),
            }
        )
        return _base(
            kind,
            agent,
            {
                "scenario_sha256": _scenario(task),
                "architecture_id": architecture_schema,
                "contract_name": _ID,
                "execution_mode": {"type": "string", "enum": ["offline", "hardware"]},
                "objective": _TEXT,
                "roles": _array(agent_id, minimum=1, maximum=32),
                "skills": _array(_TEXT, maximum=64),
                "constraints": _array(_TEXT, maximum=64),
                "steps": _array(step, minimum=1, maximum=64),
                "pass_criteria": _array(_TEXT, minimum=1, maximum=64),
                "evidence_claim_ids": _array(claim_id, minimum=1, maximum=128),
            },
        )

    if kind == "ContractReview":
        draft = task.get("contract_draft") if isinstance(task, dict) else None
        scenario = draft.get("scenario_sha256") if isinstance(draft, dict) else None
        draft_sha = task.get("draft_sha256") if isinstance(task, dict) else None
        finding = _object(
            {
                "code": _ID,
                "severity": {"type": "string", "enum": ["info", "warning", "error"]},
                "detail": _TEXT,
            }
        )
        return _base(
            kind,
            agent,
            {
                "scenario_sha256": (
                    {"type": "string", "enum": [scenario]}
                    if isinstance(scenario, str)
                    else _DIGEST
                ),
                "draft_sha256": (
                    {"type": "string", "enum": [draft_sha]}
                    if isinstance(draft_sha, str)
                    else _DIGEST
                ),
                "outcome": {
                    "type": "string",
                    "enum": ["ready_for_human_review", "revise", "rejected"],
                },
                "findings": _array(finding, maximum=64),
                "required_changes": _array(_TEXT, maximum=64),
            },
        )

    return None
