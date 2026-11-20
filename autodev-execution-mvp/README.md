# AutoDev — Execution-Control MVP

This is a **working early prototype of a coding-agent execution control plane**, stored as an independent example inside the existing private **agent-knowledge-base** repository. It is not part of the Agent Knowledge Base runtime and must be tested in isolation.

**This is not a multi-Agent system yet.** The two included patch providers are deterministic demo adapters—not Devin, an LLM, or an autonomous programmer.

## What the prototype actually does

- Creates a real Git seed repository containing a deliberately failing TaskBoard test.
- Creates a detached Git worktree from a fixed base commit for each patch attempt.
- Applies a scripted patch (or deliberately makes no progress) in that temporary worktree.
- Runs pytest with a timeout and, on POSIX, kills the subprocess group on test timeout.
- Records attempt state and failure fingerprints in SQLite, with logs saved outside worktrees.
- Stops after an attempt budget or two consecutive identical failure fingerprints.
- Emits a reviewable patch on success; rejects unsuccessful attempts.
- Removes temporary worktrees in finally, leaving the source checkout unchanged.

**Not implemented:** multi-Agent task planning / DAG orchestration, real LLM/Devin integration, Docker or VM isolation, PR creation, crash-safe reconciler, token cost metering. Git worktrees are *not* security sandboxes. Only run this against trusted code and tests.

## Requirements

Python 3.11+, git, and pytest. No API keys or paid models.

## Reproduce

From this directory (NOT from the parent knowledge-base repository root):

~~~sh
python -m pip install pytest
python -m pytest -q tests
python -m autodev seed /tmp/autodev-taskboard
python -m autodev run --repo /tmp/autodev-taskboard --output /tmp/autodev-results --provider fix
python -m autodev run --repo /tmp/autodev-taskboard --output /tmp/autodev-results --provider no-progress
~~~

Expected outcomes: the `fix` provider produces one passing run and a patch; the `no-progress` provider repeats the same failing test twice and stops with `CIRCUIT_OPEN`. Verify worktrees are removed and the base repository is unchanged.

## Repo layout

- `autodev/`: CLI, patch providers, Git worktree runner, SQLite persistence.
- `example_seed/`: minimal TaskBoard seed code and tests (one intentional defect).
- `tests/`: four real-Git integration tests for patching, budgets, circuit-breaker, cleanup, and clean-base checks.
- `evidence/`: representative local test output and structured run reports.
- `../.github/workflows/autodev-mvp.yml`: scoped GitHub Actions validation for this subfolder.

## Verification and limitations

Local verification on 2026-11-20: **4 integration tests passed**. One deterministic fix succeeded in one attempt. A no-progress case stopped after two identical failure fingerprints, and cleanup was verified. See `evidence/verification.md`.

This evidence demonstrates execution-control behavior only. It does not show that an AI agent generated a fix or that untrusted tests are safely sandboxed.

## Planned next steps

1. Docker/VM test isolation and resource limits.
2. Persisted worker leases and crash-recovery cleanup.
3. Real LLM patch provider and token usage accounting.
4. DAG scheduler with bounded re-planning.
5. GitHub PR automation and broader repository evaluation.
