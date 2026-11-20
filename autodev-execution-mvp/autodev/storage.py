"""Durable local metadata, held outside the Git worktrees."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            repo_path TEXT NOT NULL,
            base_commit TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_fingerprint TEXT,
            active_worktree TEXT,
            cleanup_success INTEGER
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS attempts (
            job_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            returncode INTEGER,
            fingerprint TEXT,
            test_ok INTEGER NOT NULL,
            PRIMARY KEY(job_id, attempt)
        )""")


def new_job(path: Path, job_id: str, repo: Path, base: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, repo_path, base_commit, status) VALUES (?, ?, ?, ?)",
            (job_id, str(repo), base, "PENDING"),
        )


def update_job(path: Path, job_id: str, **changes: object) -> None:
    valid_fields = {"status", "attempts", "last_fingerprint", "active_worktree", "cleanup_success"}
    if not changes or not set(changes).issubset(valid_fields):
        raise ValueError("Invalid job update fields")
    columns = ", ".join(f"{key} = ?" for key in changes)
    with sqlite3.connect(path) as conn:
        conn.execute(f"UPDATE jobs SET {columns} WHERE job_id = ?", (*changes.values(), job_id))


def add_attempt(path: Path, job_id: str, attempt: int, returncode: int | None,
                fingerprint: str, test_ok: bool) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO attempts VALUES (?, ?, ?, ?, ?)",
            (job_id, attempt, returncode, fingerprint, int(test_ok)),
        )
