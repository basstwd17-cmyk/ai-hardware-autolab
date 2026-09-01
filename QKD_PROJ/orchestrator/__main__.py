"""Run the daemon: ledger, runner, recorder, scheduler, pipe.

    py -3 -m QKD_PROJ.orchestrator                       offline profile, default pipe
    py -3 -m QKD_PROJ.orchestrator --profile hardware    board tools registered
    py -3 -m QKD_PROJ.orchestrator --dry-run             wire everything, run no agent

    py -3 -m QKD_PROJ.orchestrator start-job GATE "objective"
    py -3 -m QKD_PROJ.orchestrator discover "new scenario"
    py -3 -m QKD_PROJ.orchestrator status
    py -3 -m QKD_PROJ.orchestrator approve APPROVAL_ID --deny --by quantum1
    py -3 -m QKD_PROJ.orchestrator contract-preview JOB_ID
    py -3 -m QKD_PROJ.orchestrator contract-request JOB_ID --by quantum1
    py -3 -m QKD_PROJ.orchestrator contract-apply APPROVAL_ID

This is the assembly, not a new layer: every part it wires together already
exists and is tested on its own. The two decisions it does make are worth
stating.

**Hardware is off unless it is asked for.** ``--profile offline`` is the
default, and it is the runner's MCP profile as well as this process's posture:
a board-touching agent cannot be reached at all. Turning it on is one flag,
typed by a person, which is where every other layer already puts that choice.

**The approval gate is always installed.** Even offline. A gate that is only
attached in the profile that needs it is a gate that is missing the first time
somebody adds a new hardware path, and its refusals cost nothing when there is
nothing to refuse.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # pragma: no cover - direct execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.orchestrator import service as S
from QKD_PROJ.orchestrator.campaign import (
    CampaignAutoApprover,
    CampaignController,
    CampaignPolicy,
    CampaignPolicyError,
)
from QKD_PROJ.orchestrator.daemon import (
    Daemon,
    HardwareApprovalGate,
    SchedulerPolicy,
    classify_failure,
)
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.loop import Loop, LoopPolicy
from QKD_PROJ.orchestrator.runtime_events import RuntimeEventRecorder
from QKD_PROJ.knowledge.models import KnowledgeScope, KnowledgeValidationError
from QKD_PROJ.knowledge.ingest import SourcePacketKnowledgeIngestor
from QKD_PROJ.knowledge.reducer import FilesystemEvidenceResolver, KnowledgeReducer
from QKD_PROJ.knowledge.store import KnowledgeStore, KnowledgeStoreError
from QKD_PROJ.research.discovery_loop import DiscoveryLoop
from QKD_PROJ.orchestrator.workflows import WorkflowRouter
from QKD_PROJ.orchestrator.continuation import MissionContinuation
from QKD_PROJ.orchestrator.workspace import PreparedJobWorkspaceResolver
from QKD_PROJ.capabilities.catalog import CapabilityCatalog
from QKD_PROJ.boards.profiles import BoardCatalog, BoardProfileError

QKD_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = QKD_ROOT / "runs" / "jobs.db"
DEFAULT_RUNS = QKD_ROOT / "runs"
DEFAULT_CONTRACTS = QKD_ROOT / "contracts"
DEFAULT_CAMPAIGNS = QKD_ROOT / "campaigns"
DEFAULT_KNOWLEDGE = QKD_ROOT / "runs" / "knowledge.db"


def build_runner(
    profile: str,
    gate: HardwareApprovalGate,
    recorder: RuntimeEventRecorder,
    workspace_root: str = "",
):
    """The Codex runner, wrapped so what it does lands in the job's log."""
    from QKD_PROJ.orchestrator.codex_runner import CodexAgentRunner, CodexRunnerConfig

    runner = CodexAgentRunner(
        config=CodexRunnerConfig(profile=profile),
        event_sink=recorder.sink,
        hardware_approval=gate,
        workspace_resolver=(
            PreparedJobWorkspaceResolver(Path(workspace_root)) if workspace_root else None
        ),
    )
    return recorder.wrap(runner), runner


def refusing_runner(*_: object, **__: object):
    """A runner for --dry-run: wires everything, starts no turn.

    It raises rather than returning a plausible envelope, so a dry run blocks
    its job with `runtime_failure` and leaves an honest record instead of a
    fabricated plan.
    """

    def refuse(agent, task):
        raise RuntimeError(
            f"--dry-run: {agent.name} was not started. Everything else is wired."
        )

    return refuse, None


def build_service(args: argparse.Namespace) -> tuple[S.Service, object]:
    ledger = JobLedger(Path(args.ledger))
    recorder = RuntimeEventRecorder(ledger)
    capability_catalog = CapabilityCatalog.load_default(QKD_ROOT.parent)
    board_catalog = BoardCatalog.load_default(QKD_ROOT.parent)
    campaign_name = str(getattr(args, "campaign", "") or "")
    campaign_sha256 = str(getattr(args, "campaign_sha256", "") or "")
    if bool(campaign_name) != bool(campaign_sha256):
        raise CampaignPolicyError(
            "--campaign and --campaign-sha256 must be supplied together"
        )
    if campaign_name and args.profile != "hardware":
        raise CampaignPolicyError("an automatic hardware campaign requires --profile hardware")
    auto_approver = None
    campaign = None
    active_campaign_sha256 = None
    active_campaign_contracts: dict[str, str] = {}
    if campaign_name:
        if Path(campaign_name).name != campaign_name:
            raise CampaignPolicyError(
                "--campaign must be one plain filename in QKD_PROJ/campaigns"
            )
        campaign = CampaignPolicy.load(
            DEFAULT_CAMPAIGNS / campaign_name,
            campaign_sha256,
            contracts_dir=Path(args.contracts),
            project_root=QKD_ROOT.parent,
        )
        auto_approver = CampaignAutoApprover(ledger, campaign, runs_dir=Path(args.runs))
        active_campaign_sha256 = campaign.sha256
        active_campaign_contracts = {
            permission.contract: permission.contract_sha256
            for permission in campaign.permissions
        }
    gate = HardwareApprovalGate(
        ledger,
        contracts_dir=Path(args.contracts),
        project_root=QKD_ROOT.parent,
        on_refusal=lambda message: print(f"[approval refused] {message}", file=sys.stderr),
        active_campaign_sha256=active_campaign_sha256,
        active_campaign_contracts=active_campaign_contracts,
    )
    if args.dry_run:
        runner, raw = refusing_runner()
    else:
        runner, raw = build_runner(
            args.profile,
            gate,
            recorder,
            str(getattr(args, "workspace_root", "") or ""),
        )

    knowledge_store = None
    knowledge_scope = None
    knowledge_ingestor = None
    knowledge_name = str(getattr(args, "knowledge_db", "") or "")
    if knowledge_name:
        knowledge_store = KnowledgeStore(Path(knowledge_name))
        scope_data = {
            key: value
            for key, value in {
                "project_id": str(getattr(args, "knowledge_project", "") or ""),
                "hardware_id": str(getattr(args, "knowledge_hardware", "") or ""),
                "revision": str(getattr(args, "knowledge_revision", "") or ""),
            }.items()
            if value
        }
        knowledge_scope = KnowledgeScope.parse(scope_data)
        knowledge_ingestor = SourcePacketKnowledgeIngestor(
            KnowledgeReducer(
                FilesystemEvidenceResolver(
                    QKD_ROOT,
                    references_root=QKD_ROOT / "references",
                )
            ),
            knowledge_store,
        )

    loop = Loop(
        ledger,
        runner,
        runs_dir=Path(args.runs),
        policy=LoopPolicy(max_iterations=args.max_iterations),
        classify_failure=classify_failure,
        auto_approver=auto_approver,
    )
    discovery = DiscoveryLoop(
        ledger,
        runner,
        knowledge_ingestor=knowledge_ingestor,
        knowledge_scope=knowledge_scope,
        classify_failure=classify_failure,
    )
    continuation = MissionContinuation(ledger, Path(args.contracts))
    workflows = WorkflowRouter(ledger, loop, discovery, continuation)
    campaign_controller = CampaignController(
        ledger,
        loop,
        gate,
        campaigns_dir=DEFAULT_CAMPAIGNS,
        contracts_dir=Path(args.contracts),
        project_root=QKD_ROOT.parent,
        runs_dir=Path(args.runs),
        hardware_enabled=args.profile == "hardware",
        router=workflows,
        initial_policy=campaign,
    )
    continuation.campaign_controller = campaign_controller
    daemon = Daemon(
        ledger,
        workflows,
        policy=SchedulerPolicy(base_backoff_s=args.backoff, max_rate_limit_retries=args.retries),
    )
    service = S.Service(
        ledger,
        daemon,
        config=S.ServiceConfig(
            interval_s=args.interval,
            pipe_name=args.pipe,
            runs_dir=Path(args.runs),
            project_root=QKD_ROOT.parent,
            contracts_dir=Path(args.contracts),
            runtime_profile=args.profile,
            workspace_mode=("isolated" if getattr(args, "workspace_root", "") else "shared"),
        ),
        knowledge_store=knowledge_store,
        knowledge_scope=knowledge_scope,
        campaign_controller=campaign_controller,
        capability_catalog=capability_catalog,
        board_catalog=board_catalog,
    )
    return service, raw


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
    parser = argparse.ArgumentParser(prog="qkd-daemon", description=__doc__)
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--runs", default=str(DEFAULT_RUNS))
    parser.add_argument("--contracts", default=str(DEFAULT_CONTRACTS))
    parser.add_argument(
        "--knowledge-db",
        default=str(DEFAULT_KNOWLEDGE),
        help="append-only knowledge events and rebuildable search projection",
    )
    parser.add_argument("--knowledge-project", default="qkd-proj")
    parser.add_argument("--knowledge-hardware", default="rtase-v1")
    parser.add_argument("--knowledge-revision", default="v1.0")
    parser.add_argument("--pipe", default=None, help=f"default {S.DEFAULT_PIPE}")
    parser.add_argument("--profile", choices=("offline", "hardware"), default="offline")
    parser.add_argument(
        "--campaign",
        default="",
        help="plain YAML filename in QKD_PROJ/campaigns; requires its SHA-256",
    )
    parser.add_argument(
        "--campaign-sha256",
        default="",
        help="exact digest reviewed by the person activating --campaign",
    )
    parser.add_argument("--interval", type=float, default=S.DEFAULT_INTERVAL_S)
    parser.add_argument("--backoff", type=float, default=60.0)
    parser.add_argument("--retries", type=int, default=6)
    parser.add_argument("--max-iterations", type=int, default=8)
    parser.add_argument(
        "--workspace-root",
        default="",
        help=(
            "directory of pre-created <job_id> Git worktrees. Omit to use the shared "
            "checkout; a missing or mismatched workspace blocks the job"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="wire everything, start no agent")
    parser.add_argument("--ticks", type=int, default=None, help="stop after this many ticks")

    sub = parser.add_subparsers(dest="command")
    job = sub.add_parser("start-job", help="open a job and let the daemon pick it up")
    job.add_argument("gate")
    job.add_argument("objective")
    job.add_argument("--scenario", default="")

    discover = sub.add_parser(
        "discover",
        help="research a scenario for which preflight finds no reviewed contract",
    )
    discover.add_argument("objective")
    discover.add_argument("--scenario", default="")

    sub.add_parser("status", help="ask a running daemon what it is doing")

    decide = sub.add_parser("approve", help="record a decision on a pending approval")
    decide.add_argument("approval_id")
    decide.add_argument("--by", required=True)
    decide.add_argument("--deny", action="store_true")
    decide.add_argument("--note", default="")

    contract_preview = sub.add_parser(
        "contract-preview",
        help="compile a reviewed discovery draft without installing it",
    )
    contract_preview.add_argument("job_id")

    contract_request = sub.add_parser(
        "contract-request",
        help="request human approval for the exact compiled contract bytes",
    )
    contract_request.add_argument("job_id")
    contract_request.add_argument("--by", required=True)

    contract_apply = sub.add_parser(
        "contract-apply",
        help="install a contract covered by an already-granted approval",
    )
    contract_apply.add_argument("approval_id")

    steer = sub.add_parser("steer", help="leave a message for the next agent")
    steer.add_argument("job_id")
    steer.add_argument("message")
    steer.add_argument("--by", required=True)

    stop = sub.add_parser("cancel", help="stop a job for good")
    stop.add_argument("job_id")
    stop.add_argument("--by", required=True)
    stop.add_argument("--reason", default="")

    args = parser.parse_args(argv)

    if args.command == "start-job":
        scenario = args.scenario or args.objective
        try:
            preview_answer = S.send(
                {"command": "preflight", "scenario": scenario},
                args.pipe or S.DEFAULT_PIPE,
            )
        except OSError as exc:
            print(f"no daemon on {args.pipe or S.DEFAULT_PIPE}: {exc}", file=sys.stderr)
            return 2
        preview = preview_answer.get("preflight") if preview_answer.get("ok") else None
        selected = preview.get("selected_contract") if isinstance(preview, dict) else None
        if (
            not isinstance(preview, dict)
            or preview.get("status") != "ready"
            or not isinstance(selected, dict)
        ):
            print(json.dumps(preview_answer, ensure_ascii=False, indent=2))
            return 1
        selected_name = selected.get("name")
        if selected_name != args.gate:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": (
                            f"requested gate {args.gate!r} does not match preflight "
                            f"selection {selected_name!r}"
                        ),
                        "preflight": preview,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        return _send(
            args,
            {
                "command": "start",
                "gate": selected_name,
                "objective": args.objective,
                "scenario": scenario,
                "contract_sha256": selected.get("sha256"),
                "scenario_sha256": preview.get("scenario_sha256"),
            },
        )
    if args.command == "discover":
        scenario = args.scenario or args.objective
        try:
            preview_answer = S.send(
                {"command": "preflight", "scenario": scenario},
                args.pipe or S.DEFAULT_PIPE,
            )
        except OSError as exc:
            print(f"no daemon on {args.pipe or S.DEFAULT_PIPE}: {exc}", file=sys.stderr)
            return 2
        preview = preview_answer.get("preflight") if preview_answer.get("ok") else None
        if not isinstance(preview, dict) or preview.get("status") != "contract_gap":
            print(json.dumps(preview_answer, ensure_ascii=False, indent=2))
            return 1
        return _send(
            args,
            {
                "command": "discover",
                "objective": args.objective,
                "scenario": scenario,
                "scenario_sha256": preview.get("scenario_sha256"),
            },
        )
    if args.command == "status":
        return _send(args, {"command": "status"})
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
    if args.command == "contract-preview":
        return _send(
            args,
            {"command": "contract-promotion-preview", "job_id": args.job_id},
        )
    if args.command == "contract-request":
        return _send(
            args,
            {
                "command": "contract-promotion-request",
                "job_id": args.job_id,
                "requested_by": args.by,
            },
        )
    if args.command == "contract-apply":
        return _send(
            args,
            {
                "command": "contract-promotion-apply",
                "approval_id": args.approval_id,
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
        CampaignPolicyError,
        KnowledgeValidationError,
        KnowledgeStoreError,
        BoardProfileError,
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
        f"  campaign  {args.campaign or '(manual approvals only)'}\n"
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
