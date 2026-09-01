from __future__ import annotations

import json

import pytest

from QKD_PROJ.orchestrator.workspace import (
    MARKER,
    PreparedJobWorkspaceResolver,
    WorkspaceError,
)


JOB = "job_123456789abc"


def test_resolver_accepts_only_a_marker_bound_git_workspace(tmp_path):
    workspace = tmp_path / JOB
    (workspace / "QKD_PROJ").mkdir(parents=True)
    (workspace / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    (workspace / MARKER).write_text(
        json.dumps({"version": 1, "job_id": JOB, "base_commit": "a" * 40}),
        encoding="utf-8",
    )

    assert PreparedJobWorkspaceResolver(tmp_path)({"job_id": JOB}) == workspace.resolve()


@pytest.mark.parametrize("job_id", ["../escape", "job_short", "JOB_123456789ABC", ""])
def test_resolver_rejects_noncanonical_job_ids(tmp_path, job_id):
    with pytest.raises(WorkspaceError, match="canonical job_id"):
        PreparedJobWorkspaceResolver(tmp_path)({"job_id": job_id})


def test_resolver_refuses_a_workspace_for_a_different_job(tmp_path):
    workspace = tmp_path / JOB
    (workspace / "QKD_PROJ").mkdir(parents=True)
    (workspace / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    (workspace / MARKER).write_text(
        json.dumps({"version": 1, "job_id": "job_aaaaaaaaaaaa", "base_commit": "b" * 40}),
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="does not bind"):
        PreparedJobWorkspaceResolver(tmp_path)({"job_id": JOB})
