"""Batch resume/checkpoint fixtures using local byte files and a stub ffprobe."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "download_batch.py"
SPEC = importlib.util.spec_from_file_location("creator_download_batch_fixture", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
VIDEO_ID = "7600000000000000001"


class CreatorDownloadBatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "media"
        self.manifest = self.root / "download-manifest.json"
        self.rows, self.meta = MODULE.normalize_catalog({"catalog_complete": True, "videos": [
            {"video_id": VIDEO_ID, "title": "Fixture work"}]})
        self.args = SimpleNamespace(backend=MODULE.DEFAULT_BACKEND, proxy=None, timeout=30,
                                    download_timeout=60, attempts=1, workers=1, limit=0)
        self.backend = SimpleNamespace(verify_media=Mock(return_value={"verification": "ffprobe",
            "video_streams": [{"codec_name": "h264"}], "audio_streams": [{"codec_name": "aac"}],
            "duration_seconds": 2.0}))
        self.calls = []

    def fake_download(self, args):
        self.calls.append(args)
        path = self.root / "Fixture author" / ("Fixture work-" + VIDEO_ID + ".mp4")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"offline local fixture; never decoded" * 100)
        return {"status": "ok", "video_id": VIDEO_ID, "path": str(path),
                "author": "Fixture author", "title": "Fixture work", "transcoded": False}

    def batch(self, download=None):
        return MODULE.DownloadBatch(self.rows, self.meta, self.root, self.manifest,
                                    self.backend, self.args, download_fn=download or self.fake_download)

    def run_batch(self, batch):
        with contextlib.redirect_stdout(io.StringIO()):
            return batch.run()

    def test_default_backend_is_bundled_and_original_api_can_be_imported(self):
        self.assertEqual(MODULE.DEFAULT_BACKEND, SCRIPT.parent / "download_douyin.py")
        backend = MODULE.load_backend(MODULE.DEFAULT_BACKEND)
        self.assertTrue(callable(backend.download))
        self.assertTrue(callable(backend.verify_media))
        self.assertTrue(hasattr(backend.CurlClient, "_ordered_proxies"))

    def test_verified_resume_rechecks_hash_and_probe_without_redownloading(self):
        self.assertEqual(self.run_batch(self.batch())["downloaded"], 1)
        self.assertEqual(self.run_batch(self.batch())["skipped"], 1)
        self.assertEqual(len(self.calls), 1)
        saved = json.loads(self.manifest.read_text())["videos"][VIDEO_ID]
        self.assertEqual(saved["status"], "verified")
        self.assertEqual(saved["last_action"], "reverified_existing")
        self.assertTrue(saved["checksum_readback"])
        self.assertGreaterEqual(self.backend.verify_media.call_count, 2)

    def test_same_length_media_tamper_forces_new_download_instead_of_verified_skip(self):
        self.run_batch(self.batch())
        saved = json.loads(self.manifest.read_text())["videos"][VIDEO_ID]
        path = Path(saved["path"])
        original = path.read_bytes()
        path.write_bytes(b"corrupted" + original[9:])
        self.assertEqual(self.run_batch(self.batch())["downloaded"], 1)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(path.read_bytes(), original)

    def test_committed_file_after_interrupted_manifest_is_adopted_without_request(self):
        self.fake_download(None)
        self.calls.clear()
        forbidden = Mock(side_effect=AssertionError("Adoption must not start a request"))
        self.assertEqual(self.run_batch(self.batch(forbidden))["adopted"], 1)
        forbidden.assert_not_called()
        saved = json.loads(self.manifest.read_text())["videos"][VIDEO_ID]
        self.assertEqual(saved["status"], "verified")
        self.assertEqual(saved["last_action"], "adopted_existing")

    def test_wrong_result_identity_marks_failure_and_keeps_original_file(self):
        path = self.root / "unexpected.mp4"

        def wrong_result(args):
            path.write_bytes(b"keep offline source unchanged" * 100)
            return {"status": "ok", "video_id": "7600000000000000002", "path": str(path)}

        self.assertEqual(self.run_batch(self.batch(wrong_result))["failed"], 1)
        saved = json.loads(self.manifest.read_text())["videos"][VIDEO_ID]
        self.assertEqual(saved["status"], "failed")
        self.assertNotIn("path", saved)
        self.assertEqual(path.read_bytes(), b"keep offline source unchanged" * 100)

    def test_sample_limits_manifest_selection(self):
        self.rows.append({"video_id": "7600000000000000002", "canonical_url": "https://www.douyin.com/video/7600000000000000002"})
        self.args.limit = 1
        result = self.run_batch(self.batch())
        self.assertEqual(result["selected"], 1)
        self.assertEqual(set(json.loads(self.manifest.read_text())["videos"]), {VIDEO_ID})


if __name__ == "__main__":
    unittest.main()
