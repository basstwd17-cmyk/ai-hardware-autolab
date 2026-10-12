"""Per-job device delegation from the user's submission, not from a model Plan.

Plans may change implementation and contracts without another approval click.
They cannot change the submission's configured device identities. The existing
drivers still enforce electrical bounds, effects and immutable run evidence.
No credentials are stored here and no device is contacted during evaluation.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import yaml

from ai_hardware_autolab.hwtools.safety import Contracts, SafetyGate
from ai_hardware_autolab.hwtools.result import sha256_file
from ai_hardware_autolab.hwtools.ssh import bbb_endpoint
from . import jobs as J
from .campaign import AutoApprovalEvaluation, CampaignAutoApprover, CampaignPolicy, _permission
from .campaign_proposal import CampaignProposalService, NATIVE_OPERATIONS
from .pagination import iter_events


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _mention_state(text, aliases):
    """Conservative target recognition, not a task planner or routing table.

    An explicit exclusion/comparison in a target's clause wins over a positive
    occurrence. Ambiguous language asks for clarification rather than granting.
    """
    positive = False
    for clause in re.split(r"[\n;!?。]|(?<=말고)|(?<=하고)|(?<=않고)|(?<=하지마)|\bbut\b|(?<=[^0-9])\.(?![0-9])", text.lower()):
        if not any(re.search(r"(?<![a-z0-9])" + re.escape(a.lower()) + r"(?![a-z0-9])", clause)
                   for a in aliases if a):
            continue
        if re.search(r"금지|제외|접근하지|사용하지|건드리지|말고|아니라|비교|참고만|\b(no|not|without|except|compare|don't|do not)\b", clause):
            return -1
        positive = True
    return int(positive)


def submission_access(scenario, *, project_root, contracts_dir, board_catalog=None, now=J.utc_now):
    """Called only by the trusted control plane on a new hardware submission."""
    root = Path(project_root).resolve()
    maps = {Path(contracts_dir).resolve() / "board-map.yaml": []}
    for profile in getattr(board_catalog, "profiles", ()):
        maps.setdefault((root / profile.board_map).resolve(), []).extend(
            [profile.board_id, *profile.aliases])
    devices = []
    for path, aliases in maps.items():
        if not path.is_relative_to(root) or not path.is_file():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            continue
        board = data.get("board") or {}
        board_aliases = [*aliases, str(board.get("name") or board.get("id") or "")]
        parent_selected = _mention_state(scenario, board_aliases) == 1
        ref = path.relative_to(root).as_posix()
        kinds = {
            "scope": ["scope", "oscilloscope", "오실로스코프", "스코프", str((data.get("scope") or {}).get("model") or ""), str((data.get("scope") or {}).get("host") or "")],
            "bbb": ["BBB", "BeagleBone Black", "비글본"],
            "cpld": ["CPLD", str((data.get("cpld") or {}).get("device") or "")],
            "board": board_aliases,
        }
        for kind, names in kinds.items():
            if kind != "board" and not data.get(kind):
                continue
            # Naming a board delegates its integrated controllers, not the
            # separate scope or an unrelated power supply on the same bench.
            mention = _mention_state(scenario, names)
            selected = mention == 1 or (mention == 0 and parent_selected and kind in {"bbb", "cpld"})
            if not selected:
                continue
            identity_names = names[4:] if kind == "scope" else (names[1:] if kind == "cpld" else [])
            row = {"kind": kind, "device_map": ref, "map_sha256": sha256_file(path),
                   "explicit_identity": (kind == "board" or (parent_selected and kind in {"bbb", "cpld"})
                                         or _mention_state(scenario, identity_names) == 1)}
            if kind == "bbb":
                row["target"] = bbb_endpoint(data)
            if kind == "scope":
                row["target"] = {key: data[kind].get(key) for key in ("model", "host", "port")}
                if (any(not row["target"][key] or str(row["target"][key]).upper() == "UNVERIFIED" for key in ("model", "host"))
                        or type(row["target"]["port"]) is not int):
                    continue
                # Submission delegates the instrument, not a measurement recipe.
                # Channel references describe wiring/work; changing the acquisition
                # channel does not require new consent for the same instrument.
                row["channels"] = [1, 2, 3, 4]
            devices.append(row)
    # A generic name identifying several configured instruments is not enough
    # to choose an endpoint. Never guess the first map in directory order.
    for kind in {row["kind"] for row in devices}:
        matched = [row for row in devices if row["kind"] == kind]
        identities = {json.dumps(row.get("target", row["device_map"]), sort_keys=True) for row in matched}
        if len(identities) > 1:
            if kind == "scope":
                # The same model at two addresses remains ambiguous even when
                # that model is named; an address distinguishes the two.
                for row in matched:
                    duplicate_model = sum(d["target"]["model"] == row["target"]["model"] for d in matched) > 1
                    if duplicate_model:
                        row["explicit_identity"] = _mention_state(scenario, [str(row["target"]["host"])]) == 1
            devices = [row for row in devices if row["kind"] != kind or row["explicit_identity"]]
    for device in devices:
        device.pop("explicit_identity")
    body = {"version": 1, "source": "user_scenario", "scenario_sha256": hashlib.sha256(scenario.strip().encode()).hexdigest(),
            "expires_at": (J._moment(now()) + timedelta(hours=12)).isoformat(),
            "max_executions": 256, "devices": devices}
    return {**body, "sha256": _digest(body)}


class ScenarioAutoApprover:
    def __init__(self, ledger, *, project_root, contracts_dir, runs_dir, enabled, now=J.utc_now):
        self.ledger, self.root = ledger, Path(project_root).resolve()
        self.contracts_dir, self.runs_dir = Path(contracts_dir).resolve(), Path(runs_dir).resolve()
        self.enabled, self.now = enabled, now
        self.proposer = CampaignProposalService(ledger, None, project_root=self.root,
            contracts_dir=self.contracts_dir, campaigns_dir=self.root / "ai_hardware_autolab/campaigns", now=now)

    def source(self, job_id):
        briefs = self.ledger.handoffs(job_id, "Brief", 0)
        if len(briefs) != 1:
            return None
        brief = briefs[0].payload
        resources = brief.get("resources") or {}
        scenario = str(brief.get("scenario") or "")
        # The conversation workflow keeps user inputs together and never
        # manufactures a legacy Plan/resources packet. Accept the same signed
        # delegation from that envelope without widening it.
        if brief.get("workflow") == "conversation-prototype-v1":
            from .conversation_flow import conversation_brief
            brief = conversation_brief(self.ledger, job_id)
            inputs = brief.get("inputs") or {}
            resources = {
                "execution_mode": brief.get("execution_mode"),
                "scenario_access": inputs.get("scenario_access"),
            }
            scenario = str(inputs.get("authorization_scenario") or inputs.get("scenario") or "")
        access = resources.get("scenario_access")
        if resources.get("execution_mode") != "hardware" or not isinstance(access, dict):
            return None
        body = {k: v for k, v in access.items() if k != "sha256"}
        if (access.get("version") != 1 or access.get("source") != "user_scenario"
                or access.get("sha256") != _digest(body)
                or access.get("scenario_sha256") != hashlib.sha256(scenario.strip().encode()).hexdigest()):
            return None
        return access

    def applies(self, job_id):
        return self.source(job_id) is not None

    def _policy(self, job, step, agent):
        access = self.source(job.job_id)
        if not self.enabled or access is None:
            raise ValueError("this job has no hardware scenario delegation")
        if any(e.kind in {"campaign.activated", "campaign.deactivated"} for e in iter_events(self.ledger, job.job_id)):
            raise ValueError("an explicit campaign decision superseded this scenario delegation")
        name, expected = step.get("contract"), step.get("contract_sha256")
        data, digest = self.proposer._load_boundary(name, expected)
        bound = Contracts.load(name, self.contracts_dir, project_root=self.root)
        ref = bound.board_map_path.resolve().relative_to(self.root).as_posix()
        tool, blockers = step.get("tool"), []
        kind = {"measure_scope": "scope", "measure_board_scope": "scope", "run_bbb": "bbb",
                "deploy_pru": "bbb", "scan_jtag": "cpld", "program_cpld_sram": "cpld"}.get(tool, "board")
        allowed = [d for d in access["devices"] if d["kind"] == kind and d["device_map"] == ref
                   and d["map_sha256"] == sha256_file(bound.board_map_path)]
        if len(allowed) != 1:
            raise ValueError("device not identified in the submitted scenario, or its connection map changed")
        selected = allowed[0]
        maximum = access["max_executions"]
        if tool == "measure_scope":
            envelope = SafetyGate(bound, allow_hardware=True).instrument_scope()
            if any(device != selected["target"] for device in envelope["devices"]):
                raise ValueError("scope endpoint differs from the submitted scenario")
            # Preserve historical narrower grants; never rewrite signed Briefs.
            # New submissions explicitly include all four instrument channels.
            if selected["channels"] and any(c["channel"] not in selected["channels"] for c in envelope["scope_channels"]):
                raise ValueError("scope channel exceeds the user's explicit restriction")
            raw = self.proposer._instrument_permission(name, digest, step, maximum)
        elif tool in NATIVE_OPERATIONS:
            raw = self.proposer._native_permission(name, digest, step, blockers, maximum)
            if kind == "bbb":
                target = {key: raw["device"].get(key) for key in ("host", "user", "port")}
                if target != selected["target"] or any(str(v).upper() == "UNVERIFIED" for v in target.values()):
                    raise ValueError("BBB endpoint differs from the submitted scenario or is unverified")
        else:
            self.proposer._hardware_safety(data, blockers)
            profile, _ = self.proposer._profile(data, blockers)
            if profile is not None and (self.root / profile.board_map).resolve() != bound.board_map_path.resolve():
                blockers.append("profile execution map differs from the authorized device map")
            plans = self.ledger.handoffs(job.job_id, "Plan", job.iteration)
            steps = plans[-1].payload.get("steps", []) if plans else [step]
            raw = self.proposer._permission(name, digest, step, steps, profile, blockers, maximum)
        if blockers or raw is None:
            raise ValueError("; ".join(blockers) or "unsupported device operation")
        raw = self.proposer._policy_permission(raw)
        permission = _permission(raw, self.contracts_dir, self.root, 2)
        policy_digest = _digest({"job_id": job.job_id, "submission": access["sha256"]})
        policy = CampaignPolicy(2, "scenario-" + job.job_id, "user_scenario", access["expires_at"],
            access["scenario_sha256"], maximum, 64, (permission,), policy_digest, self.contracts_dir, self.root)
        return CampaignAutoApprover(self.ledger, policy, now=self.now, runs_dir=self.runs_dir)

    def evaluate(self, job, step, agent, *, check_budget=True):
        try:
            approver = self._policy(job, step, agent)
            result = approver.evaluate(job, step, agent, check_budget=check_budget)
            return replace(result, reason="user's submitted scenario delegates this device" if result.allowed else result.reason)
        except (OSError, ValueError, RuntimeError) as exc:
            return AutoApprovalEvaluation(False, str(exc), campaign_id="scenario-" + job.job_id,
                campaign_sha256=(self.source(job.job_id) or {}).get("sha256", ""))

    def apply(self, approval, evaluation):
        # Same durable grant-count vocabulary; actor makes the source distinct
        # from a human button click or an independently signed Campaign.
        actor = "scenario:" + approval.job_id
        self.ledger.append_event(approval.job_id, "campaign.approval.evaluated", {
            "campaign_id": evaluation.campaign_id, "campaign_sha256": evaluation.campaign_sha256,
            "permission_id": evaluation.permission_id, "approval_id": approval.approval_id,
            "allowed": evaluation.allowed, "reason": evaluation.reason, "source": "user_scenario",
        }, actor)
        if not evaluation.allowed:
            return approval
        return self.ledger.decide_approval(approval.approval_id, True, actor, note=evaluation.reason,
            bundle_sha256=evaluation.campaign_sha256, now=self.now())

    def validate_grant(self, approval, job, step, agent):
        evaluated = self.evaluate(job, step, agent, check_budget=False)
        return (evaluated.allowed and approval.decided_by == "scenario:" + job.job_id
                and approval.bundle_sha256 == evaluated.campaign_sha256
                and approval.artifacts == evaluated.artifacts)
