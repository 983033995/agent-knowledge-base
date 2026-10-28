"""Reject private data and missing runtime files in a built wheel."""
from __future__ import annotations

from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    wheels = list((ROOT / "dist").glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("Expected one wheel in dist/")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        forbidden = {"vault", ".distiller", ".codegraph", ".workbuddy", "tests", "__pycache__"}
        for name in names:
            parts = Path(name).parts
            if forbidden.intersection(parts) or Path(name).name in {"config.json", "distiller.json"} or "PROJECT-REVIEW-" in name:
                raise ValueError("Distribution contains a private or runtime file: " + name)
        required = {"akb_runtime/scripts/akb.py", "akb_runtime/scripts/distiller.py",
                    "akb_runtime/akb_launcher.py", "akb_runtime/config.example.json",
                    "akb_runtime/distiller.example.json"}
        if not required.issubset(names):
            raise ValueError("Distribution missing required runtime files")
    print("Wheel inspected: private data excluded; launchers and safe defaults present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
