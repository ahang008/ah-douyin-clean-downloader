from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/package_skill.py"
SPEC = importlib.util.spec_from_file_location("release_archive", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReleaseArchiveTests(unittest.TestCase):
    def fixture(self, root):
        for name in MODULE.ROOT_FILES:
            (root / name).write_text("synthetic release document\n")
        for name in ("scripts/run_creator.py", "scripts/requirements.txt", "references/creator-batch.md",
                     "agents/openai.yaml", "tests/fixtures/synthetic.json"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic\n")

    def test_private_runtime_and_media_are_excluded_and_archive_is_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            for name in (".venv/bin/python", ".browser-session/Cookies", "media/video.mp4",
                         "local-transcripts/raw.md", "catalog/transcripts.jsonl",
                         "scripts/__pycache__/cached.pyc", "scripts/media/private.py",
                         "scripts/captured-cookie.json", "tests/fixtures/real.mp4"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("private runtime or user data\n")
            first = MODULE.build_package(root, root / "dist/first.zip", "v0.3.0")
            second = MODULE.build_package(root, root / "dist/second.zip", "v0.3.0")
            self.assertEqual(first["sha256"], second["sha256"])
            with zipfile.ZipFile(first["output"]) as archive:
                self.assertEqual(archive.testzip(), None)
                names = archive.namelist()
                self.assertIn(MODULE.NAME + "/scripts/run_creator.py", names)
                self.assertEqual(len(names), len(MODULE.ROOT_FILES) + 5)
                self.assertFalse(any("private runtime" in archive.read(name).decode() for name in names))

    def test_symlink_cannot_copy_a_file_outside_the_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            (root / "scripts/linked.py").symlink_to(root / "LICENSE")
            with self.assertRaisesRegex(ValueError, "Symlinks"):
                MODULE.build_package(root, root / "dist/release.zip", "v0.3.0")


if __name__ == "__main__":
    unittest.main()
