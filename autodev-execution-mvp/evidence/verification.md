# Local verification evidence (2026-11-20)

Commands executed in the AutoDev MVP project root:

~~~sh
python -m pytest -q tests
python -m autodev seed /tmp/autodev-proof-seed
python -m autodev run --repo /tmp/autodev-proof-seed --output /tmp/autodev-proof-runs --provider fix
python -m autodev run --repo /tmp/autodev-proof-seed --output /tmp/autodev-proof-runs --provider no-progress
~~~

| Verification | Observed result |
| --- | --- |
| Runner integration tests | 4 passed |
| Deterministic demo bug fix | SUCCEEDED in 1 attempt; patch emitted |
| Deterministic repeated failure | CIRCUIT_OPEN after 2 identical fingerprints |
| Git worktree cleanup | cleanup_success: true in both runs |
| Base seed repository | Unmodified, asserted by tests |

Artifacts: `test-results.txt`, `success-run.json`, `circuit-run.json`,
`demo-fix.patch`, `demo-fix-test.log`, `demo-no-progress-test.log`.

These are **local recorded tests**, not a claim that GitHub Actions has already passed after publication.
The providers are scripted, not LLMs. Tests run on the host, not a hardened sandbox.
