"""Integration tests use REAL git worktrees and subprocess-based pytest."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from autodev.providers import DemoFixProvider, DemoNoProgressProvider
from autodev.runner import JobConfig, run_job
from autodev.seed import make_seed


def worktrees(repo: Path) -> list[str]:
    return subprocess.check_output(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        text=True,
    ).split("\n\n")


@pytest.fixture
def seeded_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "seed"
    make_seed(repo)
    return repo


def test_patch_passes_and_base_repo_remains_unchanged(seeded_repo: Path, tmp_path: Path):
    before = worktrees(seeded_repo)
    result = run_job(seeded_repo, tmp_path / "runs", DemoFixProvider())
    assert result["status"] == "SUCCEEDED"
    assert result["stop_reason"] == "tests_passed"
    assert result["cleanup_success"] is True
    assert len(result["attempts"]) == 1
    assert (tmp_path / "runs" / str(result["patch_file"])).exists()
    patch = (tmp_path / "runs" / str(result["patch_file"])).read_text()
    assert '+        task["done"] = True' in patch
    assert "BUG: should become True" in (seeded_repo / "taskboard.py").read_text()
    assert worktrees(seeded_repo) == before
    data = json.loads((tmp_path / "runs" / str(result["report_file"])).read_text())
    assert data["status"] == "SUCCEEDED"


def test_repeated_failure_trips_circuit_without_touching_base(seeded_repo: Path, tmp_path: Path):
    before = worktrees(seeded_repo)
    result = run_job(seeded_repo, tmp_path / "runs", DemoNoProgressProvider())
    assert result["status"] == "CIRCUIT_OPEN"
    assert result["stop_reason"] == "no_progress_same_failure"
    assert len(result["attempts"]) == 2
    assert result["attempts"][0]["fingerprint"] == result["attempts"][1]["fingerprint"]
    assert result["cleanup_success"] is True
    assert worktrees(seeded_repo) == before
    assert not list((tmp_path / "runs").glob("*.patch"))


def test_dirty_base_is_rejected(seeded_repo: Path, tmp_path: Path):
    (seeded_repo / "taskboard.py").write_text("dirty\n")
    with pytest.raises(ValueError, match="clean"):
        run_job(seeded_repo, tmp_path / "runs", DemoFixProvider())


def test_attempt_budget_halts_even_without_repeat_breaker(seeded_repo: Path, tmp_path: Path):
    result = run_job(
        seeded_repo, tmp_path / "runs", DemoNoProgressProvider(),
        JobConfig(max_attempts=1, max_same_fingerprint=3),
    )
    assert result["status"] == "CIRCUIT_OPEN"
    assert result["stop_reason"] == "attempt_budget_exhausted"
    assert len(result["attempts"]) == 1
