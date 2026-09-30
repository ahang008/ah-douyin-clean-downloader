"""Metrics renames retain ASR bytes, exporter integrity, and resume ability."""
import csv
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def helper(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIXTURE = helper(ROOT / "tests/test_creator_export.py", "metrics_export_fixture")
NAMING = helper(ROOT / "scripts/name_artifacts.py", "metrics_naming_integration")
CONTROLLER = helper(ROOT / "scripts/run_creator.py", "metrics_controller_integration")
PATHS = helper(ROOT / "scripts/artifact_paths.py", "metrics_paths_integration")


class CreatorMetricsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = FIXTURE.CreatorExportTests("test_check_hashes_all_inputs_and_writes_no_exports")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        # Capture realistic public counts, including a genuine zero and omissions.
        recorder = FIXTURE.BROWSER.BrowserCatalog(FIXTURE.SEC_UID, self.fixture.catalog_path)
        works = [self.fixture.work(number) for number in (1, 2)]
        for work in works:
            work["statistics"] = {"digg_count": 1234, "comment_count": 0, "share_count": 90, "play_count": 0}
        payload = self.fixture.page(works, 0, 0)
        recorder.add(0, payload, json.dumps(payload).encode())
        self.catalog = recorder.save()
        self.original_hashes = {}
        for video_id in self.fixture.ids:
            directory = self.fixture.root / "local-transcripts" / video_id
            evidence_path = directory / PATHS.OUTPUT_NAMES["evidence"]
            evidence = json.loads(evidence_path.read_text())
            evidence["model_settings_key"] = "fixture-settings-key"
            evidence_path.write_text(json.dumps(evidence))
            self.original_hashes[video_id] = {
                "media": self.fixture.manifest["videos"][video_id]["sha256"],
                **{kind: evidence["outputs"][kind]["sha256"] for kind in ("markdown", "srt")},
            }

    def test_renamed_library_exports_same_text_and_asr_resume_verifies_new_paths(self):
        NAMING.reconcile_names(self.fixture.root, self.fixture.catalog_path)
        self.assertEqual(self.fixture.run_export(), 0)
        output = self.fixture.root / "catalog/transcripts-2.jsonl"
        rows = [json.loads(line) for line in output.read_text().splitlines()]
        for row in rows:
            video_id = row["video_id"]
            directory = self.fixture.root / "local-transcripts" / video_id
            paths = PATHS.resolve_artifact_paths(directory)
            self.assertTrue(FIXTURE.ASR.resume_matches(directory, self.original_hashes[video_id]["media"], "fixture-settings-key"))
            for kind, path in (("media", Path(row["original_video_path"])), ("markdown", paths["markdown"]), ("srt", paths["srt"])):
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), self.original_hashes[video_id][kind])
                self.assertTrue(path.name.startswith("赞1234_评0_藏未获取_转90_"))
            self.assertEqual(row["statistics"]["comment_count"], 0)
            self.assertIsNone(row["statistics"]["collect_count"])
            self.assertIsNone(row["statistics"]["play_count"])
            self.assertTrue(row["statistics_captured_at"])

    def test_snapshot_and_csv_use_dynamic_names_and_distinguish_missing_from_zero(self):
        NAMING.reconcile_names(self.fixture.root, self.fixture.catalog_path)
        summary = CONTROLLER.snapshot(self.fixture.root, self.fixture.catalog_path)
        self.assertTrue(summary["all_public_videos_transcribed"])
        self.assertEqual(summary["library_transcript_saved_total"], 2)
        CONTROLLER.write_library_index(self.fixture.root, self.fixture.catalog_path)
        with (self.fixture.root / "catalog/作品数据指标.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["comment_count"], "0")
        self.assertEqual(rows[0]["collect_count"], "未获取")
        self.assertEqual(rows[0]["play_count_availability"], "zero_unverified")
        self.assertTrue(Path(rows[0]["transcript_path"]).is_file())
        self.assertEqual(rows[0]["statistics_captured_at"], self.catalog["videos"][0]["statistics_captured_at"])

    def test_snapshot_marks_existing_corpus_stale_when_report_no_longer_matches(self):
        self.assertEqual(self.fixture.run_export(), 0)
        self.assertTrue(CONTROLLER.snapshot(self.fixture.root, self.fixture.catalog_path)["corpus_exported"])
        report_path = self.fixture.root / "corpus-export.json"
        report = json.loads(report_path.read_text())
        report["entries"] = 1
        report_path.write_text(json.dumps(report))
        self.assertFalse(CONTROLLER.snapshot(self.fixture.root, self.fixture.catalog_path)["corpus_exported"])

    def test_snapshot_rejects_tampered_corpus_files_even_when_paths_exist(self):
        for kind in ("markdown", "jsonl"):
            with self.subTest(kind=kind):
                self.assertEqual(self.fixture.run_export(), 0)
                report = json.loads((self.fixture.root / "corpus-export.json").read_text())
                self.assertTrue(CONTROLLER.snapshot(self.fixture.root, self.fixture.catalog_path)["corpus_exported"])
                Path(report[kind]).write_text("bad", encoding="utf-8")
                summary = CONTROLLER.snapshot(self.fixture.root, self.fixture.catalog_path)
                self.assertTrue(summary["all_public_videos_transcribed"])
                self.assertFalse(summary["corpus_exported"])

    def test_cover_metadata_enriches_index_csv_and_corpus_without_changing_asr(self):
        video_id = self.fixture.ids[0]
        image = self.fixture.root / "covers" / (video_id + ".jpg")
        image.parent.mkdir()
        image.write_bytes(b"\xff\xd8\xffcover fixture")
        catalog_hash = hashlib.sha256(self.fixture.catalog_path.read_bytes()).hexdigest()
        work = self.catalog["videos"][0]
        metadata = {"video_id": video_id, "catalog_sha256": catalog_hash,
                    "published_caption": work["title"], "title_candidate": "封面测试标题",
                    "hashtags": ["#减肥", "#视频"], "cover_text_raw": "封面第一行\n封面第二行",
                    "cover_status": "ocr_low_confidence", "cover_image_path": str(image),
                    "human_verified": False}
        meta_path = self.fixture.root / "catalog/作品标题标签封面.jsonl"
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False) + "\n", encoding="utf-8")
        before = (self.fixture.root / "local-transcripts" / video_id / PATHS.OUTPUT_NAMES["markdown"]).read_bytes()
        CONTROLLER.write_library_index(self.fixture.root, self.fixture.catalog_path)
        with (self.fixture.root / "catalog/作品数据指标.csv").open(encoding="utf-8-sig", newline="") as stream:
            csv_first = next(csv.DictReader(stream))
        self.assertEqual(csv_first["title_candidate"], "封面测试标题")
        self.assertEqual(csv_first["hashtags"], "#减肥 #视频")
        self.assertEqual(csv_first["cover_status"], "ocr_low_confidence")
        self.assertEqual(self.fixture.run_export(), 0)
        exported = [json.loads(line) for line in (self.fixture.root / "catalog/transcripts-2.jsonl").read_text().splitlines()]
        self.assertEqual(exported[0]["title_candidate"], "封面测试标题")
        self.assertEqual(exported[0]["cover_text_raw"], "封面第一行\n封面第二行")
        self.assertFalse(exported[0]["cover_human_verified"])
        self.assertIn("封面文字（机器识别，待人工核对", next(self.fixture.root.glob("*条机器逐字稿合集.md")).read_text())
        self.assertEqual((self.fixture.root / "local-transcripts" / video_id / PATHS.OUTPUT_NAMES["markdown"]).read_bytes(), before)

    def test_stale_cover_metadata_is_not_presented_as_current(self):
        video_id = self.fixture.ids[0]
        path = self.fixture.root / "catalog/作品标题标签封面.jsonl"
        path.write_text(json.dumps({"video_id": video_id, "catalog_sha256": "0" * 64,
                                    "published_caption": self.catalog["videos"][0]["title"],
                                    "title_candidate": "旧标题", "hashtags": ["#旧标签"],
                                    "cover_status": "ok", "cover_text_raw": "旧封面字"}, ensure_ascii=False) + "\n")
        CONTROLLER.write_library_index(self.fixture.root, self.fixture.catalog_path)
        with (self.fixture.root / "catalog/作品数据指标.csv").open(encoding="utf-8-sig", newline="") as stream:
            first = next(csv.DictReader(stream))
        self.assertEqual(first["cover_status"], "未获取")
        self.assertNotIn("旧封面字", (self.fixture.root / "视频与逐字稿索引.md").read_text())
        self.assertIn(video_id, CONTROLLER.snapshot(self.fixture.root, self.fixture.catalog_path)["cover_metadata_missing_ids"])

    def test_tampered_metrics_cannot_be_exported_with_original_page_evidence(self):
        value = json.loads(self.fixture.catalog_path.read_text())
        value["videos"][0]["statistics"]["digg_count"] += 1
        self.fixture.catalog_path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "hash-verified public pagination"):
            self.fixture.run_export("--check")

    def test_unsafe_dynamic_pointer_is_rejected_before_export(self):
        path = self.fixture.evidence()
        value = json.loads(path.read_text())
        value["outputs"]["markdown"]["path"] = str(self.fixture.root / "outside.md")
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            self.fixture.run_export("--check")
        CONTROLLER.write_library_index(self.fixture.root, self.fixture.catalog_path)
        with (self.fixture.root / "catalog/作品数据指标.csv").open(encoding="utf-8-sig", newline="") as stream:
            first = next(csv.DictReader(stream))
        self.assertEqual(first["transcript_path"], "")
        self.assertEqual(first["srt_path"], "")
        self.assertIn("路径待核验", (self.fixture.root / "视频与逐字稿索引.md").read_text())

    def test_collect_stage_renames_and_refreshes_corpus_without_download_or_asr(self):
        stages = []

        def existing_catalog(command, log, root, catalog_path, limit, stage):
            stages.append(stage)
            if stage in ("collect", "metadata"):
                return 0  # Our fixture already contains a real, complete SDK capture.
            if stage == "export":
                return self.fixture.run_export()
            raise AssertionError("Metadata refresh must not download or transcribe")

        with patch.object(sys, "argv", [str(ROOT / "scripts/run_creator.py"),
                                        "https://www.douyin.com/user/" + FIXTURE.SEC_UID,
                                        "--root", str(self.fixture.root), "--stage", "collect"]), \
             patch.object(CONTROLLER, "run_logged", side_effect=existing_catalog), \
             patch.object(CONTROLLER.subprocess, "Popen", side_effect=AssertionError("Unexpected child process")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(CONTROLLER.main(), 0)
        self.assertEqual(stages, ["collect", "metadata", "export"])
        summary = json.loads((self.fixture.root / "pipeline-summary.json").read_text())
        self.assertTrue(summary["corpus_exported"])
        self.assertTrue(summary["all_public_videos_transcribed"])
        self.assertEqual(summary["artifact_naming"]["renamed"], 6)


if __name__ == "__main__":
    unittest.main()
