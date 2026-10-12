"""Deterministic, read-only intake for a person's scenario.

Preflight answers a narrower question than the planning agent: which reviewed
contract is the scenario most likely asking for, and does the wording clearly
say whether the board may be touched?  It never creates a job and never asks a
model.  Weak or ambiguous matches are a contract gap, not permission to guess.

The selected contract travels with its SHA-256.  ``job.start`` repeats this
inspection and checks the selected name, ready status, contract digest and
scenario digest together. A client therefore cannot substitute a different
contract merely by supplying that other file's valid digest.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

from ai_hardware_autolab.orchestrator.mission_contract import MissionContract

from ai_hardware_autolab.agents.schema import AgentDefinition, load_all

CONTRACTS_DIR = Path(__file__).resolve().parents[1] / "contracts"

READY = "ready"
CONTRACT_GAP = "contract_gap"
OFFLINE = "offline"
HARDWARE = "hardware"
UNDETERMINED = "undetermined"

OFFLINE_OPERATIONS = {"diamond_build"}
MEASUREMENT_MARKERS = (
    "measure",
    "measurement",
    "scope",
    "oscilloscope",
    "capture",
    "diagnostic",
    "readback",
    "측정",
    "오실로스코프",
    "스코프",
    "캡처",
    "진단",
    "읽기",
)
BOARD_CHANGE_MARKERS = (
    "jtag",
    "sram",
    "programming",
    "program cpld",
    "cpld program",
    "bitstream load",
    "deploy",
    "reboot",
    "load bitstream",
    "on board",
    "보드에서",
    "프로그래밍",
    "cpld 프로그램",
    "sram 프로그램",
    "배포",
    "재부팅",
    "로드",
    "연결해서",
)
OFFLINE_MARKERS = (
    "offline",
    "software only",
    "build only",
    "without hardware",
    "no hardware",
    "develop",
    "implement",
    "design",
    "compile",
    "source code",
    "review",
    "summarize",
    "하드웨어 없이",
    "보드 없이",
    "소프트웨어만",
    "빌드만",
    "개발",
    "구현",
    "설계",
    "컴파일",
    "코드",
    "분석",
    "검토",
    "요약",
    "문서",
)

ALIASES = {
    "게이트": ("gate",),
    "빌드": ("build", "diamond"),
    "컴파일": ("build", "diamond"),
    "개발": ("build", "design"),
    "구현": ("build", "design"),
    "설계": ("design",),
    "측정": ("measure", "measurement"),
    "오실로스코프": ("scope", "oscilloscope", "capture"),
    "스코프": ("scope", "capture"),
    "진단": ("diagnostic",),
    "프로그래밍": ("program", "sram"),
    "프로그램": ("program", "sram"),
    "배포": ("deploy",),
    "디지털": ("digital",),
    "경로": ("path",),
    "검증": ("verify", "verification"),
}

_TOKEN = re.compile(r"[a-z0-9가-힣]+", re.IGNORECASE)
_GATE = re.compile(r"(?:gate|게이트)\s*[-_ ]?(\d+)", re.IGNORECASE)

# Intake ambiguity margins, not evidence thresholds. They are deliberately
# named here and covered by the scenario corpus in test_scenario_preflight.py:
# weak and closely ranked prose must ask a person, while an exact filename must
# win. Changing one requires changing that corpus with a concrete scenario.
MIN_MATCH_SCORE = 4.0
MIN_SCORE_GAP = 1.5
MIN_SCORE_RATIO = 1.25
TECHNICAL_TERM_BONUS = 0.8
TECHNICAL_TERM_LENGTH = 7

# Workflow words say what the person wants done, not which reviewed contract
# authorises it.  A candidate supported only by these terms is more dangerous
# than no match: "build and verify an STM32 design" must not select an
# unrelated CPLD gate merely because both contracts mention building.
NON_IDENTIFYING_TERMS = frozenset(
    {
        "an",
        "and",
        "build",
        "compile",
        "code",
        "design",
        "diamond",
        "develop",
        "diagnostic",
        "implement",
        "firmware",
        "for",
        "from",
        "hardware",
        "measure",
        "measurement",
        "into",
        "of",
        "on",
        "only",
        "path",
        "program",
        "programming",
        "readback",
        "report",
        "review",
        "run",
        "simulation",
        "source",
        "test",
        "the",
        "to",
        "use",
        "using",
        "verify",
        "verification",
        "with",
        "without",
    }
)


class PreflightError(ValueError):
    """The preview cannot be produced or no longer matches its contract."""


@dataclass(frozen=True)
class Contract:
    name: str
    test_id: str
    status: str
    purpose: tuple[str, ...]
    allowed_operations: tuple[str, ...]
    sha256: str
    terms: frozenset[str]
    data: dict[str, Any]


@dataclass(frozen=True)
class Candidate:
    contract: Contract
    score: float
    matched_terms: tuple[str, ...]
    explicit: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.contract.name,
            "test_id": self.contract.test_id,
            "status": self.contract.status,
            "sha256": self.contract.sha256,
            "score": round(self.score, 3),
            "matched_terms": list(self.matched_terms),
            "explicit": self.explicit,
        }


class ScenarioPreflight:
    def __init__(
        self,
        contracts_dir: Path | None = None,
        agents: Sequence[AgentDefinition] | None = None,
    ) -> None:
        self.contracts_dir = Path(contracts_dir or CONTRACTS_DIR).resolve()
        self.agents = tuple(agents if agents is not None else load_all())

    def inspect(self, scenario: str) -> dict[str, Any]:
        scenario = scenario.strip()
        if not scenario:
            raise PreflightError("a scenario is required")

        contracts = self._contracts()
        if not contracts:
            raise PreflightError(f"no executable YAML contracts in {self.contracts_dir}")
        candidates, reason_code = self._rank(scenario, contracts)
        selected = self._select(candidates, reason_code)
        mode = self._execution_mode(scenario, selected.contract if selected else None)

        status = READY
        detail = "A unique reviewed contract and execution boundary were found."
        if selected is None:
            status = CONTRACT_GAP
            detail = (
                "No reviewed contract matches the scenario."
                if reason_code == "no_contract_match"
                else (
                    "Several reviewed contracts overlap. The mission will compare them and "
                    "derive one scenario-specific contract; the operator does not need to "
                    "choose a gate."
                )
            )
        elif mode == UNDETERMINED:
            status = CONTRACT_GAP
            reason_code = "execution_scope_unclear"
            detail = "Say whether this is offline work or may touch the board."

        roles = self._roles(scenario, selected.contract if selected else None, mode)
        skills = sorted(
            {
                skill
                for agent in self.agents
                if agent.name in roles
                for skill in agent.skills
            }
        )
        artifacts = self._artifacts(selected.contract if selected else None, roles)
        digest = hashlib.sha256(scenario.encode("utf-8")).hexdigest()
        objective = objective_for(scenario)
        mission = MissionContract.draft(scenario, objective, mode)

        return {
            "status": status,
            "reason_code": reason_code,
            "detail": detail,
            "scenario": scenario,
            "objective": objective,
            "mission_id": mission.mission_id,
            "mission_state": "draft" if selected is None else "baseline_found",
            "mission_contract": mission.describe(),
            "scenario_sha256": digest,
            "execution_mode": mode,
            "approval_required": mode == HARDWARE,
            "selected_contract": selected.describe() if selected else None,
            "candidates": [candidate.describe() for candidate in candidates[:3]],
            "roles": roles,
            "skills": skills,
            "expected_artifacts": artifacts,
        }

    def verify_contract(self, name: str, expected_sha256: str) -> None:
        if Path(name).name != name or Path(name).suffix.lower() not in (".yaml", ".yml"):
            raise PreflightError("a preflight contract must be one plain YAML filename")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise PreflightError("contract_sha256 must be a full SHA-256 digest")
        path = (self.contracts_dir / name).resolve()
        if path.parent != self.contracts_dir or not path.is_file():
            raise PreflightError(f"preflight contract does not exist: {name}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual.lower() != expected_sha256.lower():
            raise PreflightError(
                f"{name} changed after preflight: expected {expected_sha256}, found {actual}"
            )

    @staticmethod
    def verify_scenario(scenario: str, expected_sha256: str) -> None:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise PreflightError("scenario_sha256 must be a full SHA-256 digest")
        actual = hashlib.sha256(scenario.strip().encode("utf-8")).hexdigest()
        if actual.lower() != expected_sha256.lower():
            raise PreflightError(
                f"scenario changed after preflight: expected {expected_sha256}, found {actual}"
            )

    def _contracts(self) -> list[Contract]:
        contracts: list[Contract] = []
        for path in sorted(self.contracts_dir.glob("*.y*ml")):
            try:
                raw = path.read_bytes()
                data = yaml.safe_load(raw) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise PreflightError(f"cannot read contract {path.name}: {exc}") from exc
            if not isinstance(data, dict) or not data.get("test_id"):
                continue
            purpose_value = data.get("purpose") or []
            purpose = (
                tuple(str(value) for value in purpose_value)
                if isinstance(purpose_value, list)
                else (str(purpose_value),)
            )
            allowed_value = data.get("allowed_operations") or []
            allowed = (
                tuple(str(value) for value in allowed_value)
                if isinstance(allowed_value, list)
                else ()
            )
            searchable = " ".join(
                [path.name, str(data.get("test_id")), str(data.get("status", "")), *purpose, *allowed]
            )
            contracts.append(
                Contract(
                    name=path.name,
                    test_id=str(data["test_id"]),
                    status=str(data.get("status", "unknown")),
                    purpose=purpose,
                    allowed_operations=allowed,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    terms=frozenset(_terms(searchable)),
                    data=data,
                )
            )
        return contracts

    def _rank(self, scenario: str, contracts: Sequence[Contract]) -> tuple[list[Candidate], str]:
        normalized = _normalize(scenario)
        query = _expanded_terms(scenario)
        gate_numbers = set(_GATE.findall(normalized))

        by_name = [
            contract
            for contract in contracts
            if _normalize(contract.name) in normalized
            or _normalize(contract.test_id) in normalized
        ]
        if len(by_name) == 1:
            contract = by_name[0]
            return [
                Candidate(contract, 100.0, tuple(sorted(query & contract.terms)), True)
            ], "selected_explicitly"
        if len(by_name) > 1:
            return [
                Candidate(contract, 100.0, tuple(sorted(query & contract.terms)), True)
                for contract in by_name
            ], "ambiguous_contract"

        by_gate: list[Contract] = []
        for contract in contracts:
            stem = Path(contract.name).stem.lower()
            contract_gate = re.match(r"gate(\d+)(?:-|$)", stem)
            if contract_gate and contract_gate.group(1) in gate_numbers:
                by_gate.append(contract)

        if len(by_gate) == 1:
            contract = by_gate[0]
            return [Candidate(contract, 100.0, tuple(sorted(query & contract.terms)), True)], "selected_explicitly"

        frequencies: dict[str, int] = {}
        for contract in contracts:
            for term in contract.terms:
                frequencies[term] = frequencies.get(term, 0) + 1
        ranked: list[Candidate] = []
        total = len(contracts)
        for contract in contracts:
            matched = query & contract.terms
            # Explicit filenames and gate numbers were handled above.  Fuzzy
            # matching needs at least one component, interface, signal or
            # other domain anchor; generic workflow overlap alone is not a
            # contract identity.
            anchors = matched - NON_IDENTIFYING_TERMS
            if not anchors:
                continue
            score = sum(1.0 + math.log(total / frequencies[term]) for term in matched)
            # Long technical identifiers carry more information than generic
            # prose such as "verify" or "path".
            score += sum(
                TECHNICAL_TERM_BONUS
                for term in matched
                if any(ch.isdigit() for ch in term) or len(term) >= TECHNICAL_TERM_LENGTH
            )
            if score > 0:
                ranked.append(Candidate(contract, score, tuple(sorted(matched))))
        ranked.sort(key=lambda item: (-item.score, item.contract.name))
        if not ranked or ranked[0].score < MIN_MATCH_SCORE:
            return ranked, "no_contract_match"
        if len(ranked) > 1 and (
            ranked[0].score - ranked[1].score < MIN_SCORE_GAP
            or ranked[0].score < ranked[1].score * MIN_SCORE_RATIO
        ):
            return ranked, "ambiguous_contract"
        return ranked, "selected_unique_match"

    @staticmethod
    def _select(candidates: Sequence[Candidate], reason_code: str) -> Candidate | None:
        return candidates[0] if candidates and reason_code.startswith("selected_") else None

    @staticmethod
    def _execution_mode(scenario: str, contract: Contract | None) -> str:
        normalized = _normalize(scenario)
        explicit_no_hardware = any(
            marker in normalized
            for marker in ("without hardware", "no hardware", "하드웨어 없이", "보드 없이")
        )
        offline = any(marker in normalized for marker in OFFLINE_MARKERS)
        hardware = any(marker in normalized for marker in MEASUREMENT_MARKERS + BOARD_CHANGE_MARKERS)
        if explicit_no_hardware or (offline and not hardware):
            return OFFLINE
        if hardware:
            return HARDWARE
        if contract and contract.allowed_operations and set(contract.allowed_operations) <= OFFLINE_OPERATIONS:
            return OFFLINE
        return UNDETERMINED

    @staticmethod
    def _roles(scenario: str, contract: Contract | None, mode: str) -> list[str]:
        roles = ["gate-architect"]
        normalized = _normalize(scenario)
        allowed = set(contract.allowed_operations if contract else ())
        if mode == OFFLINE and ("diamond_build" in allowed or any(m in normalized for m in OFFLINE_MARKERS)):
            roles.append("design-builder")
        if mode == HARDWARE:
            measurement = any(marker in normalized for marker in MEASUREMENT_MARKERS)
            board_change = any(marker in normalized for marker in BOARD_CHANGE_MARKERS)
            if measurement:
                roles.append("measurement")
            if board_change or not measurement:
                roles.append("board-operator")
        roles.extend(["verifier", "reporter"])
        return roles

    @staticmethod
    def _artifacts(contract: Contract | None, roles: Iterable[str]) -> list[str]:
        role_set = set(roles)
        artifacts = ["run_record", "verdict", "report"]
        if contract and "test_design" in contract.data and "design-builder" in role_set:
            artifacts.append("bitstream")
        if "measurement" in role_set:
            artifacts.append("measurement_data")
            if contract and "scope_capture" in contract.data:
                artifacts.append("scope_capture")
        if "board-operator" in role_set:
            artifacts.append("board_state")
        return artifacts


def objective_for(scenario: str, max_length: int = 160) -> str:
    compact = " ".join(scenario.split())
    if not compact:
        raise PreflightError("a scenario is required")
    if max_length < 1:
        raise PreflightError("objective length must be positive")
    return compact if len(compact) <= max_length else compact[: max_length - 1].rstrip() + "…"


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).lower().replace("_", " ").replace("-", " ")


def _terms(value: str) -> set[str]:
    return {token for token in _TOKEN.findall(_normalize(value)) if len(token) >= 2}


def _expanded_terms(value: str) -> set[str]:
    terms = _terms(value)
    expanded = set(terms)
    for term in terms:
        expanded.update(ALIASES.get(term, ()))
    return expanded
