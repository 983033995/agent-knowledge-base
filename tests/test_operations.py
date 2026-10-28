from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import operations
import distiller


class OperationsTests(unittest.TestCase):
    def report(self, sync=None, pending=False, failed=0, queue=0, worker=False):
        if sync is None:
            sync = {"last_sync": None, "by_source": {}, "records": 0}
        with patch.object(operations.memory_sources, "discover_sources", return_value=[]), patch.object(operations.memory_sync, "sync_status", return_value=sync), patch.object(operations.memory_sync, "_load_pending", return_value={} if pending else None), patch.object(operations.distiller, "collect_status", return_value={"failed": failed, "queue": queue, "worker_alive": worker}), patch.object(operations, "failure_audit_report", return_value={"status": "ok", "failed_total": failed, "counts": {}}):
            return operations.runtime_report()

    def test_never_synced_is_not_healthy(self):
        result = self.report()
        self.assertEqual(result["components"]["sync"]["state"], "never_synced")
        self.assertEqual(result["status"], "attention")
        self.assertEqual(result["cloud_requests"], 0)
        self.assertTrue(all(row["client_connection"] == "not_probed" for row in result["components"]["sources"].values()))

    def test_pending_recovery_has_priority_over_previous_success(self):
        result = self.report(sync={"last_sync": {"at": "2026-01-01T00:00:00+00:00", "stats": {}}, "by_source": {}}, pending=True)
        self.assertEqual(result["components"]["sync"]["state"], "pending_recovery")

    def test_partial_failures_are_explicit(self):
        result = self.report(sync={"last_sync": {"at": "2026-01-01T00:00:00+00:00", "stats": {"failed_files": 1}}, "by_source": {"qoder": 2}})
        self.assertEqual(result["components"]["sync"]["state"], "partial")
        self.assertEqual(result["components"]["sync"]["active_sources"], ["qoder"])

    def test_invalid_and_future_times_are_not_fresh_success(self):
        for timestamp, expected in [("bad", "invalid"), ((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat(), "future")]:
            result = self.report(sync={"last_sync": {"at": timestamp}, "by_source": {}})
            self.assertEqual(result["components"]["sync"]["timestamp_status"], expected)
            self.assertEqual(result["status"], "attention")

    def test_scoped_sync_cannot_claim_full_coverage(self):
        result = self.report(sync={"last_sync": {"at": "2026-01-01T00:00:00+00:00", "source": "qoder", "kind": "memory"}, "by_source": {"qoder": 2}})
        self.assertEqual(result["components"]["sync"]["state"], "scoped_sync")
        self.assertFalse(result["components"]["sync"]["coverage_verified"])
        self.assertEqual(result["status"], "attention")

    def test_malformed_last_sync_is_a_component_error(self):
        result = self.report(sync={"last_sync": "broken"})
        self.assertEqual(result["components"]["sync"]["state"], "error")
        result = self.report(sync={"last_sync": {"stats": "broken"}})
        self.assertEqual(result["components"]["sync"]["state"], "error")

    def test_queue_without_worker_and_historical_failures_are_distinct(self):
        result = self.report(failed=2, queue=1)
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("distillation_failures_present", codes)
        self.assertIn("queued_without_worker", codes)

    def test_component_errors_do_not_leak_exception_content(self):
        with patch.object(operations.memory_sources, "discover_sources", side_effect=ValueError("secret")), patch.object(operations.memory_sync, "sync_status", side_effect=ValueError("secret")), patch.object(operations.distiller, "collect_status", side_effect=ValueError("secret")), patch.object(operations, "failure_audit_report", return_value={"status": "ok", "failed_total": 0, "counts": {}}):
            result = operations.runtime_report()
        self.assertEqual(result["status"], "error")
        self.assertNotIn("secret", str(result))

    def test_failure_audit_classifies_current_stale_legacy_and_unreadable_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = root / "vault"
            vault.mkdir()
            names = ("current", "stale", "legacy", "unreadable", "already")
            paths = {name: vault / f"{name}.md" for name in names}
            for name, path in paths.items():
                path.write_text(f"# {name}\n", encoding="utf-8")
            before = {path: path.read_bytes() for path in paths.values()}
            entries = [
                ("current", {"note": str(paths["current"]), "input_hash": "current-hash", "input_context_hash": "current-context"}),
                ("stale", {"note": str(paths["stale"]), "input_hash": "old-hash", "input_context_hash": "current-context"}),
                ("legacy", {"note": str(paths["legacy"])}),
                ("missing", {}),
                ("unreadable", {"note": str(paths["unreadable"])}),
                ("already", {"note": str(paths["already"]), "input_hash": "current-hash", "input_context_hash": "current-context"}),
                ("outside", {"note": str(root / "private.md")}),
            ]
            resolved = {
                "current": ((str(paths["current"]), "current-hash", "current-context"), ""),
                "stale": ((str(paths["stale"]), "current-hash", "current-context"), ""),
                "legacy": ((str(paths["legacy"]), "current-hash", "current-context"), ""),
                "unreadable": (None, "read_failed"),
                "already": (None, "already_succeeded"),
            }
            with patch.dict(os.environ, {"AKB_VAULT": str(vault), "AKB_DISTILLER_AUTOSTART": "0"}), patch.object(distiller, "load_settings", return_value={"state_dir": root / "state"}), patch.object(distiller, "_read_queue_state", return_value=([], entries, "sqlite")), patch.object(distiller, "_retry_input", side_effect=lambda item: resolved[Path(item["note"]).stem]):
                result = operations.failure_audit_report()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["failed_total"], 7)
            self.assertEqual(result["classified_total"], 7)
            self.assertEqual(result["counts"], {
                "current": 1,
                "stale": 1,
                "legacy": 1,
                "missing": 1,
                "unreadable": 1,
                "already_succeeded": 1,
                "unsafe_path": 1,
                "changed_during_audit": 0,
            })
            self.assertEqual(before, {path: path.read_bytes() for path in paths.values()})
            self.assertEqual(set(root.iterdir()), {vault})

    def test_failure_audit_errors_are_not_reported_as_zero_and_are_redacted(self):
        with patch.object(distiller, "load_settings", side_effect=ValueError("secret transcript path")):
            result = operations.failure_audit_report()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "failed_ledger_unavailable")
        self.assertNotIn("secret", str(result))
        self.assertNotIn("failed_total", result)

    def test_failure_audit_empty_state_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = root / "vault"
            vault.mkdir()
            state = root / "distiller-state"
            env = {
                "AKB_VAULT": str(vault),
                "AKB_STATE_DB": str(root / "state.sqlite3"),
                "AKB_DISTILLER_STATE_DIR": str(state),
                "AKB_DISTILLER_CONFIG": str(root / "missing-config.json"),
                "AKB_DISTILLER_AUTOSTART": "0",
            }
            with patch.dict(os.environ, env):
                result = operations.failure_audit_report()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["failed_total"], 0)
            self.assertTrue(result["read_only"])
            self.assertEqual(set(root.iterdir()), {vault})

    def test_runtime_report_exposes_failure_audit_and_marks_unavailable_as_error(self):
        audit = {"status": "ok", "failed_total": 2, "counts": {"current": 1, "legacy": 1}}
        with patch.object(operations.memory_sources, "discover_sources", return_value=[]), patch.object(operations.memory_sync, "sync_status", return_value={"last_sync": None, "by_source": {}}), patch.object(operations.memory_sync, "_load_pending", return_value=None), patch.object(operations.distiller, "collect_status", return_value={"failed": 2, "queue": 0, "worker_alive": False}), patch.object(operations, "failure_audit_report", return_value=audit):
            result = operations.runtime_report()
        self.assertEqual(result["components"]["distiller"]["failure_audit"], audit)
        audit_error = {"status": "error", "error_code": "failed_ledger_unavailable"}
        with patch.object(operations.memory_sources, "discover_sources", return_value=[]), patch.object(operations.memory_sync, "sync_status", return_value={"last_sync": None, "by_source": {}}), patch.object(operations.memory_sync, "_load_pending", return_value=None), patch.object(operations.distiller, "collect_status", return_value={"failed": 0, "queue": 0, "worker_alive": False}), patch.object(operations, "failure_audit_report", return_value=audit_error):
            failed = operations.runtime_report()
        self.assertEqual(failed["status"], "error")
        self.assertIn("distillation_failure_audit_unavailable", {item["code"] for item in failed["issues"]})


if __name__ == "__main__":
    unittest.main()
