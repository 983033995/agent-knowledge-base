from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
_SCHEMA = (
    "CREATE TABLE documents (namespace TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(namespace,key))",
    "CREATE TABLE jobs (namespace TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('queue','failed')), key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(namespace,kind,key))",
    "CREATE TABLE migrations (namespace TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(namespace,source))",
    "CREATE TABLE sync_items (namespace TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('records','notes','files')), key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(namespace,kind,key))",
)


def _encode(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _decode(value: str) -> dict[str, Any]:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("SQLite 状态记录不是对象，已停止操作")
    return payload


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"旧状态文件无法读取：{path.name}") from exc
    return decode_json_object(raw, path.name)


def decode_json_object(raw: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (OSError, ValueError) as exc:
        raise ValueError(f"旧状态文件无法读取：{name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"旧状态文件不是对象：{name}")
    return value


class Transaction:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get_document(self, namespace: str, key: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT payload FROM documents WHERE namespace=? AND key=?", (namespace, key)).fetchone()
        return _decode(row[0]) if row else None

    def put_document(self, namespace: str, key: str, value: dict[str, Any]) -> None:
        self.connection.execute("INSERT INTO documents VALUES (?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET payload=excluded.payload", (namespace, key, _encode(value)))

    def delete_document(self, namespace: str, key: str) -> None:
        self.connection.execute("DELETE FROM documents WHERE namespace=? AND key=?", (namespace, key))

    def get_job(self, namespace: str, kind: str, key: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT payload FROM jobs WHERE namespace=? AND kind=? AND key=?", (namespace, kind, key)).fetchone()
        return _decode(row[0]) if row else None

    def put_job(self, namespace: str, kind: str, key: str, entry: dict[str, Any]) -> None:
        self.connection.execute("INSERT INTO jobs VALUES (?,?,?,?) ON CONFLICT(namespace,kind,key) DO UPDATE SET payload=excluded.payload", (namespace, kind, key, _encode(entry)))

    def delete_job(self, namespace: str, kind: str, key: str) -> None:
        self.connection.execute("DELETE FROM jobs WHERE namespace=? AND kind=? AND key=?", (namespace, kind, key))

    def list_jobs(self, namespace: str, kind: str) -> list[tuple[str, dict[str, Any]]]:
        return [(row[0], _decode(row[1])) for row in self.connection.execute("SELECT key,payload FROM jobs WHERE namespace=? AND kind=? ORDER BY key", (namespace, kind))]

    def migration(self, namespace: str, source: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT payload FROM migrations WHERE namespace=? AND source=?", (namespace, source)).fetchone()
        return _decode(row[0]) if row else None

    def mark_migration(self, namespace: str, source: str, value: dict[str, Any]) -> None:
        self.connection.execute("INSERT INTO migrations VALUES (?,?,?)", (namespace, source, _encode(value)))

    def get_sync_state(self, namespace: str) -> dict[str, Any] | None:
        metadata = self.get_document(namespace, "sync-state")
        if metadata is None:
            return None
        for kind in ("records", "notes", "files"):
            metadata[kind] = {row[0]: _decode(row[1]) for row in self.connection.execute("SELECT key,payload FROM sync_items WHERE namespace=? AND kind=?", (namespace, kind))}
        return metadata

    def put_sync_state(self, namespace: str, state: dict[str, Any]) -> None:
        self.put_document(namespace, "sync-state", {key: value for key, value in state.items() if key not in {"records", "notes", "files"}})
        self.connection.execute("DELETE FROM sync_items WHERE namespace=?", (namespace,))
        for kind in ("records", "notes", "files"):
            self.connection.executemany("INSERT INTO sync_items VALUES (?,?,?,?)", [(namespace, kind, key, _encode(value)) for key, value in state.get(kind, {}).items()])


class StateStore:
    def __init__(self, path: Path):
        self.db_path = Path(path).expanduser().absolute()

    def _connect(self, writable: bool) -> sqlite3.Connection | None:
        if self.db_path.is_symlink():
            raise ValueError("SQLite 状态数据库不可为软链接")
        if not writable and not self.db_path.exists():
            return None
        if writable:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(str(self.db_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        mode = "rw" if writable else "ro"
        connection = sqlite3.connect(self.db_path.as_uri() + f"?mode={mode}", uri=True, isolation_level=None, timeout=10)
        try:
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA foreign_keys=ON")
            if writable:
                connection.execute("PRAGMA synchronous=FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def finalize_journal(self) -> None:
        connection = self._connect(True)
        assert connection is not None
        try:
            if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise ValueError("SQLite 状态数据库版本不受支持")
            if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
                raise ValueError("无法切换 SQLite 日志模式，请等待活跃连接退出")
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        connection = self._connect(True)
        assert connection is not None
        try:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                if tables:
                    raise ValueError("SQLite 状态数据库缺少版本，拒绝覆盖已有表")
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise ValueError("SQLite 状态数据库版本不受支持")
            yield Transaction(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[Transaction | None]:
        connection = self._connect(False)
        if connection is None:
            yield None
            return
        try:
            connection.execute("BEGIN")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and not connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                yield None
            elif version != SCHEMA_VERSION:
                raise ValueError("SQLite 状态数据库版本不受支持")
            else:
                yield Transaction(connection)
        except sqlite3.OperationalError as exc:
            if "readonly" in str(exc).lower() or "read-only" in str(exc).lower():
                raise ValueError("SQLite 状态库需要崩溃恢复；请执行 state-migrate 或由下一次写入恢复") from exc
            raise
        finally:
            connection.rollback()
            connection.close()

    def import_json_jobs(self, namespace: str, legacy_dir: Path) -> dict[str, Any]:
        legacy_dir = Path(legacy_dir).resolve()
        source = "json-jobs:" + str(legacy_dir)
        with self.transaction() as transaction:
            previous = transaction.migration(namespace, source)
            if previous is not None:
                return {**previous, "already_imported": True}
            counts: dict[str, Any] = {"queue": 0, "failed": 0, "files": []}
            for kind in ("queue", "failed"):
                for path in sorted((legacy_dir / kind).glob("*.json")):
                    raw = path.read_bytes()
                    entry = decode_json_object(raw, path.name)
                    if not isinstance(entry.get("note"), str) or not entry["note"].strip():
                        raise ValueError(f"旧任务缺少输入路径：{path.name}")
                    for field in ("attempts", "not_before"):
                        if field in entry:
                            try:
                                numeric = float(entry[field])
                            except (TypeError, ValueError) as exc:
                                raise ValueError(f"旧任务字段无效：{path.name}") from exc
                            if not math.isfinite(numeric) or numeric < 0:
                                raise ValueError(f"旧任务字段无效：{path.name}")
                    existing = transaction.get_job(namespace, kind, path.stem)
                    if existing is not None and existing != entry:
                        raise ValueError(f"旧任务与 SQLite 已有任务冲突：{path.name}")
                    transaction.put_job(namespace, kind, path.stem, entry)
                    counts[kind] += 1
                    counts["files"].append({"name": str(path.relative_to(legacy_dir)), "sha256": hashlib.sha256(raw).hexdigest()})
            transaction.mark_migration(namespace, source, counts)
            return {**counts, "already_imported": False}
