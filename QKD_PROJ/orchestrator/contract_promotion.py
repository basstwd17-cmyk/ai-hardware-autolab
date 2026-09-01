"""Promote one reviewed discovery draft into an executable contract.

Discovery deliberately stops at ``ContractDraft`` + ``ContractReview``.  An
LLM-authored draft is not authority and it is not a file under ``contracts/``.
This module is the narrow boundary between those two worlds:

* it replays and validates the complete discovery handoff chain;
* it compiles a deterministic YAML document (the model supplies no YAML);
* a durable approval names the draft, review and exact compiled-byte digests;
* installation is create-only and atomic, then recorded in the job event log.

The first compiler profile is intentionally small: offline ``build_cpld``
contracts with an explicit pin expectation.  Hardware contract generation is
not approximated from prose; unsupported steps fail closed until a typed
compiler exists for them.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from QKD_PROJ.agents.schema import load_all
from QKD_PROJ.boards.profiles import BoardProfile, BoardProfileError
from QKD_PROJ.orchestrator import jobs as J
from QKD_PROJ.orchestrator.jobs import JobLedger, canonical_payload
from QKD_PROJ.research.architecture import (
    ArchitectureDecision,
    ContractDraft,
    ContractReview,
    DraftStep,
)
from QKD_PROJ.research.discovery_loop import (
    DISCOVERY_GATE,
    MISSION_GATE,
    MISSION_WORKFLOW,
    WORKFLOW,
)
from QKD_PROJ.research.mission import ChangeAnalysis, MissionPlan
from QKD_PROJ.research.models import ResearchPlan, SourcePacket, scenario_sha256


PROMOTION_STEP = "contract-promotion"
PROMOTED_EVENT = "contract.promoted"
AUTO_PROMOTION_ACTOR = "policy:reviewed-offline-contract"
REVIEWED_COMPILER_ACTOR = "policy:reviewed-contract-compiler"
COMPILER_VERSION = 1
_PLAIN_CONTRACT = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\.yaml")

# A generated offline contract starts with the smallest authority needed for
# its only supported action.  Keeping the complement explicit makes a later
# hardware extension a reviewed compiler change rather than an omission.
_OFFLINE_FORBIDDEN = (
    "board_upload",
    "bbb_ssh_command",
    "drive_D1CLK",
    "drive_D2CLK",
    "drive_any_D1B_bus",
    "drive_any_D2B_bus",
    "enable_analog_output",
    "jtag_scan",
    "nonvolatile_flash",
    "oscilloscope_capture",
    "pru_deploy",
    "readback_or_program_status",
    "sram_fast_program",
)
_PROFILE_OPERATIONS = {
    "run_board_build": "firmware_build",
    "run_board_stage:upload": "board_upload",
    "run_board_stage:measure": "oscilloscope_capture",
    "measure_board_scope:measure": "oscilloscope_capture",
}


class PromotionError(RuntimeError):
    """A proposal is absent, ambiguous, changed, or not authorised."""


@dataclass(frozen=True)
class PromotionProposal:
    job_id: str
    filename: str
    contract_sha256: str
    draft_sha256: str
    review_sha256: str
    scenario_sha256: str
    contract: dict[str, Any]
    content: bytes = field(repr=False)
    mission_sha256: str | None = None
    change_sha256: str | None = None

    @property
    def artifacts(self) -> dict[str, str]:
        result = {
            "compiled_contract": self.contract_sha256,
            "contract_draft": self.draft_sha256,
            "contract_review": self.review_sha256,
            "scenario": self.scenario_sha256,
        }
        if self.mission_sha256:
            result["mission_plan"] = self.mission_sha256
        if self.change_sha256:
            result["change_analysis"] = self.change_sha256
        return result

    def describe(self) -> dict[str, Any]:
        result = {
            "job_id": self.job_id,
            "filename": self.filename,
            "contract_sha256": self.contract_sha256,
            "draft_sha256": self.draft_sha256,
            "review_sha256": self.review_sha256,
            "scenario_sha256": self.scenario_sha256,
            "contract": self.contract,
        }
        if self.mission_sha256:
            result["mission_sha256"] = self.mission_sha256
        if self.change_sha256:
            result["change_sha256"] = self.change_sha256
        return result


class ContractPromotionService:
    """Compile, request approval for, and install reviewed contracts."""

    def __init__(self, ledger: JobLedger, contracts_dir: Path) -> None:
        self.ledger = ledger
        self.contracts_dir = Path(contracts_dir).resolve()
        self.project_root = (
            self.contracts_dir.parents[1]
            if self.contracts_dir.name == "contracts"
            and self.contracts_dir.parent.name == "QKD_PROJ"
            else self.contracts_dir.parent
        )

    def preview(self, job_id: str) -> PromotionProposal:
        """Return the exact candidate bytes and digests without writing."""

        job = self.ledger.job(job_id)
        if job.gate not in (DISCOVERY_GATE, MISSION_GATE):
            raise PromotionError(f"job {job_id} is not a discovery or mission job")
        if job.state != J.DONE or job.outcome != "contract_gap":
            raise PromotionError(
                f"job {job_id} must be done as contract_gap before promotion; "
                f"it is {job.state}/{job.outcome}"
            )

        brief_row = self._one(job_id, "Brief")
        packet_row = self._one(job_id, "SourcePacket")
        decision_row = self._one(job_id, "ArchitectureDecision")
        draft_row = self._one(job_id, "ContractDraft")
        review_row = self._one(job_id, "ContractReview")

        brief = brief_row.payload
        resources = brief.get("resources") if isinstance(brief, dict) else None
        workflow = resources.get("workflow") if isinstance(resources, dict) else None
        if workflow not in (WORKFLOW, MISSION_WORKFLOW):
            raise PromotionError("Brief does not name a promotable workflow")
        scenario = brief.get("scenario")
        if not isinstance(scenario, str) or not scenario.strip():
            raise PromotionError("discovery Brief has no scenario")
        digest = scenario_sha256(scenario)
        if resources.get("scenario_sha256") != digest:
            raise PromotionError("discovery Brief scenario digest does not match its text")

        try:
            mission_row = None
            change_row = None
            mission = None
            if workflow == MISSION_WORKFLOW:
                mission_row = self._one(job_id, "MissionPlan")
                mission = MissionPlan.parse(mission_row.payload)
                plan = mission.as_research_plan()
            else:
                plan = ResearchPlan.parse(self._one(job_id, "ResearchPlan").payload)
            packet = SourcePacket.parse(packet_row.payload, plan=plan)
            if mission is not None:
                changes = self.ledger.handoffs(job_id, "ChangeAnalysis", 0)
                # A genuinely new design has no baseline to compare.  Older
                # mission rows always contain ChangeAnalysis, so accept and
                # validate it when present, but do not invent one merely to
                # satisfy a fixed workflow shape.
                if changes:
                    if len(changes) != 1:
                        raise PromotionError(
                            f"job {job_id} needs at most one iteration-0 ChangeAnalysis; "
                            f"found {len(changes)}"
                        )
                    change_row = changes[0]
                    ChangeAnalysis.parse(change_row.payload, mission=mission, packet=packet)
                elif mission.baseline_contract is not None:
                    raise PromotionError(
                        "a mission with a baseline contract needs one ChangeAnalysis"
                    )
            decision = ArchitectureDecision.parse(
                decision_row.payload, plan=plan, packet=packet
            )
            available_agents = (
                set(mission.execution_agents)
                if mission is not None
                else {item.name for item in load_all()}
            )
            available_tools = {
                item
                for item in resources.get("available_tools", [])
                if isinstance(item, str)
            }
            draft = ContractDraft.parse(
                draft_row.payload,
                decision=decision,
                packet=packet,
                available_agents=available_agents,
                available_tools=available_tools,
                available_skills=(set(mission.execution_skills) if mission is not None else None),
            )
            review = ContractReview.parse(review_row.payload, draft=draft)
        except ValueError as exc:
            raise PromotionError(f"discovery handoff chain is invalid: {exc}") from exc

        if decision.status != "selected":
            raise PromotionError(
                f"architecture is {decision.status}; only a selected architecture can be promoted"
            )
        if review.outcome != "ready_for_human_review":
            raise PromotionError(
                f"contract review is {review.outcome}; it is not ready for human approval"
            )

        filename = f"{draft.contract_name}.yaml"
        if _PLAIN_CONTRACT.fullmatch(filename) is None:
            raise PromotionError(f"unsafe generated contract filename: {filename!r}")
        contract = self._compile(
            job_id,
            draft,
            draft_row.sha256,
            review_row.sha256,
            resources,
        )
        content = yaml.safe_dump(
            contract,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=100,
        ).encode("utf-8")
        return PromotionProposal(
            job_id=job_id,
            filename=filename,
            contract_sha256=hashlib.sha256(content).hexdigest(),
            draft_sha256=draft_row.sha256,
            review_sha256=review_row.sha256,
            scenario_sha256=digest,
            contract=contract,
            content=content,
            mission_sha256=mission_row.sha256 if mission_row is not None else None,
            change_sha256=change_row.sha256 if change_row is not None else None,
        )

    def request(
        self, job_id: str, *, requested_by: str = "contract-promoter"
    ) -> tuple[PromotionProposal, J.Approval]:
        """Create (or return) the one approval bound to these exact bytes."""

        if not requested_by.strip():
            raise PromotionError("a promotion request must name who requested it")
        proposal = self.preview(job_id)
        self._check_existing_target(proposal)
        for approval in self._job_approvals(job_id):
            if (
                approval.step_id == PROMOTION_STEP
                and approval.contract == proposal.filename
                and approval.artifacts == proposal.artifacts
            ):
                return proposal, approval
        try:
            approval = self.ledger.request_approval(
                job_id,
                proposal.filename,
                (
                    f"Install {proposal.filename} as a reviewed generated contract. "
                    "The signature covers the discovery draft, reviewer handoff, scenario, "
                    "and exact compiled YAML bytes."
                ),
                artifacts=proposal.artifacts,
                requested_by=requested_by.strip(),
                step_id=PROMOTION_STEP,
            )
        except J.JobLedgerError as exc:
            raise PromotionError(str(exc)) from exc
        return proposal, approval

    def apply(self, approval_id: str) -> PromotionProposal:
        """Install the approved bytes without overwriting any contract."""

        try:
            approval = self.ledger.approval(approval_id)
        except J.JobLedgerError as exc:
            raise PromotionError(str(exc)) from exc
        if approval.step_id != PROMOTION_STEP:
            raise PromotionError(f"approval {approval_id} is not a contract promotion")
        if approval.state != J.APPROVAL_GRANTED:
            raise PromotionError(
                f"approval {approval_id} is {approval.state}; only a human grant can install it"
            )

        proposal = self.preview(approval.job_id)
        if approval.contract != proposal.filename or approval.artifacts != proposal.artifacts:
            raise PromotionError(
                "the approved promotion no longer matches the deterministic proposal"
            )
        target = self._target(proposal.filename)
        promoted = self._promotion_events(proposal.job_id, approval_id)
        if promoted:
            if not target.is_file():
                raise PromotionError(
                    f"{proposal.filename} was recorded as promoted but is missing from disk"
                )
            self._require_digest(target, proposal.contract_sha256)
            return proposal

        if target.exists():
            if not target.is_file():
                raise PromotionError(f"promotion target is not a file: {target}")
            self._require_digest(target, proposal.contract_sha256)
        else:
            self.contracts_dir.mkdir(parents=True, exist_ok=True)
            self._install_create_only(target, proposal.content)

        self.ledger.append_event(
            proposal.job_id,
            PROMOTED_EVENT,
            {
                "approval_id": approval_id,
                "contract": proposal.filename,
                "contract_sha256": proposal.contract_sha256,
                "draft_sha256": proposal.draft_sha256,
                "review_sha256": proposal.review_sha256,
            },
            approval.decided_by,
        )
        return proposal

    def apply_reviewed_offline(self, job_id: str) -> PromotionProposal:
        """Install a reviewed offline proposal under deterministic policy.

        This is deliberately *not* a hardware approval shortcut.  ``preview``
        replays the complete sourced handoff chain, the compiler accepts only
        the offline tool subset, and installation remains create-only.  The
        submitted scenario authorises local source/build work; a later board
        or instrument step still needs an exact campaign or human grant.
        """

        proposal = self.preview(job_id)
        if proposal.contract.get("status") != "reviewed_generated_offline":
            raise PromotionError("automatic promotion is limited to reviewed offline contracts")
        return self._apply_reviewed(
            proposal,
            authority="reviewed_offline_policy",
            actor=AUTO_PROMOTION_ACTOR,
        )

    def apply_reviewed(self, job_id: str) -> PromotionProposal:
        """Install deterministic reviewed bytes without granting execution.

        Hardware contracts may be installed here because installing YAML does
        not touch hardware.  Their steps remain unreachable until a person
        activates a campaign covering the exact contract/profile/scenario
        digests.  This leaves one human decision -- the campaign -- rather
        than a contract-file click followed by a hardware click.
        """

        proposal = self.preview(job_id)
        if proposal.contract.get("status") not in (
            "reviewed_generated_offline",
            "reviewed_generated_hardware",
        ):
            raise PromotionError("reviewed compiler produced an unknown contract status")
        return self._apply_reviewed(
            proposal,
            authority="reviewed_compiler_policy",
            actor=REVIEWED_COMPILER_ACTOR,
        )

    def _apply_reviewed(
        self,
        proposal: PromotionProposal,
        *,
        authority: str,
        actor: str,
    ) -> PromotionProposal:
        self._check_existing_target(proposal)
        target = self._target(proposal.filename)
        promoted = [
            event
            for event in self._events(proposal.job_id)
            if event.kind == PROMOTED_EVENT
            and event.payload.get("contract_sha256") == proposal.contract_sha256
        ]
        if promoted:
            if not target.is_file():
                raise PromotionError(
                    f"{proposal.filename} was recorded as promoted but is missing from disk"
                )
            self._require_digest(target, proposal.contract_sha256)
            return proposal

        if not target.exists():
            self.contracts_dir.mkdir(parents=True, exist_ok=True)
            self._install_create_only(target, proposal.content)
        self.ledger.append_event(
            proposal.job_id,
            PROMOTED_EVENT,
            {
                "approval_id": None,
                "authority": authority,
                "contract": proposal.filename,
                "contract_sha256": proposal.contract_sha256,
                "draft_sha256": proposal.draft_sha256,
                "review_sha256": proposal.review_sha256,
            },
            actor,
        )
        return proposal

    def _compile(
        self,
        job_id: str,
        draft: ContractDraft,
        draft_sha256: str,
        review_sha256: str,
        resources: dict[str, Any],
    ) -> dict[str, Any]:
        board_profile = self._board_profile(resources)
        if all(step.tool == "build_cpld" for step in draft.steps):
            if draft.execution_mode != "offline":
                raise PromotionError("build_cpld steps cannot form a hardware contract")
            compiled_steps = [self._compile_offline_step(step) for step in draft.steps]
            allowed_operations = ["diamond_build"]
        elif board_profile is not None:
            compiled_steps = [
                self._compile_profile_step(step, board_profile) for step in draft.steps
            ]
            allowed_operations = sorted(
                {
                    self._profile_operation(step, board_profile)
                    for step in draft.steps
                }
            )
        else:
            raise PromotionError(
                "generated contract tools need either the typed CPLD compiler or a selected "
                "digest-bound board profile"
            )
        top = re.sub(r"[^a-z0-9_]", "_", draft.contract_name.lower())
        if not top or top[0].isdigit():
            top = f"generated_{top}"
        source_root = f"QKD_PROJ/generated/{top}"
        contract: dict[str, Any] = {
            "schema_version": 1,
            "test_id": draft.contract_name,
            "status": f"reviewed_generated_{draft.execution_mode}",
            "purpose": [draft.objective],
            "allowed_operations": allowed_operations,
            "forbidden_operations": [
                operation for operation in _OFFLINE_FORBIDDEN
                if operation not in allowed_operations
            ],
            "generated_from": {
                "compiler_version": COMPILER_VERSION,
                "job_id": job_id,
                "scenario_sha256": draft.scenario_sha256,
                "architecture_id": draft.architecture_id,
                "draft_sha256": draft_sha256,
                "review_sha256": review_sha256,
                "evidence_claim_ids": list(draft.evidence_claim_ids),
            },
            "orchestration": {
                "roles": list(draft.roles),
                "skills": list(draft.skills),
                "constraints": list(draft.constraints),
                "steps": compiled_steps,
            },
            "pass_criteria": {
                "statements": list(draft.pass_criteria),
                "conditions": [
                    {"step_id": step.step_id, **dict(condition)}
                    for step in draft.steps
                    for condition in step.advance_when
                ],
            },
            "stop_conditions": list(draft.constraints),
        }
        if board_profile is None:
            contract["test_design"] = {
                "top": top,
                "sources": {
                    "hdl": f"{source_root}/{top}.v",
                    "constraints": f"{source_root}/{top}.lpf",
                },
            }
        else:
            contract["board_profile"] = {
                "board_id": board_profile.board_id,
                "revision": board_profile.revision,
                "path": board_profile.describe()["profile"],
                "sha256": board_profile.sha256,
                "board_map": board_profile.board_map,
            }
        return contract

    def _board_profile(self, resources: dict[str, Any]) -> BoardProfile | None:
        development = resources.get("board_development")
        selected = development.get("selected") if isinstance(development, dict) else None
        if not isinstance(selected, dict):
            return None
        reference = selected.get("profile")
        expected = selected.get("profile_sha256")
        if not isinstance(reference, str) or not isinstance(expected, str):
            raise PromotionError("selected board profile lacks path or digest")
        path = (self.project_root / reference).resolve()
        try:
            profile = BoardProfile.load(path, project_root=self.project_root)
        except BoardProfileError as exc:
            raise PromotionError(str(exc)) from exc
        if profile.sha256 != expected:
            raise PromotionError(
                f"board profile changed after mission intake: expected {expected}, "
                f"found {profile.sha256}"
            )
        return profile

    @staticmethod
    def _profile_operation(step: DraftStep, profile: BoardProfile) -> str:
        stage = next(
            (item for item in profile.stages if item.stage_id == step.tool_args.get("stage_id")),
            None,
        )
        if stage is None:
            raise PromotionError(f"step {step.step_id} names an unknown board-profile stage")
        key = step.tool if step.tool == "run_board_build" else f"{step.tool}:{stage.phase}"
        try:
            return _PROFILE_OPERATIONS[key]
        except KeyError as exc:
            raise PromotionError(
                f"step {step.step_id} uses unsupported profiled tool/phase {key!r}"
            ) from exc

    @classmethod
    def _compile_profile_step(
        cls, step: DraftStep, profile: BoardProfile
    ) -> dict[str, Any]:
        if set(step.tool_args) != {"stage_id"}:
            raise PromotionError(
                f"profiled step {step.step_id} may supply only stage_id"
            )
        stage_id = step.tool_args.get("stage_id")
        if not isinstance(stage_id, str):
            raise PromotionError(f"profiled step {step.step_id}.stage_id must be text")
        stage = next((item for item in profile.stages if item.stage_id == stage_id), None)
        if stage is None or stage.status != "ready":
            raise PromotionError(
                f"profiled step {step.step_id} names a missing or adapter_required stage"
            )
        if stage.tool != step.tool or stage.agent != step.agent:
            raise PromotionError(
                f"profiled step {step.step_id} differs from the reviewed stage agent/tool"
            )
        hardware = stage.mode == "hardware"
        if step.touches_hardware != hardware:
            raise PromotionError(
                f"profiled step {step.step_id} hardware flag differs from the board profile"
            )
        cls._profile_operation(step, profile)
        return {
            "id": step.step_id,
            "agent": step.agent,
            "tool": step.tool,
            "tool_args": {"stage_id": stage_id},
            "intent": step.intent,
            "touches_hardware": hardware,
            "unattended": not hardware,
            "advance_when": [dict(item) for item in step.advance_when],
        }

    @staticmethod
    def _compile_offline_step(step: DraftStep) -> dict[str, Any]:
        if step.touches_hardware:
            raise PromotionError(f"offline step {step.step_id} claims hardware access")
        if step.tool != "build_cpld":
            raise PromotionError(
                f"offline promotion does not compile tool {step.tool!r}; only build_cpld is supported"
            )
        if set(step.tool_args) != {"expected_io"}:
            raise PromotionError(
                f"build step {step.step_id} must supply only an explicit expected_io pin map"
            )
        expected = step.tool_args["expected_io"]
        if not isinstance(expected, dict):
            raise PromotionError(f"build step {step.step_id} expected_io must be an object")
        required = {"ports", "io_type", "drive_ma", "slew"}
        if set(expected) != required:
            raise PromotionError(
                f"build step {step.step_id} expected_io must contain exactly {sorted(required)}"
            )
        raw_ports = expected["ports"]
        if not isinstance(raw_ports, list) or not raw_ports:
            raise PromotionError(f"build step {step.step_id} expected_io.ports cannot be empty")
        ports: dict[str, int] = {}
        pins: set[int] = set()
        for index, raw in enumerate(raw_ports):
            if not isinstance(raw, dict) or set(raw) != {"name", "pin"}:
                raise PromotionError(
                    f"build step {step.step_id} expected_io.ports[{index}] needs name and pin"
                )
            name = raw["name"]
            pin = raw["pin"]
            if not isinstance(name, str) or not name.strip() or not isinstance(pin, int) or isinstance(pin, bool) or pin < 1:
                raise PromotionError(
                    f"build step {step.step_id} has an invalid expected I/O name or pin"
                )
            name = name.strip().lower()
            if name in ports:
                raise PromotionError(f"build step {step.step_id} repeats port {name!r}")
            if pin in pins:
                raise PromotionError(f"build step {step.step_id} repeats CPLD pin {pin}")
            ports[name] = pin
            pins.add(pin)
        drive = expected["drive_ma"]
        if not isinstance(drive, int) or isinstance(drive, bool) or drive < 1:
            raise PromotionError(f"build step {step.step_id} drive_ma must be a positive integer")
        io_type = expected["io_type"]
        slew = expected["slew"]
        if not isinstance(io_type, str) or not io_type.strip() or not isinstance(slew, str) or not slew.strip():
            raise PromotionError(f"build step {step.step_id} io_type and slew must be text")
        return {
            "id": step.step_id,
            "agent": step.agent,
            "tool": step.tool,
            "tool_args": {
                "expected_io": {
                    "ports": ports,
                    "io_type": io_type.strip(),
                    "drive_ma": drive,
                    "slew": slew.strip(),
                }
            },
            "intent": step.intent,
            "touches_hardware": False,
            "unattended": True,
            "advance_when": [dict(item) for item in step.advance_when],
        }

    def _one(self, job_id: str, kind: str) -> J.Handoff:
        rows = self.ledger.handoffs(job_id, kind, 0)
        if len(rows) != 1:
            raise PromotionError(
                f"job {job_id} needs exactly one iteration-0 {kind}; found {len(rows)}"
            )
        row = rows[0]
        actual = hashlib.sha256(canonical_payload(row.payload)).hexdigest()
        if actual != row.sha256:
            raise PromotionError(f"stored {kind} payload no longer matches its ledger digest")
        return row

    def _target(self, filename: str) -> Path:
        target = (self.contracts_dir / filename).resolve()
        if target.parent != self.contracts_dir:
            raise PromotionError(f"contract path escapes contracts directory: {filename}")
        return target

    def _check_existing_target(self, proposal: PromotionProposal) -> None:
        target = self._target(proposal.filename)
        if not target.exists():
            return
        if not target.is_file():
            raise PromotionError(f"promotion target is not a file: {target}")
        self._require_digest(target, proposal.contract_sha256)

    @staticmethod
    def _require_digest(path: Path, expected: str) -> None:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise PromotionError(
                f"refusing to overwrite {path.name}: existing digest is {actual}, expected {expected}"
            )

    @staticmethod
    def _install_create_only(target: Path, content: bytes) -> None:
        """Publish fully flushed bytes atomically, without replacement.

        Linking a complete same-directory temporary file gives create-if-absent
        semantics on both NTFS and POSIX.  ``os.replace`` would be atomic but
        could overwrite a contract that appeared after the collision check.
        """

        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise PromotionError(
                    f"refusing to overwrite contract created concurrently: {target.name}"
                ) from exc
            except OSError as exc:
                raise PromotionError(f"could not atomically install {target.name}: {exc}") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _job_approvals(self, job_id: str) -> list[J.Approval]:
        seen: dict[str, J.Approval] = {}
        cursor = 0
        while True:
            events = self.ledger.events_since(cursor, job_id, 500)
            if not events:
                break
            for event in events:
                cursor = event.event_id
                approval_id = event.payload.get("approval_id")
                if event.kind.startswith("approval.") and isinstance(approval_id, str):
                    seen[approval_id] = self.ledger.approval(approval_id)
        return list(seen.values())

    def _events(self, job_id: str) -> list[J.Event]:
        result: list[J.Event] = []
        cursor = 0
        while True:
            events = self.ledger.events_since(cursor, job_id, 500)
            if not events:
                return result
            result.extend(events)
            cursor = events[-1].event_id

    def _promotion_events(self, job_id: str, approval_id: str) -> list[J.Event]:
        result: list[J.Event] = []
        cursor = 0
        while True:
            events = self.ledger.events_since(cursor, job_id, 500)
            if not events:
                return result
            for event in events:
                cursor = event.event_id
                if (
                    event.kind == PROMOTED_EVENT
                    and event.payload.get("approval_id") == approval_id
                ):
                    result.append(event)
