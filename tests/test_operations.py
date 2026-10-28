from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import operations


class OperationsTests(unittest.TestCase):
    def report(self, sync=None, pending=False, failed=0, queue=0, worker=False):
        if sync is None:
            sync = {"last_sync": None, "by_source": {}, "records": 0}
        with patch.object(operations.memory_sources, "discover_sources", return_value=[]), patch.object(operations.memory_sync, "sync_status", return_value=sync), patch.object(operations.memory_sync, "_load_pending", return_value={} if pending else None), patch.object(operations.distiller, "collect_status", return_value={"failed": failed, "queue": queue, "worker_alive": worker}):
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
        with patch.object(operations.memory_sources, "discover_sources", side_effect=ValueError("secret")), patch.object(operations.memory_sync, "sync_status", side_effect=ValueError("secret")), patch.object(operations.distiller, "collect_status", side_effect=ValueError("secret")):
            result = operations.runtime_report()
        self.assertEqual(result["status"], "error")
        self.assertNotIn("secret", str(result))


if __name__ == "__main__":
    unittest.main()
