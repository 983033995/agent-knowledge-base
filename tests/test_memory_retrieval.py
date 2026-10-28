from __future__ import annotations

import json
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
        self.env = patch.dict(os.environ, {
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
