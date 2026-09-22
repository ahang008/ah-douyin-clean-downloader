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
    def test_final_deliverables_are_markdown_and_srt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output = Path(temp_name)
            result = MODULE.transcript_destinations(output, "作者", "标题", "7123456789012345678")
            self.assertEqual(set(result), {"markdown", "srt"})
            self.assertEqual(result["markdown"].suffix, ".md")
            self.assertEqual(result["srt"].suffix, ".srt")
            self.assertEqual(result["markdown"].stem, result["srt"].stem)
            self.assertIn("校对后逐字稿", result["markdown"].name)
            self.assertEqual(result["markdown"].parent.name, "作者")

    def test_final_path_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            output = Path(temp_name)
            first = MODULE.transcript_destinations(output, "作者", "标题", "7123456789012345678")
            first["srt"].touch()
            second = MODULE.transcript_destinations(output, "作者", "标题", "7123456789012345678")
            self.assertNotEqual(first["markdown"], second["markdown"])
            self.assertTrue(second["markdown"].name.endswith("-2.md"))
            self.assertTrue(second["srt"].name.endswith("-2.srt"))

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
