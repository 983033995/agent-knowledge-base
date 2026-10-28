from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import akb
import mcp_server


class ManagementAuditTests(unittest.TestCase):
    def test_new_read_tools_create_no_missing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "AKB_CONFIG": str(ROOT / "config.example.json"),
                "AKB_VAULT": str(root / "vault"), "AKB_STATE_DB": str(root / "state.sqlite3"),
                "AKB_DISTILLER_STATE_DIR": str(root / "state"),
                "AKB_SYNC_STATE_DIR": str(root / "sync"),
                "AKB_DISTILLER_CONFIG": str(root / "absent.json"),
                "AKB_DISTILLER_AUTOSTART": "0",
            }
            with patch.dict(os.environ, environment):
                akb._CONFIG_CACHE = None
                failed = mcp_server.call_tool("memory_failure_audit", {})
                knowledge = mcp_server.call_tool("memory_knowledge_audit", {})
            self.assertEqual(failed["failed_total"], 0)
            self.assertEqual(knowledge["status"], "error")
            self.assertEqual(list(root.iterdir()), [])
            annotations = {tool["name"]: tool["annotations"] for tool in mcp_server.TOOLS}
            self.assertTrue(annotations["memory_failure_audit"]["readOnlyHint"])
            self.assertTrue(annotations["memory_knowledge_audit"]["readOnlyHint"])

    def test_mcp_fixture_search_requires_explicit_type(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"AKB_CONFIG": str(ROOT / "config.example.json"),
                "AKB_VAULT": str(root / "vault"), "AKB_SEARCH_INDEX": str(root / "index.json"),
                "AKB_DISTILLER_AUTOSTART": "0"}):
                akb._CONFIG_CACHE = None
                akb._SEARCH_INDEX_MEM = None
                akb.write_note("10-技术笔记", "fixture.md",
                               {"title": "isolatedfixture", "type": "validation"}, "isolatedfixture")
                ordinary = mcp_server.call_tool("memory_search", {"query": "isolatedfixture", "include_inactive": True})
                explicit = mcp_server.call_tool("memory_search", {"query": "isolatedfixture", "memory_type": "validation"})
                self.assertEqual(ordinary, [])
                self.assertEqual(len(explicit), 1)


if __name__ == "__main__":
    unittest.main()
