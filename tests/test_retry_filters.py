from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import akb
import distiller


class RetryFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.env = patch.dict(os.environ, {
            "AKB_STATE_DB": str(self.state / "state.sqlite3"),
            "AKB_DISTILLER_STATE_DIR": str(self.state),
            "AKB_DISTILLER_CONFIG": str(self.root / "absent.json"),
            "AKB_DISTILLER_AUTOSTART": "0",
            "AKB_DISTILLER_ENABLED": "1",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.settings = distiller.load_settings()
        self.store = distiller.queue_store(self.settings)
        self.ns = distiller.queue_namespace(self.settings)
        self.model = patch.object(distiller, "request_model", side_effect=AssertionError("no model"))
        self.model.start()
        self.addCleanup(self.model.stop)

    def note(self, name="one", **metadata):
        path = self.root / (name + ".md")
        path.write_text(akb.render_frontmatter({"title": name, **metadata}) + "private-body-" + name + "\n", encoding="utf-8")
        return path

    def entry(self, note, error="HTTP 400 private-error", **extra):
        _, _, body_hash, context_hash = distiller._note_input(note)
        return {"note": str(note), "input_hash": body_hash,
                "input_context_hash": context_hash, "attempts": 4,
                "last_error": error, **extra}

    def legacy(self, key, entry, kind="failed"):
        distiller.atomic_write_json(self.state / kind / (key + ".json"), entry)

    def put(self, key, entry, kind="failed"):
        distiller._migrate_queue(self.settings)
        with distiller.queue_state_lock(self.settings):
            distiller._put_job(self.settings, kind, key, entry)

    def jobs(self, kind="failed"):
        with self.store.read() as tx:
            return dict(tx.list_jobs(self.ns, kind)) if tx else {}

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.is_dir(), p.stat().st_mtime_ns,
                None if p.is_dir() else p.read_bytes()) for p in self.root.rglob("*")}

    def test_empty_preview_creates_nothing(self):
        before = self.snapshot()
        with patch.object(distiller, "_migrate_queue", side_effect=AssertionError), \
             patch.object(distiller, "queue_state_lock", side_effect=AssertionError), \
             patch.object(distiller, "note_lock", side_effect=AssertionError), \
             patch.object(distiller, "spawn_worker", side_effect=AssertionError):
            result = distiller.retry_failed(dry_run=True)
        self.assertEqual(result["selected_inputs"], 0)
        self.assertEqual(self.snapshot(), before)

    def test_legacy_preview_does_not_create_database_or_locks(self):
        note = self.note()
        self.legacy("a", {"note": str(note), "last_error": "HTTP 400"})
        before = self.snapshot()
        with patch.dict(os.environ, {"AKB_DISTILLER_AUTOSTART": "1"}), \
             patch.object(distiller, "spawn_worker", side_effect=AssertionError):
            result = distiller.retry_failed(dry_run=True)
        self.assertEqual(result["selected_inputs"], 1)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.store.db_path.exists())
        applied = distiller.retry_failed()
        self.assertEqual(applied["selected_inputs"], result["selected_inputs"])
        self.assertEqual(applied["moved"], 1)

    def test_sqlite_preview_preserves_all_bytes_and_mtimes(self):
        self.put("a", self.entry(self.note()))
        before = self.snapshot()
        result = distiller.retry_failed(dry_run=True)
        self.assertEqual(result["selected_failure_records"], 1)
        self.assertEqual(self.snapshot(), before)

    def test_filter_limit_counts_inputs_and_only_deletes_matching_records(self):
        first, second = self.note(), self.note("two")
        entry = self.entry(first)
        for key, value in (("a", entry), ("b", entry),
                           ("c", self.entry(first, "HTTP 500")),
                           ("d", self.entry(second, "http 400"))):
            self.put(key, value)
        preview = distiller.retry_failed(dry_run=True, error_contains="hTtP 400", limit=1)
        self.assertEqual(preview["eligible_inputs"], 2)
        self.assertEqual(preview["selected_inputs"], 1)
        self.assertEqual(preview["selected_failure_records"], 2)
        self.assertEqual(preview["skipped"]["limit"], 1)
        applied = distiller.retry_failed(error_contains="hTtP 400", limit=1)
        for key in ("eligible_inputs", "selected_inputs", "selected_failure_records", "filters", "skipped"):
            self.assertEqual(applied[key], preview[key])
        self.assertEqual(applied["moved"], 1)
        self.assertEqual(set(self.jobs()), {"c", "d"})
        self.assertEqual(len(self.jobs("queue")), 1)

    def test_unlimited_and_hashless_duplicates(self):
        one, two = self.note(), self.note("two")
        self.legacy("a", {"note": str(one)})
        self.legacy("b", self.entry(one))
        self.legacy("c", self.entry(two))
        preview = distiller.retry_failed(dry_run=True)
        self.assertEqual(preview["selected_inputs"], 2)
        self.assertEqual(preview["selected_failure_records"], 3)
        self.assertEqual(distiller.retry_failed()["moved"], 2)
        self.assertEqual(self.jobs(), {})

    def test_negative_limit_rejected_without_io(self):
        with patch.object(distiller, "load_settings", side_effect=AssertionError):
            with self.assertRaises(ValueError):
                distiller.retry_failed(dry_run=True, limit=-1)
            with self.assertRaises(ValueError):
                distiller.retry_failed(limit=-1)

    def test_stale_success_missing_and_read_failure_have_separate_counts(self):
        stale, context, success, unreadable = (self.note(name) for name in ("stale", "context", "success", "unreadable"))
        self.put("a", self.entry(stale, input_hash="old"))
        self.put("b", self.entry(context, input_context_hash="old"))
        entry = self.entry(success)
        success.write_text(akb.render_frontmatter({"status": "distilled", "distilled_input_hash": entry["input_hash"], "distilled_input_context_hash": entry["input_context_hash"]}) + "private-body-success\n", encoding="utf-8")
        self.put("c", entry)
        self.put("d", {"note": str(self.root / "missing.md")})
        self.put("e", self.entry(unreadable))
        original = distiller._note_input

        def read(path):
            if path.resolve() == unreadable.resolve():
                raise PermissionError("private path")
            return original(path)

        with patch.object(distiller, "_note_input", side_effect=read):
            for dry in (True, False):
                result = distiller.retry_failed(dry_run=dry)
                self.assertEqual(result["skipped"]["stale"], 2)
                for reason in ("already_succeeded", "missing_input", "read_failed"):
                    self.assertEqual(result["skipped"][reason], 1, (reason, result))
                self.assertEqual(result["moved"], 0)
        self.assertEqual(len(self.jobs()), 5)
        self.assertFalse(self.jobs("queue"))

    def test_existing_inflight_queue_is_untouched(self):
        note = self.note()
        entry = self.entry(note)
        queued = {**entry, "attempts": 3, "not_before": 9999999999,
                  "processing": {"token": "claim", "pid": os.getpid()}}
        self.put("a", entry)
        self.put(distiller.queue_key(note), queued, "queue")
        result = distiller.retry_failed()
        self.assertEqual(result["moved"], 0)
        self.assertEqual(self.jobs("queue")[distiller.queue_key(note)], queued)
        self.assertFalse(self.jobs())

    def test_different_queued_version_is_not_reset(self):
        note = self.note()
        self.put("a", self.entry(note))
        queued = self.entry(note, input_hash="different", processing={"token": "claim"})
        self.put(distiller.queue_key(note), queued, "queue")
        result = distiller.retry_failed()
        self.assertEqual(result["skipped"]["queue_conflict"], 1)
        self.assertEqual(self.jobs("queue")[distiller.queue_key(note)], queued)
        self.assertIn("a", self.jobs())

    def test_apply_rechecks_legacy_version_and_permission_under_lock(self):
        for mode in ("stale", "permission"):
            with self.subTest(mode=mode):
                note = self.note(mode)
                self.put(mode, {"note": str(note)})
                original_lock = distiller.note_lock

                @contextlib.contextmanager
                def changed_lock(path):
                    with original_lock(path):
                        if mode == "permission":
                            raise PermissionError("denied")
                        path.write_text("changed body\n", encoding="utf-8")
                        yield

                with patch.object(distiller, "note_lock", changed_lock):
                    result = distiller.retry_failed()
                self.assertEqual(result["moved"], 0)
                self.assertIn(mode, self.jobs())
                self.assertGreaterEqual(result["skipped"]["stale" if mode == "stale" else "read_failed"], 1)

    def test_changed_failure_record_is_preserved(self):
        note = self.note()
        self.put("a", self.entry(note))
        original_lock = distiller.note_lock

        @contextlib.contextmanager
        def changed_lock(path):
            with original_lock(path):
                distiller._put_job(self.settings, "failed", "a", self.entry(note, "HTTP 500"))
                yield

        with patch.object(distiller, "note_lock", changed_lock):
            result = distiller.retry_failed(error_contains="400")
        self.assertEqual(result["skipped"]["changed"], 1)
        self.assertEqual(self.jobs()["a"]["last_error"], "HTTP 500")
        self.assertFalse(self.jobs("queue"))

    def test_preview_does_not_expose_body_errors_or_filter_secret(self):
        secret = "private-error"
        self.put("a", self.entry(self.note(), secret))
        payload = json.dumps(distiller.retry_failed(dry_run=True, error_contains=secret))
        self.assertNotIn(secret, payload)
        self.assertNotIn("private-body", payload)
        self.assertNotIn(str(self.root), payload)

    def test_status_error_categories_and_valid_iso_time_order(self):
        note = self.note()
        values = [
            ("HTTP 400 private-error", "2026-01-01T10:00:00+10:00"),
            ("http400 secret", "2026-01-01T01:00:00Z"),
            ("unknown private failure", "bad-time"),
            ("timed out", None),
            ("", "2026-01-01T00:30:00"),
        ]
        for index, (error, failed_at) in enumerate(values):
            self.legacy(str(index), self.entry(note, error, failed_at=failed_at))
        before = self.snapshot()
        status = distiller.collect_status()
        self.assertEqual(status["failed_by_error"], {"HTTP400": 2, "other": 1, "timeout": 1, "unknown": 1})
        self.assertEqual(status["oldest_failed_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(status["latest_failed_at"], "2026-01-01T01:00:00+00:00")
        self.assertEqual(status["unknown_failed_at"], 2)
        self.assertEqual(self.snapshot(), before)

    def test_corrupt_legacy_preview_is_read_only(self):
        path = self.state / "failed" / "bad.json"
        path.parent.mkdir(parents=True)
        path.write_text("{broken", encoding="utf-8")
        before = self.snapshot()
        result = distiller.retry_failed(dry_run=True)
        self.assertEqual(result["skipped"]["read_failed"], 1)
        self.assertEqual(self.snapshot(), before)

    def test_apply_spawns_only_when_enabled_and_requested(self):
        self.put("a", self.entry(self.note()))
        with patch.dict(os.environ, {"AKB_DISTILLER_AUTOSTART": "1"}), patch.object(distiller, "spawn_worker") as worker:
            result = distiller.retry_failed()
        self.assertTrue(result["worker_started"])
        worker.assert_called_once()

    def test_storage_failure_rolls_back_whole_transition(self):
        self.put("a", self.entry(self.note()))
        with patch.object(distiller, "_delete_job", side_effect=ValueError("storage failed")):
            with self.assertRaisesRegex(ValueError, "storage failed"):
                distiller.retry_failed()
        self.assertIn("a", self.jobs())
        self.assertFalse(self.jobs("queue"))

    def test_worker_environment_and_cli_flags(self):
        self.put("a", self.entry(self.note()))
        with patch.object(distiller, "spawn_worker") as worker:
            self.assertFalse(distiller.retry_failed()["worker_started"])
            worker.assert_not_called()
        self.put("b", self.entry(self.note("two")))
        with patch.dict(os.environ, {"AKB_DISTILLER_AUTOSTART": "1", "AKB_DISTILLER_ENABLED": "0"}), patch.object(distiller, "spawn_worker") as worker:
            self.assertFalse(distiller.retry_failed()["worker_started"])
            worker.assert_not_called()
        with patch.object(distiller, "retry_failed", return_value={}) as retry, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(distiller.main(["retry-failed", "--dry-run", "--error-contains", "400", "--limit", "2"]), 0)
        retry.assert_called_once_with(dry_run=True, error_contains="400", limit=2)


if __name__ == "__main__":
    unittest.main()
