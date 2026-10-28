from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import akb
import memory_sync


class UnifiedIntegrationTests(unittest.TestCase):
    def test_five_client_sessions_sync_and_replay_without_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "home"
            workspace = root / "project"
            workspace.mkdir()
            fixtures = {
                ".qoder/projects/p/q.jsonl": [
                    {"type": "workspace-directories", "directories": [str(workspace)]},
                    {"type": "user", "uuid": "q1", "message": {"role": "user", "content": "Qoder独特经验"}},
                ],
                ".codex/sessions/c.jsonl": [
                    {"type": "session_meta", "payload": {"id": "c1", "cwd": str(workspace)}},
                    {"type": "response_item", "payload": {"type": "message", "id": "c2", "role": "user", "content": "Codex独特经验"}},
                ],
                ".workbuddy/projects/p/w.jsonl": [
                    {"type": "message", "id": "w1", "cwd": str(workspace), "role": "user", "content": "WorkBuddy独特经验"},
                ],
                ".dsh/sessions/d/session.jsonl": [
                    {"type": "session", "id": "d1", "cwd": str(workspace)},
                    {"type": "user/message", "data": {"id": "d2", "source": {"kind": "user"}, "content": "DeepSeek独特经验"}},
                ],
                ".cursor/projects/p/agent-transcripts/s.jsonl": [
                    {"role": "user", "cwd": str(workspace), "message": {"content": "Cursor独特经验"}},
                ],
            }
            before = {}
            for relative, rows in fixtures.items():
                path = home / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
                path.write_text(text, encoding="utf-8")
                before[str(path)] = path.read_bytes()
            with patch.dict(os.environ, {
                "AKB_VAULT": str(root / "vault"),
                "AKB_SYNC_STATE_DIR": str(root / "sync"),
                "AKB_SEARCH_INDEX": str(root / "index.json"),
                "AKB_DISTILLER_AUTOSTART": "0",
            }):
                try:
                    first = memory_sync.sync_sources(home=home, kind="session", apply=True)
                    self.assertEqual(first["errors"], [])
                    self.assertEqual(first["stats"]["imported"], 5)
                    self.assertEqual(first["cloud_requests"], 0)
                    self.assertEqual(akb.search_notes("独特经验"), [])
                    self.assertEqual(len(akb.search_notes("独特经验", include_sessions=True, project=str(workspace))), 5)
                    second = memory_sync.sync_sources(home=home, kind="session", apply=True)
                    self.assertEqual(second["stats"]["unchanged"], 5)
                    self.assertEqual(memory_sync.sync_status()["by_source"], {"qoder": 1, "codex": 1, "workbuddy": 1, "deepseek": 1, "cursor": 1})
                finally:
                    akb._CONFIG_CACHE = None
                    akb._CONFIG_CACHE_KEY = None
                    akb._SEARCH_INDEX_MEM = None
                    akb._SEARCH_INDEX_MEM_KEY = None
            self.assertEqual(before, {path: Path(path).read_bytes() for path in before})

    def test_new_management_tools_through_stdio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_sync_status", "arguments": {}}},
            ]
            env = {**os.environ, "AKB_VAULT": str(root / "vault"), "AKB_SYNC_STATE_DIR": str(root / "sync"), "AKB_DISTILLER_AUTOSTART": "0"}
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts/mcp_server.py")],
                input="".join(json.dumps(row) + "\n" for row in requests),
                text=True, capture_output=True, env=env, timeout=10, check=True,
            )
            responses = [json.loads(line) for line in completed.stdout.splitlines()]
            self.assertEqual(responses[0]["result"]["serverInfo"]["version"], "0.6.0")
            names = {tool["name"] for tool in responses[1]["result"]["tools"]}
            self.assertEqual(len(names), 10)
            self.assertIn("memory_sync", names)
            result = json.loads(responses[2]["result"]["content"][0]["text"])
            self.assertEqual(result["records"], 0)
            self.assertFalse((root / "vault").exists())
            self.assertFalse((root / "sync").exists())


if __name__ == "__main__":
    unittest.main()
