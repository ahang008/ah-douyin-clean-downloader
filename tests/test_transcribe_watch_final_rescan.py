"""Offline watch-completion regressions; no model, decoder, or network calls."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "transcribe_local.py"
SPEC = importlib.util.spec_from_file_location("transcribe_watch_fixture", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

FIRST_ID = "7600000000000000001"
SECOND_ID = "7600000000000000002"


class WatchFinalRescanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = self.root / "download-manifest.json"
        self.done = self.root / "download.done"
        self.output = self.root / "transcripts"
        self.calls = []
        self.events = io.StringIO()
        self.model = {"repository": "fixture/model", "local_path": str(self.root / "model")}

    def media_row(self, video_id, status="verified"):
        media = self.root / (video_id + ".mp4")
        media.write_bytes(b"temporary fixture; never decoded")
        return {"video_id": video_id, "status": status, "path": str(media)}

    def write_manifest(self, *rows):
        value = {"schema_version": 1, "videos": {row["video_id"]: row for row in rows}}
        self.manifest.write_text(json.dumps(value), encoding="utf-8")

    def saved_outcome(self, job, *_unused):
        self.calls.append(job["video_id"])
        return {"status": "machine_draft_saved", "video_id": job["video_id"],
                "character_count": 2, "segment_count": 1, "warnings": []}

    def run_watch(self, transcribe=None):
        argv = [str(SCRIPT), "--manifest", str(self.manifest), "--output-dir", str(self.output),
                "--watch", "--done-file", str(self.done), "--poll-seconds", "1"]
        with patch.object(MODULE.sys, "argv", argv), \
                patch.object(MODULE, "resolve_model", return_value=self.model), \
                patch.object(MODULE.shutil, "which", return_value="/fixture/tool"), \
                patch.object(MODULE, "transcribe_job", side_effect=transcribe or self.saved_outcome), \
                patch.object(MODULE.time, "sleep", side_effect=AssertionError("completed watch must not sleep")), \
                patch.dict(os.environ), redirect_stdout(self.events):
            return MODULE.main()

    def state(self):
        return json.loads((self.output / "_batch-state.json").read_text(encoding="utf-8"))

    def test_committed_media_during_first_transcription_is_not_lost_at_done_marker(self):
        first = self.media_row(FIRST_ID)
        self.write_manifest(first)

        def transcribe(job, _root, _model, _settings):
            outcome = self.saved_outcome(job)
            if job["video_id"] == FIRST_ID:
                self.write_manifest(first, self.media_row(SECOND_ID))
                self.done.write_text("download complete\n", encoding="utf-8")
            return outcome

        self.assertEqual(self.run_watch(transcribe), 0)
        self.assertEqual(self.calls, [FIRST_ID, SECOND_ID])
        self.assertEqual(set(self.state()["jobs"]), {FIRST_ID, SECOND_ID})
        self.assertEqual(self.state()["pending_media_entries"], 0)

    def test_done_marker_with_failed_or_downloading_media_returns_nonzero(self):
        for status in ("failed", "downloading"):
            with self.subTest(status=status):
                self.calls.clear()
                self.write_manifest(self.media_row(FIRST_ID), self.media_row(SECOND_ID, status))
                self.done.write_text("downloader exited\n", encoding="utf-8")
                self.assertNotEqual(self.run_watch(), 0)
                self.assertEqual(self.calls, [FIRST_ID])
                self.assertEqual(self.state()["pending_media_entries"], 1)

    def test_ready_media_already_attempted_are_not_transcribed_twice(self):
        self.write_manifest(self.media_row(FIRST_ID), self.media_row(SECOND_ID))
        self.done.write_text("download complete\n", encoding="utf-8")
        self.assertEqual(self.run_watch(), 0)
        self.assertEqual(self.calls, [FIRST_ID, SECOND_ID])
        self.assertEqual(self.state()["pending_media_entries"], 0)


if __name__ == "__main__":
    unittest.main()
