"""The discovery agents resume from ResearchPlan and SourcePacket handoffs."""

from __future__ import annotations

import sqlite3

import pytest

from QKD_PROJ.orchestrator.jobs import JobLedger, JobLedgerError
from QKD_PROJ.research.discovery import DiscoveryPipeline
from QKD_PROJ.research.models import scenario_sha256


SCENARIO = "Receive two bits on a BBB and drive a DAC at 5 ns intervals."


def plan():
    return {
        "kind": "ResearchPlan",
        "agent": "system-architect",
        "scenario_sha256": scenario_sha256(SCENARIO),
        "questions": [
            {
                "id": "dac-setup",
                "question": "What setup time does the DAC require?",
                "why": "It bounds the output schedule.",
                "required_source_types": ["vendor-datasheet"],
            }
        ],
        "completion_criteria": ["The DAC timing is page-cited or explicitly open."],
        "search_budget": 4,
    }


def packet():
    return {
        "kind": "SourcePacket",
        "agent": "researcher",
        "scenario_sha256": scenario_sha256(SCENARIO),
        "queries": [],
        "documents": [],
        "claims": [],
        "open_questions": ["dac-setup"],
    }


class Runner:
    def __init__(self, fail_research=False):
        self.calls = []
        self.fail_research = fail_research

    def __call__(self, agent, task):
        self.calls.append((agent.name, task))
        if agent.name == "system-architect":
            return plan()
        if self.fail_research:
            raise RuntimeError("runtime stopped")
        return packet()


def test_discovery_records_both_handoffs_and_second_call_spends_no_turns(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    job = ledger.create_job("discovery", SCENARIO)
    runner = Runner()
    pipeline = DiscoveryPipeline(ledger, runner)

    first = pipeline.run(job.job_id, SCENARIO)
    assert [name for name, _task in runner.calls] == ["system-architect", "researcher"]
    assert not first.resumed_plan and not first.resumed_sources
    assert [item.kind for item in ledger.handoffs(job.job_id)] == [
        "ResearchPlan",
        "SourcePacket",
    ]

    runner.calls.clear()
    resumed = pipeline.run(job.job_id, SCENARIO)
    assert runner.calls == []
    assert resumed.resumed_plan and resumed.resumed_sources


def test_crash_after_plan_resumes_at_researcher(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    job = ledger.create_job("discovery", SCENARIO)
    with pytest.raises(RuntimeError, match="runtime stopped"):
        DiscoveryPipeline(ledger, Runner(fail_research=True)).run(job.job_id, SCENARIO)
    assert len(ledger.handoffs(job.job_id, "ResearchPlan")) == 1

    runner = Runner()
    result = DiscoveryPipeline(ledger, runner).run(job.job_id, SCENARIO)
    assert [name for name, _task in runner.calls] == ["researcher"]
    assert result.resumed_plan and not result.resumed_sources


def test_v1_ledger_migrates_before_accepting_discovery_handoffs(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE job_schema_info (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO job_schema_info VALUES (1)")
    conn.execute(
        """
        CREATE TABLE handoffs (
          handoff_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, iteration INTEGER NOT NULL,
          kind TEXT NOT NULL, agent TEXT NOT NULL, step_id TEXT NOT NULL DEFAULT '',
          at TEXT NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL,
          failure_signature TEXT, event_id INTEGER,
          UNIQUE (job_id, iteration, kind, agent, step_id),
          CHECK (kind IN ('Brief','Plan','StepResult','Verdict','Report')),
          CHECK (iteration >= 0)
        )
        """
    )
    conn.commit()
    conn.close()

    ledger = JobLedger(path)
    job = ledger.create_job("discovery", SCENARIO)
    recorded = ledger.record_handoff(
        job.job_id, "ResearchPlan", "system-architect", plan(), iteration=0
    )
    assert recorded.kind == "ResearchPlan"
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT version FROM job_schema_info").fetchone()[0] == 4
    conn.close()


def test_v2_ledger_migrates_before_accepting_architecture_handoffs(tmp_path):
    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE job_schema_info (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO job_schema_info VALUES (2)")
    conn.execute(
        """
        CREATE TABLE handoffs (
          handoff_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, iteration INTEGER NOT NULL,
          kind TEXT NOT NULL, agent TEXT NOT NULL, step_id TEXT NOT NULL DEFAULT '',
          at TEXT NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL,
          failure_signature TEXT, event_id INTEGER,
          UNIQUE (job_id, iteration, kind, agent, step_id),
          CHECK (kind IN ('Brief','ResearchPlan','SourcePacket','Plan','StepResult','Verdict','Report')),
          CHECK (iteration >= 0)
        )
        """
    )
    conn.commit()
    conn.close()

    ledger = JobLedger(path)
    job = ledger.create_job("discovery", SCENARIO)
    recorded = ledger.record_handoff(
        job.job_id,
        "ArchitectureDecision",
        "architecture-selector",
        {"kind": "ArchitectureDecision"},
        iteration=0,
    )
    assert recorded.kind == "ArchitectureDecision"
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT version FROM job_schema_info").fetchone()[0] == 4
    conn.close()


def test_v3_ledger_migrates_before_accepting_mission_handoffs(tmp_path):
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE job_schema_info (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO job_schema_info VALUES (3)")
    conn.execute(
        """
        CREATE TABLE handoffs (
          handoff_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, iteration INTEGER NOT NULL,
          kind TEXT NOT NULL, agent TEXT NOT NULL, step_id TEXT NOT NULL DEFAULT '',
          at TEXT NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL,
          failure_signature TEXT, event_id INTEGER,
          UNIQUE (job_id, iteration, kind, agent, step_id),
          CHECK (kind IN ('Brief','ResearchPlan','SourcePacket','ArchitectureDecision',
            'ContractDraft','ContractReview','Plan','StepResult','Verdict','Report')),
          CHECK (iteration >= 0)
        )
        """
    )
    conn.commit()
    conn.close()

    ledger = JobLedger(path)
    job = ledger.create_job("mission", SCENARIO)
    recorded = ledger.record_handoff(
        job.job_id,
        "MissionPlan",
        "mission-architect",
        {"kind": "MissionPlan"},
        iteration=0,
    )
    assert recorded.kind == "MissionPlan"
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT version FROM job_schema_info").fetchone()[0] == 4
    conn.close()
