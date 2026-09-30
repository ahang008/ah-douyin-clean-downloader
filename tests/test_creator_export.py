"""Complete-corpus fixtures built from actual collector output, fully offline."""
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

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def helper(filename, name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXPORT = helper("export_transcripts.py", "creator_export_fixture")
BROWSER = helper("collect_creator_browser.py", "export_catalog_fixture")
ASR = helper("transcribe_local.py", "export_asr_fixture")
SEC_UID = "MS4wLjExportFixtureCreator"


class CreatorExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "library"
        self.root.mkdir()
        self.catalog_path = self.root / "catalog" / "catalog.json"
        self.author = "../测试/作者:名"
        recorder = BROWSER.BrowserCatalog(SEC_UID, self.catalog_path)
        first = self.work(1)
        second = self.work(2)
        foreign = self.work(4)
        foreign["author"]["sec_uid"] = "MS4wLjForeignFixture"
        for cursor, payload in ((0, self.page([first], 90, 1)),
                                (90, self.page([first, second, self.work(3, image=True), foreign], 0, 0))):
            recorder.add(cursor, payload, json.dumps(payload).encode())
        self.catalog = recorder.save()
        self.ids = [row["video_id"] for row in self.catalog["videos"]]
        self.manifest_path = self.root / "media" / "download-manifest.json"
        self.manifest_path.parent.mkdir()
        self.manifest = {"videos": {}}
        jobs = {}
        self.inputs = [self.catalog_path, *[Path(page["file"]) for page in self.catalog["page_evidence"]]]
        for number, video_id in enumerate(self.ids, 1):
            media = self.manifest_path.parent / (video_id + ".mp4")
            media.write_bytes(b"offline fixture, never decoded" * 100)
            sha = hashlib.sha256(media.read_bytes()).hexdigest()
            self.manifest["videos"][video_id] = {"status": "verified", "path": str(media), "sha256": sha}
            segments = [{"start": 0.0, "end": 1.0, "text": f"原始机器正文{number}。"},
                        {"start": 1.0, "end": 2.0, "text": " 同音字暂时保留。"}]
            text = "".join(segment["text"] for segment in segments)
            directory = self.root / "local-transcripts" / video_id
            directory.mkdir(parents=True)
            md = directory / EXPORT.OUTPUT_NAMES["markdown"]
            srt = directory / EXPORT.OUTPUT_NAMES["srt"]
            md.write_text("# Fixture\n\n## 机器识别原文\n\n" + "\n\n".join(segment["text"].strip() for segment in segments) + "\n")
            srt.write_text(ASR.render_srt(segments))
            evidence = {"video_id": video_id, "status": "machine_draft_saved", "raw_text": text,
                        "segments": segments, "source_url": "https://www.douyin.com/video/" + video_id,
                        "source_media": {"path": str(media), "sha256": sha, "bytes": media.stat().st_size},
                        "source_audio": {"duration_seconds": 2.0},
                        "validation": ASR.validate_result({"text": text, "segments": segments}, 2.0),
                        "outputs": {"markdown": {"sha256": hashlib.sha256(md.read_bytes()).hexdigest()},
                                    "srt": {"sha256": hashlib.sha256(srt.read_bytes()).hexdigest()}}}
            evidence_path = directory / EXPORT.OUTPUT_NAMES["evidence"]
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False))
            jobs[video_id] = {"status": "machine_draft_saved"}
            self.inputs += [media, md, srt, evidence_path]
        self.manifest_path.write_text(json.dumps(self.manifest))
        state = self.root / "local-transcripts" / "_batch-state.json"
        state.write_text(json.dumps({"jobs": jobs}))
        self.inputs += [self.manifest_path, state]
        self.outputs = [self.root / "测试作者名-2条机器逐字稿合集.md",
                        self.root / "catalog" / "transcripts-2.jsonl", self.root / "corpus-export.json"]

    def work(self, number, image=False):
        return {"aweme_id": str(7600000000000000000 + number), "desc": "作品 " + str(number),
                "aweme_type": 68 if image else 0, "images": [{}] if image else None,
                "author": {"sec_uid": SEC_UID, "nickname": self.author, "uid": "123"},
                "video": {"duration": 2000}}

    def page(self, rows, returned, more):
        return {"status_code": 0, "aweme_list": rows, "max_cursor": returned, "has_more": more,
                "not_login_module": {"guide_login_tip_exist": False}}

    def run_export(self, *extra):
        with patch.object(sys, "argv", [str(SCRIPTS / "export_transcripts.py"), "--root", str(self.root), *extra]), \
             patch.object(EXPORT.time, "sleep", side_effect=AssertionError("Fixture must not wait")), \
             contextlib.redirect_stdout(io.StringIO()):
            return EXPORT.main()

    def evidence(self):
        return self.root / "local-transcripts" / self.ids[0] / EXPORT.OUTPUT_NAMES["evidence"]

    def test_complete_export_derives_safe_author_and_actual_count_without_rewriting_inputs(self):
        before = {path: path.read_bytes() for path in self.inputs}
        self.assertEqual(self.run_export(), 0)
        self.assertTrue(all(path.is_file() for path in self.outputs))
        records = [json.loads(line) for line in self.outputs[1].read_text().splitlines()]
        self.assertEqual([row["video_id"] for row in records], self.ids)
        self.assertEqual(len(records), 2)  # The image work was not treated as a video.
        self.assertEqual(self.catalog["excluded_foreign_author_count"], 1)
        for row in records:
            evidence = json.loads(Path(row["asr_evidence_path"]).read_text())
            self.assertEqual(row["machine_text"], evidence["raw_text"])
            self.assertEqual(row["source_catalog_sha256"], hashlib.sha256(self.catalog_path.read_bytes()).hexdigest())
        self.assertEqual(before, {path: path.read_bytes() for path in self.inputs})

    def test_export_rejects_same_work_id_under_target_and_foreign_authors_in_either_page_order(self):
        target = self.work(1)
        foreign = self.work(1)
        foreign["author"]["sec_uid"] = "MS4wLjForeignFixture"
        for foreign_first in (False, True):
            with self.subTest(foreign_first=foreign_first):
                path = self.root / "catalog" / f"identity-conflict-{foreign_first}.json"
                recorder = BROWSER.BrowserCatalog(SEC_UID, path)
                first, second = (foreign, target) if foreign_first else (target, foreign)
                for cursor, page in ((0, self.page([first], 90, 1)),
                                     (90, self.page([second], 0, 0))):
                    recorder.add(cursor, page, json.dumps(page).encode())
                forged = recorder.save()
                self.assertFalse(forged["catalog_complete"])
                forged["catalog_complete"] = True
                forged["errors"] = []
                with self.assertRaisesRegex(ValueError, "both the requested and a foreign author"):
                    EXPORT.validated_pagination(forged, path)

    def test_check_hashes_all_inputs_and_writes_no_exports(self):
        self.assertEqual(self.run_export("--check"), 0)
        self.assertTrue(all(not path.exists() for path in self.outputs))
        media = Path(self.manifest["videos"][self.ids[0]]["path"])
        original = media.read_bytes()
        media.write_bytes(b"tampered" + original[8:])  # Same length, different checksum.
        with self.assertRaisesRegex(ValueError, "media path/size/hash"):
            self.run_export("--check")
        self.assertTrue(all(not path.exists() for path in self.outputs))

    def test_tampered_page_cannot_replace_existing_corpus(self):
        for output in self.outputs:
            output.write_text("existing corpus bytes")
        page = Path(self.catalog["page_evidence"][0]["file"])
        page.write_bytes(page.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "file hash mismatch"):
            self.run_export()
        self.assertTrue(all(output.read_text() == "existing corpus bytes" for output in self.outputs))

    def test_forged_complete_gap_filtered_author_or_extra_video_is_rejected(self):
        original = json.loads(self.catalog_path.read_text())
        mutations = (
            lambda value: value["page_evidence"][1].update(requested_cursor=80),
            lambda value: value["page_evidence"][0]["not_login_module"].update(guide_login_tip_exist=True),
            lambda value: value["videos"][0]["author"].update(sec_uid="MS4wLjWrongCreator"),
            lambda value: value["videos"].append({"video_id": "7600000000000000009"}),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                value = json.loads(json.dumps(original))
                mutate(value)
                self.catalog_path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    self.run_export()
                self.assertTrue(all(not output.exists() for output in self.outputs))

    def test_segment_text_mismatch_is_rejected_even_if_validation_flag_remains_true(self):
        path = self.evidence()
        value = json.loads(path.read_text())
        value["segments"][0]["text"] = "不同的识别结果"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "full text differs"):
            self.run_export()
        self.assertTrue(all(not output.exists() for output in self.outputs))

    def test_srt_text_or_timecode_mismatch_is_rejected_even_with_updated_output_hash(self):
        path = self.evidence()
        value = json.loads(path.read_text())
        srt = path.parent / EXPORT.OUTPUT_NAMES["srt"]
        srt.write_text(srt.read_text().replace("00:00:01,000", "00:00:01,100"))
        value["outputs"]["srt"]["sha256"] = hashlib.sha256(srt.read_bytes()).hexdigest()
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "SRT text/timecodes"):
            self.run_export("--check")

    def test_missing_transcript_and_wrong_count_never_publish_full_exports(self):
        with self.assertRaises(ValueError):
            self.run_export("--expected-count", "139")
        self.evidence().unlink()
        self.assertEqual(self.run_export(), 2)
        self.assertTrue(all(not output.exists() for output in self.outputs))

    def test_structurally_valid_but_sparse_short_clip_is_not_exported_as_complete(self):
        evidence_path = self.evidence()
        value = json.loads(evidence_path.read_text())
        value["raw_text"] = "好"
        value["segments"] = [{"start": 0.0, "end": 1.0, "text": "好"}]
        value["source_audio"]["duration_seconds"] = 20.0
        markdown = evidence_path.parent / EXPORT.OUTPUT_NAMES["markdown"]
        srt = evidence_path.parent / EXPORT.OUTPUT_NAMES["srt"]
        markdown.write_text("# Fixture\n\n## 机器识别原文\n\n好\n")
        srt.write_text(ASR.render_srt(value["segments"]))
        value["outputs"]["markdown"]["sha256"] = hashlib.sha256(markdown.read_bytes()).hexdigest()
        value["outputs"]["srt"]["sha256"] = hashlib.sha256(srt.read_bytes()).hexdigest()
        evidence_path.write_text(json.dumps(value, ensure_ascii=False))
        self.assertEqual(self.run_export("--check"), 2)
        self.assertTrue(all(not output.exists() for output in self.outputs))

    def test_export_cannot_overwrite_original_media(self):
        media = Path(self.manifest["videos"][self.ids[0]]["path"])
        before = media.read_bytes()
        with self.assertRaisesRegex(ValueError, "must not replace"):
            self.run_export("--markdown-output", str(media))
        self.assertEqual(media.read_bytes(), before)

    def test_safe_author_fallback_and_single_component(self):
        for name in ("../..", "NUL", " ", "\n\x00"):
            self.assertEqual(EXPORT.safe_author_name(name), "抖音作者")
        self.assertEqual(EXPORT.safe_author_name("/作者\\名:*?<>|"), "作者名")


if __name__ == "__main__":
    unittest.main()
