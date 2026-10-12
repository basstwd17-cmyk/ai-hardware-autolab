"""Small contracts for autonomous development, not an engineering workflow.

The model chooses the implementation and checks. The platform owns identity,
isolation, evidence and device authority. Historical jobs retain their rules.
"""
from __future__ import annotations

import copy
from typing import Any

from QKD_PROJ.agents.schema import ValidationError, validate_handoff, validate_plan_governance

MODE = "autonomous-v1"
LEAD_HANDOFF_GUIDANCE = (
    "A short Plan may contain only objective, approach, references and acceptance_criteria "
    "(plus cautions/unknowns when relevant). No executable steps, hashes or hardware YAML are required "
    "to hand work to Builder. Use detailed direct steps only when execution inputs are already known. "
    "Choose the engineering approach, relevant existing sources, device boundary and acceptance criteria; "
    "then hand off the Plan. Builder owns executable scripts, shell quoting, encoding, source hashing "
    "and implementation debugging. Do not solve those merely to finish a Plan. If exact arguments "
    "are not already known, use execution=agent and explain the remaining preparation in instructions; "
    "do not manufacture a direct call or add a dummy offline test stage. Research genuine missing "
    "design/safety facts, including new boards, but a local command error is not a reason to restart "
    "research. Required device/candidate checks still happen before execution; never invent a hash."
)
WORKSPACE_CONTEXT = "__workspace__"
PROTECTED = (
    ".git", ".codex", ".agents", "QKD_PROJ/contracts", "QKD_PROJ/campaigns",
    "QKD_PROJ/runs", "QKD_PROJ/references", "QKD_PROJ/candidates",
    "QKD_PROJ/webui/state",
)


def autonomous(resources: Any) -> bool:
    return isinstance(resources, dict) and resources.get("development_mode") == MODE


def protected_path(path: str) -> bool:
    path = path.replace("\\", "/").casefold().strip("/")
    return any(path == prefix.casefold() or path.startswith(prefix.casefold() + "/")
               for prefix in PROTECTED) or ".git" in path.split("/")


def normalize_plan(plan: Any, resources: dict[str, Any]) -> Any:
    if not autonomous(resources) or not isinstance(plan, dict):
        return plan
    result = copy.deepcopy(plan)
    if "steps" not in result and not result.get("blocked_on") and isinstance(result.get("objective"), str):
        result = preparation_plan(result, "Lead supplied an engineering brief; Builder chooses concrete execution inputs.")
    if not isinstance(result.get("steps"), list):
        return result  # The handoff validator reports a correctable format error.
    for step in result["steps"]:
        if isinstance(step, dict) and not step.get("tool"):
            return preparation_plan(result, "Builder must select the concrete execution tool and inputs.")
        if isinstance(step, dict) and step.get("execution") == "direct":
            from .direct_execution import validate_direct_step
            try:
                validate_direct_step(step)
            except ValueError as exc:
                return preparation_plan(result, f"Direct execution needs preparation: {exc}")
    for step in result.get("steps", []):
        if not isinstance(step, dict):
            continue
        step.setdefault("agent", "builder")
        if step.get("tool") == "codex_workspace":
            step.setdefault("touches_hardware", False)
            step["contract"] = WORKSPACE_CONTEXT
        elif step.get("touches_hardware") and isinstance(result.get("hardware_contract"), dict):
            step.setdefault("contract", "$hardware_boundary")
        # Natural-language acceptance criteria belong to the reviewer, not a
        # lookup for a nonexistent manifest field such as 'report.completeness'.
        criteria = step.get("advance_when", [])
        if isinstance(criteria, list):
            previous_criteria = step.get("acceptance_criteria") or []
            measurable, review = [], (list(previous_criteria) if isinstance(previous_criteria, list) else [previous_criteria])
            for condition in criteria:
                field = condition.get("field", "") if isinstance(condition, dict) else ""
                if isinstance(field, str) and (field.startswith(("measurements.", "versions.")) or field == "verdict"
                                              or (isinstance(condition, dict) and not isinstance(condition.get("expect"), str))):
                    measurable.append(condition)
                elif isinstance(condition, dict) and "expect" in condition:
                    review.append(condition)
                else:
                    measurable.append(condition)  # validator reports malformed input
            if review:
                step["acceptance_criteria"] = review
            if not measurable:
                from .diagnostics import diagnostic_step
                if diagnostic_step(step):
                    measurable = [{"field": "measurements.diagnostic_complete", "expect": True,
                                   "source": "recorded read-only diagnosis; device verdict remains separate"}]
                elif step.get("tool") == "codex_workspace":
                    measurable = [{"field": "measurements.execution_integrity", "expect": True,
                                   "source": "recorded execution; functional coverage is reviewed separately"}]
                else:
                    measurable = [{"field": "measurements.execution_completed", "expect": True,
                                   "source": "actual tool execution, not a claim of functional success"}]
            step["advance_when"] = measurable
    return result


def preparation_plan(plan: dict[str, Any], reason: str) -> dict[str, Any]:
    """An incomplete executable plan is input to coding, never device authority."""
    original = copy.deepcopy(plan)
    # Avoid nesting the same failed preparation context on repeated rounds.
    previous = original.pop("preparation_context", None)
    intent = previous.get("original_plan", original) if isinstance(previous, dict) else original
    if not isinstance(intent, dict):
        intent = original
    return {
        "kind": "Plan", "agent": "lead-agent", "objective": plan.get("objective", "Prepare requested work"),
        **{key: plan[key] for key in ("gate", "iteration") if key in plan},
        "requirements_complete": False, "builder_preparation": True,
        "remaining_work": "Execute and verify remaining scenario requirements after preparation.",
        "preparation_context": {"reason": reason, "original_plan": intent},
        "steps": [{"id": "prepare_execution", "agent": "builder", "tool": "codex_workspace",
                   "contract": WORKSPACE_CONTEXT, "touches_hardware": False, "execution": "agent",
                   "instructions": (
                       "Prepare the remaining work from plan.preparation_context and original scenario. "
                       "Reuse completed work, existing code and candidate contract references. "
                       "Fix only missing implementation/inputs; no mandatory tests for unchanged code. "
                       "This turn cannot contact devices. Return a StepResult with prepared_plan (a Plan "
                       "for remaining actions only) when ready; the host validates it before device dispatch. "
                       "Use candidate_contract to reference existing SRAM metadata, not copy hashes or approvals. "
                       "If only analysis/code was requested, prepared_plan may ask a minimal read-only review "
                       "of the saved result. Report unresolved safety facts; never guess or enlarge authority."
                   ),
                   "advance_when": [{"field": "measurements.execution_integrity", "expect": True,
                                     "source": "preparation run only; not hardware or scenario success"}]}],
    }


def compact_task(task: dict[str, Any], *, retain_catalog: bool = False) -> dict[str, Any]:
    """Keep handoff context, not thousands of lines of repeated audit metadata."""
    result = copy.deepcopy(task)
    resources = result.get("resources") or {}
    if autonomous(resources):
        # These envelopes repeat the entire submitted scenario (including the
        # authority preamble). Keep one exact copy, not three paraphrases.
        for value in (resources.get("preflight"), resources.get("mission_contract"),
                      (resources.get("preflight") or {}).get("mission_contract")):
            if isinstance(value, dict) and value.get("scenario") == result.get("scenario"):
                value.pop("scenario", None)
                value["scenario_reference"] = "task.scenario (identical text omitted)"
        nested = (resources.get("preflight") or {}).get("mission_contract")
        if isinstance(nested, dict) and nested == resources.get("mission_contract"):
            resources["preflight"].pop("mission_contract")
            resources["preflight"]["mission_contract_reference"] = "resources.mission_contract"
    if not retain_catalog:
        for key in ("available_capabilities", "capability_candidates", "board_development", "preflight"):
            resources.pop(key, None)  # Lead's selected paths remain in its instructions.
    if result.get("knowledge_context") == resources.get("knowledge_context"):
        result.pop("knowledge_context", None)
    if result.get("source_context") == resources.get("source_context"):
        result.pop("source_context", None)
    # Keep original requirements, full Plan, human feedback, authority, errors,
    # checks and numerical evidence. Only omit the duplicate filesystem audit.
    # All original envelopes remain in SQLite and their run manifests.
    for key in ("previous_step_results", "previous_iteration_results", "step_results"):
        rows = result.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            audit = row.pop("workspace_changes", None)
            if isinstance(audit, dict):
                row["workspace_audit_reference"] = {
                    k: audit[k] for k in ("workspace", "change_digest", "review_required") if k in audit
                }
                row["workspace_audit_reference"]["detail"] = "full audit in this handoff's run manifest"
    return result


def validate_development_plan(plan: dict[str, Any], resources: dict[str, Any], *,
                              available_capabilities: list[dict[str, Any]]) -> None:
    if not autonomous(resources):
        validate_plan_governance(plan, available_capabilities=available_capabilities)
        return
    validate_handoff(plan)
    if not isinstance(plan.get("objective"), str) or not plan["objective"].strip():
        raise ValidationError("Plan needs its engineering objective")
    if type(plan.get("requirements_complete")) is not bool:
        raise ValidationError("Plan needs requirements_complete: does this plan cover the whole scenario?")
    for step in plan.get("steps", []):
        from .diagnostics import diagnostic_step
        if step.get("completion", "success") not in {"success", "diagnostic_report"}:
            raise ValidationError("completion must be success or diagnostic_report")
        if step.get("completion") == "diagnostic_report" and not diagnostic_step(step):
            raise ValidationError("diagnostic_report requires native identity_only run_bbb/measure_scope, without entrypoint")
        execution = step.get("execution", "agent")
        if execution not in ("agent", "direct"):
            raise ValidationError("step execution must be agent or direct")
        if execution == "direct":
            from .direct_execution import validate_direct_step
            try:
                validate_direct_step(step)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        if step.get("agent") != "builder":
            raise ValidationError("Lead hands implementation steps to builder")
        if type(step.get("touches_hardware")) is not bool:
            raise ValidationError("Each step must explicitly declare touches_hardware")
        if step.get("touches_hardware") and resources.get("execution_mode") != "hardware":
            raise ValidationError("Offline jobs cannot contact hardware")
        if step.get("tool") == "codex_workspace" and step["touches_hardware"]:
            raise ValidationError("Device effects must use the authorized hardware boundary")
    from QKD_PROJ.mcp_server.registry import ServerPolicy, build_tools
    effects = {tool.name: tool.touches_hardware for tool in build_tools(ServerPolicy(role="builder", allow_hardware=True))}
    for step in plan.get("steps", []):
        if effects.get(step.get("tool")) is True and step.get("touches_hardware") is not True:
            raise ValidationError("A hardware tool cannot be labeled as offline development")


class WorkspaceEvidenceContext:
    """Run metadata only. Deliberately has no require/authorize operation."""
    def __init__(self, task: dict[str, Any]):
        self.task = task

    def describe(self) -> dict[str, Any]:
        return {"type": "workspace-development", "hardware_access": False,
                "job_id": self.task.get("job_id"), "iteration": self.task.get("iteration"),
                "step_id": (self.task.get("step") or {}).get("id")}

    def pass_criteria(self) -> dict[str, Any]:
        return {"conditions": copy.deepcopy((self.task.get("step") or {}).get("advance_when", []))}
