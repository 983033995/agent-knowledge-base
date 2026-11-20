"""Patch-provider protocol and intentionally deterministic demo adapters.

The included providers are NOT language models. They enable reproducible,
independent tests of task execution, retry and rollback behavior.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class PatchProvider(Protocol):
    def apply(self, worktree: Path, attempt: int) -> None:
        """Edit files in the isolated worktree (must not touch the base repo)."""


class DemoFixProvider:
    """Fix a known defect in the bundled TaskBoard seed project."""

    def apply(self, worktree: Path, attempt: int) -> None:
        source_path = worktree / "taskboard.py"
        source = source_path.read_text(encoding="utf-8")
        broken = 'task["done"] = False  # BUG: should become True'
        fixed = 'task["done"] = True  # Task completion fix'
        if broken not in source:
            raise ValueError("Expected seeded bug was not found")
        source_path.write_text(source.replace(broken, fixed, 1), encoding="utf-8")


class DemoNoProgressProvider:
    """Deliberately leave the same failure in place to test circuit breaking."""

    def apply(self, worktree: Path, attempt: int) -> None:
        return None
