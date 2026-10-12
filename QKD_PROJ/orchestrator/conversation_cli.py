"""Run an offline scenario through the shared Lead/Builder/Verifier loop.

Creates empty isolated workspaces for standalone development. The web service
uses the same core with existing project snapshots. Status and feedback do not
invoke a model; run continues until completion, a question, cancellation or failure.
"""
import argparse
import json
import uuid
from dataclasses import asdict
from pathlib import Path

from .conversation_codex import ConversationCodexConfig, ConversationCodexRuntime, ledger_event_sink
from .conversation_flow import ConversationFlow
from .jobs import JobLedger
from .service import InstanceLock


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "run", "advance", "status", "answer", "feedback", "cancel"))
    parser.add_argument("--ledger", type=Path, required=True, help="Dedicated standalone ledger, not web-jobs.db")
    parser.add_argument("--workspaces", type=Path, required=True)
    parser.add_argument("--objective")
    parser.add_argument("--job")
    parser.add_argument("--message", help="Answer or feedback in ordinary language")
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--model")
    parser.add_argument("--effort")
    args = parser.parse_args(argv)
    creating = args.action == "create" or (args.action == "run" and not args.job)
    if args.ledger.name == "web-jobs.db":
        parser.error("use the web controls for web-jobs.db; standalone CLI owns a separate ledger")
    if creating and (not args.objective or not args.objective.strip() or args.max_turns < 3):
        parser.error("new work requires --objective and --max-turns >= 3")
    if args.action == "create" and args.job:
        parser.error("create does not accept --job")
    if not creating and not args.job:
        parser.error("--job is required")
    if not creating and args.objective is not None:
        parser.error("use feedback to change an existing request; --objective creates new work")
    if args.action in {"answer", "feedback"}:
        if not args.message or not args.message.strip():
            parser.error("--message is required")
    elif args.message is not None:
        parser.error("--message is only used by answer or feedback")
    args.workspaces.mkdir(parents=True, exist_ok=True)
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(args.ledger.with_suffix(".lock"))
    lock.acquire()
    job_id = None
    flow = None
    try:
        ledger = JobLedger(args.ledger)
        runtime = None
        if args.action in {"run", "advance"}:
            runtime = ConversationCodexRuntime(ConversationCodexConfig(args.workspaces,
                model=args.model, effort=args.effort), event_sink=ledger_event_sink(ledger))
        flow = ConversationFlow(ledger, runtime)
        job_id = args.job
        if creating:
            workspace = args.workspaces / ("work_" + uuid.uuid4().hex[:12])
            workspace.mkdir()
            job = flow.create(args.objective, workspace, max_turns=args.max_turns)
            job_id = job.job_id
            print(json.dumps({"job_id": job_id, "workspace": str(workspace.resolve()),
                              "mode": "offline", "model_calls": 0}), flush=True)
            if args.action == "create":
                return 0
        if args.action == "run":
            state = flow.run(job_id, on_progress=_print_state)
        else:
            if args.action == "answer":
                flow.answer(job_id, args.message)
            elif args.action == "feedback":
                flow.feedback(job_id, args.message)
            elif args.action == "cancel":
                flow.cancel(job_id)
            state = flow.advance(job_id) if args.action == "advance" else flow.phase(job_id)
            _print_state(state)
        return 0 if state.action not in {"blocked", "interrupted", "needs_user"} else 2
    except KeyboardInterrupt:
        if flow is not None and job_id is not None:
            flow.cancel(job_id)
            _print_state(flow.phase(job_id))
        return 130
    finally:
        lock.release()


def _print_state(state):
    print(json.dumps(asdict(state), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
