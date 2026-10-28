"""Console entry points that keep the standalone script import contract intact."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent


def runtime_environment() -> dict[str, str]:
    """Resolve installation defaults without creating files or reading credentials."""
    environment = dict(os.environ)
    home = Path(environment.get("AKB_HOME", str(Path.home() / ".local/share/agent-knowledge-base"))).expanduser().absolute()
    environment.setdefault("AKB_CONFIG", str(home / "config.json" if (home / "config.json").is_file() else ROOT / "config.example.json"))
    environment.setdefault("AKB_DISTILLER_CONFIG", str(home / "distiller.json" if (home / "distiller.json").is_file() else ROOT / "distiller.example.json"))
    # Standalone source scripts retain their historical ROOT-relative config.
    # Installed launchers instead resolve relative paths beside the chosen config.
    config_path = Path(environment["AKB_CONFIG"]).expanduser().absolute()
    if config_path == ROOT / "config.example.json":
        environment.setdefault("AKB_VAULT", str(home / "vault"))
    elif "AKB_VAULT" not in environment and config_path.is_file():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        vault_value = config.get("vault_path") if isinstance(config, dict) else None
        if isinstance(vault_value, str) and vault_value.strip():
            vault = Path(vault_value).expanduser()
            environment["AKB_VAULT"] = str(vault if vault.is_absolute() else config_path.parent / vault)
    state = home / "state"
    settings_path = Path(environment["AKB_DISTILLER_CONFIG"]).expanduser().absolute()
    if settings_path != ROOT / "distiller.example.json" and settings_path.is_file():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        state_value = settings.get("state_dir") if isinstance(settings, dict) else None
        if isinstance(state_value, str) and state_value.strip():
            state_path = Path(state_value).expanduser()
            state = state_path if state_path.is_absolute() else settings_path.parent / state_path
    environment.setdefault("AKB_DISTILLER_STATE_DIR", str(state))
    state = Path(environment["AKB_DISTILLER_STATE_DIR"]).expanduser().absolute()
    environment.setdefault("AKB_STATE_DB", str(state / "state.sqlite3"))
    environment.setdefault("AKB_SEARCH_INDEX", str(state / "search-index.json"))
    environment.setdefault("AKB_SYNC_STATE_DIR", str(state / "memory-sync"))
    environment.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return environment


def launch(script: str) -> int:
    if os.name != "posix":
        print("AKB currently supports macOS and Linux; native Windows is not supported.", file=sys.stderr)
        return 2
    os.execve(sys.executable, [sys.executable, str(ROOT / "scripts" / script), *sys.argv[1:]], runtime_environment())
    return 0


def main() -> int:
    return launch("akb.py")


def mcp() -> int:
    return launch("mcp_server.py")


def hook() -> int:
    return launch("lifecycle_hook.py")
