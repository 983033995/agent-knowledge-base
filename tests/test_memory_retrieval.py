from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import akb
import mcp_server


class RetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / "config.json"
        self.config.write_text(json.dumps({
            "vault_path": str(Path(self.temp.name) / "vault"),
            "auto_discover_obsidian": False,
            "session_directory": "00-Inbox/_agent-sessions",
            "knowledge_directory": "10-技术笔记",
            "patterns_directory": "10-技术笔记/patterns",
            "decisions_directory": "10-技术笔记",
            "managed_directories": ["00-Inbox/_agent-sessions", "10-技术笔记", "10-技术笔记/patterns"],
            "search_all_markdown": False,
            "search_directories": ["00-Inbox/_agent-sessions", "10-技术笔记"],
            "search_exclude_directories": [],
        }), encoding="utf-8")
        self.env = patch.dict(os.environ, {
            "AKB_CONFIG": str(self.config),
            "AKB_VAULT": self.temp.name,
            "AKB_SEARCH_INDEX": str(Path(self.temp.name) / "index.json"),
            "AKB_DISTILLER_AUTOSTART": "0",
        })
        self.env.start()
        akb._CONFIG_CACHE = None
        akb._CONFIG_CACHE_KEY = None
        akb._SEARCH_INDEX_MEM = None
        akb._SEARCH_INDEX_MEM_KEY = None

    def tearDown(self) -> None:
        self.env.stop()
        akb._CONFIG_CACHE = None
        akb._CONFIG_CACHE_KEY = None
        akb._SEARCH_INDEX_MEM = None
        akb._SEARCH_INDEX_MEM_KEY = None
        self.temp.cleanup()

    def note(self, filename: str, body: str = "数据库迁移知识", **metadata) -> Path:
        return akb.write_note(akb.knowledge_directory(), filename, {
            "title": filename, "type": "knowledge", "project": "", **metadata,
        }, body)

    def test_nonmatching_query_does_not_return_directory_bonus(self) -> None:
        self.note("apple.md", "苹果种植与浇水")
        self.assertEqual(akb.search_notes("不存在的故障词XYZ"), [])

    def test_ordered_cjk_fragments_recall_long_query_without_broad_overlap(self) -> None:
        self.note("retry.md", "SQLite distiller 队列 worker 临时连接错误 exponential backoff retry")
        self.assertEqual(len(akb.search_notes("连接错误临时")), 1)
        self.assertEqual(akb.search_notes("连接火箭味觉"), [])
        self.assertEqual(akb.search_notes("qzxvplmnr zzqmxvk xyzzqw"), [])

    def test_project_name_is_not_a_substring_search(self) -> None:
        self.note("a.md", project="alpha", body="数据库迁移；引用过 beta 项目")
        self.assertEqual(akb.search_notes("数据库", project="beta"), [])

    def test_project_absolute_path_distinguishes_same_basename(self) -> None:
        first = self.note("a.md", project="demo", project_path="/one/demo")
        self.note("b.md", project="demo", project_path="/two/demo")
        rows = akb.search_notes("数据库", project="/one/demo")
        self.assertEqual([row["path"] for row in rows], [first])

    def test_user_memory_is_reusable_inside_project_scope(self) -> None:
        self.note("preference.md", body="用户偏好数据库操作先备份", memory_type="feedback", scope="user")
        self.assertEqual(len(akb.search_notes("数据库", project="demo")), 1)

    def test_inactive_versions_are_not_recalled_by_default(self) -> None:
        self.note("old.md", status="superseded")
        self.note("gone.md", status="revoked")
        self.note("active.md", status="active")
        self.assertEqual(len(akb.search_notes("数据库")), 1)
        self.assertEqual(len(akb.search_notes("数据库", include_inactive=True)), 3)

    def test_session_evidence_requires_explicit_opt_in(self) -> None:
        self.note("chat.md", type="session-evidence")
        self.assertEqual(akb.search_notes("数据库"), [])
        self.assertEqual(len(akb.search_notes("数据库", include_sessions=True)), 1)

    def test_memory_type_filters_results(self) -> None:
        self.note("preference.md", memory_type="feedback")
        self.note("fact.md", memory_type="project")
        self.assertEqual(len(akb.search_notes("数据库", memory_type="feedback")), 1)

    def test_project_overview_role_can_receive_a_small_project_recall_boost(self) -> None:
        overview = self.note(
            "overview.md", body="数据库部署步骤",
            context_role="project-overview", project="demo", project_path="/workspace/demo",
        )
        detailed = self.note(
            "detail.md", body="数据库部署步骤和校验细节",
            project="demo", project_path="/workspace/demo",
        )
        results = akb.search_notes("数据库部署", project="/workspace/demo")
        self.assertEqual(results[0]["path"], overview)
        self.assertIn(detailed, [row["path"] for row in results])

    def test_validation_and_test_fixture_notes_require_explicit_type_opt_in(self) -> None:
        validation = self.note("validation.md", type="validation")
        fixture = self.note("fixture.md", type="test-fixture")
        project_fixture = self.note("project-fixture.md", type="knowledge", memory_type="validation")
        self.assertEqual(akb.search_notes("数据库"), [])
        self.assertEqual(akb.search_notes("数据库", include_inactive=True), [])
        self.assertEqual(
            {row["path"] for row in akb.search_notes("数据库", memory_type="validation")},
            {validation, project_fixture},
        )
        self.assertEqual(
            {row["path"] for row in akb.search_notes("数据库", memory_type="test-fixture")},
            {fixture},
        )

    def test_current_distilled_source_is_read_once_per_search_and_invalidated(self) -> None:
        body = "source body for two generated notes"
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        shared = {
            "type": "knowledge", "source": "gemini-distiller", "project": "demo",
            "project_path": "/workspace/demo", "project_key": "/workspace/demo", "scope": "project",
        }
        output_paths = [
            self.note("generated-one.md", source_input_hash=body_hash, source_context_hash="ctx", source_session=f"{akb.session_directory()}/source.md", **shared),
            self.note("generated-two.md", source_input_hash=body_hash, source_context_hash="ctx", source_session=f"{akb.session_directory()}/source.md", **shared),
        ]
        output_metas = [akb.read_note(path)[0] for path in output_paths]
        source = akb.write_note(akb.session_directory(), "source.md", {
            "title": "source", "type": "session", "project": "demo",
            "project_path": "/workspace/demo", "project_key": "/workspace/demo", "scope": "project",
            "distilled_input_hash": body_hash,
            "distilled_paths": [path.relative_to(akb.vault_path()).as_posix() for path in output_paths],
        }, body)
        self.assertTrue(source.is_file())

        snapshots = {}
        with patch.object(akb, "read_note", wraps=akb.read_note) as read:
            for path, metadata in zip(output_paths, output_metas):
                self.assertTrue(akb._current_distilled_output(path, metadata, snapshots))
            self.assertEqual(read.call_count, 1)

        source_meta, _ = akb.read_note(source)
        akb.write_note(akb.session_directory(), "source.md", source_meta, body + " updated", overwrite=True)
        with patch.object(akb, "read_note", wraps=akb.read_note) as read:
            self.assertFalse(akb._current_distilled_output(output_paths[0], output_metas[0], snapshots))
            self.assertEqual(read.call_count, 1)

    def test_explicit_config_is_selected_and_cache_tracks_config_path(self) -> None:
        first = akb.load_config()
        alternate = Path(self.temp.name) / "alternate.json"
        alternate.write_text(json.dumps({
            "vault_path": str(Path(self.temp.name) / "alternate-vault"),
            "auto_discover_obsidian": False,
            "knowledge_directory": "custom-knowledge",
        }), encoding="utf-8")
        with patch.dict(os.environ, {"AKB_CONFIG": str(alternate), "AKB_VAULT": ""}):
            loaded = akb.load_config()
        self.assertEqual(first["knowledge_directory"], "10-技术笔记")
        self.assertEqual(loaded["knowledge_directory"], "custom-knowledge")
        self.assertEqual(loaded["vault_path"], str((Path(self.temp.name) / "alternate-vault").resolve()))

    def test_missing_local_config_falls_back_to_example(self) -> None:
        missing = Path(self.temp.name) / "missing-config.json"
        example = Path(self.temp.name) / "config.example.json"
        example.write_text(json.dumps({
            "vault_path": str(Path(self.temp.name) / "example-vault"),
            "auto_discover_obsidian": False,
            "knowledge_directory": "example-knowledge",
        }), encoding="utf-8")
        with patch.object(akb, "CONFIG_PATH", missing), patch.object(akb, "EXAMPLE_CONFIG_PATH", example), patch.dict(
            os.environ, {"AKB_CONFIG": "", "AKB_VAULT": ""},
        ):
            loaded = akb.load_config()
            example_vault = akb.ensure_vault()
        self.assertEqual(loaded["knowledge_directory"], "example-knowledge")
        self.assertEqual(loaded["vault_path"], str((Path(self.temp.name) / "example-vault").resolve()))
        self.assertEqual(loaded["vault_path_source"], "default")
        self.assertEqual(example_vault, (Path(self.temp.name) / "example-vault").resolve())

        with patch.object(akb, "CONFIG_PATH", missing), patch.object(akb, "EXAMPLE_CONFIG_PATH", example), patch.dict(
            os.environ, {"AKB_CONFIG": "", "AKB_VAULT": str(Path(self.temp.name) / "override-vault")},
        ):
            overridden = akb.load_config()
        self.assertEqual(overridden["vault_path"], str((Path(self.temp.name) / "override-vault").resolve()))
        self.assertEqual(overridden["vault_path_source"], "env")

    def test_context_never_exceeds_budget(self) -> None:
        self.note("long.md", body="数据库" * 1000)
        for budget in (30, 100, 300):
            self.assertLessEqual(len(akb.build_context(query="数据库", max_chars=budget)), budget)

    def test_context_marks_possible_conflicts(self) -> None:
        self.note("fact.md", conflict_candidates=["other.md"])
        self.assertIn("冲突候选", akb.build_context(query="数据库"))

    def test_metadata_roundtrips_numbers_and_nested_source_refs(self) -> None:
        metadata = {"count": 2, "source_refs": [{"source": "qoder", "id": "memory"}], "object": {"x": 3}}
        parsed, _ = akb.parse_frontmatter(akb.render_frontmatter(metadata))
        self.assertEqual(parsed, metadata)

    def test_mcp_search_exposes_inactive_and_type_filters(self) -> None:
        self.note("feedback.md", status="revoked", memory_type="feedback")
        self.assertEqual(mcp_server.call_tool("memory_search", {"query": "数据库"}), [])
        self.assertEqual(len(mcp_server.call_tool("memory_search", {
            "query": "数据库", "include_inactive": True, "memory_type": "feedback",
        })), 1)

    def test_audit_commands_report_read_only_status_and_error_exit(self) -> None:
        import knowledge_audit
        import operations

        parser = akb.build_parser()
        failure = parser.parse_args(["failure-audit"])
        with patch.object(operations, "failure_audit_report", return_value={"status": "error"}), patch("sys.stdout"):
            self.assertEqual(failure.func(failure), 1)
        knowledge = parser.parse_args(["knowledge-audit"])
        with patch.object(knowledge_audit, "audit_report", return_value={"status": "partial"}), patch("sys.stdout"):
            self.assertEqual(knowledge.func(knowledge), 0)
        self.assertEqual(parser.parse_args(["search", "数据库", "--memory-type", "validation"]).memory_type, "validation")
        self.assertEqual(parser.parse_args(["context", "--memory-type", "test-fixture"]).memory_type, "test-fixture")

    def test_mcp_list_preserves_existing_tools_and_adds_management(self) -> None:
        tools = {tool["name"] for tool in mcp_server.TOOLS}
        self.assertTrue({"memory_context", "memory_search", "memory_read", "memory_neighbors", "memory_health", "memory_capture", "memory_status"} <= tools)
        self.assertTrue({"memory_sources", "memory_sync", "memory_sync_status"} <= tools)

    def test_project_scoped_path_only_does_not_match_another_project(self) -> None:
        self.note("private.md", scope="project", project_path="/one/demo")
        self.assertEqual(akb.search_notes("数据库", project="other"), [])
        self.assertEqual(akb.search_notes("数据库", project="/two/demo"), [])

    def test_unknown_project_path_does_not_match_same_basename(self) -> None:
        self.note("private.md", project="demo")
        self.assertEqual(akb.search_notes("数据库", project="/two/demo"), [])

    def test_metadata_update_read_failure_does_not_change_file(self) -> None:
        path = self.note("safe.md")
        before = path.read_bytes()
        with patch.object(Path, "read_text", side_effect=PermissionError("模拟读取失败")):
            with self.assertRaises(PermissionError):
                akb.update_note_metadata(path, {"status": "distilling"})
        self.assertEqual(path.read_bytes(), before)

    def test_metadata_update_expected_version_rejects_stale_body(self) -> None:
        path = self.note("safe.md", body="新正文")
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "版本已变化"):
            akb.update_note_metadata(path, {"status": "distilled"}, expected_input_hash="oldhash")
        self.assertEqual(path.read_bytes(), before)

    def test_quoted_secrets_with_spaces_and_escapes_are_fully_redacted(self) -> None:
        text = '{"password": "first second \\"third\\"", "token": "fixture-token", "api_key": "fixture-key"}'
        cleaned = akb.redact_text(text)
        for secret in ("first", "second", "third", "fixture-token", "fixture-key"):
            self.assertNotIn(secret, cleaned)

    def test_search_index_never_persists_legacy_body_secrets(self) -> None:
        path = Path(self.temp.name) / akb.knowledge_directory() / "legacy.md"
        path.parent.mkdir(parents=True)
        path.write_text('---\ntitle: legacy\n---\n数据库 api_key=fixture-legacy-secret', encoding="utf-8")
        akb.search_notes("数据库")
        text = (Path(self.temp.name) / "index.json").read_text(encoding="utf-8")
        self.assertNotIn("fixture-legacy-secret", text)
        self.assertIn("REDACTED", text)

    def test_matching_legacy_cache_is_sanitized_on_hit_and_rewritten(self) -> None:
        path = self.note("cached.md")
        index = {
            "version": 2, "vault": str(akb.vault_path()), "notes": {
                str(path.relative_to(akb.vault_path())): {
                    "mtime_ns": path.stat().st_mtime_ns, "size": path.stat().st_size,
                    "title": "数据库", "project": "", "tags": "", "haystack": "数据库 password=cache-secret",
                    "excerpt": "数据库 password=cache-secret", "metadata": {"api_key": "metadata-secret"},
                },
            },
        }
        target = Path(self.temp.name) / "index.json"
        target.write_text(json.dumps(index), encoding="utf-8")
        akb._SEARCH_INDEX_MEM = None
        rows = akb.search_notes("数据库")
        serialized = json.dumps(rows, default=str) + target.read_text(encoding="utf-8")
        self.assertNotIn("cache-secret", serialized)
        self.assertNotIn("metadata-secret", serialized)

    def test_unversioned_legacy_outputs_are_only_explicitly_visible(self) -> None:
        source = akb.create_session_intake(source="test", title="会话", content="旧正文", session_id="stable")
        output = akb.capture_note(source="gemini-distiller", title="旧输出", summary="旧独特标记", metadata_extra={"source_session": str(source.relative_to(akb.vault_path()))})
        akb.update_note_metadata(source, {"distilled_paths": [str(output.relative_to(akb.vault_path()))]})
        akb.create_session_intake(source="test", title="会话", content="新正文", session_id="stable")
        self.assertNotIn(output, [row["path"] for row in akb.search_notes("旧独特标记")])
        self.assertIn(output, [row["path"] for row in akb.search_notes("旧独特标记", include_inactive=True)])

    def test_mcp_annotations_distinguish_reads_from_mutations(self) -> None:
        tools = {tool["name"]: tool for tool in mcp_server.TOOLS}
        for name in ("memory_search", "memory_read", "memory_context", "memory_sync_status"):
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"])
            self.assertFalse(tools[name]["annotations"]["openWorldHint"])
        self.assertFalse(tools["memory_capture"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["memory_capture"]["annotations"]["openWorldHint"])
        self.assertFalse(tools["memory_sync"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["memory_sync"]["annotations"]["destructiveHint"])

    def test_cli_sync_preview_is_default(self) -> None:
        args = akb.build_parser().parse_args(["sync"])
        self.assertFalse(args.apply)
        self.assertEqual(args.kind, "memory")


if __name__ == "__main__":
    unittest.main()
