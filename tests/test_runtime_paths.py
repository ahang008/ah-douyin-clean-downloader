from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/runtime_paths.py"
SPEC = importlib.util.spec_from_file_location("skill_runtime_paths", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RuntimePathsTests(unittest.TestCase):
    def test_installed_skill_cli_works_without_venv_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
                "mlx_whisper.exe" if os.name == "nt" else "mlx_whisper")
            cli.parent.mkdir(parents=True)
            cli.write_text("#!/bin/sh\nexit 0\n")
            cli.chmod(0o755)
            with patch.object(MODULE.shutil, "which", return_value=None):
                self.assertEqual(MODULE.find_command("mlx_whisper", root), str(cli.resolve()))
                self.assertIsNone(MODULE.find_command("ffmpeg", root))

    def test_existing_path_cli_remains_supported_when_no_bundled_runtime_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(MODULE.shutil, "which", return_value="/public/bin/mlx_whisper"):
                self.assertEqual(MODULE.find_command("mlx_whisper", Path(directory)), "/public/bin/mlx_whisper")


if __name__ == "__main__":
    unittest.main()
