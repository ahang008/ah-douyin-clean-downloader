"""Exercise the existing single-video workflow with local synthetic side effects."""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/transcribe_douyin.py"
SPEC = importlib.util.spec_from_file_location("single_workflow_compatibility", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SingleWorkflowCompatibilityTests(unittest.TestCase):
    def test_preparation_is_a_draft_and_cleanup_preserves_reviewed_deliverables(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(share_text=["synthetic authorized video"], output_dir=directory,
                                      proxy=None, timeout=60, download_timeout=1800, model=MODULE.DEFAULT_MODEL)
            commands = []

            def downloaded(download_args):
                media = Path(download_args.output_dir) / "author/original.mp4"
                media.parent.mkdir(parents=True)
                media.write_bytes(b"synthetic original")
                return {"path": str(media), "author": "合成作者", "title": "测试标题",
                        "video_id": "7600000000000000011", "duration_seconds": 2.0}

            def recognized(command, **kwargs):
                commands.append(command)
                draft = Path(command[command.index("--output-dir") + 1])
                (draft / "机器识别稿.txt").write_text("机器识别文本。\n")
                (draft / "机器识别稿.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\n机器识别文本。\n")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(MODULE, "require_command", return_value="/synthetic/bin/mlx_whisper"), \
                 patch.object(MODULE.DOWNLOADER, "download", side_effect=downloaded), \
                 patch.object(MODULE.subprocess, "run", side_effect=recognized):
                result = MODULE.prepare_transcript(args)
            try:
                self.assertEqual(result["status"], "draft_ready")
                self.assertFalse(Path(result["final_path"]).exists())
                self.assertFalse(Path(result["final_srt_path"]).exists())
                self.assertTrue(Path(result["draft_path"]).is_file())
                command = commands[0]
                self.assertEqual(command[command.index("--model") + 1], MODULE.DEFAULT_MODEL)
                self.assertEqual(command[command.index("--condition-on-previous-text") + 1], "False")
                final = Path(result["final_path"])
                final_srt = Path(result["final_srt_path"])
                final.write_text("校对后的正文。\n")
                final_srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n校对后的正文。\n")
                MODULE.cleanup_work_dir(result["work_dir"])
                self.assertFalse(Path(result["work_dir"]).exists())
                self.assertEqual(final.read_text(), "校对后的正文。\n")
                self.assertEqual(set(path.suffix for path in final.parent.iterdir()), {".md", ".srt"})
            finally:
                if Path(result["work_dir"]).exists():
                    MODULE.cleanup_work_dir(result["work_dir"])


if __name__ == "__main__":
    unittest.main()
