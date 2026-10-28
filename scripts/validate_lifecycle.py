"""Emit comparable five-source adapter receipts using isolated synthetic data.

This validates subprocess contracts, not a real client's hook installation.
It never calls a model, scans a real client home, or changes production data.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("qoder", "cursor", "codex", "workbuddy", "deepseek")


def validate() -> dict:
    receipts = []
    with tempfile.TemporaryDirectory(prefix="akb-adapter-acceptance-") as directory:
        base = Path(directory).resolve()
        for source in SOURCES:
            root = base / source
            workspace = root / "project"
            vault = root / "vault"
            notes = vault / "10-技术笔记"
            workspace.mkdir(parents=True)
            notes.mkdir(parents=True)
            marker = "synthetic-context-" + source
            seed = notes / "seed.md"
            seed.write_text("---\n" + "title: 项目概览\ntype: project\nscope: project\nproject_path: " +
                            json.dumps(str(workspace)) + "\n---\n" + marker + "\n", encoding="utf-8")
            environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                "AKB_CONFIG": str(ROOT / "config.example.json"), "AKB_VAULT": str(vault),
                "AKB_STATE_DB": str(root / "state.sqlite3"),
                "AKB_SEARCH_INDEX": str(root / "index.json"),
                "AKB_SYNC_STATE_DIR": str(root / "sync"),
                "AKB_DISTILLER_STATE_DIR": str(root / "state"),
                "AKB_DISTILLER_CONFIG": str(root / "absent.json"),
                "AKB_DISTILLER_AUTOSTART": "0", "AKB_DISTILLER_ENABLED": "1"}
            sid = "synthetic-" + source
            event = {"cwd": str(workspace), "session_id": sid}
            bridge = ROOT / "scripts/deepseek_lifecycle_bridge.py"
            hook = ROOT / "scripts/lifecycle_hook.py"

            def run(action, payload):
                command = [sys.executable, str(bridge), action] if source == "deepseek" else [
                    sys.executable, str(hook), source, action]
                result = subprocess.run(command, input=json.dumps(payload), capture_output=True,
                                        text=True, env=environment, timeout=15)
                if result.returncode or result.stderr:
                    raise ValueError("Adapter command failed for " + source)
                return json.loads(result.stdout) if result.stdout.strip() else None

            started = run("recall" if source == "deepseek" else "start", event)
            if source == "deepseek":
                context = started["context"]
            elif source == "cursor":
                context = started["additional_context"]
            else:
                context = started["hookSpecificOutput"]["additionalContext"]
            if marker not in context:
                raise ValueError("Context was not injected for " + source)
            public = "synthetic-public-learning-" + source
            hidden = "synthetic-hidden-tool-" + source
            if source == "deepseek":
                stop = {**event, "events": [
                    {"role": "user", "text": public, "id": "seq:1"},
                    {"role": "tool", "text": hidden},
                    {"role": "assistant", "text": "<akb-recalled-context>" + marker +
                        "</akb-recalled-context>synthetic-answer", "id": "seq:2"}]}
            else:
                rows = []
                if source == "codex":
                    rows = [
                        {"type": "session_meta", "payload": {"id": sid, "cwd": str(workspace)}},
                        {"type": "response_item", "payload": {"type": "message", "id": "one", "role": "user", "content": public}},
                        {"type": "response_item", "payload": {"type": "message", "id": "two", "role": "assistant",
                            "content": "<akb-recalled-context>" + marker + "</akb-recalled-context>synthetic-answer"}},
                        {"type": "response_item", "payload": {"type": "function_call_output", "output": hidden}},
                    ]
                else:
                    rows = [
                        {"type": "user", "uuid": "one", "cwd": str(workspace),
                            "message": {"role": "user", "content": public}},
                        {"type": "assistant", "uuid": "two", "cwd": str(workspace),
                            "message": {"role": "assistant", "content": "<akb-recalled-context>" +
                                marker + "</akb-recalled-context>synthetic-answer"}},
                        {"type": "tool", "message": {"role": "tool", "content": hidden}},
                    ]
                transcript = root / "transcript.jsonl"
                transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                stop = {**event, "transcript_path": str(transcript)}
            for action in (("capture",) * 3 if source == "deepseek" else ("stop", "end", "stop")):
                run(action, stop)
            intakes = list((vault / "00-Inbox/_agent-sessions").glob("*.md"))
            if len(intakes) != 1:
                raise ValueError("Duplicate or missing intake for " + source)
            intake = intakes[0].read_text(encoding="utf-8")
            if public not in intake or hidden in intake or marker in intake:
                raise ValueError("Capture filtering failed for " + source)
            connection = sqlite3.connect((root / "state.sqlite3").as_uri() + "?mode=ro", uri=True)
            try:
                jobs = connection.execute("SELECT COUNT(*) FROM jobs WHERE kind='queue'").fetchone()[0]
                cursors = connection.execute("SELECT payload FROM documents WHERE namespace LIKE 'lifecycle:%'").fetchall()
                messages = sum(len(json.loads(row[0]).get("messages", {})) for row in cursors)
            finally:
                connection.close()
            if jobs != 1 or messages != 2:
                raise ValueError("Queue or cursor contract failed for " + source)
            receipts.append({
                "source": source, "session_id": sid, "evidence_kind": "isolated-adapter-contract",
                "start_context_sha256": hashlib.sha256(context.encode()).hexdigest(),
                "intake_sha256": hashlib.sha256(intake.encode()).hexdigest(),
                "intakes": 1, "queued": jobs, "cursor_messages": messages,
                "duplicate_stop_idempotent": True, "injection_recapture": False,
                "tool_content_captured": False, "native_client_verified": False,
            })
    return {"schema_version": 1, "status": "passed", "cloud_requests": 0, "sources": receipts,
            "scope": "Synthetic subprocess adapter contracts; not native-client acceptance."}


def main() -> int:
    print(json.dumps(validate(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
