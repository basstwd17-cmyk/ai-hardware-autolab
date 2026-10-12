"""The pinned approval-2 bitstream candidate must not drift.

An approval names one file, not a design. Diamond stamps the build time into
the bitstream header, so a rebuild of identical sources yields a different
file that would still satisfy any design-level check. If the pinned artifact
could be replaced by such a rebuild without anything failing, the approval
would silently come to mean a file nobody approved.

runs/ is excluded by .gitignore, so the original build output is untracked and
a git clean deletes it. That is why the candidate is copied into a tracked
directory and checked here rather than referenced in place.

These tests read files and hash them. They do not build, program, or touch the
board.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PROJ = Path(__file__).resolve().parents[1]
SPEC = PROJ / "contracts" / "gate1-test-spec.yaml"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def candidate() -> dict:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    return spec["sram_program_candidate"]


@pytest.fixture(scope="module")
def candidate_dir(candidate: dict) -> Path:
    # The contract stores a repo-relative path; PROJ is ai_hardware_autolab itself.
    return PROJ.parent / candidate["directory"]


def test_candidate_approval_was_consumed(candidate: dict) -> None:
    """Every approved execution has been consumed and cannot be reused."""
    assert candidate["approved"] is False
    assert candidate["status"] == "executed"
    assert candidate["approval_max_executions"] == 2
    assert candidate["approval_executions_used"] == 2
    assert candidate["execution_result"] == "PASS"
    assert candidate["latest_execution_result"] == "PASS"


def test_bitstream_matches_the_pinned_raw_hash(
    candidate: dict, candidate_dir: Path
) -> None:
    """The whole point: this exact file, byte for byte."""
    bitstream = candidate_dir / candidate["bitstream"]
    assert bitstream.is_file(), f"Pinned bitstream is missing: {bitstream}"
    assert bitstream.stat().st_size == candidate["bitstream_bytes"]
    assert sha256(bitstream) == candidate["bitstream_sha256"]


def test_bitstream_design_hash_matches(candidate: dict, candidate_dir: Path) -> None:
    """Cross-check against the rebuild-stable hash.

    A mismatch here means the design changed, not just the timestamp -- a
    different failure from the raw-hash test and worth telling apart.
    """
    from ai_hardware_autolab.hwtools.cpld import design_sha256

    bitstream = candidate_dir / candidate["bitstream"]
    assert design_sha256(bitstream) == candidate["bitstream_design_sha256"]


def test_design_hash_agrees_with_build_result(candidate: dict) -> None:
    """The candidate must be one of the three builds the contract signed off."""
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    assert (
        candidate["bitstream_design_sha256"]
        == spec["build_result"]["bitstream_design_sha256"]
    )
    assert candidate["from_run"] in spec["build_result"]["runs"]


def test_sources_still_match_the_candidate(candidate: dict) -> None:
    """If the HDL or the LPF moved, the pinned bitstream no longer describes them.

    The LPF matters as much as the Verilog here: it is the file that places
    dbg0 on pin 117, which is Gate 1's entire isolation argument.
    """
    sources = PROJ / "gate1_cpld"
    for name, expected in candidate["source_sha256"].items():
        path = sources / name
        assert path.is_file(), f"Source named by the candidate is missing: {path}"
        assert sha256(path) == expected, (
            f"{name} has changed since the candidate was pinned. The candidate "
            f"is now stale -- pin a new one rather than editing this contract."
        )


def test_companion_artifacts_match(candidate: dict, candidate_dir: Path) -> None:
    """The pad report and build log are the evidence behind the bitstream."""
    for name, expected in candidate["companion_sha256"].items():
        path = candidate_dir / name
        assert path.is_file(), f"Companion artifact is missing: {path}"
        assert sha256(path) == expected


def test_approval_form_records_the_completed_execution(candidate: dict) -> None:
    form = PROJ.parent / candidate["approval_form"]
    assert form.is_file(), f"Approval form is missing: {form}"
    text = form.read_text(encoding="utf-8")
    assert "승인됨" in text
    assert candidate["execution_run"] in text
    assert candidate["bitstream_sha256"] in text


def test_source_hashes_agree_between_contract_sections(candidate: dict) -> None:
    """build_result and the candidate must not disagree about the sources."""
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    assert candidate["source_sha256"] == spec["build_result"]["source_sha256"]


def test_recorded_runs_exist_on_disk() -> None:
    """Catches the defect that was found when the candidate was pinned.

    Two of the three recorded run names pointed at directories that did not
    exist. Skipped on a fresh clone, where runs/ is absent by design.
    """
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    runs = PROJ / "runs"
    if not runs.is_dir():
        pytest.skip("runs/ is gitignored and absent in this checkout")
    for name in spec["build_result"]["runs"]:
        assert (runs / name).is_dir(), (
            f"build_result.runs names '{name}', which is not a directory under "
            f"{runs}. The evidence chain for the pinned bitstream is broken."
        )
