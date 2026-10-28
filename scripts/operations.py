from __future__ import annotations

import datetime as dt
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import akb
import distiller
import memory_sources
import memory_sync


_FAILURE_CATEGORIES = (
    "current",
    "stale",
    "legacy",
    "missing",
    "unreadable",
    "already_succeeded",
    "unsafe_path",
    "changed_during_audit",
)


def failure_audit_report() -> dict[str, Any]:
    """Classify failed distillation entries against current inputs without writes."""
    try:
        settings = distiller.load_settings()
        _, failed_entries, backend = distiller._read_queue_state(settings)
        counts = Counter({category: 0 for category in _FAILURE_CATEGORIES})
        by_note: dict[str, tuple[tuple[str, str, str] | None, str]] = {}

        for _, entry in failed_entries:
            note_value = str(entry.get("note") or "").strip()
            if not note_value:
                counts["missing"] += 1
                continue
            candidate = Path(note_value).expanduser()
            safe_path = akb._safe_vault_path(candidate)
            if safe_path is None:
                counts["unsafe_path"] += 1
                continue

            key = str(safe_path)
            if key not in by_note:
                try:
                    before = safe_path.stat()
                except FileNotFoundError:
                    by_note[key] = (None, "missing_input")
                except (OSError, ValueError):
                    by_note[key] = (None, "read_failed")
                else:
                    try:
                        identity, reason = distiller._retry_input({"note": key})
                        after = safe_path.stat()
                    except FileNotFoundError:
                        by_note[key] = (None, "missing_input")
                    except (OSError, ValueError, UnicodeError):
                        by_note[key] = (None, "read_failed")
                    else:
                        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                            by_note[key] = (None, "changed_during_audit")
                        else:
                            by_note[key] = (identity, reason)

            identity, reason = by_note[key]
            if reason == "missing_input":
                counts["missing"] += 1
            elif reason == "read_failed":
                counts["unreadable"] += 1
            elif reason == "changed_during_audit":
                counts["changed_during_audit"] += 1
            elif reason == "already_succeeded":
                counts["already_succeeded"] += 1
            elif identity is None:
                # An unexpected resolver result is visible as unreadable rather
                # than being silently counted as a clean or empty ledger.
                counts["unreadable"] += 1
            elif (
                (distiller._entry_input_hash(entry) and distiller._entry_input_hash(entry) != identity[1])
                or (
                    distiller._entry_input_context_hash(entry)
                    and distiller._entry_input_context_hash(entry) != identity[2]
                )
            ):
                counts["stale"] += 1
            elif not distiller._entry_input_hash(entry) or not distiller._entry_input_context_hash(entry):
                counts["legacy"] += 1
            else:
                counts["current"] += 1

        return {
            "status": "ok",
            "backend": backend,
            "failed_total": len(failed_entries),
            "classified_total": sum(counts.values()),
            "counts": dict(counts),
            "read_only": True,
            "cloud_requests": 0,
        }
    except (OSError, ValueError, UnicodeError, sqlite3.Error, RuntimeError, TypeError, AttributeError):
        return {
            "status": "error",
            "error_code": "failed_ledger_unavailable",
            "read_only": True,
            "cloud_requests": 0,
        }


def runtime_report() -> dict[str, Any]:
    observed = dt.datetime.now(dt.timezone.utc)
    issues: list[dict[str, str]] = []
    components: dict[str, Any] = {}
    discovered: dict[str, dict[str, Any]] = {}
    for source in memory_sources.SOURCES:
        try:
            items = memory_sources.discover_sources(source=source, metadata_only=True)
            discovered[source] = {
                "status": "discovered" if items else "not_found",
                "items": len(items),
                "by_kind": dict(Counter(item["kind"] for item in items)),
                "client_connection": "not_probed",
            }
        except (OSError, ValueError):
            discovered[source] = {"status": "error", "client_connection": "not_probed"}
            issues.append({"code": "source_discovery_failed", "source": source, "severity": "warning"})
    components["sources"] = discovered
    try:
        sync = memory_sync.sync_status()
        pending = memory_sync._load_pending() is not None
        last = sync.get("last_sync")
        if last is not None and not isinstance(last, dict):
            raise ValueError("invalid last sync")
        stats = last.get("stats", {}) if isinstance(last, dict) else {}
        if not isinstance(stats, dict):
            raise ValueError("invalid sync stats")
        age = None
        timestamp_status = "absent"
        if isinstance(last, dict) and last.get("at"):
            try:
                timestamp = dt.datetime.fromisoformat(str(last["at"]))
                if timestamp.tzinfo is None:
                    raise ValueError("timezone required")
                age = (observed - timestamp.astimezone(dt.timezone.utc)).total_seconds()
                timestamp_status = "valid" if age >= 0 else "future"
                if age < 0:
                    age = None
            except (ValueError, TypeError, OverflowError):
                timestamp_status = "invalid"
        sync["state"] = "pending_recovery" if pending else "never_synced" if not last else "partial" if last.get("partial") or stats.get("failed_files", 0) else "synced"
        sync["last_run_scope"] = {"source": last.get("source"), "kind": last.get("kind")} if last else None
        sync["coverage_verified"] = False
        if sync["state"] == "synced":
            sync["state"] = "scoped_sync" if last.get("source") != "all" or last.get("kind") != "all" else "synced"
        sync["last_sync_age_seconds"] = round(age, 1) if age is not None else None
        sync["timestamp_status"] = timestamp_status
        sync["active_sources"] = sorted(sync.get("by_source", {}))
        components["sync"] = sync
        if sync["state"] != "synced":
            issues.append({"code": "sync_" + sync["state"], "severity": "warning"})
        if timestamp_status in {"invalid", "future"}:
            issues.append({"code": "sync_timestamp_" + timestamp_status, "severity": "warning"})
    except (OSError, ValueError, sqlite3.Error):
        components["sync"] = {"state": "error"}
        issues.append({"code": "sync_state_unavailable", "severity": "error"})
    try:
        status = distiller.collect_status()
        components["distiller"] = {key: status[key] for key in (
            "enabled", "model", "backend", "queue", "failed", "unique_failed_notes", "legacy_failed_entries",
            "worker_alive", "next_not_before", "failed_by_error", "oldest_failed_at", "latest_failed_at",
        ) if key in status}
        failure_audit = failure_audit_report()
        components["distiller"]["failure_audit"] = failure_audit
        if status["failed"]:
            issues.append({"code": "distillation_failures_present", "severity": "warning"})
        if status["queue"] and not status["worker_alive"]:
            issues.append({"code": "queued_without_worker", "severity": "warning"})
        if failure_audit["status"] != "ok":
            issues.append({"code": "distillation_failure_audit_unavailable", "severity": "error"})
    except (OSError, ValueError, sqlite3.Error):
        components["distiller"] = {"state": "error"}
        issues.append({"code": "distiller_state_unavailable", "severity": "error"})
    return {
        "observed_at": observed.isoformat(timespec="seconds"),
        "status": "error" if any(issue["severity"] == "error" for issue in issues) else "attention" if issues else "ok",
        "components": components,
        "issues": issues,
        "client_validation": "Filesystem discovery is not proof of a live client connection.",
        "cloud_requests": 0,
    }
