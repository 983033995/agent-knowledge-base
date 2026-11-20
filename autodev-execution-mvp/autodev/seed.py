"""Create a genuine, committed seed repository for the runner's first tests."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


SEED_FILES = Path(__file__).resolve().parent.parent / "example_seed"


def make_seed(target: Path) -> str:
    if target.exists() and any(target.iterdir()):
        raise ValueError("Seed destination must be an empty directory")
    target.mkdir(parents=True, exist_ok=True)
    for path in SEED_FILES.iterdir():
        if path.is_file():
            shutil.copy2(path, target / path.name)
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(target), *args],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    run("init", "-q")
    run("config", "user.name", "AutoDev Demo")
    run("config", "user.email", "autodev-demo@example.invalid")
    run("add", ".")
    run("commit", "-qm", "Seed: deliberately broken TaskBoard")
    return run("rev-parse", "HEAD")
