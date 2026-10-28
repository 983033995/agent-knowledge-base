#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import akb  # noqa: E402
import distiller  # noqa: E402


class DistillerVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name) / "distiller-state"
        self.old_env = self.set_env(
            AKB_STATE_DB=str(self.state_dir / "state.sqlite3"),
            AKB_VAULT=self.temp_dir.name,
            AKB_SEARCH_INDEX=str(Path(self.temp_dir.name) / "search-index.json"),
            AKB_DISTILLER_STATE_DIR=str(self.state_dir),
            AKB_DISTILLER_AUTOSTART="0",
            AKB_DISTILLER_ENABLED="1",
            AKB_DISTILLER_MAX_ATTEMPTS="1",
        )
        akb._CONFIG_CACHE = None
        akb._CONFIG_CACHE_KEY = None
        akb.ensure_vault()

    def tearDown(self) -> None:
        self.restore_env(self.old_env)
        akb._CONFIG_CACHE = None
        akb._CONFIG_CACHE_KEY = None
        self.temp_dir.cleanup()

    @staticmethod
    def set_env(**values: str) -> dict[str, str | None]:
        old: dict[str, str | None] = {}
        for key, value in values.items():
            old[key] = os.environ.get(key)
            os.environ[key] = value
        return old

    @staticmethod
    def restore_env(old: dict[str, str | None]) -> None:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def intake(self, content: str = "原始会话内容") -> Path:
        return akb.create_capture_intake(
            source="test",
            title="版本化提炼",
            project="demo",
            raw_context=content,
        )

    def jobs(self, kind: str = "queue") -> list[tuple[str, dict]]:
        settings = distiller.load_settings()
        with distiller.queue_store(settings).read() as tx:
            return tx.list_jobs(distiller.queue_namespace(settings), kind) if tx else []

    def queue_file(self) -> str:
        return self.jobs()[0][0]

    def read_job(self, key: str, kind: str = "queue") -> dict | None:
        return dict(self.jobs(kind)).get(key)

    def put_job(self, key: str, entry: dict, kind: str = "queue") -> None:
        settings = distiller.load_settings()
        with distiller.queue_state_lock(settings):
            distiller._put_job(settings, kind, key, entry)

    @staticmethod
    def no_value_result() -> dict[str, object]:
        return {"keep": False}

    @staticmethod
    def keep_result(title: str, marker: str = "版本化召回标记") -> dict[str, object]:
        return {
            "keep": True,
            "title": title,
            "summary": marker,
            "knowledge": [marker],
            "patterns": [],
            "decisions": [],
            "gotchas": [],
            "next_steps": [],
        }

    def make_ready(self, path: str) -> None:
        entry = self.read_job(path)
        self.assertIsNotNone(entry)
        assert entry is not None
        entry["not_before"] = 0
        self.put_job(path, entry)

    def replace_body(self, path: Path, body: str) -> None:
        with akb.note_lock(path):
            metadata, _ = akb.read_note(path)
            akb.atomic_write_text(path, akb.render_frontmatter(metadata) + body.strip() + "\n")

    def distilled_paths(self, note: Path) -> list[Path]:
        metadata, _ = akb.read_note(note)
        return [Path(self.temp_dir.name) / value for value in metadata.get("distilled_paths", [])]

    def test_real_intake_rewrite_preserves_lineage_and_retires_output(self) -> None:
        note = akb.create_session_intake(source="test", title="会话", content="旧正文", project="demo", session_id="same-session")
        distiller.enqueue(note)
        queued = self.queue_file()
        with patch.object(distiller, "request_model", return_value=self.keep_result("旧标题", "原生重写主题旧")):
            distiller.process_entry(queued, distiller.load_settings())
        old = self.distilled_paths(note)[0]
        same = akb.create_session_intake(source="test", title="会话", content="新正文", project="demo", session_id="same-session")
        self.assertEqual(same, note)
        self.assertEqual(self.distilled_paths(note), [old])
        distiller.enqueue(note)
        with patch.object(distiller, "request_model", return_value=self.keep_result("新标题", "原生重写主题新")):
            distiller.process_entry(self.queue_file(), distiller.load_settings())
        self.assertEqual(akb.read_note(old)[0]["status"], "superseded")
        self.assertEqual(len(akb.search_notes("原生重写主题")), 1)

    def test_same_input_enqueue_recovers_pending_publication(self) -> None:
        note = self.intake()
        distiller.enqueue(note)
        queued = self.queue_file()
        with patch.object(distiller, "request_model", return_value=self.keep_result("待发布")):
            with patch.object(distiller, "_activate_outputs", side_effect=RuntimeError("中断激活")):
                distiller.process_entry(queued, distiller.load_settings())
        metadata, _ = akb.read_note(note)
        self.assertIn("distillation_publication", metadata)
        result = distiller.enqueue(note)
        self.assertFalse(result["queued"])
        self.assertNotIn("distillation_publication", akb.read_note(note)[0])
        self.assertEqual(akb.read_note(note)[0]["status"], "distilled")

    def test_scope_change_during_model_request_invalidates_claim(self) -> None:
        note = self.intake()
        distiller.enqueue(note)
        queued = self.queue_file()

        def change_scope(*args):
            akb.update_note_metadata(note, {"project_path": "/work/private/demo", "scope": "project"})
            distiller.enqueue(note)
            return self.keep_result("旧范围输出")

        with patch.object(distiller, "request_model", side_effect=change_scope):
            self.assertFalse(distiller.process_entry(queued, distiller.load_settings()))
        self.assertEqual(self.distilled_paths(note), [])
        with patch.object(distiller, "request_model", return_value=self.keep_result("新范围输出")):
            self.assertTrue(distiller.process_entry(queued, distiller.load_settings()))
        self.assertEqual(akb.read_note(self.distilled_paths(note)[0])[0]["project_path"], "/work/private/demo")

    def test_legacy_output_is_retained_without_blocking_new_publication(self) -> None:
        note = self.intake()
        legacy = akb.capture_note(source="gemini-distiller", title="旧笔记", summary="legacy-only-marker", metadata_extra={"source_session": str(note.relative_to(akb.vault_path()))})
        before = legacy.read_bytes()
        akb.update_note_metadata(note, {"distilled_paths": [str(legacy.relative_to(akb.vault_path()))]})
        distiller.enqueue(note)
        with patch.object(distiller, "request_model", return_value=self.keep_result("新笔记", "new-only-marker")):
            self.assertTrue(distiller.process_entry(self.queue_file(), distiller.load_settings()))
        self.assertEqual(legacy.read_bytes(), before)
        self.assertNotIn(legacy, [row["path"] for row in akb.search_notes("legacy-only-marker")])
        self.assertEqual(len(akb.search_notes("new-only-marker")), 1)

    def test_duplicate_enqueue_preserves_same_version_retry_state_and_success_deduplicates(self) -> None:
        note = self.intake()
        self.assertTrue(distiller.enqueue(note)["queued"])
        queued = self.queue_file()
        original = self.read_job(queued)
        self.assertIsNotNone(original)
        assert original is not None
        original["attempts"] = 2
        original["not_before"] = 9876543210
        self.put_job(queued, original)

        self.assertTrue(distiller.enqueue(note)["queued"])
        merged = self.read_job(queued)
        self.assertIsNotNone(merged)
        assert merged is not None
        self.assertEqual(merged["input_hash"], original["input_hash"])
        self.assertEqual(merged["attempts"], 2)
        self.assertEqual(merged["not_before"], 9876543210)

        self.make_ready(queued)
        with patch.object(distiller, "request_model", return_value=self.no_value_result()) as model:
            self.assertTrue(distiller.process_entry(queued, distiller.load_settings()))
        self.assertEqual(model.call_count, 1)
        self.assertFalse(bool(self.read_job(queued)))

        duplicate = distiller.enqueue(note)
        self.assertFalse(duplicate["queued"])
        self.assertEqual(duplicate["reason"], "already_distilled")
        self.assertFalse((self.state_dir / "queue").exists() and list((self.state_dir / "queue").glob("*.json")))

    def test_changed_body_creates_new_version_and_reprocesses(self) -> None:
        note = self.intake("初始输入")
        distiller.enqueue(note)
        first_queue = self.queue_file()
        self.make_ready(first_queue)
        with patch.object(distiller, "request_model", return_value=self.no_value_result()) as model:
            self.assertTrue(distiller.process_entry(first_queue, distiller.load_settings()))
        self.assertEqual(model.call_count, 1)
        first_metadata, _ = akb.read_note(note)
        first_hash = first_metadata["distilled_input_hash"]

        self.replace_body(note, "更新后的独立正文")
        self.assertTrue(distiller.enqueue(note)["queued"])
        second_queue = self.queue_file()
        second_entry = self.read_job(second_queue)
        self.assertIsNotNone(second_entry)
        assert second_entry is not None
        self.assertNotEqual(second_entry["input_hash"], first_hash)
        self.assertEqual(second_entry["attempts"], 0)
        second_metadata, _ = akb.read_note(note)
        self.assertEqual(second_metadata["status"], "needs-distillation")

        self.make_ready(second_queue)
        with patch.object(distiller, "request_model", return_value=self.no_value_result()) as model:
            self.assertTrue(distiller.process_entry(second_queue, distiller.load_settings()))
        self.assertEqual(model.call_count, 1)
        final_metadata, _ = akb.read_note(note)
        self.assertEqual(final_metadata["status"], "distilled-no-value")
        self.assertEqual(final_metadata["distilled_input_hash"], second_entry["input_hash"])

    def test_versioned_outputs_ignore_title_and_only_current_version_is_recalled(self) -> None:
        note = self.intake("第一个输入")
        distiller.enqueue(note)
        first_queue = self.queue_file()
        self.make_ready(first_queue)
        with patch.object(
            distiller,
            "request_model",
            return_value=self.keep_result("旧标题", "仅旧版本和新版本共有的主题"),
        ):
            self.assertTrue(distiller.process_entry(first_queue, distiller.load_settings()))
        first_paths = self.distilled_paths(note)
        self.assertEqual(len(first_paths), 1)
        first_path = first_paths[0]
        first_metadata, _ = akb.read_note(first_path)
        first_hash = first_metadata["source_input_hash"]
        self.assertNotIn("旧标题", first_path.name)

        self.replace_body(note, "第二个输入")
        distiller.enqueue(note)
        second_queue = self.queue_file()
        self.make_ready(second_queue)
        with patch.object(
            distiller,
            "request_model",
            return_value=self.keep_result("新标题", "仅当前版本召回标记"),
        ):
            self.assertTrue(distiller.process_entry(second_queue, distiller.load_settings()))

        second_paths = self.distilled_paths(note)
        self.assertEqual(len(second_paths), 1)
        second_path = second_paths[0]
        second_metadata, _ = akb.read_note(second_path)
        self.assertNotEqual(first_path, second_path)
        self.assertNotEqual(first_hash, second_metadata["source_input_hash"])
        self.assertNotIn("新标题", second_path.name)
        self.assertEqual(akb.read_note(first_path)[0]["status"], "superseded")
        self.assertEqual(second_metadata["status"], "active")
        recalled = akb.search_notes("仅当前版本召回标记", project="demo")
        self.assertEqual([row["path"].resolve() for row in recalled], [second_path.resolve()])

    def test_keep_false_retires_previous_outputs_from_retrieval(self) -> None:
        note = self.intake("值得沉淀的输入")
        distiller.enqueue(note)
        first_queue = self.queue_file()
        self.make_ready(first_queue)
        with patch.object(
            distiller,
            "request_model",
            return_value=self.keep_result("旧输出", "keep false 后不应召回"),
        ):
            self.assertTrue(distiller.process_entry(first_queue, distiller.load_settings()))
        old_path = self.distilled_paths(note)[0]

        self.replace_body(note, "后续内容没有长期价值")
        distiller.enqueue(note)
        second_queue = self.queue_file()
        self.make_ready(second_queue)
        with patch.object(distiller, "request_model", return_value=self.no_value_result()):
            self.assertTrue(distiller.process_entry(second_queue, distiller.load_settings()))

        metadata, _ = akb.read_note(note)
        self.assertEqual(metadata["status"], "distilled-no-value")
        self.assertEqual(metadata["distilled_paths"], [])
        self.assertEqual(akb.read_note(old_path)[0]["status"], "superseded")
        self.assertEqual(akb.search_notes("keep false 后不应召回", project="demo"), [])

    def test_modified_previous_output_is_preserved_and_reports_failure(self) -> None:
        note = self.intake("初始版本")
        distiller.enqueue(note)
        first_queue = self.queue_file()
        self.make_ready(first_queue)
        with patch.object(
            distiller,
            "request_model",
            return_value=self.keep_result("受保护输出", "人工编辑保护标记"),
        ):
            self.assertTrue(distiller.process_entry(first_queue, distiller.load_settings()))
        old_path = self.distilled_paths(note)[0]
        with akb.note_lock(old_path):
            old_metadata, old_body = akb.read_note(old_path)
            self.assertEqual(old_metadata["managed_by"], distiller.MANAGED_BY)
            self.assertEqual(old_metadata["content_hash"], distiller.input_hash(old_body))
            self.assertEqual(
                old_metadata["metadata_fingerprint"],
                distiller._metadata_fingerprint(old_metadata),
            )
            old_metadata["title"] = "人工覆盖的标题"
            akb.atomic_write_text(
                old_path,
                akb.render_frontmatter(old_metadata) + old_body.rstrip() + "\n\n人工补充。\n",
            )

        self.replace_body(note, "需要重新提炼的后续版本")
        distiller.enqueue(note)
        queued = self.queue_file()
        self.make_ready(queued)
        with patch.object(
            distiller,
            "request_model",
            return_value=self.keep_result("新输出", "新版本标记"),
        ):
            self.assertTrue(distiller.process_entry(queued, distiller.load_settings()))

        preserved_metadata, preserved_body = akb.read_note(old_path)
        intake_metadata, _ = akb.read_note(note)
        self.assertEqual(preserved_metadata["status"], "active")
        self.assertEqual(preserved_metadata["title"], "人工覆盖的标题")
        self.assertIn("人工补充。", preserved_body)
        self.assertEqual(intake_metadata["status"], "distillation-failed")
        self.assertEqual(len(self.jobs("failed")), 1)

    def test_outputs_preserve_project_path_project_key_and_scope(self) -> None:
        note = self.intake("项目范围的输入")
        akb.update_note_metadata(
            note,
            {
                "project_path": "/workspaces/demo",
                "project_key": "demo-key",
                "scope": "project",
            },
        )
        distiller.enqueue(note)
        queued = self.queue_file()
        self.make_ready(queued)
        with patch.object(
            distiller,
            "request_model",
            return_value=self.keep_result("项目路径继承", "项目路径继承标记"),
        ):
            self.assertTrue(distiller.process_entry(queued, distiller.load_settings()))

        output_metadata, _ = akb.read_note(self.distilled_paths(note)[0])
        self.assertEqual(output_metadata["project_path"], "/workspaces/demo")
        self.assertEqual(output_metadata["project_key"], "demo-key")
        self.assertEqual(output_metadata["scope"], "project")

    def test_pending_output_is_not_recalled_before_publication(self) -> None:
        note = self.intake("发布中断前的输入")
        metadata, body = akb.read_note(note)
        pending = distiller.distilled_notes(
            note,
            self.keep_result("待发布输出", "待发布不可召回标记"),
            distiller.load_settings(),
            input_metadata=metadata,
            source_hash=distiller.input_hash(body),
        )

        pending_metadata, _ = akb.read_note(pending[0])
        self.assertEqual(pending_metadata["status"], "pending")
        self.assertEqual(pending_metadata["type"], "session-evidence")
        self.assertEqual(akb.search_notes("待发布不可召回标记", project="demo"), [])

    def test_interrupted_publication_leaves_no_active_new_output_and_recovers(self) -> None:
        note = self.intake("初版输入")
        distiller.enqueue(note)
        first_queue = self.queue_file()
        self.make_ready(first_queue)
        with patch.object(
            distiller,
            "request_model",
            return_value=self.keep_result("初版输出", "旧版本中断标记"),
        ):
            self.assertTrue(distiller.process_entry(first_queue, distiller.load_settings()))
        old_path = self.distilled_paths(note)[0]

        self.replace_body(note, "新版本输入")
        distiller.enqueue(note)
        queued = self.queue_file()
        self.make_ready(queued)
        with patch.object(
            distiller,
            "request_model",
            return_value=self.keep_result("新版本输出", "中断后待发布标记"),
        ):
            with patch.object(distiller, "_activate_outputs", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    distiller.process_entry(queued, distiller.load_settings())

        interrupted_metadata, _ = akb.read_note(note)
        pending_path = self.distilled_paths(note)[0]
        self.assertEqual(interrupted_metadata["status"], "distilled")
        self.assertIn("distillation_publication", interrupted_metadata)
        self.assertEqual(akb.read_note(old_path)[0]["status"], "superseded")
        self.assertEqual(akb.read_note(pending_path)[0]["status"], "pending")
        self.assertEqual(akb.search_notes("中断后待发布标记", project="demo"), [])

        with patch.object(distiller, "request_model") as model:
            self.assertTrue(distiller.process_entry(queued, distiller.load_settings()))
        self.assertEqual(model.call_count, 0)
        recovered_metadata, _ = akb.read_note(note)
        self.assertNotIn("distillation_publication", recovered_metadata)
        self.assertEqual(akb.read_note(pending_path)[0]["status"], "active")

    def test_model_receives_claimed_snapshot_not_later_note_contents(self) -> None:
        note = self.intake("模型只能看到这个认领版本")
        distiller.enqueue(note)
        queued = self.queue_file()
        self.make_ready(queued)
        seen: dict[str, object] = {}

        def model(_note: Path, model_settings: dict[str, object]) -> dict[str, object]:
            claimed = model_settings.get("_claimed_input_snapshot")
            self.assertIsInstance(claimed, dict)
            assert isinstance(claimed, dict)
            seen.update(claimed)
            self.replace_body(note, "模型不应看到这个后续版本")
            return self.no_value_result()

        with patch.object(distiller, "request_model", side_effect=model):
            self.assertFalse(distiller.process_entry(queued, distiller.load_settings()))

        self.assertIn("模型只能看到这个认领版本", str(seen["body"]))
        self.assertNotIn("模型不应看到这个后续版本", str(seen["body"]))
        self.assertTrue(bool(self.read_job(queued)))

    def test_note_locked_writer_does_not_lose_body_during_final_commit(self) -> None:
        note = self.intake("等待并发写入")
        distiller.enqueue(note)
        queued = self.queue_file()
        self.make_ready(queued)
        model_started = threading.Event()
        release_model = threading.Event()
        writer_holding_lock = threading.Event()
        release_writer = threading.Event()
        completed: list[bool] = []

        def model(*_args: object, **_kwargs: object) -> dict[str, object]:
            model_started.set()
            self.assertTrue(release_model.wait(timeout=3))
            return self.no_value_result()

        def writer() -> None:
            with akb.note_lock(note):
                metadata, _ = akb.read_note(note)
                writer_holding_lock.set()
                self.assertTrue(release_writer.wait(timeout=3))
                akb.atomic_write_text(
                    note,
                    akb.render_frontmatter(metadata) + "锁保护的新正文\n",
                )

        with patch.object(distiller, "request_model", side_effect=model):
            worker_thread = threading.Thread(
                target=lambda: completed.append(distiller.process_entry(queued, distiller.load_settings()))
            )
            worker_thread.start()
            self.assertTrue(model_started.wait(timeout=3))
            writer_thread = threading.Thread(target=writer)
            writer_thread.start()
            self.assertTrue(writer_holding_lock.wait(timeout=3))
            release_model.set()
            release_writer.set()
            writer_thread.join(timeout=3)
            worker_thread.join(timeout=3)

        self.assertFalse(writer_thread.is_alive())
        self.assertFalse(worker_thread.is_alive())
        self.assertEqual(completed, [False])
        metadata, body = akb.read_note(note)
        self.assertEqual(body.strip(), "锁保护的新正文")
        self.assertEqual(metadata["status"], "needs-distillation")
        current = self.read_job(queued)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current["input_hash"], distiller.input_hash(body))

    def test_inflight_body_update_keeps_new_queue_and_discards_old_result(self) -> None:
        note = self.intake("旧正文")
        distiller.enqueue(note)
        queued = self.queue_file()
        self.make_ready(queued)
        started = threading.Event()
        release = threading.Event()
        completed: list[bool] = []

        def slow_model(*_args: object, **_kwargs: object) -> dict[str, object]:
            started.set()
            self.assertTrue(release.wait(timeout=3))
            return self.no_value_result()

        with patch.object(distiller, "request_model", side_effect=slow_model):
            thread = threading.Thread(
                target=lambda: completed.append(distiller.process_entry(queued, distiller.load_settings()))
            )
            thread.start()
            self.assertTrue(started.wait(timeout=3))
            self.replace_body(note, "新正文")
            self.assertTrue(distiller.enqueue(note)["queued"])
            replacement = self.read_job(queued)
            self.assertIsNotNone(replacement)
            assert replacement is not None
            release.set()
            thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        self.assertEqual(completed, [False])
        metadata, body = akb.read_note(note)
        self.assertEqual(metadata["status"], "needs-distillation")
        self.assertEqual(replacement["input_hash"], distiller.input_hash(body))
        self.assertTrue(bool(self.read_job(queued)))

        self.make_ready(queued)
        with patch.object(distiller, "request_model", return_value=self.no_value_result()) as model:
            self.assertTrue(distiller.process_entry(queued, distiller.load_settings()))
        self.assertEqual(model.call_count, 1)

    def test_terminal_failures_use_one_file_per_input_version_and_report_duplicates(self) -> None:
        note = self.intake()
        distiller.enqueue(note)
        queued = self.queue_file()
        entry = self.read_job(queued)
        self.assertIsNotNone(entry)
        assert entry is not None
        self.make_ready(queued)
        failure = distiller.DistillerError("结果格式无效")
        with patch.object(distiller, "request_model", side_effect=failure):
            self.assertTrue(distiller.process_entry(queued, distiller.load_settings()))

        failed, failed_entry = self.jobs("failed")[0]
        self.assertEqual(failed, distiller._failed_key(note, entry["input_hash"], entry["input_context_hash"]))
        self.assertEqual(failed_entry["last_error"], "结果格式无效")
        self.assertTrue(failed_entry["failed_at"])
        self.put_job("legacy-duplicate", failed_entry, "failed")
        status = distiller.collect_status()
        self.assertEqual(status["unique_failed_inputs"], 1)
        self.assertEqual(status["duplicate_failed_entries"], 1)
        self.assertFalse(status["worker_alive"])

        retried = distiller.retry_failed()
        self.assertEqual(retried["moved"], 1)
        self.assertEqual(retried["failed"], 0)
        retried_queue = self.queue_file()
        self.make_ready(retried_queue)
        with patch.object(distiller, "request_model", side_effect=failure):
            self.assertTrue(distiller.process_entry(retried_queue, distiller.load_settings()))
        self.assertEqual(len(self.jobs("failed")), 1)
        self.assertEqual(self.jobs("failed")[0][0], failed)

    def test_retry_failed_skips_a_stale_input_version(self) -> None:
        note = self.intake("失败版本")
        distiller.enqueue(note)
        queued = self.queue_file()
        self.make_ready(queued)
        with patch.object(
            distiller,
            "request_model",
            side_effect=distiller.DistillerError("结果格式无效"),
        ):
            self.assertTrue(distiller.process_entry(queued, distiller.load_settings()))

        self.replace_body(note, "后续新版本")
        result = distiller.retry_failed()
        self.assertEqual(result["moved"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["queue"], 0)

    def test_legacy_queue_entry_without_input_hash_is_processed(self) -> None:
        note = self.intake()
        queue_dir = self.state_dir / "queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        queued = queue_dir / f"{distiller.queue_key(note)}.json"
        distiller.atomic_write_json(
            queued,
            {
                "note": str(note),
                "queued_at": "2026-01-01T00:00:00",
                "not_before": 0,
                "attempts": 3,
            },
        )

        distiller._migrate_queue(distiller.load_settings())
        with patch.object(distiller, "request_model", return_value=self.no_value_result()) as model:
            self.assertTrue(distiller.process_entry(queued.stem, distiller.load_settings()))
        self.assertTrue(queued.exists())
        self.assertEqual(model.call_count, 1)
        metadata, body = akb.read_note(note)
        self.assertEqual(metadata["status"], "distilled-no-value")
        self.assertEqual(metadata["distilled_input_hash"], distiller.input_hash(body))


if __name__ == "__main__":
    unittest.main()
