from __future__ import annotations

import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from ai_hardware_autolab.orchestrator.workspace import (
    MARKER,
    PreparedJobWorkspaceResolver,
    WorkspaceManager,
    WorkspaceError,
)


JOB = "job_123456789abc"


def test_resolver_accepts_only_a_marker_bound_git_workspace(tmp_path):
    workspace = tmp_path / JOB
    (workspace / "ai_hardware_autolab").mkdir(parents=True)
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
    (workspace / "ai_hardware_autolab").mkdir(parents=True)
    (workspace / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    (workspace / MARKER).write_text(
        json.dumps({"version": 1, "job_id": "job_aaaaaaaaaaaa", "base_commit": "b" * 40}),
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="does not bind"):
        PreparedJobWorkspaceResolver(tmp_path)({"job_id": JOB})


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _source_repo(tmp_path):
    root = tmp_path / "source"
    (root / "ai_hardware_autolab" / "contracts").mkdir(parents=True)
    (root / "ai_hardware_autolab" / "base.py").write_text("value = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text("ai_hardware_autolab/workspaces/\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=test", "-c", "user.email=test@local", "commit", "-m", "base")
    return root


def test_manager_snapshots_dirty_and_untracked_source_without_touching_branch(tmp_path):
    source = _source_repo(tmp_path)
    original_head = _git(source, "rev-parse", "HEAD")
    (source / "ai_hardware_autolab" / "base.py").write_text("value = 2\n", encoding="utf-8")
    (source / "ai_hardware_autolab" / "new.py").write_text("new = True\n", encoding="utf-8")

    manager = WorkspaceManager(source / "ai_hardware_autolab" / "workspaces", source)
    workspace = manager({"job_id": JOB, "iteration": 1})

    assert (workspace / "ai_hardware_autolab" / "base.py").read_text(encoding="utf-8") == "value = 2\n"
    assert (workspace / "ai_hardware_autolab" / "new.py").is_file()
    marker = json.loads((workspace / MARKER).read_text(encoding="utf-8"))
    assert marker["source_commit"] == original_head
    assert marker["base_commit"] != original_head
    assert _git(source, "rev-parse", "HEAD") == original_head
    manager.audit({"job_id": JOB, "iteration": 1}, "lead-agent")


def test_manager_keeps_a_gitlink_as_a_gitlink_instead_of_copying_its_directory(tmp_path):
    source = _source_repo(tmp_path)
    linked = source / "STM32Cube" / "linked-project"
    linked.mkdir(parents=True)
    (linked / "source.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    _git(linked, "init")
    _git(linked, "add", ".")
    _git(
        linked,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@local",
        "commit",
        "-m",
        "linked",
    )
    linked_commit = _git(linked, "rev-parse", "HEAD")
    _git(
        source,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{linked_commit},STM32Cube/linked-project",
    )
    _git(
        source,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@local",
        "commit",
        "-m",
        "record gitlink",
    )

    manager = WorkspaceManager(source / "ai_hardware_autolab" / "workspaces", source)
    task = {"job_id": JOB, "iteration": 1}
    workspace = manager(task)

    assert "160000" in _git(workspace, "ls-files", "--stage", "STM32Cube/linked-project")
    manager.audit(task, "lead-agent")


def test_new_workspace_excludes_archived_docs_and_caches_but_keeps_recipe_source(tmp_path):
    source = _source_repo(tmp_path)
    names = ["docs/history/old.md", "tmp/old.log", "webui/cloudflare/.wrangler/cache.json",
             "gate12_pru_dac1/source.v", "README.md", ".rgignore"]
    for name in names:
        path = source / "ai_hardware_autolab" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    # Exercise exclusion of both tracked files and untracked source copies.
    _git(source, "add", "ai_hardware_autolab/docs/history/old.md")
    _git(source, "-c", "user.name=test", "-c", "user.email=test@local", "commit", "-m", "history")
    manager = WorkspaceManager(source / "ai_hardware_autolab/workspaces", source)
    workspace = manager({"job_id": JOB})
    for name in names[:3]:
        assert not (workspace / "ai_hardware_autolab" / name).exists()
        assert (source / "ai_hardware_autolab" / name).is_file()
    for name in names[3:]:
        assert (workspace / "ai_hardware_autolab" / name).is_file()
    manager.audit({"job_id": JOB}, "lead-agent")


def test_manager_records_builder_changes_inside_plan_scope(tmp_path):
    source = _source_repo(tmp_path)
    manager = WorkspaceManager(source / "ai_hardware_autolab" / "workspaces", source)
    task = {
        "job_id": JOB,
        "iteration": 1,
        "step": {"id": "implement"},
        "resources": {"write_scope": ["ai_hardware_autolab/generated"]},
    }
    workspace = manager(task)
    generated = workspace / "ai_hardware_autolab" / "generated" / "result.py"
    generated.parent.mkdir(parents=True)
    generated.write_text("answer = 42\n", encoding="utf-8")

    report = manager.audit(task, "builder")

    assert report["outside_write_scope"] == []
    assert report["changes"][0]["path"] == "ai_hardware_autolab/generated/result.py"
    assert report["changes"][0]["after"] == hashlib.sha256(generated.read_bytes()).hexdigest()
    assert report["outputs"] == [{
        "path": "ai_hardware_autolab/generated/result.py",
        "sha256": hashlib.sha256(generated.read_bytes()).hexdigest(),
        "size_bytes": generated.stat().st_size,
    }]

    published = manager.publish(task, report)

    assert published["published"][0]["path"] == "ai_hardware_autolab/generated/result.py"
    assert (source / "ai_hardware_autolab" / "generated" / "result.py").read_text(
        encoding="utf-8"
    ) == "answer = 42\n"


def test_manager_refuses_to_publish_over_a_concurrent_main_checkout_edit(tmp_path):
    source = _source_repo(tmp_path)
    manager = WorkspaceManager(source / "ai_hardware_autolab" / "workspaces", source)
    task = {
        "job_id": JOB,
        "iteration": 1,
        "step": {"id": "implement"},
        "resources": {"write_scope": ["ai_hardware_autolab/base.py"]},
    }
    workspace = manager(task)
    (workspace / "ai_hardware_autolab" / "base.py").write_text("value = 2\n", encoding="utf-8")
    report = manager.audit(task, "builder")
    (source / "ai_hardware_autolab" / "base.py").write_text("user = 99\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="concurrent|after the Builder turn began"):
        manager.publish(task, report)

    assert (source / "ai_hardware_autolab" / "base.py").read_text(encoding="utf-8") == "user = 99\n"


def test_manager_blocks_changes_outside_builder_scope_and_clears_pending_audit(tmp_path):
    source = _source_repo(tmp_path)
    manager = WorkspaceManager(source / "ai_hardware_autolab" / "workspaces", source)
    task = {
        "job_id": JOB,
        "iteration": 1,
        "resources": {"write_scope": ["ai_hardware_autolab/generated"]},
    }
    workspace = manager(task)
    (workspace / "ai_hardware_autolab" / "base.py").write_text("escaped = True\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="outside its reviewed write_scope"):
        manager.audit(task, "builder")

    marker = json.loads((workspace / MARKER).read_text(encoding="utf-8"))
    assert marker["pending_audit"] is None
    assert marker["last_audit"]["outside_write_scope"] == ["ai_hardware_autolab/base.py"]


def test_manager_materializes_hashed_capability_and_compiled_contract(tmp_path):
    source = _source_repo(tmp_path)
    asset = source / "ai_hardware_autolab" / "asset.py"
    asset.write_text("verified = True\n", encoding="utf-8")
    expected_asset = hashlib.sha256(asset.read_bytes()).hexdigest()
    capability = SimpleNamespace(
        capability_id="verified-asset",
        digest="f" * 64,
        assets=({"path": "ai_hardware_autolab/asset.py", "sha256": expected_asset},),
    )
    catalog = SimpleNamespace(capabilities=(capability,))
    manager = WorkspaceManager(
        source / "ai_hardware_autolab" / "workspaces", source, capability_catalog=catalog
    )

    lead = {"job_id": JOB, "iteration": 1}
    workspace = manager(lead)
    manager.audit(lead, "lead-agent")

    contract = source / "ai_hardware_autolab" / "contracts" / "generated.yaml"
    contract.write_text("schema_version: 1\n", encoding="utf-8")
    contract_sha = hashlib.sha256(contract.read_bytes()).hexdigest()
    builder = {
        "job_id": JOB,
        "iteration": 1,
        "resources": {
            "selected_capabilities": [{"id": "verified-asset"}],
            "write_scope": [],
        },
        "plan": {
            "compiled_contract": {"name": contract.name, "sha256": contract_sha}
        },
    }
    manager(builder)

    assert hashlib.sha256((workspace / "ai_hardware_autolab" / "asset.py").read_bytes()).hexdigest() == expected_asset
    assert hashlib.sha256((workspace / "ai_hardware_autolab" / "contracts" / contract.name).read_bytes()).hexdigest() == contract_sha
    manager.audit(builder, "builder")


def test_snapshot_applies_staged_source_deletions(tmp_path):
    source = _source_repo(tmp_path)
    removed = source / 'obsolete.txt'
    removed.write_text('old source', encoding='utf-8')
    _git(source, 'add', 'obsolete.txt')
    _git(source, '-c', 'user.name=test', '-c', 'user.email=test@local', '-c', 'maintenance.auto=false', 'commit', '-m', 'old source')
    _git(source, 'rm', 'obsolete.txt')
    manager = WorkspaceManager(source / 'ai_hardware_autolab/workspaces', source)
    workspace = manager.prepare(JOB)
    assert not (workspace / 'obsolete.txt').exists()
    assert (workspace / 'ai_hardware_autolab/base.py').is_file()
