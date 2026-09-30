"""Offline tests for public captions, official selected covers and OCR provenance."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collect_cover_metadata.py"
SPEC = importlib.util.spec_from_file_location("cover_metadata_fixture", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
IDS = ("7600000000000000001", "7600000000000000002")


def catalog(*captions: str) -> dict:
    return {
        "method": "official_browser_sdk_response_capture",
        "catalog_visibility": "public",
        "catalog_complete": True,
        "video_count": len(captions),
        "videos": [{"video_id": IDS[number], "title": caption}
                   for number, caption in enumerate(captions)],
    }


class CaptionTests(unittest.TestCase):
    def test_full_published_caption_kept_with_adjacent_and_duplicate_tags(self):
        caption = "减肥赛道做 IP，影响用户决策\n#减肥 #减肥赛道#减肥IP #减肥"
        raw, candidate, tags = MODULE.parse_caption(caption)
        self.assertEqual(raw, caption)
        self.assertEqual(candidate, "减肥赛道做 IP，影响用户决策")
        self.assertEqual(tags, ["#减肥", "#减肥赛道", "#减肥IP"])

    def test_tag_first_has_no_invented_standalone_title(self):
        raw, candidate, tags = MODULE.parse_caption("#一起减肥打卡 ＃体重打卡 后文")
        self.assertEqual(raw, "#一起减肥打卡 ＃体重打卡 后文")
        self.assertIsNone(candidate)
        self.assertEqual(tags, ["#一起减肥打卡", "#体重打卡"])

    def test_no_tags_and_missing_caption(self):
        self.assertEqual(MODULE.parse_caption("标题\n  第二行"), ("标题\n  第二行", "标题 第二行", []))
        self.assertEqual(MODULE.parse_caption(None), ("", None, []))


class CatalogAndCoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog_path = self.root / "catalog.json"

    def save_catalog(self, value):
        self.catalog_path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_official_catalog_provenance_and_content_hash(self):
        self.save_catalog(catalog("开头 #减肥", "#大基数 后文"))
        works, digest, complete = MODULE.load_catalog(self.catalog_path)
        self.assertTrue(complete)
        self.assertEqual(digest, hashlib.sha256(self.catalog_path.read_bytes()).hexdigest())
        self.assertEqual(len(works), 2)
        self.assertEqual(works[0]["published_caption"], "开头 #减肥")
        self.assertEqual(works[1]["title_candidate"], None)
        self.assertEqual(works[1]["official_url"], "https://www.douyin.com/video/" + IDS[1])
        self.assertFalse(works[0]["human_verified"])

    def test_rejects_unofficial_or_inconsistent_catalog(self):
        for alteration in ({"method": "manually_entered"}, {"catalog_visibility": "private"},
                           {"video_count": 2}, {"videos": [{"video_id": IDS[0], "title": "a"},
                                                        {"video_id": IDS[0], "title": "b"}]}):
            with self.subTest(alteration=alteration):
                value = {**catalog("one"), **alteration}
                self.save_catalog(value)
                with self.assertRaises(ValueError):
                    MODULE.load_catalog(self.catalog_path)

    def test_only_official_https_image_hosts_without_redirects(self):
        self.assertTrue(MODULE.official_cover_url("https://p3.douyinpic.com/aweme/123?x=1"))
        self.assertTrue(MODULE.official_cover_url("https://p3.byteimg.com/aweme/123"))
        for value in ("http://p3.douyinpic.com/x", "https://p3.douyinpic.com.evil/x",
                      "https://user@p3.douyinpic.com/x", "https://127.0.0.1/x",
                      "https://p3.douyinpic.com:444/x", "javascript:alert(1)"):
            with self.subTest(value=value):
                self.assertFalse(MODULE.official_cover_url(value))

    def test_exact_video_id_selected_from_feed_not_first_item(self):
        requested = []
        wanted = "https://p3.douyinpic.com/selected"
        payload = {"status_code": 0, "aweme_list": [
            {"aweme_id": IDS[1], "video": {"cover": {"url_list": ["https://p3.douyinpic.com/wrong"]}}},
            {"aweme_id": IDS[0], "video": {"cover": {"url_list": [wanted]}}},
        ]}

        def fake_curl(url, output, _agent, _proxy, _seconds):
            requested.append(url)
            output.write_bytes(json.dumps(payload).encode() if "aweme/v1/feed" in url else b"image")
            return True

        def fake_convert(_source, destination):
            destination.write_bytes(b"\xff\xd8\xff" + b"x" * 128)
            return True

        with (patch.object(MODULE, "curl_to", side_effect=fake_curl),
              patch.object(MODULE, "convert_to_jpeg", side_effect=fake_convert),
              patch.object(MODULE, "valid_jpeg", return_value=False),
              patch.object(MODULE, "routes", return_value=[None])):
            result = MODULE.fetch_one(IDS[0], self.root, None)
        self.assertEqual(result["cover_status"], "ocr_pending")
        self.assertEqual(result["cover_source"], "official_aweme_feed.video.cover")
        self.assertIn(wanted, requested)
        self.assertNotIn("https://p3.douyinpic.com/wrong", requested)

    def test_missing_exact_id_never_attaches_other_cover(self):
        payload = {"status_code": 0, "aweme_list": [
            {"aweme_id": IDS[1], "video": {"cover": {"url_list": ["https://p3.douyinpic.com/wrong"]}}}
        ]}

        def fake_curl(url, output, _agent, _proxy, _seconds):
            output.write_bytes(json.dumps(payload).encode())
            return True

        with (patch.object(MODULE, "curl_to", side_effect=fake_curl),
              patch.object(MODULE, "valid_jpeg", return_value=False),
              patch.object(MODULE, "routes", return_value=[None])):
            result = MODULE.fetch_one(IDS[0], self.root, None)
        self.assertEqual(result["cover_status"], "metadata_id_not_returned")
        self.assertIsNone(result["cover_image_path"])

    def test_cached_web_detail_cover_keeps_its_actual_source(self):
        cover = self.root / (IDS[0] + ".jpg")
        cover.write_bytes(b"\xff\xd8\xff" + b"x" * 128)
        marker = cover.with_suffix(".source.json")
        marker.write_text(json.dumps({"video_id": IDS[0],
                                      "cover_source": "official_aweme_web_detail.video.cover",
                                      "cover_image_sha256": hashlib.sha256(cover.read_bytes()).hexdigest()}))
        with patch.object(MODULE, "valid_jpeg", return_value=True):
            result = MODULE.fetch_one(IDS[0], self.root, None)
        self.assertEqual(result["cover_source"], "official_aweme_web_detail.video.cover")
        cover.write_bytes(cover.read_bytes() + b"changed")
        with patch.object(MODULE, "valid_jpeg", return_value=True):
            stale = MODULE.fetch_one(IDS[0], self.root, None)
        self.assertEqual(stale["cover_source"], "cached_cover_source_unverified")


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog_path = self.root / "source-catalog.json"
        self.save_catalog(catalog("标题 | <x> #减肥", "#大基数 后文"))

    def save_catalog(self, value):
        self.catalog_path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_ocr_status_marks_low_confidence_and_failures(self):
        self.assertEqual(MODULE.ocr_status([{"text": "预廋转场", "confidence": 0.5}]), "ocr_low_confidence")
        self.assertEqual(MODULE.ocr_status([{"text": "预瘦转场", "confidence": 0.9}]), "ok")
        self.assertEqual(MODULE.ocr_status([{"text": "字", "confidence": float("nan")}]), "ocr_low_confidence")
        self.assertEqual(MODULE.ocr_status([]), "ocr_empty")
        self.assertEqual(MODULE.ocr_status([], "image_decode_failed"), "ocr_image_decode_failed")

    def test_ocr_success_and_failure_preserve_verified_web_detail_source(self):
        works, _, _ = MODULE.load_catalog(self.catalog_path)
        image = self.root / "covers" / (IDS[0] + ".jpg")
        image.parent.mkdir()
        image.write_bytes(b"synthetic image bytes")
        output = self.root / "catalog" / MODULE.RECORD_NAME

        class FakeProcess:
            def __init__(self, lines, exit_code):
                self.stdout = iter(lines)
                self.exit_code = exit_code
            def wait(self):
                return self.exit_code

        for lines, code, expected in (([json.dumps({"video_id": IDS[0], "ocr_lines": [
                {"text": "封面字", "confidence": 0.9}]}) + "\n"], 0, "ok"),
                                      ([], 1, "ocr_process_failed")):
            with self.subTest(expected=expected):
                records = {IDS[0]: MODULE.record(IDS[0], image, "ocr_pending",
                                                 source=MODULE.WEB_DETAIL_SOURCE)}
                with patch.object(MODULE.subprocess, "Popen", return_value=FakeProcess(lines, code)):
                    MODULE.run_ocr(works, {IDS[0]: image}, Path("synthetic-ocr.swift"), records, output)
                self.assertEqual(records[IDS[0]]["cover_status"], expected)
                self.assertEqual(records[IDS[0]]["cover_source"], MODULE.WEB_DETAIL_SOURCE)

    def test_changed_web_detail_cover_cannot_skip_with_stale_source_marker(self):
        works, _, _ = MODULE.load_catalog(self.catalog_path)
        cover = self.root / "covers" / (IDS[0] + ".jpg")
        cover.parent.mkdir()
        cover.write_bytes(b"\xff\xd8\xff" + b"a" * 128)
        marker = cover.with_suffix(".source.json")
        marker.write_text(json.dumps({"video_id": IDS[0], "cover_source": MODULE.WEB_DETAIL_SOURCE,
                                      "cover_image_sha256": hashlib.sha256(cover.read_bytes()).hexdigest()}))
        old = MODULE.record(IDS[0], cover, "ok", [{"text": "旧封面", "confidence": 0.9}],
                            source=MODULE.WEB_DETAIL_SOURCE)
        MODULE.save_records(self.root / "catalog" / MODULE.RECORD_NAME, works, {IDS[0]: old})
        argv = [str(SCRIPT), "--catalog", str(self.catalog_path), "--root", str(self.root), "--limit", "1"]
        with patch.object(MODULE.shutil, "which", return_value="/bin/tool"), \
             patch.object(MODULE, "valid_jpeg", return_value=True), \
             patch.object(MODULE, "fetch_one", side_effect=AssertionError("valid marker should skip")), \
             patch("sys.argv", argv):
            self.assertEqual(MODULE.main(), 0)

        cover.write_bytes(b"\xff\xd8\xff" + b"b" * 128)

        def synthetic_ocr(_works, images, _script, records, _output):
            self.assertIn(IDS[0], images)
            previous = records[IDS[0]]
            records[IDS[0]] = MODULE.record(IDS[0], images[IDS[0]], "ocr_empty",
                                            source=previous["cover_source"])

        with patch.object(MODULE.shutil, "which", return_value="/bin/tool"), \
             patch.object(MODULE, "valid_jpeg", return_value=True), \
             patch.object(MODULE, "run_ocr", side_effect=synthetic_ocr), \
             patch.object(MODULE.time, "sleep", return_value=None), \
             patch("sys.argv", argv):
            self.assertEqual(MODULE.main(), 0)
        rows = [json.loads(line) for line in (self.root / "catalog" / MODULE.RECORD_NAME).read_text().splitlines()]
        self.assertEqual(rows[0]["cover_source"], "cached_cover_source_unverified")
        self.assertEqual(rows[0]["cover_status"], "ocr_empty")

    def test_metadata_stage_rejects_covers_directory_symlink(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "covers").symlink_to(outside, target_is_directory=True)
        with patch.object(MODULE.shutil, "which", return_value="/bin/tool"), \
             patch("sys.argv", [str(SCRIPT), "--catalog", str(self.catalog_path),
                                "--root", str(self.root)]):
            self.assertEqual(MODULE.main(), 2)
        self.assertEqual(list(outside.iterdir()), [])

    def test_failure_keeps_one_metadata_row_per_catalog_video(self):
        with (patch.object(MODULE.shutil, "which", return_value="/bin/tool"),
              patch.object(MODULE, "valid_jpeg", return_value=False),
              patch.object(MODULE, "fetch_one", side_effect=lambda vid, _dir, _proxy:
                           MODULE.record(vid, None, "metadata_request_failed")),
              patch.object(MODULE.time, "sleep", return_value=None),
              patch("sys.argv", [str(SCRIPT), "--catalog", str(self.catalog_path),
                                 "--root", str(self.root)])):
            code = MODULE.main()
        self.assertEqual(code, 1)
        rows = [json.loads(line) for line in (self.root / "catalog" / MODULE.RECORD_NAME).read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["published_caption"], "标题 | <x> #减肥")
        self.assertEqual(rows[0]["title_candidate"], "标题 | <x>")
        self.assertEqual(rows[0]["hashtags"], ["#减肥"])
        self.assertEqual(rows[0]["cover_text_raw"], "")
        self.assertFalse(rows[0]["human_verified"])
        index = (self.root / MODULE.INDEX_NAME).read_text()
        self.assertIn("标题 &#124; &lt;x&gt;", index)
        self.assertNotIn("<x>", index)
        report = json.loads((self.root / "catalog" / MODULE.REPORT_NAME).read_text())
        self.assertFalse(report["all_catalog_covers_processed"])
        self.assertEqual(report["video_downloads"], 0)

    def test_cached_cover_is_skipped_and_caption_refresh_updates_hash(self):
        works, old_hash, _ = MODULE.load_catalog(self.catalog_path)
        cover = self.root / "covers" / (IDS[0] + ".jpg")
        cover.parent.mkdir()
        cover.write_bytes(b"\xff\xd8\xff" + b"x" * 128)
        first = MODULE.record(IDS[0], cover, "ocr_low_confidence",
                              [{"text": "预廋转场", "confidence": 0.5, "bbox": [0, 0, 1, 1]}],
                              "2026-09-30T01:02:03+00:00")
        MODULE.save_records(self.root / "catalog" / MODULE.RECORD_NAME, works, {IDS[0]: first})
        self.save_catalog(catalog("新标题 #减脂", "#大基数 后文"))
        with (patch.object(MODULE.shutil, "which", return_value="/bin/tool"),
              patch.object(MODULE, "valid_jpeg", return_value=True),
              patch.object(MODULE, "fetch_one", side_effect=AssertionError("must not fetch")),
              patch("sys.argv", [str(SCRIPT), "--catalog", str(self.catalog_path),
                                 "--root", str(self.root), "--limit", "1"])):
            code = MODULE.main()
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in (self.root / "catalog" / MODULE.RECORD_NAME).read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["published_caption"], "新标题 #减脂")
        self.assertEqual(rows[0]["cover_text_raw"], "预廋转场")
        self.assertNotEqual(rows[0]["catalog_sha256"], old_hash)
        self.assertEqual(rows[1]["cover_status"], "not_attempted")
        report = json.loads((self.root / "catalog" / MODULE.REPORT_NAME).read_text())
        self.assertTrue(report["all_selected_covers_processed"])
        self.assertFalse(report["all_catalog_covers_processed"])


if __name__ == "__main__":
    unittest.main()
