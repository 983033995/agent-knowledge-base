#!/usr/bin/env python3
"""Read-only aggregate quality audit for the active Markdown knowledge vault."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import akb
import memory_sync


INACTIVE_STATUSES = {"superseded", "revoked", "conflict", "ignored", "pending"}
FIXTURE_TYPES = {"validation", "test-fixture", "test_fixture"}
CONTEXT_FIELDS = ("project", "project_path", "project_key", "scope")
EMPTY_FIELD = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*|[-*]\s*)?(适用条件|验证方式)\s*[：:]?\s*$"
)


def _source_lineage(
    output_path: Path,
    output_metadata: dict[str, Any],
    vault: Path,
    source_cache: dict[Path, tuple[dict[str, Any], str] | str],
) -> tuple[str, bool, bool]:
    """Return (lineage label, reference present, reference readable)."""
    reference = str(output_metadata.get("source_session") or "").strip()
    if not reference:
        # This matches the existing search contract: no source reference is not
        # a reason by itself to hide a legacy note.
        return "legacy_no_source_reference", False, False

    raw_source = Path(reference).expanduser()
    if not raw_source.is_absolute():
        raw_source = vault / raw_source
    source_path = akb._safe_vault_path(raw_source)
    if source_path is None:
        return "source_outside_vault", True, False
    if source_path not in source_cache:
        if not source_path.is_file():
            source_cache[source_path] = "missing_source"
        else:
            try:
                before = source_path.stat()
                source_metadata, source_body = akb.read_note(source_path)
                after = source_path.stat()
            except (OSError, ValueError, UnicodeError):
                source_cache[source_path] = "source_unreadable"
            else:
                if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                    source_cache[source_path] = "source_changed_during_audit"
                else:
                    body_hash = hashlib.sha256(source_body.strip().encode("utf-8")).hexdigest()
                    source_cache[source_path] = (source_metadata, body_hash)

    cached = source_cache[source_path]
    if isinstance(cached, str):
        return cached, True, False
    source_metadata, source_body_hash = cached
    if source_metadata.get("distillation_publication"):
        return "publication_pending", True, True

    published_paths = source_metadata.get("distilled_paths")
    if not isinstance(published_paths, list):
        return "source_pointer_missing", True, True
    resolved_paths: set[Path | None] = set()
    for value in published_paths:
        raw_value = str(value or "").strip()
        if not raw_value:
            continue
        candidate = Path(raw_value).expanduser()
        if not candidate.is_absolute():
            candidate = vault / candidate
        resolved_paths.add(akb._safe_vault_path(candidate))
    if output_path.resolve() not in resolved_paths:
        return "output_not_published", True, True

    expected_hash = str(output_metadata.get("source_input_hash") or "")
    if not expected_hash:
        return "legacy_missing_input_hash", True, True
    if expected_hash != source_body_hash or source_metadata.get("distilled_input_hash") != expected_hash:
        return "stale_source_content", True, True
    if any(str(output_metadata.get(key) or "") != str(source_metadata.get(key) or "") for key in CONTEXT_FIELDS):
        return "stale_source_scope", True, True
    return "current", True, True


def _note_kind(relative: str, metadata: dict[str, Any], knowledge_prefix: str, patterns_prefix: str, decisions_prefix: str) -> str:
    if patterns_prefix and (relative == patterns_prefix.rstrip("/") or relative.startswith(patterns_prefix)):
        return "pattern"
    note_type = str(metadata.get("type") or "").casefold()
    if note_type in {"decision", "decision-record", "decision_record"} and decisions_prefix and (relative == decisions_prefix.rstrip("/") or relative.startswith(decisions_prefix)):
        return "decision"
    if knowledge_prefix and (relative == knowledge_prefix.rstrip("/") or relative.startswith(knowledge_prefix)):
        return "knowledge"
    return "other"


def audit_report() -> dict[str, Any]:
    """Scan current search-scope Markdown once; never create or alter local state."""
    observed = dt.datetime.now(dt.timezone.utc)
    try:
        vault = akb.vault_path()
        if not vault.is_dir():
            return {
                "status": "error",
                "observed_at": observed.isoformat(timespec="seconds"),
                "error_code": "vault_unavailable",
                "read_only": True,
                "cloud_requests": 0,
            }
        config = akb.load_config()
        knowledge_prefix = akb.configured_directory_from(config, "knowledge_directory").rstrip("/") + "/"
        patterns_prefix = akb.configured_directory_from(config, "patterns_directory").rstrip("/") + "/"
        decisions_prefix = akb.configured_directory_from(config, "decisions_directory").rstrip("/") + "/"
        notes = list(akb.iter_notes(readonly=True))
    except (OSError, ValueError, UnicodeError, RuntimeError, TypeError):
        return {
            "status": "error",
            "observed_at": observed.isoformat(timespec="seconds"),
            "error_code": "knowledge_audit_setup_failed",
            "read_only": True,
            "cloud_requests": 0,
        }

    source_cache: dict[Path, tuple[dict[str, Any], str] | str] = {}
    published_notes: set[str] | None = None

    status_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    model_counts: Counter[str] = Counter()
    scope_counts: Counter[str] = Counter()
    evidence_counts: Counter[str] = Counter()
    lineage_counts: Counter[str] = Counter()
    empty_maintenance_fields: Counter[str] = Counter()
    title_groups: dict[str, list[tuple[str, str, str, bool]]] = defaultdict(list)
    counts: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    uncertain_sync_visibility = False
    unreadable_notes = 0

    try:
        for path in notes:
            try:
                metadata, body = akb.read_note(path)
            except (OSError, ValueError, UnicodeError):
                unreadable_notes += 1
                continue
            relative = path.relative_to(vault).as_posix()
            status = str(metadata.get("status") or "active")
            note_type = str(metadata.get("type") or "<missing>")
            model = str(metadata.get("model") or metadata.get("distiller_model") or "<missing>")
            scope = str(metadata.get("scope") or "<missing>")
            evidence = str(metadata.get("evidence_level") or "<missing>")
            generated = metadata.get("source") == "gemini-distiller" or metadata.get("managed_by") == "akb-distiller"
            inactive = status in INACTIVE_STATUSES
            fixture = note_type.casefold() in FIXTURE_TYPES
            session_evidence = note_type == "session-evidence"
            sync_managed = metadata.get("managed_by") == "akb-sync"

            counts["notes_scanned"] += 1
            status_counts[status] += 1
            type_counts[note_type] += 1
            if generated:
                counts["generated"] += 1
                model_counts[model] += 1
                scope_counts[scope] += 1
                evidence_counts[evidence] += 1
                if not metadata.get("source_input_hash") or not metadata.get("source_context_hash"):
                    counts["legacy_generated"] += 1
                if inactive:
                    counts["inactive_generated"] += 1
                lineage, source_present, source_readable = _source_lineage(path, metadata, vault, source_cache)
                lineage_counts[lineage] += 1
                if source_present:
                    counts["generated_with_source_reference"] += 1
                if source_readable:
                    counts["generated_with_readable_source"] += 1
                if inactive:
                    counts["generated_visible_by_lineage"] += 0
                elif lineage in {"current", "legacy_no_source_reference"}:
                    counts["generated_visible_by_lineage"] += 1

                for field in EMPTY_FIELD.findall(body):
                    empty_maintenance_fields[field] += 1

            if sync_managed:
                if published_notes is None and not uncertain_sync_visibility:
                    try:
                        published_notes = memory_sync.published_notes()
                    except (OSError, ValueError, sqlite3.Error):
                        uncertain_sync_visibility = True
                sync_published = published_notes is not None and relative in published_notes
                if not sync_published:
                    counts["sync_managed_not_visible"] += 1
            else:
                sync_published = True

            default_visible = (
                not inactive
                and not session_evidence
                and not fixture
                and sync_published
                and (not generated or lineage in {"current", "legacy_no_source_reference"})
            )
            if default_visible:
                counts["eligible_for_default_retrieval"] += 1
            if fixture:
                counts["validation_or_fixture_notes"] += 1
            if inactive:
                counts["inactive"] += 1
            if session_evidence:
                counts["session_evidence_excluded"] += 1

            if generated:
                counts["generated_default_visible"] += int(default_visible)
                counts["generated_default_excluded"] += int(not default_visible)

            kind = _note_kind(relative, metadata, knowledge_prefix, patterns_prefix, decisions_prefix)
            title = akb.note_title(path, metadata).casefold()
            body_hash = hashlib.sha256(body.strip().encode("utf-8")).hexdigest()
            title_groups[title].append((kind, status, body_hash, generated))

        collision_counts: Counter[str] = Counter(
            {
                "duplicate_title_groups": 0,
                "knowledge_pattern_pair_groups": 0,
                "same_kind_collision_groups": 0,
                "multiple_active_groups": 0,
                "identical_body_groups": 0,
            }
        )
        for group in title_groups.values():
            if len(group) < 2:
                continue
            collision_counts["duplicate_title_groups"] += 1
            kinds = Counter(row[0] for row in group)
            if len(group) == 2 and kinds == Counter({"knowledge": 1, "pattern": 1}):
                collision_counts["knowledge_pattern_pair_groups"] += 1
            elif max(kinds.values()) > 1:
                collision_counts["same_kind_collision_groups"] += 1
            if sum(status == "active" for _, status, _, _ in group) > 1:
                collision_counts["multiple_active_groups"] += 1
            if len({body_hash for _, _, body_hash, _ in group}) < len(group):
                collision_counts["identical_body_groups"] += 1

        if unreadable_notes:
            errors["note_read_errors"] += unreadable_notes
        if uncertain_sync_visibility:
            errors["sync_visibility_unavailable"] += 1
        return {
            "status": "partial" if errors else "ok",
            "observed_at": observed.isoformat(timespec="seconds"),
            "read_only": True,
            "cloud_requests": 0,
            "notes_scanned": counts["notes_scanned"],
            "unreadable_notes": unreadable_notes,
            "effective": {
                "eligible_for_default_retrieval": counts["eligible_for_default_retrieval"],
                "generated_default_visible": counts["generated_default_visible"],
                "generated_default_excluded": counts["generated_default_excluded"],
                "generated_visible_by_lineage": counts["generated_visible_by_lineage"],
                "sync_managed_not_visible": counts["sync_managed_not_visible"],
                "validation_or_fixture_notes": counts["validation_or_fixture_notes"],
                "session_evidence_excluded": counts["session_evidence_excluded"],
                "inactive": counts["inactive"],
            },
            "generated": {
                "total": counts["generated"],
                "legacy_missing_fingerprints": counts["legacy_generated"],
                "inactive": counts["inactive_generated"],
                "with_source_reference": counts["generated_with_source_reference"],
                "with_readable_source": counts["generated_with_readable_source"],
                "lineage": dict(sorted(lineage_counts.items())),
                "models": dict(sorted(model_counts.items())),
                "scopes": dict(sorted(scope_counts.items())),
                "evidence_levels": dict(sorted(evidence_counts.items())),
            },
            "note_statuses": dict(sorted(status_counts.items())),
            "note_types": dict(sorted(type_counts.items())),
            "empty_maintenance_fields": dict(sorted(empty_maintenance_fields.items())),
            "title_collisions": dict(collision_counts),
            "issues": dict(sorted(errors.items())),
        }
    except (OSError, ValueError, UnicodeError, RuntimeError, TypeError, sqlite3.Error):
        return {
            "status": "error",
            "observed_at": observed.isoformat(timespec="seconds"),
            "error_code": "knowledge_audit_failed",
            "read_only": True,
            "cloud_requests": 0,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读汇总 Agent 知识库来源与质量")
    parser.parse_args(argv)
    result = audit_report()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
