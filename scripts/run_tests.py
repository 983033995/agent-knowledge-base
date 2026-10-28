"""Run the repository's unittest suite with all mutable state isolated."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="akb-tests-") as directory:
        root = Path(directory)
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
            "AKB_CONFIG": str(ROOT / "config.example.json"),
            "AKB_VAULT": str(root / "vault"), "AKB_STATE_DB": str(root / "state.sqlite3"),
            "AKB_SYNC_STATE_DIR": str(root / "sync"), "AKB_SEARCH_INDEX": str(root / "index.json"),
            "AKB_DISTILLER_STATE_DIR": str(root / "distiller"),
            "AKB_DISTILLER_CONFIG": str(root / "absent.json"),
            "AKB_DISTILLER_ENABLED": "1", "AKB_DISTILLER_AUTOSTART": "0",
        }
        return subprocess.call([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-q"],
                               cwd=ROOT, env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
