#!/usr/bin/env python3
"""Small, dependency-free local knowledge base for Agent sessions.

The vault is plain Markdown so Obsidian remains the source of truth. This
module is intentionally usable both as a CLI and as an import from the MCP
stdio server.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
DEFAULT_VAULT = ROOT / "vault"
DEFAULT_OBSIDIAN_REGISTRY = Path.home() / "Library/Application Support/obsidian/obsidian.json"
NOTE_DIRS = (
    "00-Inbox",
    "10-Projects",
    "20-Knowledge",
    "30-Patterns",
    "40-Decisions",
    "90-Sessions",
    "99-Archive",
)

_CONFIG_CACHE: dict[str, Any] | None = None
_CONFIG_CACHE_KEY: tuple[Any, ...] | None = None
_CONFIG_CACHE_REGISTRY_PATH: Path | None = None


class AmbiguousNoteReference(ValueError):
    """A wikilink or note reference resolves to more than one Markdown file."""

    def __init__(self, reference: str, candidates: list[Path]) -> None:
        self.reference = reference
        self.candidates = tuple(candidates)
        paths = ", ".join(str(path.relative_to(vault_path())) for path in candidates[:5])
        super().__init__(f"笔记引用不唯一：{reference!r}（候选：{paths}）")


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def iso_now() -> str:
    return now().isoformat(timespec="seconds")


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _path_value(value: Any) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else ROOT / path


def truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _truthy(value: Any, default: bool = False) -> bool:
    return truthy(value, default)


def _obsidian_registry_path(config: dict[str, Any]) -> Path:
    value = (
        os.environ.get("AKB_OBSIDIAN_REGISTRY")
        or config.get("obsidian_registry_path")
        or str(DEFAULT_OBSIDIAN_REGISTRY)
    )
    return _path_value(value)


def _active_obsidian_vault(config: dict[str, Any]) -> Path | None:
    if not _truthy(config.get("auto_discover_obsidian"), True):
        return None
    registry_path = _obsidian_registry_path(config)
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    vaults = registry.get("vaults") if isinstance(registry, dict) else None
    if not isinstance(vaults, dict):
        return None

    candidates: list[Path] = []
    for value in vaults.values():
        if not isinstance(value, dict) or value.get("open") is not True:
            continue
        raw_path = value.get("path")
        if not raw_path:
            continue
        path = _path_value(raw_path).resolve()
        if path.is_dir() and path not in candidates:
            candidates.append(path)
    return candidates[0] if len(candidates) == 1 else None


def load_config() -> dict[str, Any]:
    global _CONFIG_CACHE, _CONFIG_CACHE_KEY, _CONFIG_CACHE_REGISTRY_PATH

    env_vault = os.environ.get("AKB_VAULT")
    env_registry = os.environ.get("AKB_OBSIDIAN_REGISTRY")
    registry_hint = _CONFIG_CACHE_REGISTRY_PATH or DEFAULT_OBSIDIAN_REGISTRY
    if env_registry:
        registry_hint = _path_value(env_registry)
    probe_key = (
        env_vault,
        env_registry,
        _mtime_ns(CONFIG_PATH),
        str(registry_hint),
        _mtime_ns(registry_hint),
    )
    if _CONFIG_CACHE is not None and _CONFIG_CACHE_KEY == probe_key:
        return dict(_CONFIG_CACHE)

    config: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取配置 {CONFIG_PATH}: {exc}") from exc

    configured = env_vault or config.get("vault_path")
    if env_vault:
        vault = _path_value(env_vault)
        source = "env"
    else:
        configured_path = _path_value(configured) if configured else None
        active = _active_obsidian_vault(config)
        if active is not None and (configured_path is None or active != configured_path.resolve()):
            vault = active
            source = "obsidian"
        else:
            vault = configured_path or DEFAULT_VAULT
            source = "config" if configured_path is not None else "default"
    config["vault_path"] = str(vault.resolve())
    config["vault_path_source"] = source
    _CONFIG_CACHE_REGISTRY_PATH = _obsidian_registry_path(config)
    _CONFIG_CACHE_KEY = (
        env_vault,
        env_registry,
        _mtime_ns(CONFIG_PATH),
        str(_CONFIG_CACHE_REGISTRY_PATH),
        _mtime_ns(_CONFIG_CACHE_REGISTRY_PATH),
    )
    _CONFIG_CACHE = dict(config)
    return config


def configured_directory(key: str, legacy_default: str | None = None) -> str:
    return configured_directory_from(load_config(), key, legacy_default)


def configured_directory_from(
    config: dict[str, Any],
    key: str,
    legacy_default: str | None = None,
) -> str:
    value = config.get(key)
    if value is not None and str(value).strip():
        return str(value).strip()
    raise RuntimeError(f"config.json 缺少必填目录配置 {key}")


def session_directory() -> str:
    return configured_directory("session_directory")


def knowledge_directory() -> str:
    return configured_directory("knowledge_directory")


def patterns_directory() -> str:
    return configured_directory("patterns_directory")


def decisions_directory() -> str:
    return configured_directory("decisions_directory")


def managed_directories() -> tuple[str, ...]:
    configured = load_config().get("managed_directories")
    if isinstance(configured, list) and configured:
        return tuple(str(item) for item in configured if str(item).strip())
    return NOTE_DIRS


def vault_path() -> Path:
    return Path(load_config()["vault_path"])


def ensure_vault() -> Path:
    vault = vault_path()
    config = load_config()
    if not vault.exists() and config.get("vault_path_source") == "config":
        raise RuntimeError(f"Obsidian Vault 不存在：{vault}；请检查 vault_path 或打开 Obsidian Vault")
    for directory in managed_directories():
        (vault / directory).mkdir(parents=True, exist_ok=True)
    return vault


def slugify(value: str, fallback: str = "note") -> str:
    text = re.sub(r"\s+", "-", value.strip().lower())
    text = re.sub(r"[^0-9a-zA-Z\u3400-\u9fff._-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-._")
    return text[:80] or fallback


def short_hash(value: str, length: int = 10) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:length]


def redact_text(value: str) -> str:
    """Remove common credential-shaped strings before writing local notes."""
    patterns = (
        (
            r"(?i)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            "[REDACTED_PRIVATE_KEY]",
            re.DOTALL,
        ),
        (r"(?i)\bsk-[A-Za-z0-9_-]{16,}", "[REDACTED_API_KEY]", 0),
        (r"\bgh[pousr]_[A-Za-z0-9_]{20,}", "[REDACTED_GITHUB_TOKEN]", 0),
        (r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED_AWS_KEY]", 0),
        (r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", "Bearer [REDACTED_TOKEN]", 0),
        (
            r"(?i)\b(password|passwd|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|secret|authorization|cookie)\b['\"]?\s*[:=]\s*['\"]?[^\s,;}'\"]+",
            r"\1=[REDACTED]",
            0,
        ),
        (
            r"(?i)([?&](?:api[_-]?key|access_token|refresh_token|client_secret|token)=)[^&\s]+",
            r"\1[REDACTED]",
            0,
        ),
    )
    quoted_secret = re.compile(
        r'''(?i)(["'](?:password|passwd|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|secret|authorization|cookie|token)["']\s*:\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''
    )
    redacted = quoted_secret.sub(lambda match: match[1] + '"[REDACTED]"', value)
    for pattern, replacement, flags in patterns:
        redacted = re.sub(pattern, replacement, redacted, flags=flags)
    return redacted


def _redaction_marker_for_key(key: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "", key.lower())
    if "privatekey" in normalized:
        return "[REDACTED_PRIVATE_KEY]"
    if "githubtoken" in normalized:
        return "[REDACTED_GITHUB_TOKEN]"
    if "awskey" in normalized:
        return "[REDACTED_AWS_KEY]"
    if any(
        item in normalized
        for item in (
            "apikey",
            "password",
            "passwd",
            "accesstoken",
            "refreshtoken",
            "clientsecret",
            "authorization",
            "secret",
            "token",
        )
    ):
        return "[REDACTED_TOKEN]"
    return None


def redact_value(value: Any, *, key: str = "") -> Any:
    marker = _redaction_marker_for_key(key) if key else None
    if marker:
        return marker
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item, key=key) for item in value]
    if isinstance(value, dict):
        return {
            str(item_key): redact_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    return value


def yaml_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value, allow_nan=False)
    if value is None:
        return "null"
    return json.dumps(str(value), ensure_ascii=False)


def render_frontmatter(metadata: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in metadata.items():
        lines.append(f"{key}: {yaml_value(redact_value(value))}")
    lines.extend(["---", ""])
    return "\n".join(lines)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    marker = text.find("\n---", 4)
    if marker < 0:
        return {}, text
    header = text[4:marker]
    body = text[marker + 4 :]
    if body.startswith("\n"):
        body = body[1:]
    metadata: dict[str, Any] = {}
    for line in header.splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        raw = raw.strip()
        try:
            metadata[key] = json.loads(raw)
        except json.JSONDecodeError:
            metadata[key] = raw.strip("\"'")
    return metadata, body


def note_title(path: Path, metadata: dict[str, Any]) -> str:
    return redact_text(str(metadata.get("title") or path.stem.replace("-", " ")))


def iter_notes(*, readonly: bool = False) -> Iterable[Path]:
    vault = vault_path() if readonly else ensure_vault()
    config = load_config()
    if config.get("search_all_markdown"):
        excluded = [str(item).strip() for item in config.get("search_exclude_directories", []) if str(item).strip()]
        included = [str(item).strip() for item in config.get("search_include_directories", []) if str(item).strip()]
        for path in vault.rglob("*.md"):
            if path.name.lower() == "readme.md":
                continue
            relative = path.relative_to(vault).as_posix()
            if _matches_directory_rule(relative, excluded):
                continue
            if included and not _matches_directory_rule(relative, included):
                continue
            yield path
        return
    directories = config.get("search_directories") or config.get("managed_directories") or NOTE_DIRS
    seen: set[Path] = set()
    for directory in directories:
        root = vault / directory
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            if path.name.lower() == "readme.md":
                continue
            if path in seen:
                continue
            seen.add(path)
            yield path


def iter_all_notes() -> Iterable[Path]:
    """Iterate all Vault Markdown for link validation, independent of search scope."""
    vault = ensure_vault()
    for path in vault.rglob("*.md"):
        if path.name.lower() == "readme.md":
            continue
        if ".obsidian" in path.relative_to(vault).parts:
            continue
        yield path


def _matches_directory_rule(relative: str, rules: Iterable[str]) -> bool:
    normalized_relative = relative.replace("\\", "/").strip("/")
    for rule in rules:
        normalized = str(rule).replace("\\", "/").strip("/")
        if normalized in {"", "."}:
            return True
        if normalized_relative == normalized or normalized_relative.startswith(f"{normalized}/"):
            return True
    return False


def read_note(path: Path) -> tuple[dict[str, Any], str]:
    return parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))


def note_text(path: Path, metadata: dict[str, Any], body: str) -> str:
    return " ".join(
        [
            note_title(path, metadata),
            str(metadata.get("project", "")),
            str(metadata.get("tags", "")),
            body,
        ]
    ).lower()


def tokenize(value: str) -> list[str]:
    return [token for token in re.findall(r"[0-9a-zA-Z\u3400-\u9fff]+", value.lower()) if token]


SEARCH_INDEX_NAME = "search-index.json"
SEARCH_EXCERPT_CHARS = 1200
_SEARCH_INDEX_MEM: dict[str, Any] | None = None
_SEARCH_INDEX_MEM_KEY: tuple[str, str] | None = None


def search_cache_enabled() -> bool:
    return truthy(os.environ.get("AKB_SEARCH_CACHE"), True)


def hook_light_enabled() -> bool:
    return truthy(os.environ.get("AKB_HOOK_LIGHT"), True)


def search_index_path() -> Path:
    override = os.environ.get("AKB_SEARCH_INDEX")
    if override:
        path = Path(override).expanduser()
        return path if path.is_absolute() else ROOT / path
    return ROOT / ".distiller" / SEARCH_INDEX_NAME


def _stat_note(path: Path) -> tuple[int, int, float]:
    try:
        info = path.stat()
        return int(info.st_mtime_ns), int(info.st_size), float(info.st_mtime)
    except OSError:
        return 0, 0, 0.0


def _empty_search_index(vault: Path) -> dict[str, Any]:
    return {"version": 2, "vault": str(vault), "generated_at": iso_now(), "notes": {}}


def _load_search_index(vault: Path) -> dict[str, Any]:
    global _SEARCH_INDEX_MEM, _SEARCH_INDEX_MEM_KEY
    index_file = search_index_path()
    cache_key = (str(vault), str(index_file))
    if _SEARCH_INDEX_MEM is not None and _SEARCH_INDEX_MEM_KEY == cache_key:
        if str(_SEARCH_INDEX_MEM.get("vault") or "") == str(vault):
            return _SEARCH_INDEX_MEM
    payload = _empty_search_index(vault)
    try:
        raw = json.loads(index_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        raw = None
    if isinstance(raw, dict) and raw.get("version") == 2 and str(raw.get("vault") or "") == str(vault) and isinstance(raw.get("notes"), dict):
        payload = raw
    _SEARCH_INDEX_MEM = payload
    _SEARCH_INDEX_MEM_KEY = cache_key
    return payload


def _persistable_search_notes(notes: dict[str, Any]) -> dict[str, Any]:
    allowed = {"mtime_ns", "size", "title", "project", "tags", "haystack", "excerpt", "metadata"}
    persistable: dict[str, Any] = {}
    for key, value in notes.items():
        if not isinstance(value, dict):
            continue
        persistable[str(key)] = {field: value[field] for field in allowed if field in value}
    return persistable


def _save_search_index(index: dict[str, Any]) -> None:
    global _SEARCH_INDEX_MEM, _SEARCH_INDEX_MEM_KEY
    payload = {
        "version": 2,
        "vault": str(index.get("vault") or ""),
        "generated_at": iso_now(),
        "notes": _persistable_search_notes(index.get("notes") if isinstance(index.get("notes"), dict) else {}),
    }
    index["generated_at"] = payload["generated_at"]
    _SEARCH_INDEX_MEM = index
    _SEARCH_INDEX_MEM_KEY = (payload["vault"], str(search_index_path()))
    try:
        atomic_write_text(
            search_index_path(),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        )
    except OSError:
        pass


def _cached_note_record(path: Path, relative: str, index: dict[str, Any]) -> dict[str, Any]:
    mtime_ns, size, modified = _stat_note(path)
    notes = index.setdefault("notes", {})
    cached = notes.get(relative)
    if (
        isinstance(cached, dict)
        and int(cached.get("mtime_ns") or 0) == mtime_ns
        and int(cached.get("size") or 0) == size
        and "haystack" in cached
    ):
        sanitized = redact_value(cached)
        changed = sanitized != cached
        if changed:
            cached = sanitized
            notes[relative] = cached
        metadata = cached.get("metadata") if isinstance(cached.get("metadata"), dict) else {}
        return {
            "path": path,
            "relative": relative,
            "mtime_ns": mtime_ns,
            "size": size,
            "modified": modified,
            "title": str(cached.get("title") or note_title(path, metadata)),
            "project": str(cached.get("project") or ""),
            "tags": str(cached.get("tags") or ""),
            "haystack": str(cached.get("haystack") or ""),
            "excerpt": str(cached.get("excerpt") or ""),
            "metadata": metadata,
            "cache_hit": not changed,
        }

    metadata, body = read_note(path)
    metadata = redact_value(metadata)
    body = redact_text(body)
    title = note_title(path, metadata)
    persisted = {
        "mtime_ns": mtime_ns,
        "size": size,
        "title": title,
        "project": str(metadata.get("project") or ""),
        "tags": str(metadata.get("tags") or ""),
        "haystack": note_text(path, metadata, body),
        "excerpt": clean_excerpt(body, SEARCH_EXCERPT_CHARS),
        "metadata": redact_value(metadata) if isinstance(metadata, dict) else {},
    }
    notes[relative] = persisted
    return {
        "path": path,
        "relative": relative,
        "modified": modified,
        "cache_hit": False,
        **persisted,
    }


def _note_in_directories(relative: str, directories: Iterable[str] | None) -> bool:
    if not directories:
        return True
    return any(
        relative == directory.rstrip("/") or relative.startswith(directory.rstrip("/") + "/")
        for directory in directories
        if str(directory).strip()
    )


def _current_distilled_output(path: Path, metadata: dict[str, Any]) -> bool:
    reference = str(metadata.get("source_session") or "").strip()
    if not reference:
        return True
    source = _safe_vault_path(Path(reference) if Path(reference).is_absolute() else vault_path() / reference)
    if source is None or not source.is_file():
        return False
    source_metadata, source_body = read_note(source)
    if source_metadata.get("distillation_publication"):
        return False
    paths = source_metadata.get("distilled_paths")
    if not isinstance(paths, list):
        return False
    normalized = {_safe_vault_path(Path(value) if Path(str(value)).is_absolute() else vault_path() / str(value)) for value in paths}
    if path.resolve() not in normalized:
        return False
    expected_hash = str(metadata.get("source_input_hash") or "")
    if not expected_hash:
        return False
    if expected_hash:
        current_hash = hashlib.sha256(source_body.strip().encode("utf-8")).hexdigest()
        if expected_hash != current_hash or source_metadata.get("distilled_input_hash") != expected_hash:
            return False
    context_keys = ("project", "project_path", "project_key", "scope")
    if any(str(metadata.get(key) or "") != str(source_metadata.get(key) or "") for key in context_keys):
        return False
    return True


def search_notes(
    query: str = "",
    project: str = "",
    limit: int = 8,
    directories: Iterable[str] | None = None,
    memory_type: str = "",
    include_inactive: bool = False,
    include_sessions: bool = False,
) -> list[dict[str, Any]]:
    query_tokens = tokenize(query)
    project_lower = project.lower().strip()
    vault = vault_path()
    config = load_config()
    knowledge_prefix = configured_directory_from(config, "knowledge_directory").rstrip("/") + "/"
    patterns_prefix = configured_directory_from(config, "patterns_directory").rstrip("/") + "/"
    decisions_prefix = configured_directory_from(config, "decisions_directory").rstrip("/") + "/"
    session_prefix = configured_directory_from(config, "session_directory").rstrip("/") + "/"
    use_cache = search_cache_enabled()
    index = _load_search_index(vault) if use_cache else _empty_search_index(vault)
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    published_paths: set[str] | None = None
    dirty = False
    for path in iter_notes():
        relative = str(path.relative_to(vault))
        if not _note_in_directories(relative, directories):
            continue
        seen.add(relative)
        if use_cache:
            before = index.get("notes", {}).get(relative)
            record = _cached_note_record(path, relative, index)
            if not record.get("cache_hit") or not isinstance(before, dict):
                dirty = True
            full = str(record.get("haystack") or "")
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            title = str(record.get("title") or note_title(path, metadata))
            body = str(record.get("excerpt") or "")
            modified = float(record.get("modified") or 0)
        else:
            metadata, body = read_note(path)
            full = note_text(path, metadata, body)
            title = note_title(path, metadata)
            _, _, modified = _stat_note(path)
        if not include_inactive and str(metadata.get("status") or "active") in {"superseded", "revoked", "conflict", "ignored", "pending"}:
            continue
        if not include_inactive and metadata.get("managed_by") == "akb-sync":
            from memory_sync import published_notes

            if published_paths is None:
                published_paths = published_notes()
            if relative not in published_paths:
                continue
        if not include_inactive and metadata.get("source") == "gemini-distiller" and not _current_distilled_output(path, metadata):
            continue
        if not include_sessions and metadata.get("type") == "session-evidence":
            continue
        if memory_type and str(metadata.get("memory_type") or metadata.get("type") or "") != memory_type:
            continue
        note_project = str(metadata.get("project") or "").strip()
        note_project_path = str(metadata.get("project_path") or metadata.get("project_key") or "").strip()
        if project_lower:
            project_scoped = metadata.get("scope") == "project" or bool(note_project or note_project_path)
            if project_scoped:
                if Path(project).is_absolute():
                    if not note_project_path or str(Path(note_project_path).expanduser().resolve()) != str(Path(project).expanduser().resolve()):
                        continue
                else:
                    display_name = note_project or (Path(note_project_path).name if note_project_path else "")
                    if not display_name or display_name.casefold() != project_lower:
                        continue
        title_lower = title.lower()
        score = 0
        for token in query_tokens:
            if token in title_lower:
                score += 12
            elif token in full:
                score += 3
        if query_tokens and not score:
            continue
        if not query_tokens:
            score = 1
        if relative.startswith(patterns_prefix):
            score += 10
        elif relative.startswith(knowledge_prefix):
            score += 8
        elif relative.startswith(decisions_prefix):
            score += 7
        elif relative.startswith(session_prefix):
            score -= 1
        if project_lower and str(metadata.get("project") or "").lower().strip() == project_lower:
            score += 8
        results.append(
            {
                "path": path,
                "relative": relative,
                "metadata": metadata,
                "body": body,
                "title": title,
                "score": score,
                "modified": modified,
            }
        )
    if use_cache:
        notes = index.setdefault("notes", {})
        extra = [key for key in list(notes) if key not in seen and not (vault / key).is_file()]
        if extra:
            for key in extra:
                notes.pop(key, None)
            dirty = True
        if dirty:
            _save_search_index(index)
    results.sort(key=lambda item: (item["score"], item["modified"]), reverse=True)
    return results[: max(1, min(limit, 50))]


def clean_excerpt(body: str, limit: int = 2600) -> str:
    compact = re.sub(r"\n{3,}", "\n\n", body.strip())
    compact = redact_text(compact)
    return compact[:limit].rstrip() + ("…" if len(compact) > limit else "")


@contextmanager
def note_lock(path: Path):
    override = os.environ.get("AKB_NOTE_LOCK_DIR")
    directory = Path(override).expanduser() if override else vault_path() / ".akb" / "note-locks"
    directory.mkdir(parents=True, exist_ok=True)
    key = short_hash(str(path.resolve()), length=32)
    with (directory / f"{key}.lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write_text(path: Path, content: str) -> None:
    """Write a text file via same-directory replacement to avoid partial notes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o644
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        pass
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        os.chmod(temporary_name, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = ""
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


WIKILINK_PATTERN = re.compile(r"!?\[\[([^\]]+)\]\]")


def wikilink_targets(text: str) -> list[str]:
    """提取正文中的 Obsidian wikilink，忽略 fenced code 和外部 URL。"""
    without_code = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    without_code = re.sub(r"`[^`\n]*`", "", without_code)
    targets: list[str] = []
    seen: set[str] = set()
    for raw in WIKILINK_PATTERN.findall(without_code):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://")):
            continue
        if target.casefold() in {"wikilink", "wikilinks"}:
            continue
        if Path(target).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf", ".mp3", ".wav", ".mp4", ".mov"}:
            continue
        key = target.casefold()
        if key not in seen:
            seen.add(key)
            targets.append(target)
    return targets


def _note_index(notes: list[Path]) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}

    def add(key: str, path: Path) -> None:
        normalized = key.replace("\\", "/").strip("/").casefold()
        if not normalized:
            return
        rows = index.setdefault(normalized, [])
        if path not in rows:
            rows.append(path)

    vault = vault_path()
    for path in notes:
        relative = path.relative_to(vault).as_posix()
        add(relative, path)
        add(relative[:-3] if relative.endswith(".md") else relative, path)
        add(path.name, path)
        add(path.stem, path)
        metadata, _ = read_note(path)
        add(note_title(path, metadata), path)
    return index


def _safe_vault_path(path: Path) -> Path | None:
    try:
        resolved = path.expanduser().resolve()
        resolved.relative_to(vault_path().resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _resolve_note_reference(
    reference: str,
    *,
    source_path: Path | None,
    notes: list[Path],
    index: dict[str, list[Path]],
) -> Path | None:
    clean = reference.strip()
    if clean.startswith("[[") and clean.endswith("]]"):
        clean = clean[2:-2]
    clean = clean.lstrip("!").split("|", 1)[0].split("#", 1)[0].strip()
    if not clean or clean.startswith(("http://", "https://")):
        return None

    direct_candidates: list[Path] = []
    raw_path = Path(clean).expanduser()
    if raw_path.is_absolute():
        direct_candidates.append(raw_path)
    else:
        if source_path is not None:
            direct_candidates.append(source_path.parent / raw_path)
        direct_candidates.append(vault_path() / raw_path)
    for candidate in list(direct_candidates):
        if candidate.suffix.lower() != ".md":
            direct_candidates.append(candidate.with_suffix(".md"))
    for candidate in direct_candidates:
        safe = _safe_vault_path(candidate)
        if safe is not None and safe.is_file() and safe.suffix.lower() == ".md":
            return safe

    key = clean.replace("\\", "/").strip("/").casefold()
    matches = list(index.get(key, []))
    if not key.endswith(".md"):
        matches.extend(index.get(f"{key}.md", []))
    unique = list(dict.fromkeys(matches))
    if len(unique) > 1:
        raise AmbiguousNoteReference(reference, unique)
    return unique[0] if unique else None


def resolve_note_reference(reference: str) -> Path:
    """把相对路径、文件名或笔记标题解析为 Vault 内的 Markdown 文件。"""
    notes = list(iter_notes())
    path = _resolve_note_reference(
        reference,
        source_path=None,
        notes=notes,
        index=_note_index(notes),
    )
    if path is None:
        raise FileNotFoundError(f"找不到 Vault 笔记：{reference}")
    return path


def _note_summary(path: Path, excerpt_limit: int = 700) -> dict[str, Any]:
    metadata, body = read_note(path)
    return {
        "path": str(path.relative_to(vault_path())),
        "title": note_title(path, metadata),
        "type": redact_value(metadata.get("type", "")),
        "project": redact_value(metadata.get("project", "")),
        "updated": redact_value(metadata.get("updated", "")),
        "excerpt": clean_excerpt(body, excerpt_limit),
    }


def read_note_reference(reference: str, max_chars: int = 20000) -> dict[str, Any]:
    """读取已解析的 Vault 笔记；只允许读取 iter_notes() 纳入的 Markdown。"""
    path = resolve_note_reference(reference)
    metadata, body = read_note(path)
    maximum = max(1000, min(int(max_chars), 50000))
    content = redact_text(body.strip())
    if len(content) > maximum:
        content = content[:maximum].rstrip() + "…"
    return {
        "path": str(path.relative_to(vault_path())),
        "title": note_title(path, metadata),
        "metadata": redact_value(metadata),
        "content": content,
    }


def note_neighbors(
    reference: str,
    *,
    limit: int = 20,
    include_backlinks: bool = True,
) -> dict[str, Any]:
    """返回一篇笔记的出链、反向链接和未解析链接。"""
    notes = list(iter_notes())
    all_notes = list(iter_all_notes())
    index = _note_index(all_notes)
    source = _resolve_note_reference(
        reference,
        source_path=None,
        notes=notes,
        index=index,
    )
    if source is None:
        raise FileNotFoundError(f"找不到 Vault 笔记：{reference}")
    if source not in set(notes):
        raise FileNotFoundError(f"笔记不在当前 Agent 检索范围内：{reference}")
    _, body = read_note(source)
    maximum = max(1, min(int(limit), 50))

    links: list[dict[str, Any]] = []
    unresolved: list[str] = []
    ambiguous: list[dict[str, Any]] = []
    out_of_scope: list[dict[str, str]] = []
    visible_paths = set(notes)
    seen_paths: set[Path] = set()
    for target in wikilink_targets(body):
        try:
            resolved = _resolve_note_reference(
                target,
                source_path=source,
                notes=notes,
                index=index,
            )
        except AmbiguousNoteReference as exc:
            ambiguous.append(
                {
                    "target": target,
                    "candidates": [str(path.relative_to(vault_path())) for path in exc.candidates],
                }
            )
            continue
        if resolved is None:
            unresolved.append(target)
        elif resolved not in visible_paths:
            out_of_scope.append(
                {
                    "path": str(resolved.relative_to(vault_path())),
                    "title": note_title(resolved, read_note(resolved)[0]),
                }
            )
        elif resolved != source and resolved not in seen_paths:
            seen_paths.add(resolved)
            links.append(_note_summary(resolved))

    backlinks: list[dict[str, Any]] = []
    if include_backlinks:
        for candidate in notes:
            if candidate == source:
                continue
            _, candidate_body = read_note(candidate)
            for target in wikilink_targets(candidate_body):
                try:
                    resolved = _resolve_note_reference(
                        target,
                        source_path=candidate,
                        notes=notes,
                        index=index,
                    )
                except AmbiguousNoteReference as exc:
                    ambiguous.append(
                        {
                            "source": str(candidate.relative_to(vault_path())),
                            "target": target,
                            "candidates": [str(path.relative_to(vault_path())) for path in exc.candidates],
                        }
                    )
                    continue
                if resolved == source:
                    backlinks.append(_note_summary(candidate))
                    break

    return {
        "source": _note_summary(source),
        "links": links[:maximum],
        "backlinks": backlinks[:maximum],
        "unresolved_links": unresolved[:maximum],
        "ambiguous_links": ambiguous[:maximum],
        "out_of_scope_links": out_of_scope[:maximum],
        "counts": {
            "links": len(links),
            "backlinks": len(backlinks),
            "unresolved_links": len(unresolved),
            "ambiguous_links": len(ambiguous),
            "out_of_scope_links": len(out_of_scope),
        },
    }


def health_report(sample_limit: int = 20) -> dict[str, Any]:
    """只读扫描 Vault 的 frontmatter、链接和原始采集状态。"""
    notes = list(iter_notes())
    all_notes = list(iter_all_notes())
    index = _note_index(all_notes)
    maximum = max(1, min(int(sample_limit), 100))
    long_term_directories = {
        directory.rstrip("/")
        for directory in (knowledge_directory(), patterns_directory(), decisions_directory())
        if directory.strip()
    }

    missing_frontmatter: list[str] = []
    missing_fields: list[dict[str, Any]] = []
    missing_fields_count = 0
    broken_links: list[dict[str, str]] = []
    broken_links_count = 0
    ambiguous_links: list[dict[str, Any]] = []
    ambiguous_links_count = 0
    underlinked: list[str] = []
    pending_distillation = 0
    title_paths: dict[str, list[str]] = {}
    long_term_count = 0
    vault = vault_path()
    config = load_config()
    current_session_directory = configured_directory_from(config, "session_directory")

    def is_long_term(relative: str, _metadata: dict[str, Any]) -> bool:
        return any(relative == directory or relative.startswith(f"{directory}/") for directory in long_term_directories)

    for path in notes:
        relative = str(path.relative_to(vault))
        metadata, body = read_note(path)
        if not metadata and is_long_term(relative, metadata):
            missing_frontmatter.append(relative)
        if relative.startswith(current_session_directory.rstrip("/") + "/"):
            if metadata.get("status") == "needs-distillation":
                pending_distillation += 1

        if is_long_term(relative, metadata):
            long_term_count += 1
            title = note_title(path, metadata)
            title_paths.setdefault(title.casefold(), []).append(relative)
            required = ("title", "type", "tags", "created", "updated")
            absent = [key for key in required if not str(metadata.get(key) or "").strip()]
            if absent:
                missing_fields_count += 1
                if len(missing_fields) < maximum:
                    missing_fields.append({"path": relative, "missing": absent})
            if len(wikilink_targets(body)) < 2:
                underlinked.append(relative)

        for target in wikilink_targets(body):
            try:
                resolved = _resolve_note_reference(
                    target,
                    source_path=path,
                    notes=notes,
                    index=index,
                )
            except AmbiguousNoteReference as exc:
                ambiguous_links_count += 1
                if len(ambiguous_links) < maximum:
                    ambiguous_links.append(
                        {
                            "source": relative,
                            "target": target,
                            "candidates": [str(candidate.relative_to(vault)) for candidate in exc.candidates],
                        }
                    )
                continue
            if resolved is None:
                broken_links_count += 1
                if len(broken_links) < maximum:
                    broken_links.append({"source": relative, "target": target})

    duplicate_titles = [
        {"title": key, "paths": paths}
        for key, paths in title_paths.items()
        if len(paths) > 1
    ]
    return {
        "vault": str(vault_path()),
        "notes_checked": len(notes),
        "link_notes_checked": len(all_notes),
        "long_term_notes_checked": long_term_count,
        "pending_distillation": pending_distillation,
        "missing_frontmatter": {
            "count": len(missing_frontmatter),
            "sample": missing_frontmatter[:maximum],
        },
        "missing_long_term_fields": {
            "count": missing_fields_count,
            "sample": missing_fields,
        },
        "underlinked_long_term_notes": {
            "count": len(underlinked),
            "sample": underlinked[:maximum],
        },
        "broken_wikilinks": {
            "count": broken_links_count,
            "sample": broken_links,
        },
        "ambiguous_wikilinks": {
            "count": ambiguous_links_count,
            "sample": ambiguous_links,
        },
        "duplicate_titles": duplicate_titles[:maximum],
    }


def build_context(
    query: str = "",
    project: str = "",
    limit: int = 8,
    max_chars: int | None = None,
    directories: Iterable[str] | None = None,
    memory_type: str = "",
) -> str:
    config = load_config()
    max_chars = max_chars or int(config.get("max_context_chars", 12000))
    results = search_notes(query=query, project=project, limit=limit, directories=directories, memory_type=memory_type)
    if not results:
        return "本地 Agent 知识库暂无匹配笔记。完成有长期价值的任务后，请用 memory_capture 或 akb capture 沉淀。"

    heading = "本地 Agent 知识库召回"
    if project:
        heading += f"（项目：{project}）"
    blocks = [heading, "以下内容来自本地 Markdown，需结合当前代码和任务重新验证："]
    total = len("\n".join(blocks))
    for item in results:
        metadata = item["metadata"]
        warning = "存在同题冲突候选，请核对来源；" if metadata.get("conflict_candidates") else ""
        block = (
            f"\n\n### {item['title']}\n"
            f"来源：{item['relative']}\n"
            f"{warning}{clean_excerpt(item['body'])}"
        )
        remaining = max_chars - total
        if remaining <= 0:
            break
        if len(block) > remaining:
            blocks.append(block[:remaining])
            break
        blocks.append(block)
        total += len(block)
    return "".join(blocks)[:max_chars]


def status_report() -> dict[str, Any]:
    """Return exact counts for the currently resolved Vault."""
    vault = vault_path()
    counts: dict[str, int] = {}
    total = 0
    for path in iter_notes(readonly=True):
        relative = path.relative_to(vault)
        bucket = relative.parts[0] if len(relative.parts) > 1 else "."
        counts[bucket] = counts.get(bucket, 0) + 1
        total += 1
    return {
        "vault": str(vault),
        "vault_path_source": load_config().get("vault_path_source", "config"),
        "total_notes": total,
        "by_directory": counts,
    }


def project_from_cwd(cwd: str | None) -> str:
    if not cwd:
        return ""
    try:
        completed = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            return Path(completed.stdout.strip()).name
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd).name


def list_values(values: list[str] | None) -> list[str]:
    return [redact_text(value.strip()) for value in (values or []) if value and value.strip()]


def _safe_note_target(directory: str, filename: str) -> Path:
    vault = vault_path().resolve()
    relative = Path(directory) / Path(filename)
    target = (vault / relative).resolve()
    try:
        target.relative_to(vault)
    except ValueError as exc:
        raise ValueError("笔记路径必须位于配置的 Vault 内") from exc
    if target.suffix.lower() != ".md":
        raise ValueError("知识库只允许写入 Markdown 笔记")
    return target


def write_note(
    directory: str,
    filename: str,
    metadata: dict[str, Any],
    body: str,
    overwrite: bool = False,
) -> Path:
    vault = ensure_vault()
    target = _safe_note_target(directory, filename)
    target_dir = target.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    cleaned_body = redact_text(body).strip()
    if metadata.get("managed_by") == "akb-distiller":
        metadata = dict(redact_value(metadata))
        metadata["content_hash"] = hashlib.sha256(cleaned_body.encode("utf-8")).hexdigest()
        protected = {key: value for key, value in metadata.items() if key != "metadata_fingerprint"}
        metadata["metadata_fingerprint"] = hashlib.sha256(json.dumps(protected, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
    content = render_frontmatter(redact_value(metadata)) + cleaned_body + "\n"
    with note_lock(target):
        if target.exists() and not overwrite:
            target = target_dir / f"{target.stem}-{short_hash(body)}.md"
        if target.exists() and metadata.get("type") == "session" and overwrite:
            existing_metadata, _ = read_note(target)
            old_mtime = _int_value(existing_metadata.get("transcript_mtime_ns"))
            new_mtime = _int_value(metadata.get("transcript_mtime_ns"))
            if old_mtime and new_mtime and old_mtime > new_mtime:
                return target
            merged = dict(metadata)
            for key, value in existing_metadata.items():
                if key.startswith("distilled_") or key == "distillation_publication":
                    merged[key] = value
            content = render_frontmatter(redact_value(merged)) + redact_text(body).strip() + "\n"
        atomic_write_text(target, content)
    return target


def update_note_metadata(path: Path, updates: dict[str, Any], *, expected_input_hash: str = "") -> Path:
    """更新已有 Markdown 笔记的 frontmatter，不改动正文。"""
    with note_lock(path):
        metadata, body = read_note(path)
        if expected_input_hash and hashlib.sha256(body.strip().encode("utf-8")).hexdigest() != expected_input_hash:
            raise ValueError("笔记输入版本已变化，拒绝提交旧结果")
        for key, value in updates.items():
            if value is None:
                metadata.pop(key, None)
            else:
                metadata[key] = value
        atomic_write_text(path, render_frontmatter(redact_value(metadata)) + redact_text(body).strip() + "\n")
    return path


def capture_note(
    *,
    source: str,
    title: str,
    summary: str = "",
    project: str = "",
    knowledge: list[str] | None = None,
    decisions: list[str] | None = None,
    gotchas: list[str] | None = None,
    next_steps: list[str] | None = None,
    tags: list[str] | None = None,
    directory: str | None = None,
    filename: str | None = None,
    overwrite: bool = False,
    metadata_extra: dict[str, Any] | None = None,
) -> Path:
    if directory is None:
        directory = knowledge_directory()
    title = redact_text(title.strip() or "未命名 Agent 经验")
    timestamp = now()
    tag_values = ["agent-memory", f"source/{slugify(source, 'unknown')}"]
    if project:
        tag_values.append(f"project/{slugify(project, 'unknown')}")
    tag_values.extend(slugify(tag, "tag") for tag in (tags or []))
    metadata = {
        "type": "knowledge",
        "title": title,
        "source": source or "manual",
        "project": project,
        "created": timestamp.isoformat(timespec="seconds"),
        "updated": timestamp.isoformat(timespec="seconds"),
        "tags": sorted(set(tag_values)),
    }
    if metadata_extra:
        metadata.update(metadata_extra)
    sections: list[str] = [f"# {title}"]
    if summary.strip():
        sections.extend(["", "## 摘要", redact_text(summary.strip())])
    for heading, values in (
        ("可复用知识", knowledge),
        ("关键决策", decisions),
        ("坑与限制", gotchas),
        ("下次复用", next_steps),
    ):
        cleaned = list_values(values)
        if cleaned:
            sections.extend(["", f"## {heading}"])
            sections.extend(f"- {value}" for value in cleaned)
    sections.extend(
        [
            "",
            "## 维护提示",
            "- 适用条件：",
            "- 验证方式：",
            "- 关联项目或会话：",
        ]
    )
    config = load_config()
    configured_knowledge = str(config.get("knowledge_directory") or "")
    default_links = config.get("default_wikilinks") or []
    if configured_knowledge and directory.startswith(configured_knowledge) and default_links:
        sections.extend(["", "## 相关链接"])
        sections.extend(f"- {redact_text(str(link))}" for link in default_links)
    if filename is None:
        filename = f"{timestamp.strftime('%Y%m%d-%H%M%S')}-{slugify(title)}.md"
    return write_note(directory, filename, metadata, "\n".join(sections), overwrite=overwrite)


def flatten_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(flatten_content(item) for item in value)
    if isinstance(value, dict):
        for key in ("text", "value", "content", "message", "output", "input"):
            if key in value:
                text = flatten_content(value[key])
                if text.strip():
                    return text
        return ""
    return str(value) if value is not None else ""


def collect_messages(value: Any, messages: list[tuple[str, str]]) -> None:
    if isinstance(value, dict):
        role = value.get("role")
        content = None
        for key in ("content", "text", "message", "output", "input"):
            if key in value:
                content = value[key]
                break
        if role in {"user", "assistant"} and content is not None:
            text = flatten_content(content).strip()
            if text:
                messages.append((str(role), text))
        for key, child in value.items():
            if key not in {"content", "text", "message", "output", "input"}:
                collect_messages(child, messages)
    elif isinstance(value, list):
        for child in value:
            collect_messages(child, messages)


def extract_transcript(path: Path, max_chars: int) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    messages: list[tuple[str, str]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    collect_messages(json.loads(raw), messages)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []

    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for role, text in messages:
        text = redact_text(text).strip()
        key = f"{role}:{text}"
        if not text or key in seen:
            continue
        seen.add(key)
        deduped.append((role, text))
    if not deduped:
        try:
            raw_text = redact_text(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return []
        return [("transcript", raw_text[-max_chars:])]

    total = 0
    selected: list[tuple[str, str]] = []
    for item in reversed(deduped):
        role, text = item
        budget = max_chars - total - len(role) - 8
        if budget <= 0:
            break
        clipped = text if len(text) <= budget else text[: max(1, budget - 14)].rstrip() + "\n[内容已截断]"
        selected.append((role, clipped))
        total += len(clipped) + len(role) + 8
        if len(clipped) < len(text):
            break
    return list(reversed(selected))


def create_session_intake(
    *,
    source: str,
    title: str,
    content: str,
    project: str = "",
    session_id: str = "",
    turn_id: str = "",
    filename: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> Path:
    """把待提炼材料快速写入配置的原始会话目录。此函数不调用任何模型。"""
    timestamp = now()
    title = redact_text(title.strip() or f"{source} 会话摘录")
    session_key = session_id or short_hash(f"{source}:{title}:{content}")
    metadata = {
        "type": "session",
        "title": title,
        "source": source or "manual",
        "project": project,
        "session_id": session_key,
        "turn_id": turn_id,
        "updated": timestamp.isoformat(timespec="seconds"),
        "tags": ["agent-session", f"source/{slugify(source, 'unknown')}"],
        "status": "needs-distillation",
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    body = "\n".join(
        [
            f"# {title}",
            "",
            "## 待提炼材料",
            redact_text(content.strip()),
            "",
            "## 提炼状态",
            "- 本笔记先由本地 Hook 或 MCP 快速落盘。",
            "- 后台 distiller 将使用固定的 gemini-3.8-flash-high 判断是否值得长期保存。",
            f"- 原始摘录保留在 {session_directory()}；长期知识写入 {knowledge_directory()}、{patterns_directory()} 或 {decisions_directory()}。",
        ]
    )
    if filename is None:
        filename = f"intake-{slugify(source, 'agent')}-{slugify(session_key[-24:], 'session')}.md"
    return write_note(
        session_directory(),
        filename,
        metadata,
        body,
        overwrite=True,
    )


def create_capture_intake(
    *,
    source: str,
    title: str,
    summary: str = "",
    project: str = "",
    raw_context: str = "",
    knowledge: list[str] | None = None,
    decisions: list[str] | None = None,
    gotchas: list[str] | None = None,
    next_steps: list[str] | None = None,
    tags: list[str] | None = None,
) -> Path:
    """把 MCP memory_capture 的输入作为候选材料写入配置的原始会话目录。"""
    sections = []
    if raw_context.strip():
        sections.extend(["### 原始上下文", raw_context.strip()])
    if summary.strip():
        sections.extend(["### Agent 提供的摘要", summary.strip()])
    for heading, values in (
        ("Agent 提供的候选知识", knowledge),
        ("Agent 提供的候选决策", decisions),
        ("Agent 提供的候选坑与限制", gotchas),
        ("Agent 提供的候选下次复用", next_steps),
    ):
        cleaned = list_values(values)
        if cleaned:
            sections.extend([heading, *[f"- {value}" for value in cleaned]])
    content = "\n\n".join(sections) or title
    stamp = now().strftime("%Y%m%d-%H%M%S")
    unique = short_hash(f"{source}:{title}:{project}:{content}:{stamp}", length=10)
    filename = f"intake-{slugify(source, 'agent')}-{stamp}-{unique}-{slugify(title)}.md"
    return create_session_intake(
        source=source,
        title=title,
        content=content,
        project=project,
        session_id=f"capture-{unique}",
        filename=filename,
        extra_metadata={
            "capture_kind": "memory_capture",
            "candidate_tags": sorted(set(slugify(tag, "tag") for tag in (tags or []))),
        },
    )


def enqueue_distillation(note_path: Path) -> dict[str, Any]:
    """将已落盘的会话交给独立后台进程；不在当前 Agent 请求内调用模型。"""
    script = ROOT / "scripts" / "distiller.py"
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "enqueue", "--note", str(note_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"提炼队列启动失败：{exc}") from exc
    if completed.returncode != 0:
        detail = redact_text(completed.stderr.strip())[:300]
        raise RuntimeError(detail or "提炼队列启动失败")
    try:
        value = json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError:
        value = {"queued": True, "raw": redact_text(completed.stdout.strip())[:300]}
    return value if isinstance(value, dict) else {"queued": True}


def ingest_transcript(
    *,
    source: str,
    transcript: str,
    session_id: str = "",
    turn_id: str = "",
    cwd: str = "",
    project: str = "",
) -> Path | None:
    if not transcript:
        return None
    config = load_config()
    messages = extract_transcript(
        Path(transcript),
        int(config.get("session_excerpt_chars", 18000)),
    )
    if not messages:
        return None
    session_key = session_id or short_hash(transcript)
    project = project or project_from_cwd(cwd)
    first_user = next((text for role, text in messages if role == "user"), "")
    title = first_user.splitlines()[0][:100] if first_user else f"{source} 会话摘录"
    timestamp = now()
    message_blocks = []
    for role, text in messages:
        message_blocks.extend(["", f"### {role}", text])
    body = "\n".join(
        [
            f"# {title}",
            "",
            "## 自动摘要",
            f"这是由 Agent 生命周期 Hook 自动采集的会话摘录。请只把有长期价值的部分提炼到 {knowledge_directory()}、{patterns_directory()} 或 {decisions_directory()}。",
            "",
            f"- 来源：{source}",
            f"- 会话：{session_key}",
            f"- 最近事件：{turn_id or 'session-end'}",
            "",
            "## 对话摘录",
            *message_blocks,
            "",
            "## 待提炼",
            "- 可复用知识：",
            "- 关键决策：",
            "- 坑与限制：",
            "- 下次复用：",
        ]
    )
    filename = f"session-{slugify(source, 'agent')}-{slugify(session_key[-24:], 'session')}.md"
    target = _safe_note_target(session_directory(), filename)
    transcript_mtime_ns = _mtime_ns(Path(transcript)) or 0
    if target.exists() and transcript_mtime_ns:
        existing_metadata, _ = read_note(target)
        existing_mtime_ns = _int_value(existing_metadata.get("transcript_mtime_ns"))
        if existing_mtime_ns > transcript_mtime_ns:
            return target
    metadata = {
        "type": "session",
        "title": title,
        "source": source,
        "project": project,
        "session_id": session_key,
        "turn_id": turn_id,
        "updated": timestamp.isoformat(timespec="seconds"),
        "tags": ["agent-session", f"source/{slugify(source, 'unknown')}"],
        "status": "needs-distillation",
    }
    if transcript_mtime_ns:
        metadata["transcript_mtime_ns"] = transcript_mtime_ns
    return write_note(
        session_directory(),
        filename,
        metadata,
        body,
        overwrite=True,
    )


def read_json_stdin() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def hook_command(event_name: str) -> int:
    event = read_json_stdin()
    if event_name == "session-start":
        ensure_vault()
        cwd = str(event.get("cwd") or "")
        project = project_from_cwd(cwd)
        if hook_light_enabled() and not project:
            return 0
        limit = 3 if hook_light_enabled() else 6
        max_chars = 3000 if hook_light_enabled() else 9000
        directories = None
        if hook_light_enabled():
            directories = [knowledge_directory(), patterns_directory()]
        matches = search_notes(
            query=project,
            project=project,
            limit=limit,
            directories=directories,
        )
        if matches:
            context = build_context(
                query=project,
                project=project,
                limit=limit,
                max_chars=max_chars,
                directories=directories,
            )
            print(json.dumps({"additionalContext": context}, ensure_ascii=False))
        return 0

    if event_name in {"stop", "session-end"}:
        transcript = str(event.get("transcript_path") or "")
        try:
            note_path = ingest_transcript(
                source="codex",
                transcript=transcript,
                session_id=str(event.get("session_id") or ""),
                turn_id=str(event.get("turn_id") or ""),
                cwd=str(event.get("cwd") or ""),
            )
            if note_path:
                enqueue_distillation(note_path)
        except Exception as exc:
            print(f"agent-memory hook warning: {exc}", file=sys.stderr)
        return 0

    raise ValueError(f"未知 Hook 事件：{event_name}")


def cmd_status(_: argparse.Namespace) -> int:
    print(json.dumps(status_report(), ensure_ascii=False, indent=2))
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    print(json.dumps(health_report(sample_limit=args.sample_limit), ensure_ascii=False, indent=2))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    results = search_notes(
        query=args.query, project=args.project, limit=args.limit,
        memory_type=args.memory_type, include_inactive=args.include_inactive,
        include_sessions=args.include_sessions,
    )
    if not results:
        print("没有找到匹配笔记。")
        return 0
    for item in results:
        print(f"{item['score']:>3}  {item['relative']}  {item['title']}")
        if args.excerpt:
            print(f"     {clean_excerpt(item['body'], args.excerpt)}")
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    print(
        build_context(
            query=args.query,
            project=args.project,
            limit=args.limit,
            max_chars=args.max_chars,
            memory_type=args.memory_type,
        )
    )
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    path = capture_note(
        source=args.source,
        title=args.title,
        summary=args.summary,
        project=args.project,
        knowledge=args.knowledge,
        decisions=args.decisions,
        gotchas=args.gotchas,
        next_steps=args.next_steps,
        tags=args.tags,
        directory=args.directory,
    )
    print(path)
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    path = ingest_transcript(
        source=args.source,
        transcript=args.transcript,
        session_id=args.session_id,
        turn_id=args.turn_id,
        cwd=args.cwd,
        project=args.project,
    )
    if path:
        try:
            enqueue_distillation(path)
        except Exception as exc:
            print(f"agent-memory warning: {exc}", file=sys.stderr)
    print(path)
    return 0


def _run_distiller(command: str, timeout: int = 4) -> int:
    script = ROOT / "scripts" / "distiller.py"
    completed = subprocess.run(
        [sys.executable, str(script), command],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.stdout.strip():
        print(completed.stdout.strip())
    if completed.returncode != 0 and completed.stderr.strip():
        print(redact_text(completed.stderr.strip()), file=sys.stderr)
    return completed.returncode


def cmd_distill_status(_: argparse.Namespace) -> int:
    return _run_distiller("status")


def cmd_distill_retry(args: argparse.Namespace) -> int:
    from distiller import retry_failed

    print(json.dumps(retry_failed(dry_run=args.dry_run, error_contains=args.error_contains, limit=args.limit), ensure_ascii=False, indent=2))
    return 0


def cmd_runtime(_: argparse.Namespace) -> int:
    from operations import runtime_report

    print(json.dumps(runtime_report(), ensure_ascii=False, indent=2))
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    from memory_sources import discover_sources

    rows = discover_sources(source=args.source, kind=args.kind)
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = counts.setdefault(row["source"], {})
        key = f"{row['kind']}:{row['format']}"
        bucket[key] = bucket.get(key, 0) + 1
    print(json.dumps({"files": len(rows), "by_source": counts, "sources": rows if args.paths else []}, ensure_ascii=False, indent=2))
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from memory_sync import sync_sources

    result = sync_sources(source=args.source, kind=args.kind, apply=args.apply, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["errors"] else 0


def cmd_sync_status(_: argparse.Namespace) -> int:
    from memory_sync import sync_status

    print(json.dumps(sync_status(), ensure_ascii=False, indent=2))
    return 0


def cmd_state_migrate(_: argparse.Namespace) -> int:
    from state_migration import migrate_local_state

    print(json.dumps(migrate_local_state(), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Obsidian Agent Knowledge Base")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status")
    status.set_defaults(func=cmd_status)

    health = subparsers.add_parser("health")
    health.add_argument("--sample-limit", type=int, default=20)
    health.set_defaults(func=cmd_health)

    runtime = subparsers.add_parser("runtime-status", help="只读汇总来源发现、同步与提炼状态，不调用模型")
    runtime.set_defaults(func=cmd_runtime)

    sources = subparsers.add_parser("sources", help="只读盘点五端原生记忆与会话文件")
    sources.add_argument("--source", choices=("all", "qoder", "codex", "workbuddy", "deepseek", "cursor"), default="all")
    sources.add_argument("--kind", choices=("all", "memory", "session"), default="all")
    sources.add_argument("--paths", action="store_true")
    sources.set_defaults(func=cmd_sources)

    sync = subparsers.add_parser("sync", help="本地增量同步；默认仅预览，不调用模型")
    sync.add_argument("--source", choices=("all", "qoder", "codex", "workbuddy", "deepseek", "cursor"), default="all")
    sync.add_argument("--kind", choices=("all", "memory", "session"), default="memory")
    sync.add_argument("--limit", type=int, default=0)
    sync.add_argument("--apply", action="store_true")
    sync.set_defaults(func=cmd_sync)

    sync_status = subparsers.add_parser("sync-status", help="查看统一记忆来源与版本状态")
    sync_status.set_defaults(func=cmd_sync_status)

    state_migrate = subparsers.add_parser("state-migrate", help="备份并迁移本地同步状态与队列到 SQLite，不启动提炼任务")
    state_migrate.set_defaults(func=cmd_state_migrate)

    search = subparsers.add_parser("search")
    search.add_argument("query")
    search.add_argument("--project", default="")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--excerpt", type=int, default=0)
    search.add_argument("--memory-type", choices=("user", "feedback", "project", "reference", "experience", "session"), default="")
    search.add_argument("--include-inactive", action="store_true")
    search.add_argument("--include-sessions", action="store_true")
    search.set_defaults(func=cmd_search)

    context = subparsers.add_parser("context")
    context.add_argument("--query", default="")
    context.add_argument("--project", default="")
    context.add_argument("--limit", type=int, default=8)
    context.add_argument("--max-chars", type=int, default=12000)
    context.add_argument("--memory-type", choices=("user", "feedback", "project", "reference", "experience"), default="")
    context.set_defaults(func=cmd_context)

    capture = subparsers.add_parser("capture")
    capture.add_argument("--source", default="manual")
    capture.add_argument("--title", required=True)
    capture.add_argument("--summary", default="")
    capture.add_argument("--project", default="")
    capture.add_argument("--knowledge", action="append", default=[])
    capture.add_argument("--decision", dest="decisions", action="append", default=[])
    capture.add_argument("--gotcha", dest="gotchas", action="append", default=[])
    capture.add_argument("--next-step", dest="next_steps", action="append", default=[])
    capture.add_argument("--tag", dest="tags", action="append", default=[])
    capture.add_argument("--directory", default=None)
    capture.set_defaults(func=cmd_capture)

    distill_status = subparsers.add_parser("distill-status")
    distill_status.set_defaults(func=cmd_distill_status)

    distill_retry = subparsers.add_parser("distill-retry")
    distill_retry.add_argument("--dry-run", action="store_true")
    distill_retry.add_argument("--error-contains", default="")
    distill_retry.add_argument("--limit", type=int, default=0)
    distill_retry.set_defaults(func=cmd_distill_retry)

    hook = subparsers.add_parser("hook")
    hook.add_argument("event", choices=("session-start", "stop", "session-end"))
    hook.set_defaults(func=lambda args: hook_command(args.event))

    ingest = subparsers.add_parser("ingest")
    ingest.add_argument("--source", default="manual")
    ingest.add_argument("--transcript", required=True)
    ingest.add_argument("--session-id", default="")
    ingest.add_argument("--turn-id", default="")
    ingest.add_argument("--cwd", default="")
    ingest.add_argument("--project", default="")
    ingest.set_defaults(func=cmd_ingest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (RuntimeError, ValueError) as exc:
        print(f"agent-memory error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
