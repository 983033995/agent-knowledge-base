from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import akb
import memory_sync


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / ".qoder" / "memory").mkdir(parents=True)
        (self.home / ".codex" / "memories").mkdir(parents=True)
        self.env = patch.dict(os.environ, {
            "AKB_VAULT": str(self.vault),
            "AKB_SYNC_STATE_DIR": str(self.root / "state"),
            "AKB_SEARCH_INDEX": str(self.root / "index.json"),
            "AKB_DISTILLER_AUTOSTART": "0",
        })
        self.env.start()
        self.module = types.ModuleType("memory_sources")
        self.module.SourceReadError = ValueError
        self.items: list[dict] = []
        self.records: dict[str, list[dict]] = {}
        self.module.discover_sources = lambda **kwargs: [
            item for item in self.items
            if kwargs["source"] in {"all", item["source"]}
            and kwargs["kind"] in {"all", item["kind"]}
        ]
        self.module.read_source = lambda item: self.records[item["path"]]
        self.modules = patch.dict(sys.modules, {"memory_sources": self.module})
        self.modules.start()
        akb._CONFIG_CACHE = None
        akb._CONFIG_CACHE_KEY = None
        akb._SEARCH_INDEX_MEM = None
        akb._SEARCH_INDEX_MEM_KEY = None

    def tearDown(self) -> None:
        self.modules.stop()
        self.env.stop()
        akb._CONFIG_CACHE = None
        akb._CONFIG_CACHE_KEY = None
        akb._SEARCH_INDEX_MEM = None
        akb._SEARCH_INDEX_MEM_KEY = None
        self.temp.cleanup()

    def add(self, source: str = "qoder", content: str = "偏好使用中文", title: str = "语言偏好", project_path: str = "") -> dict:
        directory = ".qoder/memory" if source == "qoder" else ".codex/memories"
        path = self.home / directory / f"record-{len(self.items)}.md"
        path.write_text(content, encoding="utf-8")
        item = {"source": source, "kind": "memory", "path": str(path), "format": "markdown"}
        record = {
            "source": source, "kind": "memory", "source_path": str(path), "record_id": "memory",
            "title": title, "content": content, "project": Path(project_path).name if project_path else "",
            "project_path": project_path, "memory_type": "feedback", "metadata": {},
        }
        self.items.append(item)
        self.records[str(path)] = [record]
        return record

    def change(self, record: dict, content: str) -> None:
        record["content"] = content
        Path(record["source_path"]).write_text(content, encoding="utf-8")

    def sync(self, **kwargs) -> dict:
        return memory_sync.sync_sources(home=self.home, **kwargs)

    def notes(self) -> list[Path]:
        return list(self.vault.rglob("*.md")) if self.vault.exists() else []

    def test_dry_run_writes_nothing(self) -> None:
        self.add()
        result = self.sync()
        self.assertEqual(result["stats"]["imported"], 1)
        self.assertEqual(result["cloud_requests"], 0)
        self.assertFalse(self.vault.exists())
        self.assertFalse((self.root / "state").exists())

    def test_apply_is_idempotent_and_preserves_source(self) -> None:
        record = self.add()
        source_before = Path(record["source_path"]).read_bytes()
        self.sync(apply=True)
        note = self.notes()[0]
        before = note.read_bytes()
        result = self.sync(apply=True)
        self.assertEqual(result["stats"]["unchanged"], 1)
        self.assertEqual(note.read_bytes(), before)
        self.assertEqual(Path(record["source_path"]).read_bytes(), source_before)
        self.assertEqual(len(self.notes()), 1)

    def test_same_content_across_agents_shares_note(self) -> None:
        self.add()
        self.add(source="codex")
        result = self.sync(apply=True)
        self.assertEqual(result["stats"]["deduplicated"], 1)
        self.assertEqual(len(self.notes()), 1)
        metadata, _ = akb.read_note(self.notes()[0])
        self.assertEqual(metadata["sources"], ["codex", "qoder"])
        self.assertEqual(metadata["source_count"], 2)

    def test_content_changes_supersede_previous_version(self) -> None:
        record = self.add()
        self.sync(apply=True)
        self.change(record, "偏好中文并简短回复")
        result = self.sync(apply=True)
        self.assertEqual(result["stats"]["updated"], 1)
        statuses = [akb.read_note(path)[0]["status"] for path in self.notes()]
        self.assertCountEqual(statuses, ["active", "superseded"])

    def test_revoking_one_source_preserves_other(self) -> None:
        self.add()
        self.add(source="codex")
        self.sync(apply=True)
        removed = self.items.pop(0)
        Path(removed["path"]).unlink()
        self.sync(apply=True)
        metadata, _ = akb.read_note(self.notes()[0])
        self.assertEqual(metadata["status"], "active")
        self.assertEqual(metadata["sources"], ["codex"])

    def test_deleted_last_source_revokes_note(self) -> None:
        record = self.add()
        self.sync(apply=True)
        Path(record["source_path"]).unlink()
        self.items.clear()
        self.sync(apply=True)
        self.assertEqual(akb.read_note(self.notes()[0])[0]["status"], "revoked")

    def test_unavailable_runtime_does_not_revoke(self) -> None:
        self.add()
        self.sync(apply=True)
        self.items.clear()
        with patch.object(memory_sync, "_source_root_available", return_value=False):
            self.sync(apply=True)
        self.assertEqual(akb.read_note(self.notes()[0])[0]["status"], "active")

    def test_partial_scan_does_not_revoke_unseen(self) -> None:
        self.add(content="第一条")
        self.add(content="第二条")
        self.sync(apply=True)
        result = self.sync(apply=True, limit=1)
        self.assertTrue(result["partial"])
        self.assertTrue(all(akb.read_note(path)[0]["status"] == "active" for path in self.notes()))

    def test_read_error_keeps_previous_version(self) -> None:
        self.add()
        self.sync(apply=True)
        record = next(iter(self.records.values()))[0]
        self.change(record, "改变后格式损坏")
        self.module.read_source = lambda item: (_ for _ in ()).throw(ValueError("格式损坏"))
        result = self.sync(apply=True)
        self.assertEqual(result["stats"]["failed_files"], 1)
        self.assertEqual(akb.read_note(self.notes()[0])[0]["status"], "active")

    def test_project_paths_with_same_basename_stay_separate(self) -> None:
        self.add(project_path="/work/one/demo")
        self.add(project_path="/work/two/demo")
        self.sync(apply=True)
        self.assertEqual(len(self.notes()), 2)

    def test_conflicting_titles_are_not_automatically_merged(self) -> None:
        self.add(content="中文")
        self.add(source="codex", content="英文")
        result = self.sync(apply=True)
        self.assertEqual(result["stats"]["conflict_candidates"], 2)
        for path in self.notes():
            metadata, _ = akb.read_note(path)
            self.assertEqual(metadata["status"], "active")
            self.assertEqual(len(metadata["conflict_candidates"]), 1)

    def test_indexes_are_not_imported_as_facts(self) -> None:
        record = self.add()
        record["memory_type"] = "index"
        result = self.sync(apply=True)
        self.assertEqual(result["stats"]["skipped_indexes"], 1)
        self.assertEqual(self.notes(), [])

    def test_secrets_are_redacted_before_persisting(self) -> None:
        self.add(content="api_key=never-store-this-secret")
        self.sync(apply=True)
        text = self.notes()[0].read_text(encoding="utf-8")
        self.assertNotIn("never-store-this-secret", text)
        self.assertIn("REDACTED", text)
        manifest = json.dumps(memory_sync._load_state(), ensure_ascii=False)
        self.assertNotIn("never-store-this-secret", manifest)
        self.assertFalse((self.root / "state/manifest.json").exists())

    def test_human_edit_aborts_without_overwrite(self) -> None:
        record = self.add()
        self.sync(apply=True)
        path = self.notes()[0]
        path.write_text(path.read_text(encoding="utf-8") + "人工修改\n", encoding="utf-8")
        before = path.read_bytes()
        self.change(record, "新内容")
        with self.assertRaisesRegex(ValueError, "人工修改"):
            self.sync(apply=True)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.notes()), 1)

    def test_corrupt_manifest_is_not_silently_overwritten(self) -> None:
        self.add()
        state = self.root / "state"
        state.mkdir()
        manifest = state / "manifest.json"
        manifest.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "清单无法读取"):
            self.sync(apply=True)
        self.assertEqual(manifest.read_text(encoding="utf-8"), "{broken")

    def test_source_selection_does_not_revoke_other_agents(self) -> None:
        self.add()
        self.add(source="codex", content="另一条内容")
        self.sync(apply=True)
        self.sync(apply=True, source="qoder")
        self.assertTrue(all(akb.read_note(path)[0]["status"] == "active" for path in self.notes()))

    def test_unchanged_sources_skip_parsing(self) -> None:
        self.add()
        self.sync(apply=True)
        with patch.object(self.module, "read_source", side_effect=AssertionError("不应重读")):
            result = self.sync(apply=True)
        self.assertEqual(result["stats"]["unchanged_files"], 1)

    def test_retry_recovers_after_note_written_before_manifest(self) -> None:
        self.add()
        from state_store import Transaction

        with patch.object(Transaction, "put_sync_state", side_effect=OSError("模拟数据库提交中断")):
            with self.assertRaises(OSError):
                self.sync(apply=True)
        self.assertEqual(len(self.notes()), 1)
        self.sync(apply=True)
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(memory_sync.sync_status()["records"], 1)

    def test_different_home_cannot_revoke_original_sources(self) -> None:
        self.add()
        self.sync(apply=True)
        other = self.root / "other-home"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "Home"):
            memory_sync.sync_sources(home=other, apply=True)
        self.assertEqual(akb.read_note(self.notes()[0])[0]["status"], "active")

    def test_incomplete_discovery_does_not_revoke_existing_source(self) -> None:
        self.add()
        self.sync(apply=True)
        self.items.clear()
        self.sync(apply=True)
        self.assertEqual(akb.read_note(self.notes()[0])[0]["status"], "active")

    def test_frontmatter_edit_is_protected(self) -> None:
        self.add()
        self.sync(apply=True)
        path = self.notes()[0]
        akb.update_note_metadata(path, {"status": "ignored"})
        with self.assertRaisesRegex(ValueError, "人工修改"):
            self.sync(apply=True)
        self.assertEqual(akb.read_note(path)[0]["status"], "ignored")

    def test_interrupted_publish_does_not_recall_orphan_and_recovers_deletion(self) -> None:
        record = self.add()
        self.sync(apply=True)
        self.change(record, "新版本只有这里有独特词")
        from state_store import Transaction

        with patch.object(Transaction, "put_sync_state", side_effect=OSError("模拟数据库发布中断")):
            with self.assertRaises(OSError):
                self.sync(apply=True)
        self.assertEqual(akb.search_notes("独特词"), [])
        Path(record["source_path"]).unlink()
        self.items.clear()
        self.sync(apply=True)
        self.assertEqual(akb.search_notes("独特词"), [])
        self.assertTrue(all(akb.read_note(path)[0]["status"] in {"superseded", "revoked"} for path in self.notes()))

    def test_json_shaped_secrets_are_redacted(self) -> None:
        self.add(content='{"api_key": "fixture-private-secret", "password": "fixture-password"}')
        self.sync(apply=True)
        text = self.notes()[0].read_text(encoding="utf-8")
        self.assertNotIn("fixture-private-secret", text)
        self.assertNotIn("fixture-password", text)

    def test_multiple_cursor_composers_share_file_but_not_scan_identity(self) -> None:
        database = self.home / ".qoder/memory/session.db"
        database.write_text("fixture", encoding="utf-8")
        for sid in ("one", "two"):
            self.items.append({"source": "qoder", "kind": "session", "path": str(database), "format": "cursor-sqlite", "composer_id": sid})
        self.module.read_source = lambda item: [{
            "source": "qoder", "kind": "session", "source_path": str(database), "record_id": item["composer_id"],
            "title": "会话", "content": "user: " + item["composer_id"], "memory_type": "session",
        }]
        first = self.sync(apply=True, kind="session")
        self.assertEqual(first["stats"]["imported"], 2)
        self.assertEqual(first["stats"]["active_notes"], 2)
        second = self.sync(apply=True, kind="session")
        self.assertEqual(second["stats"]["unchanged"], 2)
        self.assertEqual(second["stats"]["active_notes"], 2)

    def test_native_quarantine_and_archive_never_enter_default_recall(self) -> None:
        first = self.add(content="隔离独特中文内容")
        first["metadata"] = {"quarantine_reason": "restricted_agent_scope"}
        second = self.add(content="归档独特中文内容")
        second["metadata"] = {"status": "revoked"}
        self.sync(apply=True)
        self.assertEqual(akb.search_notes("独特中文内容"), [])
        self.assertEqual(len(akb.search_notes("独特中文内容", include_inactive=True)), 2)
        self.assertCountEqual([akb.read_note(path)[0]["status"] for path in self.notes()], ["conflict", "revoked"])
        self.assertEqual(self.sync(apply=True)["stats"]["unchanged"], 2)

    def test_deleted_source_can_be_restored_without_touching_timestamp(self) -> None:
        record = self.add()
        item = dict(self.items[0])
        self.sync(apply=True)
        self.items.clear()
        Path(record["source_path"]).unlink()
        self.sync(apply=True)
        self.items.append(item)
        Path(record["source_path"]).write_text(record["content"], encoding="utf-8")
        self.sync(apply=True)
        self.assertEqual(akb.read_note(self.notes()[0])[0]["status"], "active")

    def test_deleted_cursor_composer_is_revoked_while_database_exists(self) -> None:
        import sqlite3
        database = self.home / ".qoder/memory/session.db"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
        connection.executemany("INSERT INTO cursorDiskKV VALUES (?, '{}')", [("composerData:one",), ("composerData:two",)])
        connection.commit()
        for sid in ("one", "two"):
            self.items.append({"source": "qoder", "kind": "session", "path": str(database), "format": "cursor-sqlite", "composer_id": sid})
        self.module.read_source = lambda item: [{
            "source": "qoder", "kind": "session", "source_path": str(database), "record_id": item["composer_id"],
            "title": "会话", "content": "user: " + item["composer_id"], "memory_type": "session",
        }]
        self.sync(apply=True, kind="session")
        self.items.pop()
        connection.execute("DELETE FROM cursorDiskKV WHERE key = 'composerData:two'")
        connection.commit()
        connection.close()
        self.sync(apply=True, kind="session")
        self.assertCountEqual([akb.read_note(path)[0]["status"] for path in self.notes()], ["active", "revoked"])

    def test_reading_mutating_source_keeps_previous_state(self) -> None:
        record = self.add()
        self.sync(apply=True)
        self.change(record, "开始更新")

        def mutate(item):
            self.change(record, "读取期间再次变动")
            return [record]

        self.module.read_source = mutate
        result = self.sync(apply=True)
        self.assertEqual(result["stats"]["failed_files"], 1)
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(akb.read_note(self.notes()[0])[0]["status"], "active")


if __name__ == "__main__":
    unittest.main()
