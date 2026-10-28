from __future__ import annotations

import contextlib
import io
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
import lifecycle_hook


class LifecycleHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.vault = self.root / "vault"
        self.env = {
            **os.environ, "AKB_VAULT": str(self.vault),
            "AKB_STATE_DB": str(self.root / "state.sqlite3"),
            "AKB_SEARCH_INDEX": str(self.root / "index.json"),
            "AKB_SYNC_STATE_DIR": str(self.root / "sync"),
            "AKB_DISTILLER_STATE_DIR": str(self.root / "distill"),
            "AKB_DISTILLER_AUTOSTART": "0", "PYTHONDONTWRITEBYTECODE": "1",
        }

    def invoke(self, source, event, payload):
        return subprocess.run(
            ["/usr/bin/python3", str(ROOT / "scripts/lifecycle_hook.py"), source, event],
            input=payload if isinstance(payload, str) else json.dumps(payload),
            text=True, capture_output=True, env=self.env, timeout=10,
        )

    def knowledge(self):
        directory = self.vault / "10-技术笔记"
        directory.mkdir(parents=True)
        (directory / "fixture.md").write_text(
            f'---\ntitle: fixture\nproject: project\nproject_path: "{self.workspace}"\n---\n项目经验必须保留\n',
            encoding="utf-8",
        )

    def test_subprocess_start_contracts(self):
        self.knowledge()
        for source in lifecycle_hook.SOURCES:
            with self.subTest(source=source):
                event = {"cwd": str(self.workspace), "session_id": "fixture"}
                if source == "cursor":
                    event = {"workspace_roots": [str(self.workspace)], "conversation_id": "fixture", "generation_id": "one"}
                result = self.invoke(source, "start", event)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                payload = json.loads(result.stdout)
                if source == "cursor":
                    self.assertEqual(set(payload), {"additional_context"})
                    context = payload["additional_context"]
                else:
                    self.assertEqual(set(payload), {"hookSpecificOutput"})
                    self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
                    context = payload["hookSpecificOutput"]["additionalContext"]
                self.assertIn("项目经验必须保留", context)
                self.assertIn("<akb-recalled-context>", context)

    def test_subprocess_missing_or_invalid_input_is_quiet(self):
        cases = ["", "{", "[]", "null", "42", '{"cwd":NaN}',
                 '{"cwd":"/one","cwd":"/two"}', {}, {"cwd": "relative"},
                 {"cwd": 12}, {"cwd": "\x00"}, {"cwd": str(self.workspace)}]
        for payload in cases:
            with self.subTest(payload=payload):
                result = self.invoke("qoder", "stop", payload)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn(str(self.workspace), result.stderr)
        self.assertFalse(self.vault.exists())
        self.assertFalse((self.root / "state.sqlite3").exists())

    def test_cursor_multiroot_is_never_guessed_even_with_cwd(self):
        self.knowledge()
        for roots in [[], [str(self.workspace), "/other"], "not-a-list", ["relative"], [None]]:
            result = self.invoke("cursor", "start", {"cwd": str(self.workspace), "workspace_roots": roots})
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.returncode, 0)

    def test_explicit_cwd_and_single_root_normalization(self):
        with patch.object(lifecycle_hook.lifecycle, "recall", return_value="context") as recall:
            lifecycle_hook.run("cursor", "start", {"cwd": str(self.workspace)})
            recall.assert_called_once_with(str(self.workspace))
        with patch.object(lifecycle_hook.lifecycle, "capture") as capture:
            event = {"workspace_roots": [str(self.workspace)], "conversation_id": "fixture", "generation_id": "gen", "transcript_path": "/fixture.jsonl"}
            self.assertIsNone(lifecycle_hook.run("cursor", "end", event))
            normalized = capture.call_args.args[1]
            self.assertEqual(normalized["cwd"], str(self.workspace))
            self.assertEqual(normalized["conversation_id"], "fixture")
            self.assertEqual(normalized["generation_id"], "gen")
            self.assertNotIn("cwd", event)

    def test_stop_active_still_captures_and_never_requests_followup(self):
        for source in lifecycle_hook.SOURCES:
            for name in ("stop", "end"):
                with self.subTest(source=source, name=name), patch.object(lifecycle_hook.lifecycle, "capture", return_value={"followup_message": "must not escape"}) as capture:
                    event = {"cwd": str(self.workspace), "transcript_path": "/fixture.jsonl", "stop_hook_active": True}
                    self.assertIsNone(lifecycle_hook.run(source, name, event))
                    capture.assert_called_once_with(source, event)

    def test_workbuddy_session_start_requires_explicit_workspace(self):
        # WB 2.165.0 omits cwd on SessionStart; its command adapter must supply
        # CODEBUDDY_PROJECT_DIR explicitly rather than guessing in this writer.
        self.knowledge()
        result = self.invoke("workbuddy", "start", {"session_id": "fixture"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        result = self.invoke("workbuddy", "start", {"cwd": str(self.workspace)})
        output = json.loads(result.stdout)
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("项目经验必须保留", output["hookSpecificOutput"]["additionalContext"])

    def test_subprocess_capture_fixtures(self):
        for source in ("qoder", "cursor", "workbuddy"):
            transcript = self.root / f"{source}.jsonl"
            transcript.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in [
                {"type": "user", "message": {"role": "user", "content": "fixture 用户经验"}},
                {"type": "assistant", "message": {"role": "assistant", "content": "fixture 已验证"}},
            ]) + "\n", encoding="utf-8")
            event = {"session_id": "fixture", "cwd": str(self.workspace), "transcript_path": str(transcript), "stop_hook_active": True}
            if source == "cursor":
                event = {"conversation_id": "fixture", "generation_id": "one", "workspace_roots": [str(self.workspace)], "transcript_path": str(transcript)}
            for name in ("stop", "end", "stop"):
                result = self.invoke(source, name, event)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")
            notes = list(self.vault.rglob(f"lifecycle-{source}-*.md"))
            self.assertEqual(len(notes), 1)
            text = notes[0].read_text(encoding="utf-8")
            self.assertIn("fixture 用户经验", text)
            self.assertIn(str(self.workspace), text)

    def test_processing_failure_is_redacted_and_fail_open(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps({"cwd": str(self.workspace)}))), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), patch.object(lifecycle_hook.lifecycle, "recall", side_effect=RuntimeError("secret payload /private/token")):
            self.assertEqual(lifecycle_hook.main(["qoder", "start"]), 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "akb lifecycle hook: input or processing failed\n")

    def test_empty_recall_emits_nothing(self):
        with patch.object(lifecycle_hook.lifecycle, "recall", return_value=""):
            self.assertIsNone(lifecycle_hook.run("qoder", "start", {"cwd": str(self.workspace)}))

    def test_cli_rejects_unsupported_source_or_event(self):
        for source, event in (("deepseek", "start"), ("qoder", "unknown")):
            result = self.invoke(source, event, {})
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
