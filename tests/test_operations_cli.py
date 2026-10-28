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


class OperationsCLITests(unittest.TestCase):
    def test_retry_arguments_reach_distiller(self):
        arguments = akb.build_parser().parse_args(["distill-retry", "--dry-run", "--error-contains", "HTTP 400", "--limit", "3"])
        with patch("distiller.retry_failed", return_value={"dry_run": True}) as retry, patch("sys.stdout"):
            self.assertEqual(akb.cmd_distill_retry(arguments), 0)
        retry.assert_called_once_with(dry_run=True, error_contains="HTTP 400", limit=3)

    def test_memory_status_does_not_create_a_missing_vault(self):
        import mcp_server
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"AKB_VAULT": str(root / 'vault'), "AKB_STATE_DB": str(root / 'state.sqlite3'), "AKB_SYNC_STATE_DIR": str(root / 'sync'), "AKB_DISTILLER_STATE_DIR": str(root / 'distiller')}), patch('operations.runtime_report', return_value={"status": "attention"}):
                result = mcp_server.call_tool('memory_status', {})
                self.assertEqual(result['total_notes'], 0)
                self.assertEqual(list(root.iterdir()), [])

    def test_cli_preview_without_existing_state_is_zero_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {**os.environ, "AKB_VAULT": str(root / "vault"), "AKB_STATE_DB": str(root / "state.sqlite3"), "AKB_SYNC_STATE_DIR": str(root / "sync"), "AKB_DISTILLER_STATE_DIR": str(root / "distiller"), "AKB_SEARCH_INDEX": str(root / "index.json"), "AKB_DISTILLER_AUTOSTART": "0"}
            result = subprocess.run([sys.executable, str(ROOT / "scripts/akb.py"), "distill-retry", "--dry-run", "--limit", "1"], text=True, capture_output=True, env=env, timeout=10, check=True)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["moved"], 0)
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
