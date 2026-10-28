#!/usr/bin/env python3
from __future__ import annotations

import json
import http.server
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import akb  # noqa: E402
import distiller  # noqa: E402
import mcp_server  # noqa: E402


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.search_index = Path(self.temp_dir.name) / "search-index.json"
        self.env_restore = self.set_distiller_env(
            AKB_STATE_DB=str(Path(self.temp_dir.name) / "state.sqlite3"),
            AKB_DISTILLER_STATE_DIR=str(Path(self.temp_dir.name) / "distiller-state"),
            AKB_VAULT=self.temp_dir.name,
            AKB_SEARCH_INDEX=str(self.search_index),
            AKB_SEARCH_CACHE="1",
            AKB_HOOK_LIGHT="1",
            AKB_DISTILLER_AUTOSTART="0",
        )
        akb._CONFIG_CACHE = None
        akb._CONFIG_CACHE_KEY = None
        akb._SEARCH_INDEX_MEM = None
        akb._SEARCH_INDEX_MEM_KEY = None
        akb.ensure_vault()

    def tearDown(self) -> None:
        self.restore_env(self.env_restore)
        akb._CONFIG_CACHE = None
        akb._CONFIG_CACHE_KEY = None
        akb._SEARCH_INDEX_MEM = None
        akb._SEARCH_INDEX_MEM_KEY = None
        self.temp_dir.cleanup()

    def set_distiller_env(self, **values: str) -> dict[str, str | None]:
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

    def queue_job(self) -> tuple[str, dict]:
        settings = distiller.load_settings()
        with distiller.queue_store(settings).read() as tx:
            return tx.list_jobs(distiller.queue_namespace(settings), "queue")[0]

    def put_queue_job(self, key: str, entry: dict) -> None:
        settings = distiller.load_settings()
        with distiller.queue_state_lock(settings):
            distiller._put_job(settings, "queue", key, entry)

    def test_capture_and_search_redacts_credentials(self) -> None:
        path = akb.capture_note(
            source="test",
            title="缓存迁移经验",
            summary="迁移后必须清理旧 key。",
            project="demo",
            knowledge=["先做双写，再验证读路径。"],
            gotchas=["不要把 sk-test-secret-1234567890 写入笔记。"],
        )
        self.assertTrue(path.exists())
        content = path.read_text(encoding="utf-8")
        self.assertIn("[REDACTED_API_KEY]", content)
        self.assertEqual(len(akb.search_notes("缓存迁移", project="demo")), 1)

    def test_load_config_follows_active_obsidian_vault(self) -> None:
        active_vault = Path(self.temp_dir.name) / "active-vault"
        active_vault.mkdir()
        registry = Path(self.temp_dir.name) / "obsidian.json"
        registry.write_text(
            json.dumps({"vaults": {"active": {"path": str(active_vault), "open": True}}}),
            encoding="utf-8",
        )
        os.environ.pop("AKB_VAULT", None)
        old_registry = os.environ.get("AKB_OBSIDIAN_REGISTRY")
        os.environ["AKB_OBSIDIAN_REGISTRY"] = str(registry)
        try:
            config = akb.load_config()
            self.assertEqual(config["vault_path"], str(active_vault.resolve()))
            self.assertEqual(config["vault_path_source"], "obsidian")
        finally:
            if old_registry is None:
                os.environ.pop("AKB_OBSIDIAN_REGISTRY", None)
            else:
                os.environ["AKB_OBSIDIAN_REGISTRY"] = old_registry

    def test_read_and_neighbors_preserve_wikilink_graph(self) -> None:
        metadata = {
            "type": "knowledge",
            "title": "Alpha",
            "tags": ["agent-memory"],
            "created": "2026-08-16",
            "updated": "2026-08-16",
        }
        akb.write_note(
            "10-技术笔记",
            "Alpha.md",
            metadata,
            "# Alpha\n\n关联 [[Beta]]。",
        )
        akb.write_note(
            "10-技术笔记",
            "Beta.md",
            {**metadata, "title": "Beta"},
            "# Beta\n\n来源 [[Alpha]]。",
        )
        akb.write_note(
            "10-技术笔记",
            "Gamma.md",
            {**metadata, "title": "Gamma"},
            "# Gamma\n\n来源 [[Alpha]]。",
        )

        note = akb.read_note_reference("Alpha.md")
        graph = akb.note_neighbors("Alpha.md")
        self.assertEqual(note["title"], "Alpha")
        self.assertEqual(note["metadata"]["type"], "knowledge")
        self.assertEqual([row["title"] for row in graph["links"]], ["Beta"])
        self.assertEqual(
            {row["title"] for row in graph["backlinks"]},
            {"Beta", "Gamma"},
        )
        self.assertEqual(graph["unresolved_links"], [])

    def test_health_and_neighbors_report_ambiguous_wikilinks(self) -> None:
        metadata = {
            "type": "knowledge",
            "title": "Shared",
            "tags": ["agent-memory"],
            "created": "2026-08-16",
            "updated": "2026-08-16",
        }
        akb.write_note("10-技术笔记/one", "Shared.md", metadata, "# Shared")
        akb.write_note("10-技术笔记/two", "Shared.md", metadata, "# Shared")
        akb.write_note(
            "10-技术笔记",
            "AmbiguousSource.md",
            {**metadata, "title": "AmbiguousSource"},
            "# AmbiguousSource\n\n关联 [[Shared]]。",
        )

        report = akb.health_report(sample_limit=10)
        self.assertGreaterEqual(report["ambiguous_wikilinks"]["count"], 1)
        graph = akb.note_neighbors("AmbiguousSource.md")
        self.assertEqual(graph["links"], [])
        self.assertEqual(graph["counts"]["ambiguous_links"], 1)

    def test_mcp_status_reports_exact_note_count(self) -> None:
        akb.write_note(
            "10-技术笔记",
            "One.md",
            {"type": "knowledge", "title": "One"},
            "# One",
        )
        akb.write_note(
            "10-技术笔记",
            "Two.md",
            {"type": "knowledge", "title": "Two"},
            "# Two",
        )
        result = mcp_server.call_tool("memory_status", {})
        self.assertEqual(result["total_notes"], 2)
        self.assertEqual(result["by_directory"]["10-技术笔记"], 2)

    def test_health_report_is_read_only_and_finds_drift(self) -> None:
        akb.write_note(
            "10-技术笔记",
            "Broken.md",
            {
                "type": "knowledge",
                "title": "Broken",
                "tags": ["test"],
                "created": "2026-08-16",
                "updated": "2026-08-16",
            },
            "# Broken\n\n[[Missing Note]]。",
        )
        missing = Path(self.temp_dir.name) / "10-技术笔记" / "MissingFrontmatter.md"
        missing.write_text("# 没有 frontmatter\n", encoding="utf-8")

        report = akb.health_report(sample_limit=10)
        self.assertGreaterEqual(report["broken_wikilinks"]["count"], 1)
        self.assertIn(
            "10-技术笔记/MissingFrontmatter.md",
            report["missing_frontmatter"]["sample"],
        )
        self.assertGreaterEqual(report["missing_long_term_fields"]["count"], 1)
        self.assertTrue(missing.exists())

    def test_read_redacts_legacy_frontmatter_metadata(self) -> None:
        legacy = Path(self.temp_dir.name) / "10-技术笔记" / "Legacy.md"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(
            "---\n"
            "title: Legacy\n"
            "api_key: sk-legacy-secret-1234567890\n"
            "access_token: bearer-secret-value-1234567890\n"
            "---\n\n# Legacy\n",
            encoding="utf-8",
        )

        result = akb.read_note_reference("Legacy.md")
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("sk-legacy-secret-1234567890", serialized)
        self.assertNotIn("bearer-secret-value-1234567890", serialized)
        self.assertIn("[REDACTED_TOKEN]", serialized)

    def test_ingest_transcript_keeps_newer_snapshot_when_events_finish_out_of_order(self) -> None:
        transcript = Path(self.temp_dir.name) / "session.jsonl"
        transcript.write_text(
            json.dumps({"role": "user", "content": "开始任务"}) + "\n",
            encoding="utf-8",
        )
        first = akb.ingest_transcript(
            source="codex",
            transcript=str(transcript),
            session_id="session-race",
            cwd=self.temp_dir.name,
        )
        self.assertIsNotNone(first)
        first_mtime = transcript.stat().st_mtime_ns

        transcript.write_text(
            "\n".join(
                [
                    json.dumps({"role": "user", "content": "开始任务"}),
                    json.dumps({"role": "assistant", "content": "已完成新快照"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        newer_mtime = first_mtime + 1_000_000
        os.utime(transcript, ns=(newer_mtime, newer_mtime))
        latest = akb.ingest_transcript(
            source="codex",
            transcript=str(transcript),
            session_id="session-race",
            cwd=self.temp_dir.name,
        )
        self.assertEqual(latest, first)
        self.assertIn("已完成新快照", latest.read_text(encoding="utf-8"))

        os.utime(transcript, ns=(first_mtime, first_mtime))
        stale = akb.ingest_transcript(
            source="codex",
            transcript=str(transcript),
            session_id="session-race",
            cwd=self.temp_dir.name,
        )
        self.assertEqual(stale, latest)
        metadata, _ = akb.read_note(latest)
        self.assertEqual(int(metadata["transcript_mtime_ns"]), newer_mtime)

    def test_ingest_transcript_is_stable_for_same_session(self) -> None:
        transcript = Path(self.temp_dir.name) / "session.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    json.dumps({"role": "user", "content": "修复发布流程"}),
                    json.dumps({"role": "assistant", "content": "已补充回滚检查。"}),
                ]
            ),
            encoding="utf-8",
        )
        first = akb.ingest_transcript(
            source="codex",
            transcript=str(transcript),
            session_id="session-123",
            turn_id="turn-1",
            cwd=self.temp_dir.name,
        )
        second = akb.ingest_transcript(
            source="codex",
            transcript=str(transcript),
            session_id="session-123",
            turn_id="turn-2",
            cwd=self.temp_dir.name,
        )
        self.assertEqual(first, second)
        self.assertIn("回滚检查", first.read_text(encoding="utf-8"))
        self.assertEqual(len(list((Path(self.temp_dir.name) / akb.session_directory()).glob("*.md"))), 1)

    def test_mcp_initialize_and_tools_list(self) -> None:
        process = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "mcp_server.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        request = "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26"},
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            ]
        )
        output, error = process.communicate(request + "\n", timeout=5)
        self.assertEqual(process.returncode, 0, error)
        rows = [json.loads(line) for line in output.splitlines() if line.strip()]
        self.assertEqual(rows[0]["result"]["serverInfo"]["name"], "agent-memory")
        names = {tool["name"] for tool in rows[1]["result"]["tools"]}
        self.assertIn("memory_context", names)
        self.assertIn("memory_capture", names)
        self.assertIn("memory_read", names)
        self.assertIn("memory_neighbors", names)
        self.assertIn("memory_health", names)

    def test_distiller_uses_fixed_model_and_writes_long_term_notes(self) -> None:
        requests: list[dict[str, object]] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                size = int(self.headers.get("Content-Length", "0"))
                requests.append(json.loads(self.rfile.read(size).decode("utf-8")))
                response = {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "keep": True,
                                        "title": "发布回滚检查",
                                        "summary": "发布流程必须保留可验证的回滚路径。",
                                        "knowledge": ["发布前检查回滚入口。"],
                                        "patterns": ["发布前使用固定检查清单。"],
                                        "decisions": ["选择先补回滚检查，再继续发布。"],
                                        "gotchas": ["不要把密钥写入会话或知识库。"],
                                        "next_steps": ["在 CI 中保留回滚演练。"],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
                encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_args: object) -> None:
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state_dir = Path(self.temp_dir.name) / "distiller-state"
        old = self.set_distiller_env(
            AKB_DISTILLER_ENDPOINT=f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            AKB_DISTILLER_API_KEY="test-key",
            AKB_DISTILLER_STATE_DIR=str(state_dir),
            AKB_DISTILLER_AUTOSTART="0",
        )
        try:
            intake = akb.create_capture_intake(
                source="cursor",
                title="发布流程",
                project="demo",
                raw_context="修复发布流程。不要保存 sk-test-secret-1234567890。",
            )
            queued = distiller.enqueue(intake)
            self.assertTrue(queued["queued"])
            queue_file, entry = self.queue_job()
            entry["not_before"] = 0
            self.put_queue_job(queue_file, entry)

            self.assertEqual(distiller.worker(), 0)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["model"], "gemini-3.8-flash-high")
            prompt_text = json.dumps(requests[0], ensure_ascii=False)
            self.assertNotIn("sk-test-secret-1234567890", prompt_text)

            intake_metadata, _ = akb.read_note(intake)
            self.assertEqual(intake_metadata["status"], "distilled")
            all_notes = list((Path(self.temp_dir.name) / akb.knowledge_directory()).glob("*.md"))
            knowledge = [path for path in all_notes if akb.read_note(path)[0].get("type") == "distilled-knowledge"]
            decisions = [path for path in all_notes if akb.read_note(path)[0].get("type") == "distilled-decision"]
            self.assertEqual(len(knowledge), 1)
            self.assertEqual(len(decisions), 1)
            self.assertIn("gemini-3.8-flash-high", knowledge[0].read_text(encoding="utf-8"))
            patterns = list((Path(self.temp_dir.name) / akb.patterns_directory()).glob("*.md"))
            self.assertEqual(len(patterns), 1)
            self.assertEqual(akb.read_note(patterns[0])[0]["type"], "distilled-pattern")
        finally:
            self.restore_env(old)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_mcp_capture_returns_after_intake_without_waiting_for_model(self) -> None:
        state_dir = Path(self.temp_dir.name) / "mcp-distiller-state"
        old = self.set_distiller_env(
            AKB_DISTILLER_STATE_DIR=str(state_dir),
            AKB_DISTILLER_AUTOSTART="0",
        )
        try:
            result = mcp_server.call_tool(
                "memory_capture",
                {
                    "source": "workbuddy",
                    "title": "候选经验",
                    "raw_context": "只写入会话收件区，后台再提炼。",
                },
            )
            self.assertTrue(result["queued"])
            self.assertEqual(result["distiller_model"], "gemini-3.8-flash-high")
            self.assertEqual(len(list((Path(self.temp_dir.name) / akb.session_directory()).glob("*.md"))), 1)
            self.assertEqual(len(list((Path(self.temp_dir.name) / akb.knowledge_directory()).glob("*.md"))), 0)
        finally:
            self.restore_env(old)

    def test_search_cache_skips_reread_when_mtime_unchanged(self) -> None:
        akb.write_note(
            "10-技术笔记",
            "CacheHit.md",
            {"type": "knowledge", "title": "缓存命中", "project": "demo", "tags": ["agent-memory"]},
            "# 缓存命中\n\n可复用检索缓存。",
        )
        first = akb.search_notes("缓存命中", project="demo")
        self.assertEqual(len(first), 1)
        with patch.object(akb, "read_note", wraps=akb.read_note) as mocked:
            second = akb.search_notes("缓存命中", project="demo")
        self.assertEqual([row["relative"] for row in first], [row["relative"] for row in second])
        mocked.assert_not_called()

    def test_search_cache_preserves_uncached_order(self) -> None:
        for name, title in (("Alpha.md", "发布回滚检查"), ("Beta.md", "发布检查清单")):
            akb.write_note(
                "10-技术笔记",
                name,
                {"type": "knowledge", "title": title, "project": "demo", "tags": ["agent-memory"]},
                f"# {title}\n\n发布流程。",
            )
        cached = [row["relative"] for row in akb.search_notes("发布", project="demo")]
        old = self.set_distiller_env(AKB_SEARCH_CACHE="0")
        try:
            akb._SEARCH_INDEX_MEM = None
            uncached = [row["relative"] for row in akb.search_notes("发布", project="demo")]
        finally:
            self.restore_env(old)
        self.assertEqual(cached, uncached)

    def test_session_start_light_mode_limits_context(self) -> None:
        akb.write_note(
            "10-技术笔记",
            "Hook.md",
            {"type": "knowledge", "title": "demo 发布经验", "project": "demo", "tags": ["agent-memory"]},
            "# demo 发布经验\n\n" + ("召回材料。" * 80),
        )
        payload = json.dumps({"cwd": "/tmp/demo"})
        with patch.object(akb, "project_from_cwd", return_value="demo"):
            with patch.object(akb, "search_notes", wraps=akb.search_notes) as search:
                with patch.object(akb, "build_context", wraps=akb.build_context) as build:
                    with patch.object(sys, "stdin", StringIO(payload)):
                        with patch("sys.stdout", new_callable=StringIO) as out:
                            code = akb.hook_command("session-start")
        self.assertEqual(code, 0)
        self.assertTrue(search.called)
        self.assertEqual(search.call_args.kwargs["limit"], 3)
        self.assertEqual(build.call_args.kwargs["max_chars"], 3000)
        printed = json.loads(out.getvalue())
        self.assertLessEqual(len(printed["additionalContext"]), 3000)

    def test_configured_directory_requires_config_key(self) -> None:
        with self.assertRaises(RuntimeError):
            akb.configured_directory_from({}, "session_directory")

    def test_distiller_reclaims_stale_lock(self) -> None:
        state_dir = Path(self.temp_dir.name) / "stale-lock"
        old = self.set_distiller_env(
            AKB_DISTILLER_STATE_DIR=str(state_dir),
            AKB_DISTILLER_AUTOSTART="0",
        )
        try:
            lock_path = state_dir / "worker.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(json.dumps({"pid": 999999, "started_at": "2000-01-01T00:00:00"}), encoding="utf-8")
            self.assertEqual(distiller.worker(), 0)
            self.assertFalse(lock_path.exists())
        finally:
            self.restore_env(old)

    def test_distiller_keeps_transient_errors_in_queue(self) -> None:
        state_dir = Path(self.temp_dir.name) / "transient-state"
        old = self.set_distiller_env(
            AKB_DISTILLER_STATE_DIR=str(state_dir),
            AKB_DISTILLER_AUTOSTART="0",
            AKB_DISTILLER_MAX_ATTEMPTS="5",
        )
        try:
            intake = akb.create_capture_intake(
                source="cursor",
                title="瞬时失败",
                project="demo",
                raw_context="需要后台重试。",
            )
            distiller.enqueue(intake)
            queue_file, entry = self.queue_job()
            entry["not_before"] = 0
            self.put_queue_job(queue_file, entry)
            with patch.object(
                distiller,
                "request_model",
                side_effect=distiller.DistillerError("CLIProxyAPI 请求失败：Connection refused"),
            ):
                self.assertFalse(distiller.process_entry(queue_file, distiller.load_settings()))
                self.assertFalse(distiller.process_entry(queue_file, distiller.load_settings()))
            metadata, _ = akb.read_note(intake)
            self.assertEqual(metadata["status"], "distilling")
            self.assertEqual(self.queue_job()[0], queue_file)
            self.assertEqual(len(list((state_dir / "failed").glob("*.json"))), 0)
        finally:
            self.restore_env(old)

    def test_retry_failed_moves_items_back_to_queue(self) -> None:
        state_dir = Path(self.temp_dir.name) / "retry-state"
        old = self.set_distiller_env(
            AKB_DISTILLER_STATE_DIR=str(state_dir),
            AKB_DISTILLER_AUTOSTART="0",
        )
        try:
            intake = akb.create_capture_intake(
                source="cursor",
                title="失败回收",
                project="demo",
                raw_context="需要重新排队。",
            )
            failed_dir = state_dir / "failed"
            failed_dir.mkdir(parents=True, exist_ok=True)
            (failed_dir / "dead-item.json").write_text(
                json.dumps(
                    {
                        "note": str(intake),
                        "queued_at": "2026-01-01T00:00:00",
                        "not_before": 1,
                        "attempts": 4,
                        "last_error": "Connection refused",
                    }
                ),
                encoding="utf-8",
            )
            result = distiller.retry_failed()
            self.assertEqual(result["moved"], 1)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["queue"], 1)
            self.assertFalse(result["worker_started"])
            _, queued = self.queue_job()
            self.assertTrue((failed_dir / "dead-item.json").exists())
            self.assertEqual(queued["attempts"], 0)
        finally:
            self.restore_env(old)

    def test_auth_token_prefers_cliproxy_env(self) -> None:
        old = self.set_distiller_env(
            AKB_DISTILLER_API_KEY="",
            CLIPROXY_API_KEY="sk-test-from-env",
        )
        try:
            token = distiller.auth_token({"auth_command": "/bin/false"})
            self.assertEqual(token, "sk-test-from-env")
        finally:
            self.restore_env(old)

    def test_auth_token_falls_back_to_cliproxy_config(self) -> None:
        config_path = Path(self.temp_dir.name) / "cliproxy.yaml"
        config_path.write_text("api-keys:\n  - sk-from-config-file\n", encoding="utf-8")
        old = self.set_distiller_env(
            AKB_DISTILLER_API_KEY="",
            CLIPROXY_API_KEY="",
            CLIPROXYAPI_BEARER_TOKEN="",
            CLIPROXY_CONFIG=str(config_path),
        )
        try:
            token = distiller.auth_token({"auth_command": "/bin/false"})
            self.assertEqual(token, "sk-from-config-file")
        finally:
            self.restore_env(old)

    def test_update_note_metadata_drops_none_keys(self) -> None:
        path = akb.capture_note(
            source="test",
            title="清掉旧错误",
            summary="成功后不应残留失败字段。",
            project="demo",
            metadata_extra={"distillation_error": "old", "distillation_failed_at": "old"},
        )
        akb.update_note_metadata(path, {"status": "distilled", "distillation_error": None, "distillation_failed_at": None})
        metadata, _ = akb.read_note(path)
        self.assertEqual(metadata["status"], "distilled")
        self.assertNotIn("distillation_error", metadata)
        self.assertNotIn("distillation_failed_at", metadata)

    def test_auth_errors_are_transient(self) -> None:
        self.assertTrue(
            distiller.is_transient_error(
                distiller.DistillerError("读取 CLIProxyAPI 鉴权失败：timed out after 10 seconds")
            )
        )
        self.assertTrue(
            distiller.is_transient_error(
                distiller.DistillerError("CLIProxyAPI 鉴权辅助脚本返回失败")
            )
        )

    def test_enqueue_merges_existing_queue_fields(self) -> None:
        state_dir = Path(self.temp_dir.name) / "merge-state"
        old = self.set_distiller_env(
            AKB_DISTILLER_STATE_DIR=str(state_dir),
            AKB_DISTILLER_AUTOSTART="0",
        )
        try:
            intake = akb.create_capture_intake(
                source="cursor",
                title="合并入队",
                project="demo",
                raw_context="重复事件应合并。",
            )
            first = distiller.enqueue(intake)
            self.assertTrue(first["queued"])
            queue_file, original = self.queue_job()
            original["attempts"] = 3
            original["queued_at"] = "2020-01-01T00:00:00"
            original["not_before"] = 9_999_999_999
            self.put_queue_job(queue_file, original)
            distiller.enqueue(intake)
            _, merged = self.queue_job()
            self.assertEqual(merged["attempts"], 3)
            self.assertEqual(merged["queued_at"], "2020-01-01T00:00:00")
            self.assertGreaterEqual(float(merged["not_before"]), 9_999_999_999)
        finally:
            self.restore_env(old)


if __name__ == "__main__":
    unittest.main()
