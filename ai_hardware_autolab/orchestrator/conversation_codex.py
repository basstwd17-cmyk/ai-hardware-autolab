"""Codex adapter for isolated agent turns in the conversation workflow.

Reuse the JSON-RPC transport, NOT CodexAgentRunner's legacy Plan/contracts.
One work turn lets Codex inspect/edit/run/fix in its native tool loop. Hardware
is never exposed to the model process; a host adapter executes an explicit
agent connection-check or Builder device request afterward. Final message formatting gets one separate
tool-disabled correction and lost actions are never replayed.
"""
from __future__ import annotations

import json
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from .codex_transport import StdioAppServer, CodexRunnerError, _find_codex, _approval_command
from .conversation_flow import ConversationFlow, INSTRUCTIONS, ROLES
from .execution_guard import HardwareExecutionDenied, require_offline_workspace_command


def reply_schema(role, *, device_tools=()):
    statuses = ['satisfied','revise','needs_user'] if role=='verifier' else ['ready','revise','needs_user']
    return {'type':'object', 'additionalProperties':False, 'properties':{
        'status':{'type':'string','enum':statuses}, 'summary':{'type':'string'},
        'knowledge':{'type':'array','items':{'type':'object','additionalProperties':False,
            'properties':{'topic':{'type':'string'},'text':{'type':'string'}},'required':['topic','text']}},
        'artifacts':{'type':'array','items':{'type':'string'}}, 'runs':{'type':'array','items':{'type':'string'}},
        'sources':{'type':'array','items':{'type':'object','additionalProperties':False,
            'properties':{k:{'type':'string'} for k in ('title','url','finding')},'required':['title','url','finding']}},
        'target':{'type':['string','null'],'enum':[None,'lead','builder']}},
        'required':['status','summary','knowledge','artifacts','runs','sources','target']}


@dataclass(frozen=True)
class ConversationCodexConfig:
    workspaces_root: Path
    model: str | None = None
    effort: str | None = None
    timeout_seconds: float = 300
    codex_command: tuple[str, ...] = ()
    web_search: str = "live"

    def __post_init__(self):
        object.__setattr__(self, "workspaces_root", Path(self.workspaces_root).resolve(strict=True))
        if not self.workspaces_root.is_dir() or self.timeout_seconds <= 0:
            raise ValueError("existing workspaces root and positive timeout required")
        if self.web_search not in {"disabled", "cached", "live"}:
            raise ValueError("web_search must be disabled, cached or live")


def server_command(config, repair=False):
    """No QKD hardware MCP injected. Effective config is checked before a thread."""
    overrides = {
        "apps._default.enabled": False,
        "web_search": "disabled" if repair else config.web_search, "features.multi_agent": False,
        "features.shell_tool": not repair,
        "features.unified_exec": not repair,
        "features.apply_patch_freeform": not repair,
        "shell_environment_policy.inherit": "core",
        "shell_environment_policy.exclude": ["BB_*", "*PASSWORD*", "*TOKEN*", "*SECRET*"],
    }
    # Do not assume an empty table removes inherited MCPs. Inspect the effective
    # configuration before creating any thread, then disable entries by name.
    flags = [part for key, value in overrides.items()
             for part in ("-c", key + "=" + json.dumps(value))]
    # Prefer the desktop's executable on Windows over a possible older npm shim when
    # both are installed; explicit host configuration always wins.
    command = config.codex_command or (shutil.which("codex.exe") or _find_codex(),)
    return (*command, "app-server", "--stdio", *flags)


def live_client(config, workspace, repair, sink):
    command = server_command(config, repair)
    for attempt in range(2):
        client = StdioAppServer(command=command, cwd=workspace,
            timeout_seconds=config.timeout_seconds, event_sink=sink,
            client_info={"name": "qkd_conversation", "title": "QKD Conversation", "version": "0.3"})
        try:
            # Disable inherited entries locally before creating any thread and
            # verify again; never edit the user's config.
            effective = client._request("config/read", {"includeLayers": False}).get("config")
            if not isinstance(effective, dict):
                raise CodexRunnerError("App Server did not expose effective config")
            expected_web = "disabled" if repair else config.web_search
            if effective.get("web_search") != expected_web:
                raise CodexRunnerError(f"web_search configuration was not applied: expected {expected_web}")
            enabled = [(section, name) for section in ("mcp_servers", "plugins", "apps")
                       for name, value in (effective.get(section) or {}).items()
                       if not isinstance(value, dict) or value.get("enabled", section != "apps")]
            if not enabled:
                if repair and any((effective.get("features") or {}).get(key) is not False for key in
                                  ("shell_tool", "unified_exec", "apply_patch_freeform")):
                    raise CodexRunnerError("response repair tools were not disabled")
                return client
            if attempt:
                raise CodexRunnerError("offline adapter could not disable inherited integrations")
            if any("." in name or not name for _, name in enabled):
                raise CodexRunnerError("integration name cannot be safely addressed by CLI dotted overrides")
            command = (*command, *(part for section, name in enabled for part in
                       ("-c", f"{section}.{name}.enabled=false")))
        except BaseException:
            client.close()
            raise
        client.close()


class ConversationCodexRuntime:
    """Single active call; host injects config/workspace, never model reply fields.

    client_factory is only a transport test seam. No fake results in live mode.
    Native sandbox must be available; no full-access fallback or device tool.
    """
    def __init__(self, config: ConversationCodexConfig, *, client_factory=None, event_sink=None):
        self.config = config
        self.factory = client_factory or live_client
        self.event_sink = event_sink
        self._lock = threading.Lock()
        self._active = None
        self._cancelled = set()

    def _emit(self, context, role, method, params):
        if self.event_sink:
            self.event_sink({"job_id": context["job_id"], "agent": role,
                "attempt_id": context.get("attempt_id"), "method": method, "params": params})

    def cancel(self, job_id):
        with self._lock:
            self._cancelled.add(job_id)
            active = self._active
        if active and active[0] == job_id and active[2]:
            return active[1].interrupt_turn(active[2])
        return False

    def _check_cancelled(self, job_id):
        with self._lock:
            if job_id in self._cancelled:
                raise CodexRunnerError("conversation runtime cancelled")

    def _approval(self, role, workspace, repair, method, params, *, changes=None):
        # Within-root work needs no escalation. Never accept expanded roots,
        # network, command amendments, MCP or persistent approval requests.
        if method == "item/commandExecution/requestApproval":
            if repair or role != "builder":
                return {"decision": "decline"}
            cwd = Path(params.get("cwd") or workspace).resolve()
            if not cwd.is_relative_to(workspace) or any(params.get(k) for k in
                ("additionalPermissions", "networkApprovalContext")):
                return {"decision": "decline"}
            try:
                require_offline_workspace_command(_approval_command(params))
            except (HardwareExecutionDenied, CodexRunnerError):
                return {"decision": "decline"}
            # A proposed persistent prefix rule is only a suggestion. Accept
            # this command, never acceptWithExecpolicyAmendment/acceptForSession.
            return {"decision": "accept"}
        if method == "item/fileChange/requestApproval":
            if role != "builder" or repair or not changes:
                return {"decision": "decline"}
            grant_root = params.get("grantRoot")
            if grant_root and not Path(grant_root).resolve().is_relative_to(workspace):
                return {"decision": "decline"}
            for change in changes:
                if not isinstance(change, dict) or not isinstance(change.get("path"), str):
                    return {"decision": "decline"}
                paths = [change["path"]]
                kind = change.get("kind")
                if isinstance(kind, dict):
                    paths += [kind[key] for key in ("movePath", "move_path") if kind.get(key)]
                if any(not isinstance(p, str) or not (workspace / p).resolve().is_relative_to(workspace) for p in paths):
                    return {"decision": "decline"}
            return {"decision": "accept"}  # one observed in-workspace patch only
        if method == "mcpServer/elicitation/request":
            return {"action": "decline"}
        if method == "item/permissions/requestApproval":
            return {"permissions": {}, "scope": "turn"}
        raise CodexRunnerError("unsupported runtime request: " + method)

    def _turn(self, role, context, workspace, *, repair=None):
        fixing = repair is not None
        self._check_cancelled(context["job_id"])
        thread_id = None
        pending_changes = {}
        def sink(event):
            params = event.get("params", {})
            item = params.get("item") or {}
            if (event.get("method") == "item/started" and params.get("threadId") == thread_id
                    and item.get("type") == "fileChange"):
                pending_changes[item.get("id")] = item.get("changes")
            self._emit(context, role, event.get("method", "unknown"), params)
        client = self.factory(self.config, workspace, fixing, sink)
        try:
            with self._lock:
                if self._active:
                    raise CodexRunnerError("one conversation runtime call at a time")
                self._active = (context["job_id"], client, None)
            developer = INSTRUCTIONS[role] + (
                "\nThis is response formatting ONLY. No tools, research, builds, tests or device actions. "
                "Preserve facts and file references; correct the supplied final message, never invent success."
                if fixing else
                "\nUse the native web search/open tool for public datasheets and official documentation when available. "
                "Do not use shell networking, contact devices, deploy from the shell, or expand permissions. "
                "Local command network access remains disabled; it does not disable native web research. "
                "Use ordinary source inspection and, for Builder, workspace edits and commands. "
                "Fix ordinary code, argument and test errors within this turn; keep checks proportional "
                "to the request and stop when sufficient. Do not inspect platform schemas or create "
                "contracts to hand off work. Give brief progress updates. Report actual failures honestly.")
            start = {"cwd": str(workspace), "ephemeral": True, "approvalPolicy": "untrusted",
                "approvalsReviewer": "user", "developerInstructions": developer}
            if self.config.model:
                start["model"] = self.config.model
            thread_id = client.start_thread(start)
            with self._lock:
                self._active = (context["job_id"], client, thread_id)
            self._check_cancelled(context["job_id"])
            self._emit(context, role, "conversation.runtime.started", {"thread_id": thread_id, "repair": fixing})
            sandbox = {"type": "readOnly", "networkAccess": False}
            if role == "builder" and not fixing:
                sandbox = {"type": "workspaceWrite", "writableRoots": [str(workspace)],
                    "networkAccess": False, "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}
            payload = repair if fixing else {k: v for k, v in context.items()
                                            if k not in {"instructions", "reply_format"}}
            params = {"cwd": str(workspace), "approvalPolicy": "untrusted", "approvalsReviewer": "user",
                "sandboxPolicy": sandbox, "summary": "concise",
                "outputSchema": reply_schema(role),
                "input": [{"type": "text", "text": "Return one direct reply object. No nested handoff or JSON string.\n"
                           + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}]}
            if self.config.model: params["model"] = self.config.model
            if self.config.effort: params["effort"] = self.config.effort
            self._emit(context, role, "conversation.research.configured", {
                "web_search": "disabled" if fixing else self.config.web_search,
                "shell_network_access": False,
            })
            def server_request(method, request):
                return self._approval(role, workspace, fixing, method, request,
                                      changes=pending_changes.pop(request.get('itemId'), None))
            text = client.run_turn(thread_id, params, server_request=server_request)
            self._check_cancelled(context["job_id"])
            return text
        finally:
            try:
                if thread_id:
                    client.unsubscribe(thread_id)
            except Exception as exc:
                self._emit(context, role, "conversation.runtime.cleanup_warning", {"error": str(exc)})
            finally:
                client.close()
                with self._lock:
                    if self._active and self._active[1] is client:
                        self._active = None

    def __call__(self, role, context):
        if role not in ROLES or context.get("role") != role:
            raise ValueError("host role mismatch")
        if context.get("execution_mode") != "offline" or context.get("hardware_access") is not False:
            raise ValueError("conversation model turns must use the host-controlled development runtime")
        workspace = Path(context["workspace"]).resolve(strict=True)
        if (not workspace.is_dir() or workspace == self.config.workspaces_root
                or not workspace.is_relative_to(self.config.workspaces_root)):
            raise ValueError("workspace must be a child of the host-owned workspaces root")
        text = self._turn(role, context, workspace)
        for attempt in range(2):
            try:
                if len(text.encode("utf-8")) > 65536:
                    raise ValueError("final response exceeds 64 KiB")
                value = json.loads(text)
                reply, _ = ConversationFlow.validate_reply(role, value, workspace)
                return reply
            except (ValueError, TypeError) as exc:
                if attempt or len(text.encode("utf-8")) > 65536:
                    raise CodexRunnerError("final reply invalid; work was not replayed: " + str(exc)) from exc
                self._emit(context, role, "conversation.runtime.format_repair", {"error": str(exc)})
                text = self._turn(role, context, workspace, repair={
                    "original_final_message": text, "error": str(exc),
                    "instruction": "Correct only the final reply. Do not rerun completed work or fabricate artifacts."})


def ledger_event_sink(ledger):
    """Host-binds runtime observations to this attempt; deltas are not evidence."""
    def record(event):
        method = event["method"]
        if "delta" in method.lower() or not method.startswith(("item/", "turn/", "thread/tokenUsage/", "conversation.")):
            return
        params = event["params"]
        encoded = json.dumps(params, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 8192:
            params = {"truncated": True, "bytes": len(encoded.encode("utf-8")), "head": encoded[:1800]}
        # Match the UI's existing observed-runtime envelope, not a second shape.
        payload = {"method": method, "params": params, "attempt_id": event["attempt_id"]}
        item = event["params"].get("item")
        if method == "item/completed" and isinstance(item, dict) and item.get("type") == "webSearch":
            # Observed browser activity is distinct from model-authored source notes.
            # Carry it across the ephemeral role turns without persisting page bodies.
            action = item.get("action") or {}
            ledger.append_event(event["job_id"], "conversation.research.observed", {
                "attempt_id": event["attempt_id"], "item_id": item.get("id"),
                "query": str(item.get("query") or "")[:1000],
                "action": {key: action[key] for key in ("type", "url", "query", "queries", "pattern")
                           if key in action},
            }, event["agent"])
        if isinstance(item, dict) and isinstance(item.get("type"), str):
            payload["item_type"] = item["type"]
        kind = method if method.startswith("conversation.") else "runtime." + method
        ledger.append_event(event["job_id"], kind, payload, event["agent"])
    return record
