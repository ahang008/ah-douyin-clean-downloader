"""Offline safety and restart tests for source-preserving annotation copies."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_annotated_transcripts.py"
SPEC = importlib.util.spec_from_file_location("build_annotated_transcripts", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
VIDEO_ID = "7600000000000000001"


class AnnotatedTranscriptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "creator"
        (self.root / "catalog").mkdir(parents=True)
        self.catalog_path = self.root / "catalog/catalog.json"
        self.metadata_path = self.root / "catalog/作品标题标签封面.jsonl"
        self.caption = "减肥账号怎么起步 #减肥 #自媒体"
        self.work = {"video_id": VIDEO_ID, "title": self.caption}
        self.save_catalog()
        self.save_metadata(status="ocr_low_confidence", text="拍减肥视频前\n先完成一件事", image=True)

    def save_catalog(self):
        self.catalog_path.write_text(json.dumps({"catalog_complete": True, "videos": [self.work]},
                                                ensure_ascii=False), encoding="utf-8")
        self.catalog_sha = hashlib.sha256(self.catalog_path.read_bytes()).hexdigest()

    def save_metadata(self, status="ocr_low_confidence", text="", image=False):
        cover = self.root / "covers" / f"{VIDEO_ID}.jpg"
        if image:
            cover.parent.mkdir(parents=True, exist_ok=True)
            cover.write_bytes(b"\xff\xd8\xffsynthetic-cover")
        record = {
            "video_id": VIDEO_ID,
            "official_url": f"https://www.douyin.com/video/{VIDEO_ID}",
            "published_caption": self.caption,
            "title_candidate": "减肥账号怎么起步",
            "hashtags": ["#减肥", "#自媒体"],
            "catalog_sha256": self.catalog_sha,
            "cover_image_path": str(cover) if image else None,
            "cover_text_raw": text,
            "cover_status": status,
            "ocr_lines": [],
            "human_verified": False,
        }
        self.metadata_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    def machine_file(self, name="01-本地ASR机器逐字稿.md"):
        directory = self.root / "local-transcripts" / VIDEO_ID
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / name
        original = "# 机器稿\n\n## 机器识别原文\n\n原始语音逐字稿。\n".encode("utf-8")
        source.write_bytes(original)
        evidence = {
            "video_id": VIDEO_ID, "status": "machine_draft_saved",
            "source_url": f"https://www.douyin.com/video/{VIDEO_ID}",
            "raw_text": "原始语音逐字稿。",
            "outputs": {"markdown": {"path": str(source), "sha256": hashlib.sha256(original).hexdigest()}},
        }
        (directory / "01-本地ASR识别证据.json").write_text(json.dumps(evidence), encoding="utf-8")
        return source, original

    def build(self):
        return MODULE.build(self.root, self.catalog_path, self.metadata_path)

    def target(self, name="01-本地ASR机器逐字稿.md"):
        return self.root / "annotated-transcripts" / VIDEO_ID / name

    def test_no_raw_transcripts_is_successful_noop_with_zero_counts(self):
        report = self.build()
        self.assertEqual(report["counts"]["created"], 0)
        self.assertEqual(report["counts"]["annotated_copies"], 0)
        self.assertEqual(report["counts"]["no_verified_transcript"], 1)
        self.assertTrue(report["all_available_transcripts_annotated"])
        self.assertTrue((self.root / "annotated-transcripts/annotation-report.json").is_file())

    def test_copy_preserves_raw_bytes_and_idempotent_restart(self):
        source, original = self.machine_file()
        first = self.build()
        self.assertEqual(first["counts"]["created"], 1)
        copy = self.target().read_bytes()
        self.assertTrue(copy.endswith(original))
        self.assertEqual(source.read_bytes(), original)
        rendered = copy.decode("utf-8")
        self.assertIn("发布文案开头（标题候选）：减肥账号怎么起步", rendered)
        self.assertIn("#减肥 #自媒体", rendered)
        self.assertIn("拍减肥视频前<br>先完成一件事", rendered)
        self.assertIn("低置信度，待对照原图", rendered)
        self.assertIn(str(self.root / "covers" / f"{VIDEO_ID}.jpg"), rendered)
        second = self.build()
        self.assertEqual(second["counts"]["unchanged"], 1)
        self.assertEqual(self.target().read_bytes(), copy)

    def test_hand_edit_is_preserved_and_blocks_regeneration(self):
        source, original = self.machine_file()
        self.build()
        self.target().write_bytes(self.target().read_bytes() + b"hand edit")
        edited = self.target().read_bytes()
        self.save_metadata(status="ocr_empty", text="", image=True)
        with self.assertRaisesRegex(ValueError, "Hand-edited"):
            self.build()
        self.assertEqual(self.target().read_bytes(), edited)
        self.assertEqual(source.read_bytes(), original)

    def test_source_hash_and_catalog_identity_are_verified_before_writing(self):
        source, original = self.machine_file()
        source.write_bytes(original + b"tamper")
        with self.assertRaisesRegex(ValueError, "SHA differs"):
            self.build()
        self.assertFalse(self.target().exists())
        source.write_bytes(original)
        metadata = json.loads(self.metadata_path.read_text())
        metadata["catalog_sha256"] = "0" * 64
        self.metadata_path.write_text(json.dumps(metadata) + "\n")
        with self.assertRaisesRegex(ValueError, "catalog SHA differs"):
            self.build()
        self.assertFalse(self.target().exists())

    def test_unavailable_cover_and_no_text_are_distinct_nonfatal_states(self):
        self.machine_file()
        self.save_metadata(status="cover_download_failed", text="", image=False)
        report = self.build()
        self.assertEqual(report["cover_status_counts"], {"cover_download_failed": 1})
        self.assertIn("封面未取得或识别未完成", self.target().read_text())
        self.save_metadata(status="ocr_empty", text="", image=True)
        second = self.build()
        self.assertEqual(second["counts"]["updated"], 1)
        self.assertIn("不能据此断定封面无字", self.target().read_text())

    def test_sample_does_not_create_reading_copy_for_unattempted_cover(self):
        self.machine_file()
        self.save_metadata(status="not_attempted", text="", image=False)
        report = self.build()
        self.assertEqual(report["counts"]["metadata_not_attempted"], 1)
        self.assertEqual(report["counts"]["annotated_copies"], 0)
        self.assertFalse(report["all_available_transcripts_annotated"])
        self.assertFalse(self.target().exists())

    def test_metric_filename_refresh_moves_only_known_generated_copy(self):
        old_source, original = self.machine_file()
        self.build()
        new_name = f"赞100_评2_藏3_转4_标题-{VIDEO_ID}.md"
        new_source = old_source.with_name(new_name)
        old_source.rename(new_source)
        evidence_path = old_source.parent / "01-本地ASR识别证据.json"
        evidence = json.loads(evidence_path.read_text())
        evidence["outputs"]["markdown"]["path"] = str(new_source)
        evidence_path.write_text(json.dumps(evidence))
        report = self.build()
        self.assertEqual(report["counts"]["renamed"], 1)
        self.assertFalse(self.target().exists())
        self.assertTrue(self.target(new_name).read_bytes().endswith(original))
        self.assertEqual(new_source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
