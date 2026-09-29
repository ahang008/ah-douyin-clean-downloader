"""Public-counter fixtures only; no browser, network, real media, or ASR data."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def helper(filename, name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


METRICS = helper("work_metrics.py", "public_metrics_fixture")
BROWSER = helper("collect_creator_browser.py", "public_metrics_browser_fixture")
DOWNLOAD = helper("download_batch.py", "public_metrics_download_fixture")
EXPORT = helper("export_transcripts.py", "public_metrics_export_fixture")
SEC_UID = "MS4wLjPublicMetricsFixture"
VIDEO_ID = "7600000000000000001"
TIMESTAMP = "2026-09-29T01:02:03.123456+00:00"


def work(number=1, statistics=None):
    row = {"aweme_id": str(7600000000000000000 + number), "desc": "公开作品 " + str(number),
           "aweme_type": 0, "author": {"sec_uid": SEC_UID, "uid": "123", "nickname": "Fixture"},
           "video": {"duration": 1000}, "create_time": 1, "is_top": number == 1}
    if statistics is not None:
        row["statistics"] = statistics
    return row


def page(rows, cursor=0, more=0):
    return {"status_code": 0, "max_cursor": cursor, "has_more": more, "aweme_list": rows,
            "not_login_module": {"guide_login_tip_exist": False}}


class PublicMetricsTests(unittest.TestCase):
    def test_zero_is_available_and_missing_is_unknown(self):
        statistics, states = METRICS.normalized_statistics({"digg_count": 0, "comment_count": 7})
        self.assertEqual(statistics["digg_count"], 0)
        self.assertEqual(states["digg_count"], "available")
        self.assertEqual(statistics["comment_count"], 7)
        self.assertIsNone(statistics["collect_count"])
        self.assertEqual(states["collect_count"], "not_returned")
        self.assertEqual(set(statistics), set(METRICS.COUNT_FIELDS))
        self.assertEqual(METRICS.metric_text({"statistics": {"digg_count": 0}}, "digg_count"), "0")
        self.assertEqual(METRICS.metric_text({}, "digg_count"), "未获取")

    def test_invalid_types_negative_and_out_of_range_are_not_counts(self):
        invalid = [("true", True), ("false", False), ("negative", -1), ("float", 1.0),
                   ("string", "1"), ("nan", float("nan")), ("object", {}),
                   ("beyond_range", METRICS.MAX_COUNT + 1), ("huge_integer", 10 ** 5000)]
        for label, value in invalid:
            with self.subTest(case=label):
                statistics, states = METRICS.normalized_statistics({"digg_count": value})
                self.assertIsNone(statistics["digg_count"])
                self.assertEqual(states["digg_count"], "invalid")
        for value in (None, [], True, "malformed"):
            statistics, _ = METRICS.normalized_statistics(value)
            self.assertTrue(all(count is None for count in statistics.values()))
        statistics, _ = METRICS.normalized_statistics({"forward_count": 3, "download_count": 4})
        self.assertEqual((statistics["forward_count"], statistics["download_count"]), (3, 4))

    def test_play_zero_is_an_unknown_observation_with_a_round_trip_reason(self):
        statistics, availability = METRICS.normalized_statistics({"play_count": 0, "share_count": 0})
        self.assertIsNone(statistics["play_count"])
        self.assertEqual(availability["play_count"], "zero_unverified")
        self.assertEqual(statistics["share_count"], 0)
        self.assertEqual(METRICS.normalized_statistics(statistics, availability), (statistics, availability))
        self.assertEqual(METRICS.metric_text({"statistics": statistics, "statistics_availability": availability}, "play_count"), "未获取")
        positive, states = METRICS.normalized_statistics({"play_count": 12}, {"play_count": "zero_unverified"})
        self.assertEqual((positive["play_count"], states["play_count"]), (12, "available"))

    def test_only_fixed_public_fields_and_reasons_are_preserved(self):
        raw = {"digg_count": 1, "session_token": "SECRET_SENTINEL", "arbitrary_nested": {"cookie": "SECRET_SENTINEL"}}
        states = {"comment_count": "SECRET_SENTINEL", "digg_count": "invalid", "play_count": {"token": "SECRET_SENTINEL"}}
        normalized, availability = METRICS.normalized_statistics(raw, states)
        self.assertEqual(availability["digg_count"], "available")
        self.assertEqual(availability["comment_count"], "not_returned")
        self.assertNotIn("SECRET_SENTINEL", json.dumps([normalized, availability]))
        _, wrong_reason = METRICS.normalized_statistics({"digg_count": None}, {"digg_count": "zero_unverified"})
        self.assertEqual(wrong_reason["digg_count"], "not_returned")

    def test_utc_timestamp_validation_preserves_exact_legal_spelling(self):
        for value in (TIMESTAMP, "2026-09-29T01:02:03Z", "2026-09-29T01:02:03.1Z",
                      "2026-09-29T01:02:03.12Z", "2026-09-29T01:02:03.123Z",
                      "2026-09-29T01:02:03.1234Z", "2026-09-29T01:02:03.12345Z"):
            self.assertEqual(METRICS.validated_capture_time(value), value)
        for value in (None, True, "2026-09-29", "2026-09-29T01:02:03", "2026-09-31T01:02:03Z",
                      "2026-09-29T01:02:03+08:00", "2026-09-29T01:02:03+00:00:30",
                      "2026-09-29💣01:02:03+00:00", "SECRET_SENTINEL"):
            with self.subTest(timestamp=value):
                self.assertIsNone(METRICS.validated_capture_time(value))

    def test_legacy_work_has_no_new_extension_and_raw_capture_overrides_fake_time(self):
        self.assertEqual(METRICS.statistics_extension({"title": "旧作品"}), {})
        row = {"statistics": {"digg_count": 0}, "statistics_captured_at": "SECRET_SENTINEL"}
        extension = METRICS.statistics_extension(row, captured_at=TIMESTAMP)
        self.assertEqual(extension["statistics_captured_at"], TIMESTAMP)
        self.assertNotIn("SECRET_SENTINEL", json.dumps(extension))
        self.assertIsNone(METRICS.statistics_extension(row)["statistics_captured_at"])
        with self.assertRaises(ValueError):
            METRICS.statistics_extension(row, captured_at="2026-09-29")

    def test_summary_counts_zero_and_unknown_separately_and_uses_saved_capture_range(self):
        rows = [METRICS.statistics_extension({"statistics": {"digg_count": 0, "play_count": 0}}, captured_at=TIMESTAMP),
                METRICS.statistics_extension({"statistics": {"digg_count": 3, "play_count": 8}}, captured_at="2026-09-29T02:00:00Z"),
                {}]
        summary = METRICS.metrics_summary(rows)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["fields"]["digg_count"], {"available": 2, "unknown": 1})
        self.assertEqual(summary["fields"]["play_count"], {"available": 1, "unknown": 2})
        self.assertEqual(summary["capture_time_available"], 2)
        self.assertEqual(summary["capture_time_unknown"], 1)
        self.assertEqual(summary["capture_range"], {"earliest": TIMESTAMP, "latest": "2026-09-29T02:00:00Z"})
        self.assertEqual(METRICS.metrics_summary([])["capture_range"], {"earliest": None, "latest": None})


class MetricFilenameTests(unittest.TestCase):
    def test_confirmed_four_counter_format_and_id_suffix(self):
        row = {"video_id": VIDEO_ID, "title": "公开标题", "statistics": {"digg_count": 0, "comment_count": 12, "share_count": 8}}
        self.assertEqual(METRICS.metric_filename_stem(row), "赞0_评12_藏未获取_转8_公开标题-" + VIDEO_ID)
        self.assertEqual(METRICS.metric_filename_stem({"aweme_id": VIDEO_ID, "catalog_title": "清单标题"}),
                         "赞未获取_评未获取_藏未获取_转未获取_清单标题-" + VIDEO_ID)

    def test_utf8_budget_and_cross_platform_special_characters(self):
        titles = ["汉字" * 1000, "😀👩🏽‍💻👨‍👩‍👦" * 1000, "e\u0301" * 1000,
                  "../CON.txt:<>|/\\*?\x00\x7f\u202e\ud800" + "作品" * 1000, " .-_#/\\ "]
        for title in titles:
            with self.subTest(title_kind=title[:10].encode("utf-8", errors="replace")):
                stem = METRICS.metric_filename_stem({"video_id": VIDEO_ID, "title": title})
                self.assertLessEqual(len(stem.encode("utf-8")), 200)
                self.assertTrue(stem.endswith("-" + VIDEO_ID))
                self.assertFalse(any(char in stem for char in '/\\:*?"<>|'))
                self.assertFalse(any(char in stem for char in "\x00\x7f\u202e\ud800"))
                self.assertFalse(stem.endswith((" ", ".")))

    def test_large_valid_counters_fit_and_oversized_counts_become_unknown(self):
        statistics = {key: METRICS.MAX_COUNT for key in METRICS.COUNT_FIELDS}
        stem = METRICS.metric_filename_stem({"video_id": VIDEO_ID, "title": "很长" * 1000, "statistics": statistics})
        self.assertLessEqual(len(stem.encode("utf-8")), 200)
        self.assertIn("赞" + str(METRICS.MAX_COUNT), stem)
        stem = METRICS.metric_filename_stem({"video_id": VIDEO_ID, "statistics": {"digg_count": 10 ** 5000}})
        self.assertTrue(stem.startswith("赞未获取_"))
        with self.assertRaises(ValueError):
            METRICS.metric_filename_stem({"video_id": VIDEO_ID}, max_bytes=30)
        with self.assertRaises(ValueError):
            METRICS.metric_filename_stem({"video_id": VIDEO_ID}, max_bytes=201)

    def test_invalid_id_is_not_sanitized_into_a_different_work(self):
        for value in ("../" + VIDEO_ID, "not-a-video", "٧" * 19):
            with self.subTest(video_id=value):
                with self.assertRaises(ValueError):
                    METRICS.metric_filename_stem({"video_id": value})


class MetricCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.catalog_path = Path(self.temporary.name) / "catalog.json"
        self.recorder = BROWSER.BrowserCatalog(SEC_UID, self.catalog_path)

    def add(self, cursor, payload):
        return self.recorder.add(cursor, payload, json.dumps(payload).encode())

    def test_legacy_normalization_keeps_original_dictionary_shape(self):
        normalized = BROWSER.normalize_work(work(), SEC_UID)
        self.assertEqual(set(normalized), {"video_id", "source_url", "canonical_url", "title", "create_time",
                                          "duration_ms", "aweme_type", "is_video", "is_pinned", "author"})
        projected = BROWSER.public_projection(page([work()]), [normalized])["aweme_list"][0]
        self.assertEqual(BROWSER.normalize_work(projected, SEC_UID), normalized)

    def test_projection_round_trips_statistics_availability_and_exact_timestamp(self):
        for timestamp in (TIMESTAMP, "2026-09-29T01:02:03Z"):
            normalized = BROWSER.normalize_work(work(statistics={"digg_count": 0, "play_count": 0, "comment_count": -1}),
                                                SEC_UID, captured_at=timestamp)
            projected = BROWSER.public_projection(page([]), [normalized])["aweme_list"][0]
            self.assertEqual(BROWSER.normalize_work(projected, SEC_UID), normalized)

    def test_page_has_one_timestamp_and_private_fields_never_persist(self):
        payload = page([work(1, {"digg_count": 0, "play_count": 0, "private": "SECRET_SENTINEL"}), work(2)])
        payload["msToken"] = "SECRET_SENTINEL"
        payload["aweme_list"][0]["author"]["session_token"] = "SECRET_SENTINEL"
        payload["aweme_list"][0]["statistics_availability"] = {"comment_count": "SECRET_SENTINEL"}
        with patch.object(BROWSER, "now", return_value=TIMESTAMP) as clock:
            self.add(0, payload)
            clock.assert_called_once()
        catalog = self.recorder.save()
        self.assertEqual(catalog["page_evidence"][0]["captured_at"], TIMESTAMP)
        self.assertTrue(all(row["statistics_captured_at"] == TIMESTAMP for row in catalog["videos"]))
        for saved in Path(self.temporary.name).rglob("*.json"):
            self.assertNotIn("SECRET_SENTINEL", saved.read_text())
        self.assertEqual(len(EXPORT.validated_pagination(catalog, self.catalog_path)), 1)

    def test_same_cursor_counter_change_is_not_a_pagination_conflict(self):
        self.add(0, page([work(statistics={"digg_count": 3})]))
        first = self.recorder.catalog()
        file = Path(first["page_evidence"][0]["file"])
        saved = file.read_bytes()
        self.assertFalse(self.add(0, page([work(statistics={"digg_count": 99})])))
        self.assertEqual(file.read_bytes(), saved)
        self.assertEqual(self.recorder.catalog()["videos"][0]["statistics"]["digg_count"], 3)

    def test_pinned_repeat_keeps_first_page_snapshot_for_exact_export(self):
        with patch.object(BROWSER, "now", return_value=TIMESTAMP):
            self.add(0, page([work(1, {"digg_count": 3})], cursor=90, more=1))
        with patch.object(BROWSER, "now", return_value="2026-09-29T02:00:00Z"):
            self.add(90, page([work(1, {"digg_count": 99}), work(2, {"digg_count": 5})]))
        catalog = self.recorder.save()
        self.assertEqual(catalog["videos"][0]["statistics"]["digg_count"], 3)
        self.assertEqual(catalog["videos"][0]["statistics_captured_at"], TIMESTAMP)
        self.assertEqual(len(EXPORT.validated_pagination(catalog, self.catalog_path)), 2)

    def test_old_saved_projection_still_passes_full_exact_export_validation(self):
        self.add(0, page([work()]))
        catalog = self.recorder.save()
        for row in catalog["videos"]:
            for key in METRICS.STATISTICS_EXTENSION_KEYS:
                row.pop(key)
        evidence = catalog["page_evidence"][0]
        saved_path = Path(evidence["file"])
        projection = json.loads(saved_path.read_text())
        for row in projection["aweme_list"]:
            for key in METRICS.STATISTICS_EXTENSION_KEYS:
                row.pop(key)
        saved_path.write_text(json.dumps(projection))
        evidence["saved_file_sha256"] = hashlib.sha256(saved_path.read_bytes()).hexdigest()
        self.assertEqual(len(EXPORT.validated_pagination(catalog, self.catalog_path)), 1)

    def test_partial_refresh_preserves_existing_master_bytes(self):
        original = b'{"catalog_complete":true,"legacy":"unchanged bytes"}\n'
        self.catalog_path.write_bytes(original)
        self.recorder = BROWSER.BrowserCatalog(SEC_UID, self.catalog_path)
        self.add(0, page([work(1, {"digg_count": 7})], cursor=90, more=1))
        value = self.recorder.save()
        self.assertEqual(self.catalog_path.read_bytes(), original)
        attempt = json.loads(Path(value["active_attempt_catalog"]).read_text())
        self.assertEqual(attempt["videos"][0]["statistics"]["digg_count"], 7)

    def test_missing_or_malformed_statistics_do_not_break_valid_pagination(self):
        row = work()
        row["statistics"] = "SECRET_SENTINEL"
        self.add(0, page([row]))
        catalog = self.recorder.save()
        self.assertTrue(catalog["catalog_complete"])
        self.assertTrue(all(value is None for value in catalog["videos"][0]["statistics"].values()))
        self.assertNotIn("SECRET_SENTINEL", self.catalog_path.read_text())


class MetricDownloadNormalizationTests(unittest.TestCase):
    def test_legacy_download_row_stays_unchanged(self):
        rows, _ = DOWNLOAD.normalize_catalog({"catalog_complete": True, "videos": [{"video_id": VIDEO_ID, "title": "旧作品"}]})
        self.assertEqual(rows, [{"video_id": VIDEO_ID, "canonical_url": "https://www.douyin.com/video/" + VIDEO_ID,
                                 "source_url": "https://www.douyin.com/video/" + VIDEO_ID, "catalog_title": "旧作品"}])

    def test_catalog_metrics_are_sanitized_and_passed_to_download_manifest_rows(self):
        normalized = BROWSER.normalize_work(work(statistics={"digg_count": 0, "play_count": 0}), SEC_UID, captured_at=TIMESTAMP)
        normalized["statistics"]["session_token"] = "SECRET_SENTINEL"
        normalized["statistics_availability"]["comment_count"] = "SECRET_SENTINEL"
        normalized["private"] = "SECRET_SENTINEL"
        rows, _ = DOWNLOAD.normalize_catalog({"catalog_complete": True, "videos": [normalized]})
        result = rows[0]
        self.assertEqual(result["statistics"]["digg_count"], 0)
        self.assertIsNone(result["statistics"]["play_count"])
        self.assertEqual(result["statistics_availability"]["play_count"], "zero_unverified")
        self.assertEqual(result["statistics_captured_at"], TIMESTAMP)
        self.assertNotIn("SECRET_SENTINEL", json.dumps(result))
        self.assertEqual(set(result), {"video_id", "canonical_url", "source_url", "catalog_title", *METRICS.STATISTICS_EXTENSION_KEYS})


if __name__ == "__main__":
    unittest.main()
