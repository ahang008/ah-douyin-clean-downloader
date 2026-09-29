#!/usr/bin/env python3
"""Export an existing complete creator catalog and its unchanged machine drafts.

Read-only inputs: catalog, download manifest, transcript state, and ASR outputs.
No browser, credentials, LLM, download, or transcription execution is used.
The two final exports are written only after every selected video is available.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import unicodedata

OUTPUT_NAMES = {"markdown": "01-本地ASR机器逐字稿.md", "srt": "01-本地ASR机器逐字稿.srt",
                "evidence": "01-本地ASR识别证据.json"}
SUCCESS = {"machine_draft_saved", "skipped_verified"}


def load_local_helper(filename, module_name):
    spec = importlib.util.spec_from_file_location(module_name, Path(__file__).resolve().parent / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PATHS = load_local_helper("artifact_paths.py", "corpus_artifact_paths")
METRICS = load_local_helper("work_metrics.py", "corpus_public_metrics")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def safe_author_name(value):
    """A public nickname may never create directories or reserved filenames."""
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = "".join(char for char in value if char not in '/\\:*?"<>|'
                    and not unicodedata.category(char).startswith("C"))
    value = re.sub(r"\s+", " ", value).strip(" .-_#")[:72].rstrip(" .")
    if not value or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", value, re.I):
        return "抖音作者"
    return value


def validated_pagination(catalog, catalog_path):
    """Rebuild the complete public work list from hash-verified saved pages.

    These are the collector's public metadata projections, not the original
    token-bearing responses. Only saved_file_sha256 is a saved-file checksum.
    """
    helper = load_local_helper("collect_creator_browser.py", "corpus_browser_validation")
    creator = catalog.get("creator") or {}
    sec_uid = creator.get("sec_uid")
    if (not isinstance(sec_uid, str) or not re.fullmatch(r"MS4wLj[A-Za-z0-9_-]+", sec_uid)
            or catalog.get("method") != "official_browser_sdk_response_capture"
            or catalog.get("pagination_exhausted") is not True or catalog.get("has_more") is not False
            or catalog.get("catalog_visibility") != "public" or catalog.get("errors")):
        raise ValueError("A complete official browser pagination chain is required")
    pages = catalog.get("page_evidence")
    if not isinstance(pages, list) or not pages:
        raise ValueError("Saved public pagination evidence is required")
    expected_cursor, cursors, seen, evidence_paths = 0, set(), {}, []
    for number, page in enumerate(pages, 1):
        if not isinstance(page, dict):
            raise ValueError("Invalid pagination evidence record")
        requested = page.get("requested_cursor")
        if (not isinstance(requested, int) or isinstance(requested, bool) or requested < 0
                or requested != expected_cursor or requested in cursors
                or page.get("endpoint") != helper.POST_URL
                or page.get("all_authors_match_sec_uid") is not True
                or page.get("saved_payload_kind") != "public_metadata_projection"
                or (page.get("not_login_module") or {}).get("guide_login_tip_exist")):
            raise ValueError("Pagination is discontinuous, filtered, or lacks official author evidence")
        cursors.add(requested)
        filename, digest = page.get("file"), page.get("saved_file_sha256")
        if not isinstance(filename, str) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Pagination saved-file checksum is missing")
        path = Path(filename).expanduser()
        if not path.is_absolute():
            path = catalog_path.parent / path
        path = path.resolve()
        if catalog_path.parent.resolve() not in path.parents:
            raise ValueError("Pagination evidence must stay in the catalog's own directory")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Pagination evidence file hash mismatch")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Saved page data is not an object")
        rows = payload.get("aweme_list")
        returned, more = payload.get("max_cursor"), payload.get("has_more")
        if (payload.get("status_code") != 0 or not isinstance(rows, list)
                or not isinstance(returned, int) or isinstance(returned, bool) or returned < 0
                or not isinstance(more, (int, bool)) or more not in (0, 1)
                or returned != page.get("returned_cursor") or bool(more) != page.get("has_more")
                or (payload.get("not_login_module") or {}).get("guide_login_tip_exist")
                or (number == 1 and not rows) or page.get("returned_works") != len(rows)):
            raise ValueError("Saved page data does not match pagination evidence")
        fresh = 0
        for row in rows:
            work = helper.normalize_work(row, sec_uid)
            if work["video_id"] not in seen:
                seen[work["video_id"]] = {**work, "catalog_page": number}
                fresh += 1
        if fresh != page.get("new_unique_works") or (more and not fresh):
            raise ValueError("Pagination unique-work evidence is inconsistent")
        if bool(more) != (number < len(pages)):
            raise ValueError("Pagination does not end at the actual terminal response")
        expected_cursor = returned
        evidence_paths.append(path)
    videos = [row for row in seen.values() if row["is_video"]]
    others = [row for row in seen.values() if not row["is_video"]]
    if (videos != catalog.get("videos") or others != catalog.get("non_video_works")
            or len(videos) != catalog.get("video_count") or len(others) != catalog.get("non_video_work_count")
            or len(seen) != catalog.get("work_count") or expected_cursor != catalog.get("cursor")):
        raise ValueError("Catalog works differ from the hash-verified public pagination chain")
    return evidence_paths


def catalog_records(args):
    raw = args.catalog.read_bytes()
    catalog = json.loads(raw)
    videos = catalog.get("videos") or []
    if catalog.get("catalog_complete") is not True or not videos:
        raise ValueError("A complete, nonempty video catalog is required")
    ids = [str(row.get("video_id") or "") for row in videos]
    if ((args.expected_count is not None and len(ids) != args.expected_count)
            or len(ids) != len(set(ids)) or any(not re.fullmatch(r"\d{16,22}", value) for value in ids)):
        raise ValueError("Unique catalog video count does not match the requested export count")
    args.catalog_sha256 = hashlib.sha256(raw).hexdigest()
    args.evidence_paths = validated_pagination(catalog, args.catalog)
    return catalog, videos


def readiness(args, videos):
    manifest = read_json(args.root / "media/download-manifest.json").get("videos", {})
    jobs = read_json(args.root / "local-transcripts/_batch-state.json").get("jobs", {})
    ready, missing = 0, []
    for row in videos:
        video_id = row["video_id"]
        directory = args.root / "local-transcripts" / video_id
        files = list(PATHS.resolve_artifact_paths(directory).values())
        media = manifest.get(video_id, {})
        complete = jobs.get(video_id, {}).get("status") in SUCCESS and all(path.is_file() and path.stat().st_size for path in files)
        complete = complete and media.get("status") == "verified" and bool(media.get("path")) and Path(media["path"]).is_file()
        if complete:
            ready += 1
        else:
            missing.append(video_id)
    return {"ready": ready, "expected": len(videos), "missing_ids": missing, "all_ready": not missing}, manifest


def collect_existing_text(args, catalog, videos, manifest):
    records, bodies = [], []
    helper = load_local_helper("transcribe_local.py", "corpus_asr_validation")
    for number, row in enumerate(videos, 1):
        video_id = row["video_id"]
        directory = args.root / "local-transcripts" / video_id
        files = PATHS.resolve_artifact_paths(directory)
        if any(args.root not in path.resolve().parents for path in files.values()):
            raise ValueError("ASR input path leaves the selected creator library")
        evidence = read_json(files["evidence"])
        if evidence.get("video_id") != video_id or evidence.get("status") != "machine_draft_saved":
            raise ValueError("ASR evidence identity/status mismatch for " + video_id)
        media = Path(manifest[video_id]["path"]).expanduser().resolve()
        source = evidence.get("source_media") or {}
        recorded_sha = manifest[video_id].get("sha256")
        if (not isinstance(recorded_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded_sha)
                or source.get("sha256") != recorded_sha):
            raise ValueError("ASR evidence media hash differs from the verified manifest for " + video_id)
        if (not media.is_file() or not media.stat().st_size or (args.root / "media").resolve() not in media.parents
                or not isinstance(source.get("path"), str) or Path(source["path"]).expanduser().resolve() != media
                or source.get("bytes") != media.stat().st_size
                or helper.sha256_file(media) != recorded_sha):
            raise ValueError("Original media path/size/hash differs from ASR evidence for " + video_id)
        validation = evidence.get("validation", {})
        if validation.get("full_text_matches_segments") is not True or validation.get("timestamps_valid") is not True:
            raise ValueError("ASR structural validation is incomplete for " + video_id)
        duration = float((evidence.get("source_audio") or {}).get("duration_seconds", 0))
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("ASR audio duration is missing for " + video_id)
        segments = evidence.get("segments")
        if not isinstance(segments, list) or any(not isinstance(segment, dict) for segment in segments):
            raise ValueError("ASR segments are missing for " + video_id)
        helper.validate_result({"text": evidence.get("raw_text"), "segments": segments}, duration)
        digests = {}
        for kind in ("markdown", "srt"):
            digests[kind] = hashlib.sha256(files[kind].read_bytes()).hexdigest()
            if digests[kind] != evidence.get("outputs", {}).get(kind, {}).get("sha256"):
                raise ValueError("Original ASR output hash mismatch for " + video_id + " " + kind)
        original = files["markdown"].read_text(encoding="utf-8")
        _, marker, body = original.partition("## 机器识别原文\n\n")
        text = evidence.get("raw_text")
        if not marker or not isinstance(text, str) or not text.strip():
            raise ValueError("Machine text is missing for " + video_id)
        compact = lambda value: re.sub(r"\s+", "", value)
        if compact(body) != compact(text):
            raise ValueError("Markdown machine text differs from ASR evidence for " + video_id)
        if files["srt"].read_text(encoding="utf-8") != helper.render_srt(segments):
            raise ValueError("SRT text/timecodes differ from ASR segments for " + video_id)
        source = "https://www.douyin.com/video/" + video_id
        if evidence.get("source_url") != source:
            raise ValueError("ASR canonical source identity differs for " + video_id)
        record = {
            "order": number, "video_id": video_id, "title": row.get("title") or evidence.get("title") or video_id,
            "official_url": source, "author": catalog.get("creator", {}).get("nickname") or "抖音作者",
            "original_video_path": str(Path(manifest[video_id]["path"]).resolve()),
            "transcript_path": str(files["markdown"]), "srt_path": str(files["srt"]),
            "asr_evidence_path": str(files["evidence"]), "machine_text": text,
            "character_count": len(text), "transcript_status": "machine_output_not_audio_proofread",
            "source_markdown_sha256": digests["markdown"], "source_srt_sha256": digests["srt"],
            "source_catalog_sha256": args.catalog_sha256,
            "source_media_sha256": recorded_sha,
            "asr_warnings": validation.get("warnings", []),
        }
        if "statistics" in row:
            record.update({key: row.get(key) for key in
                           ("statistics", "statistics_availability", "statistics_captured_at")})
            record["create_time"] = row.get("create_time")
        records.append(record)
        bodies.append(body)
    return records, bodies


def render_markdown(records, bodies, catalog_sha256):
    count = len(records)
    author = str(records[0]["author"]).replace("\n", " ").replace("\r", " ")
    lines = [f"# {author} · {count} 条机器逐字稿合集\n\n",
             "生成时间：" + datetime.now(timezone.utc).isoformat(timespec="seconds") + "\n\n",
             f"共 {count} 条，按完整公开视频目录顺序汇总。文字来自已有本地 ASR 机器稿，未统一人工听音校对；同音词、专名和口误可能需要核对。原片、原稿、字幕与识别证据均保留。\n\n",
             f"完整目录 SHA-256：`{catalog_sha256}`\n\n",
             "## 目录\n\n"]
    for row in records:
        title = str(row["title"]).replace("\n", " ").replace("[", "\\[").replace("]", "\\]")
        lines.append(f"- [{row['order']:03d} · {title}](#video-{row['video_id']})\n")
    for row, body in zip(records, bodies):
        title = str(row["title"]).replace("\n", " ")
        lines.extend([f"\n---\n\n<a id=\"video-{row['video_id']}\"></a>\n\n",
                      f"## {row['order']:03d} · {title}\n\n",
                      f"视频 ID：{row['video_id']} · [官方作品]({row['official_url']})\n\n",
                      f"[原片](<{row['original_video_path']}>) · [原始机器稿](<{row['transcript_path']}>) · [SRT](<{row['srt_path']}>) · [识别证据](<{row['asr_evidence_path']}>)\n\n"])
        if "statistics" in row:
            lines.append("公开指标快照：赞 " + METRICS.metric_text(row, "digg_count") +
                         " · 评 " + METRICS.metric_text(row, "comment_count") +
                         " · 藏 " + METRICS.metric_text(row, "collect_count") +
                         " · 转 " + METRICS.metric_text(row, "share_count") +
                         " · 采集时间 " + str(row.get("statistics_captured_at") or "未获取") + "\n\n")
        lines.append(body.rstrip("\n") + "\n")
    return "".join(lines)


def export(args, catalog, videos, manifest):
    records, bodies = collect_existing_text(args, catalog, videos, manifest)
    source_paths = {Path(row[key]).resolve() for row in records for key in ("original_video_path", "transcript_path", "srt_path", "asr_evidence_path")}
    source_paths.update([args.catalog, *args.evidence_paths, args.root / "media/download-manifest.json",
                         args.root / "local-transcripts/_batch-state.json"])
    report_path = args.root / "corpus-export.json"
    if (args.markdown_output == args.jsonl_output or args.markdown_output in source_paths
            or args.jsonl_output in source_paths or report_path in source_paths
            or report_path in (args.markdown_output, args.jsonl_output)):
        raise ValueError("Export outputs must not replace any original input")
    if hashlib.sha256(args.catalog.read_bytes()).hexdigest() != args.catalog_sha256:
        raise ValueError("The catalog changed during corpus export; retry with the current complete catalog")
    markdown = render_markdown(records, bodies, args.catalog_sha256)
    jsonl = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records)
    decoded = [json.loads(line) for line in jsonl.splitlines()]
    if [row["video_id"] for row in decoded] != [row["video_id"] for row in videos] or len(decoded) != args.expected_count:
        raise ValueError("Export round-trip count/order validation failed")
    if any(decoded[index]["machine_text"] != records[index]["machine_text"] for index in range(len(records))):
        raise ValueError("Export round-trip machine text validation failed")
    prepared = []
    try:
        for output, content in ((args.markdown_output, markdown), (args.jsonl_output, jsonl)):
            output.parent.mkdir(parents=True, exist_ok=True)
            descriptor, filename = tempfile.mkstemp(prefix="." + output.name + ".", dir=output.parent)
            temporary = Path(filename)
            prepared.append((temporary, output))
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        for temporary, output in prepared:
            os.replace(temporary, output)
    finally:
        for temporary, _ in prepared:
            temporary.unlink(missing_ok=True)
    report = {"event": "corpus_exported", "entries": len(records),
                      "markdown": str(args.markdown_output), "jsonl": str(args.jsonl_output),
                      "total_characters": sum(row["character_count"] for row in records),
                      "source_catalog_sha256": args.catalog_sha256, "pagination_hashes_verified": True,
                      "source_outputs_preserved": True, "llm_calls": 0}
    descriptor, filename = tempfile.mkstemp(prefix=".corpus-export.", dir=args.root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(filename, report_path)
    finally:
        Path(filename).unlink(missing_ok=True)
    print(json.dumps(report, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="existing creator library folder")
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--expected-count", type=int, help="optional count assertion; otherwise derive from the complete catalog")
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--jsonl-output", type=Path)
    parser.add_argument("--wait", action="store_true", help="wait for the actual final ASR batch, without starting any other job")
    parser.add_argument("--check", action="store_true", help="report readiness only; never write either corpus")
    parser.add_argument("--poll-seconds", type=float, default=15)
    parser.add_argument("--timeout", type=float, default=7200)
    args = parser.parse_args()
    args.root = args.root.expanduser().resolve()
    args.catalog = (args.catalog or args.root / "catalog/catalog.json").expanduser().resolve()
    if (args.expected_count is not None and args.expected_count <= 0) or args.poll_seconds <= 0 or args.timeout <= 0:
        raise ValueError("Count, polling interval, and timeout must be positive")
    catalog, videos = catalog_records(args)
    if args.expected_count is None:
        args.expected_count = len(videos)
    author_name = safe_author_name(catalog.get("creator", {}).get("nickname"))
    args.markdown_output = (args.markdown_output or args.root / f"{author_name}-{args.expected_count}条机器逐字稿合集.md").expanduser().resolve()
    args.jsonl_output = (args.jsonl_output or args.root / f"catalog/transcripts-{args.expected_count}.jsonl").expanduser().resolve()
    deadline, previous = time.monotonic() + args.timeout, None
    while True:
        status, manifest = readiness(args, videos)
        if status["ready"] != previous:
            print(json.dumps({"event": "corpus_readiness", "ready": status["ready"],
                              "expected": status["expected"], "all_ready": status["all_ready"]}), flush=True)
            previous = status["ready"]
        if args.check:
            if status["all_ready"]:
                collect_existing_text(args, catalog, videos, manifest)
            return 0 if status["all_ready"] else 2
        if status["all_ready"]:
            export(args, catalog, videos, manifest)
            return 0
        if not args.wait or time.monotonic() >= deadline:
            print(json.dumps({"event": "corpus_not_exported", "missing_ids": status["missing_ids"]}), flush=True)
            return 2
        time.sleep(min(args.poll_seconds, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError) as error:
        print(json.dumps({"event": "corpus_export_failed", "reason": str(error)}, ensure_ascii=False), flush=True)
        raise SystemExit(3)
