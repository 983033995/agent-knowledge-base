from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import akb
import knowledge_audit


def write_note(vault: Path, relative: str, metadata: dict, body: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}"
        for key, value in metadata.items()
    )
    path.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")
    return path


class KnowledgeAuditTests(unittest.TestCase):
    def build_vault(self, root: Path) -> Path:
        vault = root / "vault"
        vault.mkdir()
        source_relative = "00-Inbox/_agent-sessions/source.md"
        output_relative = "10-技术笔记/patterns/generated.md"
        source_body = "经测试确认的流程。"
        source_hash = hashlib.sha256(source_body.strip().encode("utf-8")).hexdigest()
        write_note(
            vault,
            source_relative,
            {
                "type": "session-evidence",
                "status": "distilled",
                "project": "audit-fixture",
                "scope": "project",
                "distilled_input_hash": source_hash,
                "distilled_paths": [output_relative],
            },
            source_body,
        )
        write_note(
            vault,
            "10-技术笔记/ordinary.md",
            {"type": "experience", "status": "active", "title": "共享流程"},
            "人工维护的知识。",
        )
        write_note(
            vault,
            output_relative,
            {
                "source": "gemini-distiller",
                "type": "pattern",
                "status": "active",
                "title": "共享流程",
                "source_session": source_relative,
                "source_input_hash": source_hash,
                "source_context_hash": "synthetic-context",
                "project": "audit-fixture",
                "scope": "project",
                "model": "synthetic-model",
                "evidence_level": "model-extracted",
            },
            "- 适用条件：\n- 验证方式：",
        )
        write_note(
            vault,
            "10-技术笔记/patterns/stale.md",
            {
                "source": "gemini-distiller",
                "type": "pattern",
                "status": "active",
                "title": "旧来源",
                "source_session": "00-Inbox/_agent-sessions/missing.md",
                "source_input_hash": "old-hash",
                "source_context_hash": "old-context",
            },
            "来源已经丢失。",
        )
        write_note(
            vault,
            "10-技术笔记/patterns/legacy.md",
            {
                "source": "gemini-distiller",
                "type": "pattern",
                "status": "active",
                "title": "旧格式",
            },
            "旧版生成材料。",
        )
        write_note(
            vault,
            "10-技术笔记/patterns/inactive.md",
            {
                "source": "gemini-distiller",
                "type": "pattern",
                "status": "superseded",
                "title": "已替代",
            },
            "不应默认召回。",
        )
        write_note(
            vault,
            "10-技术笔记/patterns/validation.md",
            {"type": "validation", "status": "active", "title": "隔离夹具"},
            "合成测试材料。",
        )
        return vault

    def audit_in_vault(self, vault: Path):
        with patch.dict(os.environ, {"AKB_VAULT": str(vault), "AKB_DISTILLER_AUTOSTART": "0"}), patch.object(akb, "_CONFIG_CACHE", None), patch.object(akb, "_CONFIG_CACHE_KEY", None):
            return knowledge_audit.audit_report()

    def test_audit_reports_visibility_lineage_collisions_and_empty_templates_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = self.build_vault(root)
            before = {path.relative_to(root): path.read_bytes() for path in vault.rglob("*") if path.is_file()}
            result = self.audit_in_vault(vault)

            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["read_only"])
            self.assertEqual(result["generated"]["total"], 4)
            self.assertEqual(result["generated"]["legacy_missing_fingerprints"], 2)
            self.assertEqual(result["generated"]["inactive"], 1)
            self.assertEqual(result["generated"]["with_source_reference"], 2)
            self.assertEqual(result["generated"]["with_readable_source"], 1)
            self.assertEqual(result["generated"]["lineage"]["current"], 1)
            self.assertEqual(result["generated"]["lineage"]["missing_source"], 1)
            self.assertEqual(result["generated"]["lineage"]["legacy_no_source_reference"], 2)
            self.assertEqual(result["effective"]["generated_default_visible"], 2)
            self.assertEqual(result["effective"]["generated_default_excluded"], 2)
            self.assertEqual(result["effective"]["eligible_for_default_retrieval"], 3)
            self.assertEqual(result["effective"]["validation_or_fixture_notes"], 1)
            self.assertEqual(result["effective"]["session_evidence_excluded"], 1)
            self.assertEqual(result["empty_maintenance_fields"], {"适用条件": 1, "验证方式": 1})
            self.assertEqual(result["title_collisions"]["knowledge_pattern_pair_groups"], 1)
            self.assertEqual(result["title_collisions"]["same_kind_collision_groups"], 0)
            self.assertNotIn("共享流程", json.dumps(result, ensure_ascii=False))
            self.assertNotIn(str(vault), json.dumps(result, ensure_ascii=False))
            self.assertEqual(before, {path.relative_to(root): path.read_bytes() for path in vault.rglob("*") if path.is_file()})

    def test_missing_vault_is_explicit_and_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing-vault"
            result = self.audit_in_vault(missing)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error_code"], "vault_unavailable")
            self.assertFalse(missing.exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_sync_visibility_errors_are_not_silently_counted_as_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = root / "vault"
            vault.mkdir()
            write_note(
                vault,
                "10-技术笔记/synced.md",
                {"type": "experience", "status": "active", "managed_by": "akb-sync"},
                "来源同步条目。",
            )
            with patch.object(knowledge_audit.memory_sync, "published_notes", side_effect=ValueError("private path")):
                result = self.audit_in_vault(vault)
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["effective"]["sync_managed_not_visible"], 1)
            self.assertIn("sync_visibility_unavailable", result["issues"])
            self.assertNotIn("private path", str(result))
            self.assertEqual(list(vault.rglob("*.md")), [vault / "10-技术笔记/synced.md"])


if __name__ == "__main__":
    unittest.main()
