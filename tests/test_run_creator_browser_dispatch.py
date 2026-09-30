"""Controller dispatch fixtures: no browser, network, Keychain, or ASR process."""
import builtins
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_creator.py"
SPEC = importlib.util.spec_from_file_location("run_creator_dispatch_fixture", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SEC_UID = "MS4wLjFixtureCreatorDispatch"
PROFILE = "https://www.douyin.com/user/" + SEC_UID


class BrowserDispatchFixtures(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve() / "library"
        self.root.mkdir()
        self.catalog_path = self.root / "catalog" / "catalog.json"
        self.calls = []

    def tearDown(self):
        self.directory.cleanup()

    def catalog(self, complete=False, sec_uid=SEC_UID):
        value = {"creator": {"sec_uid": sec_uid}, "catalog_complete": complete,
                 "videos": [{"video_id": "7600000000000000001", "title": "fixture public work",
                             "canonical_url": "https://www.douyin.com/video/7600000000000000001"}]}
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self.catalog_path.write_text(json.dumps(value))
        return value

    def fake_run_logged(self, command, log, root, catalog_path, limit, stage):
        self.calls.append((stage, list(command)))
        if stage == "collect":
            self.catalog(complete=True)
        return 0

    def execute(self, arguments):
        real_import = builtins.__import__

        def no_old_session_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "edge_session" or name.startswith("edge_session."):
                raise AssertionError("Browser-session dispatch imported the old credential reader")
            return real_import(name, globals, locals, fromlist, level)

        with patch.object(sys, "argv", [str(SCRIPT), "--root", str(self.root), *arguments]), \
             patch.object(MODULE, "run_logged", side_effect=self.fake_run_logged), \
             patch.object(MODULE.subprocess, "Popen", side_effect=AssertionError("A real child process was attempted")), \
             patch.object(MODULE.subprocess, "call", side_effect=AssertionError("A real subprocess call was attempted")), \
             patch.object(MODULE.importlib.util, "spec_from_file_location", side_effect=AssertionError("Canonical browser dispatch imported an HTTP/session resolver")), \
             patch.object(builtins, "__import__", side_effect=no_old_session_import), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return MODULE.main()

    def test_canonical_profile_selects_browser_launcher_and_passes_session_options(self):
        self.catalog(complete=False)
        session = self.root / "isolated browser"
        code = self.execute([PROFILE, "--stage", "collect", "--browser-session",
                             "--browser-session-dir", str(session), "--browser-timeout", "417"])
        self.assertEqual(code, 0)
        self.assertEqual([stage for stage, _ in self.calls], ["collect", "metadata"])
        stage, command = self.calls[0]
        self.assertEqual(stage, "collect")
        self.assertEqual(Path(command[1]).name, "launch_browser_collector.py")
        self.assertEqual(command[command.index("--profile") + 1], PROFILE)
        self.assertEqual(command[command.index("--session-dir") + 1], str(session.resolve()))
        self.assertEqual(command[command.index("--timeout") + 1], "417")
        self.assertEqual(command[command.index("--output") + 1], str(self.catalog_path))
        for forbidden in ("--edge-session", "--edge-profile", "--keychain-timeout", "--resume"):
            self.assertNotIn(forbidden, command)

    def test_raw_sec_uid_selects_default_isolated_session_and_timeout(self):
        code = self.execute([SEC_UID, "--stage", "collect"])
        self.assertEqual(code, 0)
        command = self.calls[0][1]
        self.assertEqual(Path(command[1]).name, "launch_browser_collector.py")
        self.assertEqual(command[command.index("--profile") + 1], PROFILE)
        self.assertEqual(command[command.index("--session-dir") + 1], str(self.root / ".browser-session"))
        self.assertEqual(command[command.index("--timeout") + 1], "600")

    def test_complete_catalog_in_all_stage_skips_browser_collection(self):
        self.catalog(complete=True)
        # No media is created by these mocks, so the overall job correctly remains incomplete.
        code = self.execute([PROFILE, "--stage", "all", "--browser-session", "--serial"])
        self.assertEqual(code, 1)
        self.assertEqual([stage for stage, _ in self.calls], ["download", "transcribe", "metadata"])
        self.assertTrue(all(Path(command[1]).name != "launch_browser_collector.py" for _, command in self.calls))

    def test_download_stage_uses_existing_partial_catalog_without_collecting(self):
        self.catalog(complete=False)
        code = self.execute([PROFILE, "--stage", "download", "--browser-session"])
        self.assertEqual(code, 0)
        self.assertEqual([stage for stage, _ in self.calls], ["download"])

    def test_metadata_stage_needs_only_saved_catalog_and_no_media_process(self):
        self.catalog(complete=True)
        self.assertEqual(self.execute(["--stage", "metadata", "--limit", "1"]), 0)
        self.assertEqual([stage for stage, _ in self.calls], ["metadata"])
        command = self.calls[0][1]
        self.assertEqual(Path(command[1]).name, "collect_cover_metadata.py")
        self.assertEqual(command[command.index("--catalog") + 1], str(self.catalog_path))
        self.assertEqual(command[command.index("--limit") + 1], "1")

    def test_metadata_stage_accepts_older_browser_catalog_name(self):
        self.catalog(complete=True)
        legacy = self.catalog_path.with_name("browser-catalog.json")
        self.catalog_path.rename(legacy)
        self.assertEqual(self.execute(["--stage", "metadata"]), 0)
        self.assertEqual([stage for stage, _ in self.calls], ["metadata"])
        command = self.calls[0][1]
        self.assertEqual(command[command.index("--catalog") + 1], str(legacy))

    def test_metadata_stage_generates_reading_copies_when_records_exist(self):
        self.catalog(complete=True)
        metadata = self.root / "catalog/作品标题标签封面.jsonl"
        metadata.write_text(json.dumps({"video_id": "7600000000000000001",
                                        "catalog_sha256": hashlib.sha256(self.catalog_path.read_bytes()).hexdigest(),
                                        "cover_status": "not_attempted"}) + "\n")
        self.assertEqual(self.execute(["--stage", "metadata"]), 0)
        self.assertEqual([stage for stage, _ in self.calls], ["metadata", "annotate"])
        self.assertEqual(Path(self.calls[1][1][1]).name, "build_annotated_transcripts.py")

    def test_refresh_complete_catalog_dispatches_browser_collection(self):
        self.catalog(complete=True)
        self.execute([PROFILE, "--stage", "all", "--browser-session", "--refresh-catalog", "--serial"])
        self.assertEqual([stage for stage, _ in self.calls], ["collect", "download", "transcribe", "metadata"])
        self.assertEqual(Path(self.calls[0][1][1]).name, "launch_browser_collector.py")

    def test_removed_credential_route_is_rejected_before_any_child(self):
        self.catalog(complete=True)
        before = self.catalog_path.read_bytes()
        with self.assertRaises(SystemExit) as exit:
            self.execute([PROFILE, "--browser-session", "--edge-session"])
        self.assertEqual(exit.exception.code, 2)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.catalog_path.read_bytes(), before)

    def prepared_library(self):
        self.catalog(complete=True)
        video_id = "7600000000000000001"
        media = self.root / "media" / (video_id + ".mp4")
        media.parent.mkdir()
        media.write_bytes(b"offline fixture; not decoded")
        (media.parent / "download-manifest.json").write_text(json.dumps({"videos": {
            video_id: {"status": "verified", "path": str(media)}}}))
        directory = self.root / "local-transcripts" / video_id
        directory.mkdir(parents=True)
        for name in ("01-本地ASR机器逐字稿.md", "01-本地ASR机器逐字稿.srt", "01-本地ASR识别证据.json"):
            (directory / name).write_text("{}" if name.endswith(".json") else "offline fixture")
        (directory.parent / "_batch-state.json").write_text(json.dumps({"jobs": {
            video_id: {"status": "machine_draft_saved"}}}))

    def test_complete_all_dispatches_corpus_export_after_download_and_transcription(self):
        self.prepared_library()
        code = self.execute([PROFILE, "--serial"])
        self.assertEqual(code, 0)
        self.assertEqual([stage for stage, _ in self.calls], ["download", "transcribe", "metadata", "export"])
        command = self.calls[-1][1]
        self.assertEqual(Path(command[1]).name, "export_transcripts.py")
        self.assertEqual(command[command.index("--root") + 1], str(self.root))
        summary = json.loads((self.root / "pipeline-summary.json").read_text())
        self.assertTrue(summary["corpus_exported"])

    def test_limit_never_exports_full_corpus_even_when_selected_library_is_ready(self):
        self.prepared_library()
        self.assertEqual(self.execute([PROFILE, "--serial", "--limit", "1"]), 0)
        self.assertEqual([stage for stage, _ in self.calls], ["download", "transcribe", "metadata"])
        summary = json.loads((self.root / "pipeline-summary.json").read_text())
        self.assertFalse(summary["all_public_videos_transcribed"])
        self.assertFalse(summary["corpus_exported"])

    def test_dns_mode_is_forwarded_to_dedicated_launcher(self):
        self.assertEqual(self.execute([PROFILE, "--stage", "collect", "--dns-mode", "off"]), 0)
        command = self.calls[0][1]
        self.assertEqual(command[command.index("--dns-mode") + 1], "off")

    def test_export_validation_failure_is_nonzero_and_not_claimed_exported(self):
        self.prepared_library()
        original = self.fake_run_logged

        def failed(*arguments):
            result = original(*arguments)
            return 3 if arguments[-1] == "export" else result

        self.fake_run_logged = failed
        self.assertEqual(self.execute([PROFILE, "--serial"]), 1)
        self.assertFalse(json.loads((self.root / "pipeline-summary.json").read_text())["corpus_exported"])

    def test_cover_failure_is_reported_but_does_not_discard_ready_corpus_export(self):
        self.prepared_library()
        original = self.fake_run_logged

        def failed_cover(*arguments):
            return 1 if arguments[-1] == "metadata" else original(*arguments)

        self.fake_run_logged = failed_cover
        self.assertEqual(self.execute([PROFILE, "--serial"]), 1)
        self.assertEqual([stage for stage, _ in self.calls], ["download", "transcribe", "export"])
        self.assertTrue(json.loads((self.root / "pipeline-summary.json").read_text())["corpus_exported"])
        self.assertEqual(json.loads((self.root / "pipeline-run.json").read_text())["stage_exit_codes"]["metadata"], 1)

    def test_wrong_creator_is_rejected_without_replacing_existing_catalog(self):
        self.catalog(complete=False, sec_uid="MS4wLjAnotherCreator")
        before = self.catalog_path.read_bytes()
        with self.assertRaises(ValueError):
            self.execute([PROFILE, "--stage", "collect", "--browser-session"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.catalog_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
