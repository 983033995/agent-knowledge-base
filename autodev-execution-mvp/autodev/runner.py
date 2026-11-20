"""Bounded, isolated-by-worktree test runner for a deterministic patch provider.

WARNING: A Git worktree is NOT a security sandbox. Only run trusted local tests.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .providers import PatchProvider
from .storage import add_attempt, init_db, new_job, update_job


@dataclass(frozen=True)
class JobConfig:
    max_attempts: int = 3
    test_timeout_seconds: int = 30
    max_job_seconds: int = 120
    max_same_fingerprint: int = 2

    def __post_init__(self) -> None:
        if min(self.max_attempts, self.test_timeout_seconds,
               self.max_job_seconds, self.max_same_fingerprint) < 1:
            raise ValueError("Job budgets must be positive")


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, timeout=20,
    )
    return proc.stdout.strip()


def test_in_subprocess(cwd: Path, timeout_seconds: float) -> tuple[int | None, str]:
    """Run a trusted seed repo's tests, killing the process group on timeout."""
    if timeout_seconds <= 0:
        return None, "TIMEOUT: job-level deadline exhausted before tests\n"

    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "--tb=short"],
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=(os.name == "posix"),
    )
    try:
        stdout, _ = proc.communicate(timeout=timeout_seconds)
        return proc.returncode, stdout
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        stdout, _ = proc.communicate()
        return None, stdout + "\nTIMEOUT: test process group was terminated\n"


def failure_fingerprint(output: str, returncode: int | None) -> str:
    """Hash stable failed-test identifiers; use normalized output as fallback."""
    if returncode == 0:
        return "passed"
    failed = sorted(re.findall(r"^FAILED\s+(\S+)", output, flags=re.MULTILINE))
    if failed:
        stable = "|".join(failed)
    else:
        stable = re.sub(r"/tmp/[A-Za-z0-9_./-]+", "<TMP>", output)
        stable = re.sub(r"\d+\.\d+s", "<DURATION>", stable)
        stable = stable[-3000:]
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def run_job(repo: Path, output_dir: Path, provider: PatchProvider,
            config: JobConfig = JobConfig()) -> dict[str, object]:
    repo = repo.resolve(strict=True)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if git(repo, "status", "--porcelain"):
        raise ValueError("Base repository must have a clean working tree")
    base_commit = git(repo, "rev-parse", "HEAD")
    job_id = uuid.uuid4().hex[:12]
    db = output_dir / "jobs.sqlite3"
    init_db(db)
    new_job(db, job_id, repo, base_commit)

    report: dict[str, object] = {
        "job_id": job_id,
        "base_commit": base_commit,
        "status": "RUNNING",
        "provider": type(provider).__name__,
        "note": "Deterministic demo patch provider; NOT an LLM agent",
        "tests_run_on_host": True,
        "attempts": [],
        "cleanup_success": True,
        "token_usage": None,
    }
    deadline = time.monotonic() + config.max_job_seconds
    last_fingerprint: str | None = None
    repeats = 0

    try:
        for number in range(1, config.max_attempts + 1):
            if time.monotonic() >= deadline:
                report["status"] = "CIRCUIT_OPEN"
                report["stop_reason"] = "job_deadline_exceeded"
                break
            # mkdtemp creates a reserved path so different jobs cannot collide.
            worktree = Path(tempfile.mkdtemp(prefix=f"autodev-{job_id}-{number}-"))
            was_attached = False
            current_attempt: dict[str, object] = {"attempt": number}
            try:
                git(repo, "worktree", "add", "--detach", str(worktree), base_commit)
                was_attached = True
                update_job(db, job_id, status="RUNNING", attempts=number,
                           active_worktree=str(worktree))
                provider.apply(worktree, number)
                timeout = min(config.test_timeout_seconds, max(0, deadline - time.monotonic()))
                code, logs = test_in_subprocess(worktree, timeout)
                fingerprint = failure_fingerprint(logs, code)
                log_name = f"{job_id}-attempt-{number}.log"
                (output_dir / log_name).write_text(logs, encoding="utf-8")
                current_attempt.update({
                    "returncode": code, "fingerprint": fingerprint,
                    "test_passed": code == 0, "log_file": log_name,
                })
                add_attempt(db, job_id, number, code, fingerprint, code == 0)
                update_job(db, job_id, last_fingerprint=fingerprint)
                report["attempts"].append(current_attempt)

                if code == 0:
                    # Diff must come from the disposable worktree, not the base checkout.
                    patch = git(worktree, "diff", "--no-ext-diff", "--binary", "HEAD")
                    patch_name = f"{job_id}.patch"
                    (output_dir / patch_name).write_text(patch + "\n", encoding="utf-8")
                    report["patch_file"] = patch_name
                    report["status"] = "SUCCEEDED"
                    report["stop_reason"] = "tests_passed"
                    break

                repeats = repeats + 1 if fingerprint == last_fingerprint else 1
                last_fingerprint = fingerprint
                if repeats >= config.max_same_fingerprint:
                    report["status"] = "CIRCUIT_OPEN"
                    report["stop_reason"] = "no_progress_same_failure"
                    break
                if code is None:
                    report["status"] = "CIRCUIT_OPEN"
                    report["stop_reason"] = "test_timeout"
                    break
            except Exception as exc:
                report["status"] = "FAILED"
                report["stop_reason"] = f"execution_error:{type(exc).__name__}"
                report["error"] = str(exc)
                break
            finally:
                try:
                    if was_attached:
                        git(repo, "worktree", "remove", "--force", str(worktree))
                    else:
                        worktree.rmdir()
                except Exception as exc:
                    report["cleanup_success"] = False
                    report.setdefault("cleanup_errors", []).append(str(exc))
                if report["cleanup_success"]:
                    update_job(db, job_id, active_worktree=None)
        else:
            report["status"] = "CIRCUIT_OPEN"
            report["stop_reason"] = "attempt_budget_exhausted"
    finally:
        if report["status"] == "RUNNING":
            report["status"] = "FAILED"
            report["stop_reason"] = "unexpected_exit"
        if not report["cleanup_success"]:
            report["status"] = "FAILED_CLEANUP"
            report["stop_reason"] = "worktree_cleanup_failed"
        update_job(db, job_id, status=str(report["status"]),
                   cleanup_success=int(bool(report["cleanup_success"])))
        report_name = f"{job_id}-report.json"
        (output_dir / report_name).write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        report["report_file"] = report_name
    return report
