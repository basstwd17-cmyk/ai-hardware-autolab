"""Opening an artifact from the result card.

The registration happened when the daemon reduced the job; the click happens
whenever a person gets round to it. Everything below is a way the world can
have moved in between, and what this host does about it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.orchestrator import wire as W  # noqa: E402
from QKD_PROJ.webui.backend import artifacts as A  # noqa: E402
from QKD_PROJ.webui.backend.state import Projection  # noqa: E402

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from QKD_PROJ.webui.backend.app import DEFAULT_PORT, create_app  # noqa: E402
from QKD_PROJ.webui.backend.security import Session  # noqa: E402

BASE = f"http://127.0.0.1:{DEFAULT_PORT}"


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "project"
    (root / "runs" / "20260101T000000Z_build").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "docs" / "report.md").write_text("# report\n", encoding="utf-8")
    return root


def register(projection: Projection, root: Path, job: str, artifact: str, target: Path) -> None:
    projection.apply(
        {
            "type": W.ARTIFACT_REGISTERED,
            "jobId": job,
            "artifactId": artifact,
            "projectRoot": str(root),
            "targetPath": str(target),
        }
    )


# -- resolving ----------------------------------------------------------------


def test_a_registered_pair_resolves_to_what_it_named(project):
    projection = Projection()
    register(projection, project, "j1", "report", project / "docs" / "report.md")

    target = A.resolve(projection, "j1", "report")
    assert target.path == (project / "docs" / "report.md").resolve()
    assert target.is_directory is False
    assert target.describe() == {
        "ok": True,
        "jobId": "j1",
        "artifactId": "report",
        "kind": "file",
    }


def test_a_run_directory_resolves_as_a_directory(project):
    projection = Projection()
    run = project / "runs" / "20260101T000000Z_build"
    register(projection, project, "j1", "run:20260101T000000Z_build", run)

    target = A.resolve(projection, "j1", "run:20260101T000000Z_build")
    assert target.is_directory is True
    assert target.describe()["kind"] == "directory"


def test_an_id_belonging_to_another_job_is_not_a_near_miss(project):
    projection = Projection()
    register(projection, project, "j1", "report", project / "docs" / "report.md")

    with pytest.raises(A.ArtifactError) as refused:
        A.resolve(projection, "j2", "report")
    assert refused.value.status == 404
    assert "different artifact" in refused.value.detail


def test_an_unregistered_id_is_refused(project):
    projection = Projection()
    with pytest.raises(A.ArtifactError) as refused:
        A.resolve(projection, "j1", "nothing")
    assert refused.value.status == 404


def test_a_contested_id_is_refused_rather_than_resolved(project):
    """A second registration against a different path makes the id ambiguous."""
    projection = Projection()
    register(projection, project, "j1", "report", project / "docs" / "report.md")
    other = project / "docs" / "other.md"
    other.write_text("# other\n", encoding="utf-8")
    register(projection, project, "j1", "report", other)  # refused by the projection

    with pytest.raises(A.ArtifactError) as refused:
        A.resolve(projection, "j1", "report")
    assert refused.value.status == 409
    assert "ambiguous" in refused.value.detail


def test_a_path_outside_the_registered_root_is_refused(project, tmp_path):
    outside = tmp_path / "elsewhere.md"
    outside.write_text("not ours\n", encoding="utf-8")
    projection = Projection()
    register(projection, project, "j1", "stray", outside)

    with pytest.raises(A.ArtifactError) as refused:
        A.resolve(projection, "j1", "stray")
    assert refused.value.status == 409
    assert "project root" in refused.value.detail


def test_a_registration_without_a_root_has_nothing_to_check_against(project):
    projection = Projection()
    projection.apply(
        {
            "type": W.ARTIFACT_REGISTERED,
            "jobId": "j1",
            "artifactId": "report",
            "targetPath": str(project / "docs" / "report.md"),
        }
    )
    with pytest.raises(A.ArtifactError) as refused:
        A.resolve(projection, "j1", "report")
    assert refused.value.status == 409


def test_a_file_deleted_since_registration_is_gone_not_broken(project):
    projection = Projection()
    document = project / "docs" / "report.md"
    register(projection, project, "j1", "report", document)
    document.unlink()

    with pytest.raises(A.ArtifactError) as refused:
        A.resolve(projection, "j1", "report")
    assert refused.value.status == 410
    assert "not on disk any more" in refused.value.detail


# -- revealing ----------------------------------------------------------------


def test_the_default_reveal_shows_the_file_and_runs_nothing(project, monkeypatch):
    """The check that matters: `document` comes from an agent-written handoff.

    `os.startfile` on an agent-chosen path would execute whatever extension it
    happened to have. Revealing shows the same thing and runs nothing.
    """
    seen: list[list[str]] = []
    monkeypatch.setattr(A.sys, "platform", "win32")
    monkeypatch.setattr(A.subprocess, "run", lambda command, **_: seen.append(command))

    document = project / "docs" / "report.md"
    A.reveal(A.Openable("j1", "report", document, is_directory=False))
    A.reveal(A.Openable("j1", "run:x", project / "runs", is_directory=True))

    assert seen[0] == ["explorer", f"/select,{document}"]
    assert seen[1] == ["explorer", str(project / "runs")]
    assert all(part == "explorer" or "explorer" not in part for part in seen[0][1:])


def test_revealing_says_so_rather_than_pretending_on_another_platform(project, monkeypatch):
    monkeypatch.setattr(A.sys, "platform", "linux")
    with pytest.raises(A.ArtifactError) as refused:
        A.reveal(A.Openable("j1", "report", project / "docs" / "report.md", is_directory=False))
    assert refused.value.status == 501
    assert "nothing was opened" in refused.value.detail


# -- over HTTP ----------------------------------------------------------------


def _app(project: Path):
    session = Session(file=None)
    revealed: list[A.Openable] = []
    app = create_app(
        address="tcp://127.0.0.1:1",
        session=session,
        port=DEFAULT_PORT,
        layout_file=project / "layout.json",
        reveal=revealed.append,
    )
    return app, session, revealed


def _client(app, session):
    return TestClient(app, base_url=BASE, headers={"Authorization": f"Bearer {session.token}"})


def test_a_click_reveals_and_answers_without_a_path(project):
    app, session, revealed = _app(project)
    with _client(app, session) as http:
        register(app.state.hub.projection, project, "j1", "report", project / "docs" / "report.md")
        answer = http.post("/api/jobs/j1/artifacts/report/open")

    assert answer.status_code == 200
    body = answer.json()
    assert body == {"ok": True, "jobId": "j1", "artifactId": "report", "kind": "file"}
    # The rule the whole design rests on: no host path crosses the wire.
    assert "report.md" not in answer.text
    assert str(project) not in answer.text
    assert [t.artifact_id for t in revealed] == ["report"]


def test_a_refused_click_reveals_nothing(project):
    app, session, revealed = _app(project)
    with _client(app, session) as http:
        register(app.state.hub.projection, project, "j1", "report", project / "docs" / "report.md")

        assert http.post("/api/jobs/j2/artifacts/report/open").status_code == 404
        assert http.post("/api/jobs/j1/artifacts/absent/open").status_code == 404

        (project / "docs" / "report.md").unlink()
        gone = http.post("/api/jobs/j1/artifacts/report/open")
        assert gone.status_code == 410
        assert "record of it stands" in gone.json()["detail"]

    assert revealed == [], "nothing was shown for any of the refusals"


def test_opening_needs_the_token_like_everything_else(project):
    app, _, revealed = _app(project)
    with TestClient(app, base_url=BASE) as http:
        assert http.post("/api/jobs/j1/artifacts/report/open").status_code == 401
    assert revealed == []
