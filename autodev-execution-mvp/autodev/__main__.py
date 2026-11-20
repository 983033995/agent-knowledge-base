"""Command-line entrypoint: python -m autodev ..."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .providers import DemoFixProvider, DemoNoProgressProvider
from .runner import JobConfig, run_job
from .seed import make_seed


def main() -> None:
    p = argparse.ArgumentParser(description="AutoDev local execution-control MVP")
    commands = p.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed", help="Create a broken, versioned TaskBoard seed repository")
    seed.add_argument("destination", type=Path)
    run = commands.add_parser("run", help="Run bounded demo patch attempts in isolated worktrees")
    run.add_argument("--repo", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--provider", choices=["fix", "no-progress"], default="fix")
    run.add_argument("--attempts", type=int, default=3)
    args = p.parse_args()
    if args.command == "seed":
        print(f"Created demo repo at {args.destination}, commit {make_seed(args.destination)}")
        return
    provider = DemoFixProvider() if args.provider == "fix" else DemoNoProgressProvider()
    result = run_job(args.repo, args.output, provider, JobConfig(max_attempts=args.attempts))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
