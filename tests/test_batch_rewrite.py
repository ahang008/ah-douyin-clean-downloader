"""Offline regression tests for source preservation and incremental rewrites."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def helper(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIXTURE = helper(ROOT / "tests/test_creator_export.py", "rewrite_export_fixture")
BATCH = helper(ROOT / "scripts/batch_rewrite.py", "batch_rewrite_tests")


class BatchRewriteTests(unittest.TestCase):
    def setUp(self):
        # Reuse the setup only; do not inherit and rerun exporter's test methods.
        self.fixture = FIXTURE.CreatorExportTests("test_check_hashes_all_inputs_and_writes_no_exports")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.library = self.fixture.root
        self.base = self.library.parent
        self.work = self.base / "rewrite-work"
        self.out = self.base / "rewrite-output"
        self.catalog_path = self.fixture.catalog_path
        self.corpus([{"digg_count": 42, "comment_count": 0, "collect_count": 5, "share_count": 3},
                     {"digg_count": 0, "comment_count": 0, "collect_count": 0, "share_count": 0}])

    def corpus(self, statistics, create_times=None):
        recorder = FIXTURE.BROWSER.BrowserCatalog(FIXTURE.SEC_UID, self.catalog_path)
        works = []
        for number, counts in enumerate(statistics, 1):
            work = self.fixture.work(number)
            work["create_time"] = create_times[number - 1] if create_times else number
            if counts is not None:
                work["statistics"] = counts
            works.append(work)
        payload = self.fixture.page(works, 0, 0)
        recorder.add(0, payload, json.dumps(payload).encode())
        self.catalog = recorder.save()
        self.ids = [row["video_id"] for row in self.catalog["videos"]]
        manifest, jobs = {"videos": {}}, {}
        for number, video_id in enumerate(self.ids, 1):
            media = self.library / "media" / (video_id + ".mp4")
            media.write_bytes(("offline fixture " + video_id).encode() * 30)
            digest = hashlib.sha256(media.read_bytes()).hexdigest()
            manifest["videos"][video_id] = {"status": "verified", "path": str(media), "sha256": digest}
            segments = [{"start": 0.0, "end": 4.0, "text": f"有个细节{number}，先把事情记下来。"},
                        {"start": 4.0, "end": 9.0, "text": " 回头再看，你会发现问题。"},
                        {"start": 9.0, "end": 31.0, "text": "后续原稿讨论记录习惯，每天写下一个具体场景，先标出时间，再保留当时的想法和动作。"},
                        {"start": 31.0, "end": 44.0, "text": "最后挑选重复出现的事情，只调整一个动作，下一周继续对照。"}]
            segments[0].update(tokens=[101, 202, 303], avg_logprob=-0.25, compression_ratio=1.1,
                               temperature=0.0, no_speech_prob=0.02, seek=0, id=0)
            self.set_transcript(video_id, segments, media, digest)
            jobs[video_id] = {"status": "machine_draft_saved"}
        (self.library / "media/download-manifest.json").write_text(json.dumps(manifest))
        (self.library / "local-transcripts/_batch-state.json").write_text(json.dumps({"jobs": jobs}))

    def set_transcript(self, video_id, segments, media, media_sha):
        directory = self.library / "local-transcripts" / video_id
        directory.mkdir(parents=True, exist_ok=True)
        paths = {key: directory / filename for key, filename in FIXTURE.EXPORT.OUTPUT_NAMES.items()}
        text = "".join(segment["text"] for segment in segments)
        paths["markdown"].write_text("# Synthetic machine draft\n\n## 机器识别原文\n\n" +
                                     "\n\n".join(segment["text"].strip() for segment in segments) + "\n")
        paths["srt"].write_text(FIXTURE.ASR.render_srt(segments))
        duration = segments[-1]["end"]
        evidence = {"video_id": video_id, "status": "machine_draft_saved", "raw_text": text, "segments": segments,
                    "source_url": "https://www.douyin.com/video/" + video_id,
                    "source_media": {"path": str(media), "sha256": media_sha, "bytes": media.stat().st_size},
                    "source_audio": {"duration_seconds": duration},
                    "validation": FIXTURE.ASR.validate_result({"text": text, "segments": segments}, duration),
                    "outputs": {kind: {"sha256": hashlib.sha256(paths[kind].read_bytes()).hexdigest()}
                                for kind in ("markdown", "srt")}}
        paths["evidence"].write_text(json.dumps(evidence, ensure_ascii=False))

    def prepare(self, **changes):
        values = {"root": self.library, "catalog": None, "work_dir": self.work, "output_dir": self.out,
                  "shards": 3, "persona": "阿杭"}
        values.update(changes)
        return BATCH.prepare(SimpleNamespace(**values))

    def items(self):
        return BATCH.read_jsonl(self.work / "work-items.jsonl")

    def drafts(self):
        return [{"rank": item["rank"], "video_id": item["video_id"], "opening_segment_count": 2,
                 "title": "新的口播题目 " + str(item["rank"]), "body": "先挑一件小事，把处理步骤说清楚。第" + str(item["rank"]) + "条。",
                 "angle": "保留记录习惯的题材", "new_example": "整理当天工作笔记", "shooting": ["在桌前展示一张笔记"],
                 "first_comment": "你通常会记下哪件事？", "checks": [], "sources": []} for item in self.items()]

    def render(self, rows, partial=False, filename="drafts.jsonl"):
        path = self.base / filename
        path.write_bytes(BATCH.jsonl_bytes(rows))
        return BATCH.render(SimpleNamespace(work_dir=self.work, drafts=[path], partial=partial))

    def check(self):
        return BATCH.check(SimpleNamespace(work_dir=self.work))

    def runtime_bytes(self):
        return {str(path): path.read_bytes() for folder in (self.work, self.out) if folder.exists()
                for path in folder.rglob("*") if path.is_file() and not path.is_symlink()}

    def source_bytes(self):
        return {str(path): path.read_bytes() for path in self.library.rglob("*") if path.is_file()}

    def test_dynamic_count_balanced_shards_and_compact_inputs_without_export(self):
        self.corpus([{"digg_count": number} for number in range(5)])
        # A browser catalog takes precedence over a stale legacy catalog.
        browser = self.library / "catalog/browser-catalog.json"
        browser.write_bytes(self.catalog_path.read_bytes())
        self.catalog_path.write_text("{}")
        before = self.source_bytes()
        result = self.prepare()
        self.assertEqual((result["expected_count"], result["shard_count"]), (5, 3))
        shards = [BATCH.read_jsonl(self.work / f"inputs-{number}.jsonl") for number in (1, 2, 3)]
        self.assertEqual([len(rows) for rows in shards], [2, 2, 1])
        self.assertEqual([row["video_id"] for rows in shards for row in rows], [row["video_id"] for row in self.items()])
        row = shards[0][0]
        self.assertEqual(row["raw_text"], self.items()[0]["raw_text"])
        self.assertEqual([entry["segment_number"] for entry in row["opening_segments"]], [1, 2, 3])
        self.assertEqual(row["opening_segments"][-1]["end"], 31)
        self.assertTrue(all(set(segment) == {"segment_number", "start", "end", "text"}
                            for segment in row["opening_segments"]))
        self.assertIn("tokens", self.items()[0]["segments"][0])  # Full frozen evidence stays available.
        for field in ("statistics", "statistics_availability", "source_catalog_sha256", "original_video_path",
                      "output_path", "writing_rules", "draft_schema"):
            self.assertNotIn(field, row)
        self.assertTrue(all(item["source_paragraphs"] for item in self.items()))
        self.assertEqual(before, self.source_bytes())
        self.assertFalse((self.library / "catalog/transcripts-5.jsonl").exists())
        self.assertFalse(self.out.exists())
        self.assertFalse(self.check()["technical_complete"])

    def test_shard_count_never_produces_empty_inputs(self):
        result = self.prepare(shards=8)
        self.assertEqual(result["shard_count"], 2)
        self.assertTrue((self.work / "inputs-1.jsonl").stat().st_size)
        self.assertTrue((self.work / "inputs-2.jsonl").stat().st_size)
        self.assertFalse((self.work / "inputs-3.jsonl").exists())

    def test_zero_unknown_ties_and_complete_subset_score(self):
        a = {"digg_count": 100, "share_count": 5, "collect_count": 2, "comment_count": 10}
        b = {"digg_count": 100, "share_count": 6, "collect_count": 2, "comment_count": 1}
        zero = {key: 0 for key in BATCH.SCORE_WEIGHTS}
        unknown = {"share_count": 100, "collect_count": 100, "comment_count": 100}
        self.corpus([a, b, zero, unknown, a], create_times=[200, 100, 300, 999, 201])
        self.prepare()
        rows = self.items()
        self.assertEqual([row["video_id"] for row in rows], [self.ids[i] for i in (1, 4, 0, 2, 3)])
        self.assertEqual([row["rank"] for row in rows], list(range(1, 6)))
        zero_row, unknown_row = rows[-2:]
        self.assertEqual(zero_row["statistics"]["digg_count"], 0)
        self.assertIsNone(unknown_row["statistics"]["digg_count"])
        self.assertIn("赞0_", zero_row["folder"])
        self.assertIn("赞未获取_", unknown_row["folder"])
        self.assertIsNone(unknown_row["reference_score"])
        self.assertIsNone(unknown_row["reference_rank"])
        self.assertEqual(zero_row["reference_score"], 0)
        self.assertAlmostEqual(rows[1]["reference_score"], 63.333333, places=5)
        self.assertAlmostEqual(rows[1]["reference_percentiles"]["digg_count"], 2 / 3, places=5)
        self.assertEqual(rows[1]["reference_score"], rows[2]["reference_score"])
        self.assertEqual([row["reference_rank"] for row in rows], [1, 2, 3, 4, None])
        lone = BATCH.reference_scores([{"video_id": self.ids[0], "statistics": a}])[self.ids[0]]
        self.assertEqual(lone["reference_score"], 50)
        tied = BATCH.reference_scores([{"video_id": video_id, "statistics": a} for video_id in self.ids[:2]])
        self.assertTrue(all(row["reference_score"] == 50 for row in tied.values()))

    def test_full_render_preserves_exact_longer_opening_and_all_original_bytes(self):
        before = self.source_bytes()
        self.prepare()
        rows = self.drafts()
        self.assertLess(len(rows[0]["body"]), 150)  # No arbitrary minimum on a nonempty continuation.
        rows[0]["checks"] = ["开头同音字待对原音频"]  # The legacy schema still appears in the opening checklist.
        report = self.render(rows)
        self.assertTrue(report["technical_complete"])
        self.assertTrue(report["original_media_rehashed"])
        self.assertEqual(report["scope"], "deterministic_helper_only")
        self.assertEqual((report["helper_llm_calls"], report["helper_computer_use_calls"]), (0, 0))
        for item, draft in zip(self.items(), rows):
            text = Path(item["output_path"]).read_text()
            speech = text.split("## 口播正文\n\n", 1)[1].split("\n\n## 拍摄提示", 1)[0]
            opening = "".join(segment["text"] for segment in item["segments"][:2]).strip()
            self.assertEqual(speech, opening + "\n\n" + draft["body"])
            self.assertIn("至 9 秒", text)
        self.assertTrue((self.out / "00-2篇口播稿合集.md").is_file())
        checklist = (self.out / BATCH.INDEX_NAMES[3]).read_text()
        self.assertIn("旧字段未区分开头/正文", checklist)
        self.assertIn(rows[0]["checks"][0], checklist)
        checked = self.check()
        self.assertTrue(checked["technical_complete"])
        self.assertTrue(checked["rankings_and_local_links_verified"])
        self.assertFalse(checked["semantic_review_certified"])
        self.assertFalse(checked["audio_review_certified"])
        self.assertEqual(before, self.source_bytes())

    def test_partial_then_only_new_shard_can_complete_without_losing_earlier_script(self):
        self.prepare()
        rows = self.drafts()
        report = self.render(rows[:1], partial=True)
        first_path = Path(self.items()[0]["output_path"])
        first_bytes = first_path.read_bytes()
        self.assertFalse(report["technical_complete"])
        self.assertEqual(report["missing_ids"], [rows[1]["video_id"]])
        self.assertFalse((self.out / "00-2篇口播稿合集.md").exists())
        self.assertIn("部分交付", (self.out / BATCH.INDEX_NAMES[0]).read_text())
        self.assertFalse(self.check()["technical_complete"])
        report = self.render(rows[1:], filename="new-shard.jsonl")
        self.assertTrue(report["technical_complete"])
        self.assertEqual(report["rendered_count"], 2)
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertTrue(self.check()["technical_complete"])
        self.assertTrue((self.out / "00-2篇口播稿合集.md").is_file())
        before = self.runtime_bytes()
        with self.assertRaisesRegex(ValueError, "cannot be downgraded"):
            self.render(rows[1:], partial=True)
        self.assertEqual(before, self.runtime_bytes())

    def test_invalid_coverage_identity_rank_or_body_writes_nothing(self):
        self.prepare()
        rows = self.drafts()
        bad_cases = ([rows[0]], [rows[0], rows[0]],
                     [{**rows[0], "video_id": "7600000000000000099"}, rows[1]],
                     [{**rows[0], "rank": 2}, rows[1]],
                     [{**rows[0], "body": "  "}, rows[1]],
                     [{**rows[0], "shooting": []}, rows[1]],
                     [{**rows[0], "opening_segment_count": True}, rows[1]])
        before = self.runtime_bytes()
        for bad in bad_cases:
            with self.subTest(rows=bad):
                with self.assertRaises(ValueError):
                    self.render(bad)
                self.assertEqual(before, self.runtime_bytes())
                self.assertFalse(self.out.exists())

    def test_locked_opening_cannot_shrink_grow_change_or_repeat_in_body(self):
        self.prepare()
        rows = self.drafts()
        self.render(rows)
        before = self.runtime_bytes()
        for count in (1, 3):
            with self.assertRaisesRegex(ValueError, "locked opening"):
                self.render([{**rows[0], "opening_segment_count": count}])
            self.assertEqual(before, self.runtime_bytes())
        opening = "".join(segment["text"] for segment in self.items()[0]["segments"][:2]).strip()
        with self.assertRaisesRegex(ValueError, "repeats"):
            self.render([{**rows[0], "body": opening + "\n后续"}])
        with self.assertRaisesRegex(ValueError, "schema"):
            self.render([{**rows[0], "opening": "另一个开头"}])
        self.assertEqual(before, self.runtime_bytes())
        self.render([{**rows[0], "body": "后面的表达可以明确更新，但锁定开头保持原字。"}])
        self.assertTrue(self.check()["technical_complete"])

    def test_whitespace_normalized_source_is_not_enough_for_an_exact_opening(self):
        path = self.library / "local-transcripts" / self.ids[0] / FIXTURE.EXPORT.OUTPUT_NAMES["evidence"]
        evidence = json.loads(path.read_text())
        evidence["raw_text"] = evidence["raw_text"].replace("。", "。 ", 1)
        path.write_text(json.dumps(evidence, ensure_ascii=False))
        self.prepare()  # The exporter accepts structurally equal whitespace-normalized text.
        rows = self.drafts()
        with self.assertRaisesRegex(ValueError, "exact raw_text prefix"):
            self.render(rows)
        self.assertFalse(self.out.exists())

    def test_combined_check_notes_are_never_silently_dropped(self):
        self.prepare()
        rows = self.drafts()
        with self.assertRaisesRegex(ValueError, "at most 3 combined"):
            self.render([{**rows[0], "checks": ["正文1", "正文2"], "opening_checks": ["开头1", "开头2"]}, rows[1]])
        with self.assertRaisesRegex(ValueError, "must not repeat"):
            self.render([{**rows[0], "checks": ["相同核对"], "opening_checks": ["相同核对"]}, rows[1]])
        rows[0]["opening_checks"] = ["开头里的亲历表达待本人核对"]
        rows[0]["checks"] = ["正文专名待核对"]
        self.render(rows)
        self.assertIn(rows[0]["opening_checks"][0], (self.out / BATCH.INDEX_NAMES[3]).read_text())
        self.assertTrue(self.check()["technical_complete"])

    def test_prepare_reuses_read_only_and_rejects_parameter_or_source_drift(self):
        self.prepare()
        before = self.runtime_bytes()
        self.assertTrue(self.prepare()["read_only_reuse"])
        self.assertEqual(before, self.runtime_bytes())
        for changes in ({"persona": "另一署名"}, {"shards": 2}, {"output_dir": self.base / "other-output"}):
            with self.assertRaisesRegex(ValueError, "drifted"):
                self.prepare(**changes)
            self.assertEqual(before, self.runtime_bytes())
        path = self.library / "local-transcripts/_batch-state.json"
        state = json.loads(path.read_text())
        state["another_observation"] = True
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, "drifted"):
            self.prepare()
        with self.assertRaisesRegex(ValueError, "drifted"):
            self.check()
        self.assertEqual(before, self.runtime_bytes())

    def test_every_source_hash_including_same_length_media_is_revalidated(self):
        self.prepare()
        before = self.runtime_bytes()
        media = self.library / "media" / (self.ids[0] + ".mp4")
        data = media.read_bytes()
        media.write_bytes(b"x" + data[1:])
        with self.assertRaisesRegex(ValueError, "media path/size/hash"):
            self.check()
        with self.assertRaisesRegex(ValueError, "media path/size/hash"):
            self.render(self.drafts())
        self.assertEqual(before, self.runtime_bytes())
        media.write_bytes(data)
        md = self.library / "local-transcripts" / self.ids[0] / FIXTURE.EXPORT.OUTPUT_NAMES["markdown"]
        md.write_text(md.read_text() + "手改了机器稿")
        with self.assertRaisesRegex(ValueError, "ASR output hash"):
            self.check()
        self.assertEqual(before, self.runtime_bytes())

    def test_prepared_work_or_packet_hash_drift_is_not_accepted(self):
        self.prepare()
        for name in ("work-items.jsonl", "inputs-1.jsonl"):
            path = self.work / name
            original = path.read_bytes()
            path.write_bytes(original + b"\n")
            with self.assertRaisesRegex(ValueError, "work/input file was modified"):
                self.check()
            path.write_bytes(original)

    def test_idempotence_and_manual_script_index_or_metadata_edits_are_preserved(self):
        self.prepare()
        rows = self.drafts()
        self.render(rows)
        before = self.runtime_bytes()
        self.render(rows)
        self.assertEqual(before, self.runtime_bytes())
        paths = [Path(self.items()[0]["output_path"]), self.out / BATCH.INDEX_NAMES[0],
                 self.out / "00-2篇口播稿合集.md", self.work / "quality-audit.json",
                 self.work / "rendered-manifest.jsonl"]
        for path in paths:
            original = path.read_bytes()
            manual = original + b"\nmanual edit kept\n"
            path.write_bytes(manual)
            with self.assertRaisesRegex(ValueError, "modified; refusing"):
                self.render(rows)
            with self.assertRaisesRegex(ValueError, "modified; refusing"):
                self.check()
            self.assertEqual(path.read_bytes(), manual)
            path.write_bytes(original)
        state = self.work / "render-state.json"
        original = state.read_bytes()
        state.write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "render-state.json was modified"):
            self.render(rows)
        self.assertEqual(state.read_bytes(), original + b"\n")

    def test_unknown_existing_target_blocks_whole_batch(self):
        self.prepare()
        path = Path(self.items()[1]["output_path"])
        path.parent.mkdir(parents=True)
        path.write_text("valuable existing draft")
        before = self.runtime_bytes()
        with self.assertRaisesRegex(ValueError, "Unknown existing output"):
            self.render(self.drafts())
        self.assertEqual(before, self.runtime_bytes())
        self.assertFalse(Path(self.items()[0]["output_path"]).exists())

    def test_unknown_existing_target_with_identical_bytes_is_allowed(self):
        self.prepare()
        item, draft = self.items()[0], self.drafts()[0]
        path = Path(item["output_path"])
        path.parent.mkdir(parents=True)
        expected = BATCH.article(item, draft, BATCH.opening_for(item, 2), "阿杭").encode("utf-8")
        path.write_bytes(expected)
        report = self.render(self.drafts())
        self.assertTrue(report["technical_complete"])
        self.assertEqual(path.read_bytes(), expected)
        self.assertTrue(self.check()["technical_complete"])

    def test_generated_link_target_missing_is_not_complete(self):
        self.prepare()
        self.render(self.drafts())
        path = Path(self.items()[0]["output_path"])
        path.unlink()
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.check()

    def test_all_containment_directions_and_equal_paths_are_rejected(self):
        cases = ((self.library, self.library / "work", self.out),
                 (self.library, self.base, self.out),
                 (self.library, self.work, self.library / "out"),
                 (self.library, self.work, self.base),
                 (self.library, self.work, self.work / "out"),
                 (self.library, self.out / "work", self.out),
                 (self.library, self.library, self.out),
                 (self.library, self.work, self.work))
        before = self.source_bytes()
        for root, work, out in cases:
            with self.subTest(paths=(root, work, out)):
                with self.assertRaisesRegex(ValueError, "separate directories"):
                    self.prepare(root=root, work_dir=work, output_dir=out)
                self.assertEqual(before, self.source_bytes())
        self.assertFalse(self.work.exists())
        self.assertFalse(self.out.exists())

    def test_symlink_roots_new_parent_and_output_subdirectory_cannot_escape(self):
        library_link = self.base / "library-link"
        library_link.symlink_to(self.library, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symbolic links"):
            self.prepare(root=library_link)
        destination = self.base / "outside"
        destination.mkdir()
        parent_link = self.base / "new-parent-link"
        parent_link.symlink_to(destination, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symbolic links"):
            self.prepare(work_dir=parent_link / "not-created-yet/work")
        self.prepare()
        self.out.mkdir()
        (self.out / self.items()[0]["folder"]).symlink_to(destination, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symbolic links"):
            self.render(self.drafts())
        self.assertEqual(list(destination.iterdir()), [])
        self.assertFalse(Path(self.items()[1]["output_path"]).exists())

    def test_symlink_source_page_or_media_is_rejected_even_within_library(self):
        page = Path(self.catalog["page_evidence"][0]["file"])
        copy = page.with_name("page-copy.json")
        copy.write_bytes(page.read_bytes())
        page.unlink()
        page.symlink_to(copy)
        with self.assertRaisesRegex(ValueError, "Symbolic links"):
            self.prepare()
        page.unlink()
        page.write_bytes(copy.read_bytes())
        media = self.library / "media" / (self.ids[0] + ".mp4")
        copied_media = media.with_name("media-copy.mp4")
        copied_media.write_bytes(media.read_bytes())
        media.unlink()
        media.symlink_to(copied_media)
        with self.assertRaisesRegex(ValueError, "Symbolic links"):
            self.prepare()
        self.assertFalse(self.work.exists())

    def test_overlap_and_length_flags_remain_manual_review_hints(self):
        self.prepare()
        rows = self.drafts()
        overlap = self.items()[0]["segments"][2]["text"]
        repeated = "这段是合成稿的共享段落，只用于检查跨稿重复的提示行为，不代表违规判定也不代表现实经历。请作者自己核对具体表达。"
        rows[0]["body"] = overlap + "\n\n" + repeated
        rows[1]["body"] = "另一篇仍然保留自己的题材。\n\n" + repeated
        self.render(rows)
        audit = json.loads((self.work / "quality-audit.json").read_text())
        kinds = {flag["type"] for flag in audit["flags"]}
        self.assertIn("source_contiguous_overlap", kinds)
        self.assertIn("cross_draft_repeated_paragraph", kinds)
        self.assertTrue(all(flag["review_only"] for flag in audit["flags"]))
        self.assertTrue(audit["technical_complete"])
        self.assertFalse(audit["semantic_review_certified"])
        self.assertTrue(self.check()["technical_complete"])

    def test_cli_uses_documented_subcommands_and_structured_report(self):
        arguments = [str(ROOT / "scripts/batch_rewrite.py"), "prepare", "--root", str(self.library),
                     "--work-dir", str(self.work), "--output-dir", str(self.out)]
        output = io.StringIO()
        with patch.object(sys, "argv", arguments), contextlib.redirect_stdout(output):
            self.assertEqual(BATCH.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["event"], "rewrite_prepared")


if __name__ == "__main__":
    unittest.main()
