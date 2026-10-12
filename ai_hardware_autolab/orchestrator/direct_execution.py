"""Execute a fully specified Plan call, not a second model conversation.

No scenario/board classifier. Lead opts in per step; unknown methods, source
changes and unresolved arguments remain ordinary Builder tasks. Uses exactly
the same bound registry and daemon approval callback as model-driven calls.
"""
from __future__ import annotations

from typing import Any

from ai_hardware_autolab.mcp_server.registry import ServerPolicy, build_tools


def direct_arguments(step: dict[str, Any]) -> dict[str, Any]:
    if step.get("touches_hardware") is not True or step.get("entrypoint"):
        raise ValueError("direct execution needs a native device call without a workspace entrypoint")
    args = step.get("tool_args")
    if not isinstance(args, dict):
        raise ValueError("direct execution needs tool_args; unresolved arguments belong to Builder")
    if "contract" in args or "record_name" in args:
        raise ValueError("direct tool_args must omit contract/record_name; the host binds them")
    return {**args, "contract": step.get("contract")}


def validate_direct_step(step: dict[str, Any]) -> None:
    args = direct_arguments(step)
    tools = {t.name: t for t in build_tools(ServerPolicy(role="builder", allow_hardware=True))}
    tool = tools.get(step.get("tool"))
    if tool is None or not tool.touches_hardware:
        raise ValueError("direct execution needs an installed Builder device tool")
    schema = tool.input_schema
    props = schema.get("properties", {})
    if set(args) - props.keys():
        raise ValueError("direct tool_args contain unknown arguments")
    if any(k not in args for k in schema.get("required", [])):
        raise ValueError("direct tool_args are missing required arguments")
    types = {"string": str, "boolean": bool, "object": dict, "array": list}
    for key, value in args.items():
        expected = types.get(props.get(key, {}).get("type"))
        if expected is not None and type(value) is not expected:
            raise ValueError(f"direct tool_args.{key} must be {expected.__name__}")
    if tool.name in {"run_bbb", "measure_scope"}:
        other = "script" if tool.name == "run_bbb" else "params"
        if args.get("identity_only") is True:
            if args.get(other):
                raise ValueError("identity_only cannot include script/capture arguments")
        elif not args.get(other):
            raise ValueError(f"direct {tool.name} needs {other} or identity_only=true")


def execute_direct(agent, task, config, trusted_config, event_sink=None):
    """Already preauthorized by the runner; registry rechecks bytes at use."""
    from ai_hardware_autolab.agents.export import MCP_SERVER_NAME
    from ai_hardware_autolab.orchestrator.device_evidence import bind_device_evidence

    step = task["step"]
    args = direct_arguments(step)
    policy = ServerPolicy(role=agent.name, allow_hardware=True,
        project_root=config.project_root, device_root=trusted_config.project_root,
        contracts_dir=trusted_config.qkd_root / "contracts", runs_dir=config.runs_dir,
        hardware_tool=config.hardware_tool, hardware_contract=config.hardware_contract,
        hardware_contract_sha256=config.hardware_contract_sha256)
    tool = next(t for t in build_tools(policy) if t.name == step["tool"])
    item = {"id": f"direct-{task.get('job_id')}-{task.get('iteration')}-{step['id']}",
            "type": "mcpToolCall", "server": MCP_SERVER_NAME, "tool": tool.name,
            "arguments": args, "execution": "direct", "model_invoked": False}
    events = []

    def emit(phase, **fields):
        event = {"method": f"platform/tool/{phase}",
                 "params": {"execution": "direct", "item": {**item, **fields}}}
        events.append(event)
        if event_sink:
            event_sink(event)

    emit("started", status="inProgress")
    try:
        payload = tool.handler(args)
    except Exception as exc:
        # A transport exception is NOT permission to replay a physical action.
        # The loop blocks/reconciles an unrecorded outcome. Normal driver FAIL
        # results, in contrast, have a manifest and can be replanned below.
        emit("failed", status="failed", error={"message": str(exc)})
        return {"kind": "StepResult", "agent": agent.name, "step_id": step["id"],
                "status": "blocked", "runs": [], "execution": "direct", "model_invoked": False,
                "hardware_state_unknown": True,
                "summary": f"No attributable device outcome; reconcile before retry: {type(exc).__name__}: {exc}"}, events
    emit("completed", status="completed", result={"structuredContent": payload})
    result = payload.get("result") or {}
    envelope = {"execution": "direct", "model_invoked": False,
                "summary": f"{tool.name}: {payload.get('run_verdict')}",
                "observations": {"versions": result.get("versions", {}),
                                 "measurements": {key: value for key, value in result.get("measurements", {}).items()
                                    if key in {"identity_only", "identity_verified", "target", "model", "execution_completed",
                                               "diagnostic_complete", "failure_stage", "failure_error", "identification_response", "options_response"}}}}
    envelope = bind_device_evidence(envelope, task, events, config.runs_dir)
    if envelope.get('execution_blocked') == 'platform_deployment':
        return envelope, events
    if not envelope.get("run_binding"):
        return {**envelope, "kind": "StepResult", "agent": agent.name, "step_id": step["id"],
                "status": "blocked", "runs": [], "hardware_state_unknown": True,
                "summary": "Direct device call returned no attributable on-disk run; do not replay"}, events
    return envelope, events
