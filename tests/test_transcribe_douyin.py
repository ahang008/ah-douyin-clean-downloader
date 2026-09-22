from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "transcribe_douyin.py"
SPEC = importlib.util.spec_from_file_location("transcribe_douyin", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TranscriptTests(unittest.TestCase):
    def test_final_deliverable_is_only_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output = Path(temp_name)
            result = MODULE.transcript_destination(output, "作者", "标题", "7123456789012345678")
            self.assertEqual(result.suffix, ".md")
            self.assertIn("校对后逐字稿", result.name)
            self.assertEqual(result.parent.name, "作者")

    def test_final_path_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output = Path(temp_name)
            first = MODULE.transcript_destination(output, "作者", "标题", "7123456789012345678")
            first.touch()
            second = MODULE.transcript_destination(output, "作者", "标题", "7123456789012345678")
            self.assertNotEqual(first, second)
            self.assertTrue(second.name.endswith("-2.md"))

    def test_cleanup_rejects_arbitrary_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="not-owned-") as temp_name:
            with self.assertRaises(MODULE.DOWNLOADER.DownloadError):
                MODULE.cleanup_work_dir(temp_name)

    def test_cleanup_accepts_owned_temp_directory(self) -> None:
        path = Path(tempfile.mkdtemp(prefix="ah-douyin-transcript-"))
        (path / "draft.txt").write_text("test", encoding="utf-8")
        result = MODULE.cleanup_work_dir(str(path))
        self.assertEqual(result["status"], "cleaned")
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
