"""Offline naming, legacy-layout, and crash-recovery acceptance fixtures."""
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def helper(filename, name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NAMES = helper("name_artifacts.py", "name_artifacts_fixture")
PATHS = helper("artifact_paths.py", "name_artifact_paths_fixture")
BROWSER = helper("collect_creator_browser.py", "name_catalog_fixture")
ASR = helper("transcribe_local.py", "name_asr_fixture")
DOWNLOAD = helper("download_batch.py", "name_download_fixture")
VIDEO_ID = "7600000000000000001"
SEC_UID = "MS4wLjNamingFixtureCreator"
EXTENSIONS = ("statistics", "statistics_captured_at", "statistics_availability")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ArtifactNamingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "library"
        self.root.mkdir()
        self.catalog_path = self.root / "catalog" / "catalog.json"
        self.statistics = {"digg_count": 42, "comment_count": 0, "collect_count": 5, "share_count": 3}
        self.make_catalog()
        self.media = self.root / "media" / "Fixture author" / ("抖音-测试作品-" + VIDEO_ID + ".mp4")
        self.media.parent.mkdir(parents=True)
        self.media.write_bytes(b"offline fixture, no download or decoding" * 100)
        self.manifest_path = self.root / "media" / "download-manifest.json"
        self.manifest = {"schema_version": 1, "download_root": str(self.root / "media"), "videos": {
            VIDEO_ID: {"status": "verified", "path": str(self.media), "sha256": digest(self.media),
                       "title": "测试作品", "bytes": self.media.stat().st_size}}}
        self.manifest_path.write_text(json.dumps(self.manifest, ensure_ascii=False))
        self.directory = self.root / "local-transcripts" / VIDEO_ID
        self.directory.mkdir(parents=True)
        self.md = self.directory / PATHS.OUTPUT_NAMES["markdown"]
        self.srt = self.directory / PATHS.OUTPUT_NAMES["srt"]
        self.md.write_text("# Fixture\n\n原片旧位置：" + str(self.media) + "\n\n## 机器识别原文\n\n原始机器正文。\n")
        self.segments = [{"start": 0.0, "end": 2.0, "text": "原始机器正文。"}]
        self.srt.write_text(ASR.render_srt(self.segments))
        self.model = {"weights_sha256": "a" * 64, "config_sha256": "b" * 64, "mlx_whisper_version": "fixture"}
        self.settings = {"language": "zh", "task": "transcribe", "temperature": 0.0}
        self.evidence = {"video_id": VIDEO_ID, "status": "machine_draft_saved", "raw_text": "原始机器正文。",
                         "segments": self.segments, "source_url": "https://www.douyin.com/video/" + VIDEO_ID,
                         "completed_at": "2026-09-01T00:00:00+00:00", "model_settings_key": ASR.model_key(self.model, self.settings),
                         "source_media": {"path": str(self.media), "sha256": digest(self.media), "bytes": self.media.stat().st_size},
                         "source_audio": {"duration_seconds": 2.0},
                         "validation": {**ASR.validate_result({"text": "原始机器正文。", "segments": self.segments}, 2.0), "warnings": []},
                         # Legacy evidence can contain hashes without any output paths.
                         "outputs": {"markdown": {"sha256": digest(self.md)}, "srt": {"sha256": digest(self.srt)}}}
        self.evidence_path = self.directory / PATHS.OUTPUT_NAMES["evidence"]
        self.evidence_path.write_text(json.dumps(self.evidence, ensure_ascii=False))
        self.state_path = self.root / "local-transcripts" / "_batch-state.json"
        self.state_path.write_text(json.dumps({"jobs": {VIDEO_ID: {
            "status": "machine_draft_saved", "media_path": str(self.media)}}}))
        self.original = {"media": self.media.read_bytes(), "markdown": self.md.read_bytes(), "srt": self.srt.read_bytes()}

    def make_catalog(self, *, legacy=False):
        recorder = BROWSER.BrowserCatalog(SEC_UID, self.catalog_path)
        work = {"aweme_id": VIDEO_ID, "desc": "测试作品", "create_time": 123,
                "aweme_type": 0, "author": {"sec_uid": SEC_UID, "nickname": "Fixture author", "uid": "123"},
                "video": {"duration": 2000}, "statistics": self.statistics}
        payload = {"status_code": 0, "aweme_list": [work], "max_cursor": 0, "has_more": 0,
                   "not_login_module": {"guide_login_tip_exist": False}}
        recorder.add(0, payload, json.dumps(payload).encode())
        self.catalog = recorder.save()
        if legacy:
            for work in self.catalog["videos"]:
                for key in EXTENSIONS:
                    work.pop(key, None)
            for page in self.catalog["page_evidence"]:
                saved = Path(page["file"])
                projection = json.loads(saved.read_text())
                for row in projection["aweme_list"]:
                    for key in EXTENSIONS:
                        row.pop(key, None)
                saved.write_text(json.dumps(projection, ensure_ascii=False))
                page["saved_file_sha256"] = digest(saved)
            self.catalog_path.write_text(json.dumps(self.catalog, ensure_ascii=False))
        return self.catalog

    def reconcile(self):
        return NAMES.reconcile_names(self.root, self.catalog_path)

    def actual_paths(self):
        manifest = json.loads(self.manifest_path.read_text())["videos"][VIDEO_ID]
        paths = PATHS.resolve_artifact_paths(self.directory)
        return {"media": Path(manifest["path"]), "markdown": paths["markdown"], "srt": paths["srt"]}

    def assert_named_bytes(self, stem="赞42_评0_藏5_转3_测试作品-" + VIDEO_ID):
        for kind, path in self.actual_paths().items():
            self.assertEqual(path.stem, stem)
            self.assertEqual(path.read_bytes(), self.original[kind])
        self.assertFalse((self.root / NAMES.JOURNAL_NAME).exists())

    def test_actual_filenames_keep_bytes_and_sync_every_pointer(self):
        result = self.reconcile()
        self.assertEqual(result["renamed"], 3)
        self.assertEqual(result["renamed_videos"], 1)
        self.assert_named_bytes()
        self.assertFalse(self.media.exists())
        self.assertFalse(self.md.exists())
        self.assertFalse(self.srt.exists())
        manifest = json.loads(self.manifest_path.read_text())["videos"][VIDEO_ID]
        evidence = json.loads(self.evidence_path.read_text())
        state = json.loads(self.state_path.read_text())["jobs"][VIDEO_ID]
        self.assertEqual(evidence["source_media"]["path"], manifest["path"])
        self.assertEqual(state["media_path"], manifest["path"])
        self.assertEqual(evidence["raw_text"], self.evidence["raw_text"])
        self.assertEqual(evidence["segments"], self.evidence["segments"])
        self.assertEqual(evidence["model_settings_key"], self.evidence["model_settings_key"])
        self.assertEqual(manifest["statistics"], self.catalog["videos"][0]["statistics"])
        self.assertEqual(evidence["public_statistics"]["statistics"], manifest["statistics"])
        self.assertEqual(evidence["public_statistics"]["source_catalog_sha256"], digest(self.catalog_path))
        before = {path: path.read_bytes() for path in (self.manifest_path, self.evidence_path, self.state_path)}
        self.assertEqual(self.reconcile()["renamed"], 0)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_metric_refresh_renames_existing_bytes_without_duplicate_outputs(self):
        self.reconcile()
        old_paths = self.actual_paths()
        self.statistics["digg_count"] = 99
        self.make_catalog()
        self.assertEqual(self.reconcile()["renamed"], 3)
        self.assert_named_bytes("赞99_评0_藏5_转3_测试作品-" + VIDEO_ID)
        self.assertTrue(all(not path.exists() for path in old_paths.values()))
        self.assertEqual(len(list((self.root / "media").rglob("*.mp4"))), 1)
        self.assertEqual(len(list(self.directory.glob("*.md"))), 1)
        self.assertEqual(len(list(self.directory.glob("*.srt"))), 1)

    def test_legacy_catalog_without_observation_does_not_rename_or_rewrite(self):
        self.make_catalog(legacy=True)
        before = {path: path.read_bytes() for path in (self.media, self.md, self.srt, self.evidence_path, self.manifest_path, self.state_path)}
        result = self.reconcile()
        self.assertEqual(result["unavailable"], 1)
        self.assertEqual(result["renamed"], 0)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_explicit_missing_metrics_use_unknown_instead_of_zero(self):
        self.statistics = {}
        self.make_catalog()
        result = self.reconcile()
        self.assertEqual(result["unknown_videos"], 1)
        self.assertEqual(result["unknown_metric_values"], 4)
        self.assert_named_bytes("赞未获取_评未获取_藏未获取_转未获取_测试作品-" + VIDEO_ID)

    def test_collision_fails_before_any_original_or_metadata_changes(self):
        target = self.media.with_name("赞42_评0_藏5_转3_测试作品-" + VIDEO_ID + ".mp4")
        target.write_bytes(b"unrelated user file")
        metadata = self.manifest_path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.reconcile()
        self.assertEqual(target.read_bytes(), b"unrelated user file")
        self.assertEqual(self.media.read_bytes(), self.original["media"])
        self.assertEqual(self.manifest_path.read_bytes(), metadata)
        self.assertFalse((self.root / NAMES.JOURNAL_NAME).exists())

    def test_symbolic_link_target_is_never_adopted(self):
        target = self.directory / ("赞42_评0_藏5_转3_测试作品-" + VIDEO_ID + ".md")
        target.symlink_to(self.md)
        with self.assertRaises(FileExistsError):
            self.reconcile()
        self.assertTrue(target.is_symlink())
        self.assertTrue(self.media.exists())

    def test_tampered_machine_file_is_preserved_without_starting_a_plan(self):
        self.md.write_bytes(b"modified machine content")
        metadata = self.manifest_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.reconcile()
        self.assertEqual(self.md.read_bytes(), b"modified machine content")
        self.assertEqual(self.manifest_path.read_bytes(), metadata)
        self.assertFalse((self.root / NAMES.JOURNAL_NAME).exists())

    def test_incomplete_or_tampered_catalog_cannot_apply_new_metrics(self):
        catalog = json.loads(self.catalog_path.read_text())
        catalog["catalog_complete"] = False
        self.catalog_path.write_text(json.dumps(catalog))
        with self.assertRaisesRegex(ValueError, "complete official"):
            self.reconcile()
        self.make_catalog()
        page = Path(self.catalog["page_evidence"][0]["file"])
        page.write_bytes(page.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.reconcile()
        self.assertTrue(self.media.exists())
        self.assertEqual(self.md.read_bytes(), self.original["markdown"])

    def test_relative_existing_pointers_are_accepted_and_made_explicit(self):
        manifest = json.loads(self.manifest_path.read_text())
        manifest["videos"][VIDEO_ID]["path"] = str(self.media.relative_to(self.root / "media"))
        self.manifest_path.write_text(json.dumps(manifest))
        evidence = json.loads(self.evidence_path.read_text())
        evidence["source_media"]["path"] = manifest["videos"][VIDEO_ID]["path"]
        evidence["outputs"]["markdown"]["path"] = self.md.name
        evidence["outputs"]["srt"]["path"] = self.srt.name
        self.evidence_path.write_text(json.dumps(evidence))
        self.reconcile()
        self.assert_named_bytes()

    def test_recovery_finishes_after_first_link_without_catalog_or_formatter(self):
        actual_link = NAMES.os.link

        def interrupt_after_link(*args, **kwargs):
            actual_link(*args, **kwargs)
            raise OSError("injected interruption after first new name")

        with patch.object(NAMES.os, "link", side_effect=interrupt_after_link):
            with self.assertRaisesRegex(OSError, "injected"):
                self.reconcile()
        self.assertTrue((self.root / NAMES.JOURNAL_NAME).is_file())
        self.catalog_path.unlink()
        with patch.object(NAMES, "_catalog", side_effect=AssertionError("Recovery must use its recorded plan")), \
                patch.object(NAMES, "_helper", side_effect=AssertionError("Recovery must not recompute filenames")):
            report = self.reconcile()
        self.assertTrue(report["recovered"])
        self.assert_named_bytes()

    def test_recovery_after_each_metadata_commit_and_after_old_names_removed(self):
        for checkpoint in ("manifest", "evidence", "removed"):
            with self.subTest(checkpoint=checkpoint):
                if checkpoint != "manifest":
                    self.setUp()
                original_atomic = NAMES.atomic_json

                def interrupt_after_write(path, value):
                    original_atomic(path, value)
                    expected = self.manifest_path if checkpoint == "manifest" else self.evidence_path
                    if Path(path) == expected:
                        raise OSError("injected metadata interruption")

                if checkpoint == "removed":
                    original_remove = NAMES._remove_old_names

                    def interrupt_after_remove(items):
                        original_remove(items)
                        raise OSError("injected cleanup interruption")

                    interruption = patch.object(NAMES, "_remove_old_names", side_effect=interrupt_after_remove)
                else:
                    interruption = patch.object(NAMES, "atomic_json", side_effect=interrupt_after_write)
                with interruption:
                    with self.assertRaisesRegex(OSError, "injected"):
                        self.reconcile()
                self.assertTrue((self.root / NAMES.JOURNAL_NAME).is_file())
                self.assertTrue(self.reconcile()["recovered"])
                self.assert_named_bytes()

    def test_recovery_rejects_tampered_target_without_overwriting_either_name(self):
        with patch.object(NAMES, "_commit_metadata", side_effect=OSError("injected stop")):
            with self.assertRaises(OSError):
                self.reconcile()
        target = self.directory / ("赞42_评0_藏5_转3_测试作品-" + VIDEO_ID + ".md")
        target.unlink()  # Replace the new name, leaving the old inode untouched.
        target.write_bytes(b"user replacement")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.reconcile()
        self.assertEqual(target.read_bytes(), b"user replacement")
        self.assertEqual(self.md.read_bytes(), self.original["markdown"])
        self.assertTrue((self.root / NAMES.JOURNAL_NAME).exists())

    def test_named_outputs_resume_download_and_asr_without_inference_or_request(self):
        self.reconcile()
        forbidden_download = Mock(side_effect=AssertionError("No request is needed"))
        backend = SimpleNamespace(verify_media=Mock(return_value={"verification": "ffprobe", "video_streams": [{}],
                                                                 "audio_streams": [{}], "duration_seconds": 2.0}))
        args = SimpleNamespace(backend=DOWNLOAD.DEFAULT_BACKEND, proxy=None, timeout=30, download_timeout=60,
                               attempts=1, workers=1, limit=0)
        rows, meta = DOWNLOAD.normalize_catalog(self.catalog)
        batch = DOWNLOAD.DownloadBatch(rows, meta, self.root / "media", self.manifest_path, backend, args,
                                       download_fn=forbidden_download)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(batch.run()["skipped"], 1)
        forbidden_download.assert_not_called()
        media = self.actual_paths()["media"]
        job = {"video_id": VIDEO_ID, "media_path": str(media), "expected_media_sha256": digest(media)}
        with patch.object(ASR, "inspect_media", side_effect=AssertionError("No new ASR decode")), \
                patch.object(ASR, "extract_audio", side_effect=AssertionError("No new audio extraction")):
            self.assertEqual(ASR.transcribe_job(job, self.root / "local-transcripts", self.model, self.settings)["status"], "skipped_verified")

    def test_locks_exclude_existing_pipeline_downloader_and_asr_writers(self):
        cases = ((self.root / ".pipeline.lock", lambda: NAMES.main(["--root", str(self.root)])),
                 (self.root / "media" / "download-manifest.json.lock", self.reconcile),
                 (self.root / "local-transcripts" / ".transcribe.lock", self.reconcile))
        for lock_path, action in cases:
            with self.subTest(lock=lock_path.name), lock_path.open("a+") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    with self.assertRaisesRegex(RuntimeError, "holds"):
                        action()
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        self.assertFalse((self.root / NAMES.JOURNAL_NAME).exists())


class ArtifactPathResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve() / VIDEO_ID
        self.directory.mkdir()

    def test_missing_evidence_keeps_legacy_pending_paths(self):
        paths = PATHS.resolve_artifact_paths(self.directory)
        for kind, name in PATHS.OUTPUT_NAMES.items():
            self.assertEqual(paths[kind], self.directory / name)
            self.assertFalse(paths[kind].exists())

    def test_recorded_dynamic_path_is_authoritative_even_when_missing(self):
        paths = PATHS.resolve_artifact_paths(self.directory, {"outputs": {"markdown": {"path": "named.md"}}})
        self.assertEqual(paths["markdown"], self.directory / "named.md")
        self.assertEqual(paths["srt"], self.directory / PATHS.OUTPUT_NAMES["srt"])

    def test_outside_wrong_extension_directory_and_symlink_pointers_are_rejected(self):
        bad_paths = [self.directory.parent / "outside.md", self.directory / "wrong.srt"]
        nested = self.directory / "nested.md"
        nested.mkdir()
        bad_paths.append(nested)
        target = self.directory / "plain.md"
        target.write_text("fixture")
        link = self.directory / "linked.md"
        link.symlink_to(target)
        bad_paths.append(link)
        for path in bad_paths:
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                PATHS.resolve_artifact_paths(self.directory, {"outputs": {"markdown": {"path": str(path)}}})


if __name__ == "__main__":
    unittest.main()
