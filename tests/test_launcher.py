from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("akb_launcher", ROOT / "akb_launcher.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_defaults_are_local_disabled_and_zero_write(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "not-created"
            with patch.dict(os.environ, {"AKB_HOME": str(home)}, clear=True):
                environment = launcher.runtime_environment()
            self.assertEqual(environment["AKB_VAULT"], str(home / "vault"))
            self.assertTrue(environment["AKB_DISTILLER_CONFIG"].endswith("distiller.example.json"))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_explicit_configuration_and_state_are_preserved(self):
        with patch.dict(os.environ, {"AKB_CONFIG": "/explicit/config.json", "AKB_STATE_DB": "/explicit/state.sqlite3"}, clear=True):
            environment = launcher.runtime_environment()
        self.assertNotIn("AKB_VAULT", environment)
        self.assertEqual(environment["AKB_CONFIG"], "/explicit/config.json")
        self.assertEqual(environment["AKB_STATE_DB"], "/explicit/state.sqlite3")

    def test_user_configuration_owns_its_vault(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.json").write_text("{}")
            with patch.dict(os.environ, {"AKB_HOME": str(home)}, clear=True):
                environment = launcher.runtime_environment()
            self.assertEqual(environment["AKB_CONFIG"], str(home / "config.json"))
            self.assertNotIn("AKB_VAULT", environment)

    def test_relative_user_paths_stay_beside_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.json").write_text('{"vault_path":"notes"}')
            (home / "distiller.json").write_text('{"state_dir":"runtime"}')
            with patch.dict(os.environ, {"AKB_HOME": str(home)}, clear=True):
                environment = launcher.runtime_environment()
            self.assertEqual(environment["AKB_VAULT"], str(home / "notes"))
            self.assertEqual(environment["AKB_STATE_DB"], str(home / "runtime/state.sqlite3"))
            self.assertFalse((home / "notes").exists())
            self.assertFalse((home / "runtime").exists())


if __name__ == "__main__":
    unittest.main()
