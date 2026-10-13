"""Run the scenario daemon with Lead, Builder and Verifier.

    py -3 -m ai_hardware_autolab.orchestrator                       general development (default)
    py -3 -m ai_hardware_autolab.orchestrator --profile offline     no device access
    py -3 -m ai_hardware_autolab.orchestrator --dry-run             no model or device calls

    py -3 -m ai_hardware_autolab.orchestrator mission "scenario"
    py -3 -m ai_hardware_autolab.orchestrator status

Projects use their own tools inside an isolated workspace.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # pragma: no cover - direct execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.orchestrator import service as S
from ai_hardware_autolab.orchestrator.daemon import Daemon, SchedulerPolicy
from ai_hardware_autolab.orchestrator.jobs import JobLedger
from ai_hardware_autolab.orchestrator.workflows import WorkflowRouter
from ai_hardware_autolab.orchestrator.workspace import WorkspaceManager, WorkspaceError
from ai_hardware_autolab.orchestrator.projects import ProjectRegistry, ProjectWorkspaces, project_description
from ai_hardware_autolab.orchestrator.conversation_service import WebConversationFlow, WorkspaceConversationRuntime
from ai_hardware_autolab.orchestrator.conversation_codex import ConversationCodexConfig
from ai_hardware_autolab.knowledge.models import KnowledgeScope, KnowledgeValidationError
from ai_hardware_autolab.knowledge.store import KnowledgeStore, KnowledgeStoreError
from ai_hardware_autolab.knowledge.retriever import KnowledgeRetriever

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = PLATFORM_ROOT / "runs" / "jobs.db"
DEFAULT_RUNS = PLATFORM_ROOT / "runs"
DEFAULT_KNOWLEDGE = PLATFORM_ROOT / "runs" / "knowledge.db"
DEFAULT_WORKSPACES = PLATFORM_ROOT / "workspaces"
DEFAULT_PROFILE = "offline"




def _approval_mode(profile: str) -> str:
    if profile == "hardware":
        return "automatic device execution within submitted hardware scenarios"
    return "hardware access disabled (offline)"


def build_service(args: argparse.Namespace) -> tuple[S.Service, None]:
    """Build only the current conversation runtime; history remains readable."""
    workspace_root = getattr(args, "workspace_root", str(DEFAULT_WORKSPACES))
    if not workspace_root:
        raise ValueError("scenario jobs require a per-job workspace root")
    # Reject stale programmatic launch settings as well as removed CLI flags.
    if getattr(args, "campaign", "") or getattr(args, "campaign_sha256", ""):
        raise ValueError("Campaign execution has been retired; submit a hardware scenario")

    project_root = Path(getattr(args, "project_root", "") or PLATFORM_ROOT.parent).resolve()
    project_id = str(getattr(args, "knowledge_project", "") or
                     (project_description(project_root)["project_id"] if getattr(args, "project_root", "") else "ai-hardware-autolab"))
    ledger = JobLedger(Path(args.ledger))
    knowledge_store = None
    knowledge_scope = None
    knowledge_retriever = None
    knowledge_name = str(getattr(args, "knowledge_db", "") or "")
    if knowledge_name:
        knowledge_store = KnowledgeStore(Path(knowledge_name))
        scope_data = {
            key: value
            for key, value in {
                "project_id": project_id,
                "hardware_id": str(getattr(args, "knowledge_hardware", "") or ""),
                "revision": str(getattr(args, "knowledge_revision", "") or ""),
            }.items()
            if value
        }
        knowledge_scope = KnowledgeScope.parse(scope_data)
        knowledge_retriever = KnowledgeRetriever(knowledge_store)

    projects = ProjectRegistry(Path(args.ledger).parent / "projects.json", project_root, default_id=project_id)
    base_manager = WorkspaceManager(Path(workspace_root), project_root=project_root,
                                    source_prefixes=None if project_root != PLATFORM_ROOT.parent else ("ai_hardware_autolab", "scripts"))
    manager = ProjectWorkspaces(base_manager, projects)
    runtime = WorkspaceConversationRuntime(
        ledger, manager, ConversationCodexConfig(manager.root), dry_run=args.dry_run,
        role_models={role: getattr(args, f"{role.replace('-', '_')}_model", None) or None
                     for role in ("lead-agent", "builder", "verifier")},
        knowledge_retriever=knowledge_retriever, knowledge_scope=knowledge_scope,
    )
    conversation = WebConversationFlow(ledger, runtime, manager)
    workflows = WorkflowRouter(ledger, conversation=conversation)
    daemon = Daemon(
        ledger, workflows,
        policy=SchedulerPolicy(base_backoff_s=args.backoff, max_rate_limit_retries=args.retries),
    )
    service = S.Service(
        ledger, daemon,
        config=S.ServiceConfig(
            interval_s=args.interval, pipe_name=args.pipe,
            runs_dir=Path(args.runs), project_root=PLATFORM_ROOT.parent,
            runtime_profile=args.profile, workspace_mode="isolated",
        ),
        knowledge_store=knowledge_store, knowledge_scope=knowledge_scope,
        project_registry=projects,
    )
    return service, None


# -- one-shot subcommands ----------------------------------------------------


def _send(args: argparse.Namespace, request: dict) -> int:
    try:
        answer = S.send(request, args.pipe or S.DEFAULT_PIPE)
    except OSError as exc:
        print(f"no daemon on {args.pipe or S.DEFAULT_PIPE}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(answer, ensure_ascii=False, indent=2))
    return 0 if answer.get("ok", True) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autolab-daemon", description=__doc__)
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--runs", default=str(DEFAULT_RUNS))
    # Old PowerShell launch commands still pass this directory. Accept only
    # for startup compatibility; never restore contract-based execution.
    parser.add_argument("--contracts", dest="legacy_contracts", default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument(
        "--knowledge-db",
        default=str(DEFAULT_KNOWLEDGE),
        help="append-only knowledge events and rebuildable search projection",
    )
    parser.add_argument("--knowledge-project", default="", help="knowledge namespace; external projects get a stable ID by default")
    parser.add_argument("--knowledge-hardware", default="")
    parser.add_argument("--knowledge-revision", default="")
    parser.add_argument("--pipe", default=None, help=f"default {S.DEFAULT_PIPE}")
    parser.add_argument("--profile", choices=("offline",), default=DEFAULT_PROFILE)
    parser.add_argument("--lead-agent-model", default="", help="optional Codex model for Lead")
    parser.add_argument("--builder-model", default="", help="optional Codex model for Builder")
    parser.add_argument("--verifier-model", default="", help="optional Codex model for Verifier")
    parser.add_argument("--interval", type=float, default=S.DEFAULT_INTERVAL_S)
    parser.add_argument("--backoff", type=float, default=60.0)
    parser.add_argument("--retries", type=int, default=6)
    parser.add_argument(
        "--workspace-root",
        default=str(DEFAULT_WORKSPACES),
        help="directory for automatic per-job Git worktrees",
    )
    parser.add_argument("--project-root", default="", help="Git checkout to develop; platform installation is separate")
    parser.add_argument("--dry-run", action="store_true", help="wire everything, start no agent")
    parser.add_argument("--ticks", type=int, default=None, help="stop after this many ticks")

    sub = parser.add_subparsers(dest="command")
    mission = sub.add_parser(
        "mission",
        help="start the Lead, Builder, Verifier scenario workflow",
    )
    mission.add_argument("scenario")
    mission.add_argument("--objective", default="")
    mission.add_argument(
        "--search-policy",
        choices=("refresh", "always"),
        default="refresh",
    )

    sub.add_parser("status", help="ask a running daemon what it is doing")

    decide = sub.add_parser("approve", help="record a decision on a pending approval")
    decide.add_argument("approval_id")
    decide.add_argument("--by", required=True)
    decide.add_argument("--deny", action="store_true")
    decide.add_argument("--note", default="")

    steer = sub.add_parser("steer", help="leave a message for the next agent")
    steer.add_argument("job_id")
    steer.add_argument("message")
    steer.add_argument("--by", required=True)

    stop = sub.add_parser("cancel", help="stop a job for good")
    stop.add_argument("job_id")
    stop.add_argument("--by", required=True)
    stop.add_argument("--reason", default="")

    resume = sub.add_parser('resume-step', help='verify deployment and resume an unexecuted blocked step')
    resume.add_argument('job_id')
    resume.add_argument('--by', required=True)
    resume.add_argument('--note', required=True)

    args = parser.parse_args(argv)
    if args.legacy_contracts is not None:
        print("note: --contracts is obsolete and ignored; scenario jobs do not use execution contracts.",
              file=sys.stderr)
    del args.legacy_contracts

    if args.command == "mission":
        selected = {}
        try:
            preview_answer = S.send(
                {"command": "preflight", "scenario": args.scenario, **selected},
                args.pipe or S.DEFAULT_PIPE,
            )
        except OSError as exc:
            print(f"no daemon on {args.pipe or S.DEFAULT_PIPE}: {exc}", file=sys.stderr)
            return 2
        preview = preview_answer.get("preflight") if preview_answer.get("ok") else None
        if not isinstance(preview, dict):
            print(json.dumps(preview_answer, ensure_ascii=False, indent=2))
            return 1
        return _send(
            args,
            {
                "command": "mission",
                "objective": args.objective or preview.get("objective") or args.scenario,
                "scenario": args.scenario,
                "scenario_sha256": preview.get("scenario_sha256"),
                "search_policy": args.search_policy,
                **selected,
            },
        )
    if args.command == "status":
        return _send(args, {"command": "status"})
    if args.command == 'resume-step':
        return _send(args, {'command': 'resume-step', 'job_id': args.job_id, 'requested_by': args.by, 'note': args.note})
    if args.command == "approve":
        return _send(
            args,
            {
                "command": "approve",
                "approval_id": args.approval_id,
                "granted": not args.deny,
                "decided_by": args.by,
                "note": args.note,
            },
        )
    if args.command == "steer":
        return _send(
            args,
            {
                "command": "steer",
                "job_id": args.job_id,
                "message": args.message,
                "sent_by": args.by,
            },
        )
    if args.command == "cancel":
        return _send(
            args,
            {
                "command": "cancel",
                "job_id": args.job_id,
                "cancelled_by": args.by,
                "reason": args.reason,
            },
        )

    try:
        service, raw_runner = build_service(args)
    except (
        ValueError,
        KnowledgeValidationError,
        KnowledgeStoreError,
        WorkspaceError,
    ) as exc:
        print(f"daemon refused: {exc}", file=sys.stderr)
        return 2
    info = service.start()
    print(
        f"daemon on {info['address']}\n"
        f"  profile   {args.profile}{' (dry run: no agent will be started)' if args.dry_run else ''}\n"
        f"  ledger    {args.ledger}\n"
        f"  knowledge {args.knowledge_db}\n"
        f"  cursor    {info['cursor']}\n"
        f"  approval  {_approval_mode(args.profile)}\n"
        f"  acl sid   {info.get('sid') or '(socket transport)'}",
        file=sys.stderr,
    )
    for note in info["recovered"]:
        print(f"  recovered {note}", file=sys.stderr)
    try:
        service.run_forever(max_ticks=args.ticks)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    finally:
        service.close()
        if raw_runner is not None and hasattr(raw_runner, "close"):
            raw_runner.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
