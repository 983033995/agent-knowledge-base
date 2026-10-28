from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Any

import akb
import distiller
import history_batch
import memory_sync


def _worker_active(directory: Path) -> bool:
    path = directory / "worker.lock"
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = int(payload.get("pid") or 0)
    except (OSError, TypeError, ValueError):
        raise ValueError("无法确认旧 worker 状态，已停止迁移")
    if pid <= 0:
        raise ValueError("旧 worker 锁缺少 PID，已停止迁移")
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _legacy_files(directory: Path) -> list[Path]:
    paths = [*directory.glob("queue/*.json"), *directory.glob("failed/*.json")]
    paths.extend(path for path in (directory / "manifest.json", directory / "pending.json") if path.exists())
    return sorted(paths)


@contextmanager
def _cutover_workers(directories: list[Path]):
    held: list[tuple[Path, str]] = []
    try:
        for directory in directories:
            if _worker_active(directory):
                raise ValueError("worker 尚在运行，已停止迁移")
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "worker.lock"
            token = f"migration-{os.getpid()}-{time.time_ns()}"
            try:
                descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as exc:
                raise ValueError("worker 锁在迁移前发生变化，已停止迁移") from exc
            try:
                os.write(descriptor, json.dumps({"pid": os.getpid(), "migration_token": token}).encode("utf-8"))
            finally:
                os.close(descriptor)
            held.append((path, token))
        yield
    finally:
        for path, token in reversed(held):
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("migration_token") == token:
                    path.unlink()
            except (OSError, ValueError):
                pass


def migrate_local_state() -> dict[str, Any]:
    settings = distiller.load_settings()
    realtime = Path(settings["state_dir"]).resolve()
    history = history_batch.batch_state_dir().resolve()
    if any(_worker_active(directory) for directory in (realtime, history)):
        raise ValueError("旧版 worker 尚在运行，请等待退出后再迁移；未停止进程")
    if (history / "batch.lock").exists():
        raise ValueError("历史回填锁仍存在，无法确认安全切换窗口，已停止迁移")
    with ExitStack() as stack:
        directories = list(dict.fromkeys((realtime, history)))
        for directory in directories:
            stack.enter_context(distiller.queue_migration_guard({"state_dir": directory}))
        stack.enter_context(_cutover_workers(directories))
        return _migrate_local_state()


def _migrate_local_state() -> dict[str, Any]:
    settings = distiller.load_settings()
    realtime = Path(settings["state_dir"]).resolve()
    history = history_batch.batch_state_dir().resolve()
    sync_directory = memory_sync.state_directory().resolve()
    directories = list(dict.fromkeys((realtime, history, sync_directory)))
    batch_lock = history / "batch.lock"
    if batch_lock.exists():
        raise ValueError("历史回填锁仍存在，无法确认安全切换窗口，已停止迁移")
    files = [path for directory in directories for path in _legacy_files(directory)]
    digests = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    backup = realtime / "state-backups" / f"sqlite-{time.time_ns()}"
    backup.mkdir(parents=True, exist_ok=False)
    backup.chmod(0o700)
    for index, directory in enumerate(directories):
        for path in _legacy_files(directory):
            target = backup / str(index) / path.relative_to(directory)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    database_paths = {distiller.queue_store(settings).db_path, history_batch.batch_store().db_path, memory_sync.sync_store().db_path}
    for index, path in enumerate(sorted(database_paths)):
        if not path.exists():
            continue
        journal = Path(str(path) + "-journal")
        if journal.exists():
            raw_backup = backup / f"recovery-{index}"
            raw_backup.mkdir()
            shutil.copy2(path, raw_backup / path.name)
            shutil.copy2(journal, raw_backup / journal.name)
            from state_store import StateStore

            with StateStore(path).transaction():
                pass
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(backup / f"database-{index}.sqlite3")) as destination:
                source.backup(destination)
    results = {}
    with memory_sync._sync_lock():
        results["sync"] = memory_sync.migrate_state()
    results["history_manifest"] = history_batch.migrate_manifest()
    for name, directory in (("realtime", realtime), ("history", history)):
        queue_settings = dict(settings)
        queue_settings["state_dir"] = directory
        store = distiller.queue_store(queue_settings)
        namespace = distiller.queue_namespace(queue_settings)
        result = store.import_json_jobs(namespace, directory)
        with store.read() as transaction:
            counts = {kind: len(transaction.list_jobs(namespace, kind)) for kind in ("queue", "failed")}
            integrity = transaction.connection.execute("PRAGMA integrity_check").fetchone()[0]
        results[name] = {"imported_queue": result["queue"], "imported_failed": result["failed"], "already_imported": result["already_imported"], **counts, "integrity": integrity}
    changed = [str(path) for path in files if hashlib.sha256(path.read_bytes()).hexdigest() != digests[str(path)]]
    if changed:
        raise ValueError("迁移期间旧文件发生变化，已保留备份和数据库，请先停止旧版写入再核对")
    from state_store import StateStore

    for path in database_paths:
        StateStore(path).finalize_journal()
    report = {
        "backend": "sqlite", "databases": [str(path) for path in sorted(database_paths)],
        "backup": str(backup), "legacy_files": len(files), "legacy_preserved": True,
        "worker_started": False, "cloud_requests": 0, "results": results,
    }
    akb.atomic_write_text(backup / "migration-report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report
