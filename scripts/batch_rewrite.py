#!/usr/bin/env python3
"""Prepare, render, and check a source-preserving offline rewrite batch.

This helper does not write the scripts, judge lived experience, or hear audio.
It only reads an already complete creator library and validates model-provided
JSONL drafts. There are no browser, network, LLM, download, or ASR calls here.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import difflib
from fractions import Fraction
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from types import SimpleNamespace


def load_helper(filename, name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXPORT = load_helper("export_transcripts.py", "rewrite_corpus_export")
METRICS = load_helper("work_metrics.py", "rewrite_work_metrics")
SCHEMA_VERSION = 1
SCRIPT_NAME = "01-洗稿口播稿.md"
INDEX_NAMES = ("00-按点赞排序索引.md", "00-综合参考排序.md", "00-排序说明.md", "00-开头核对清单.md")
SCORE_WEIGHTS = {"digg_count": Fraction(4, 10), "share_count": Fraction(3, 10),
                 "collect_count": Fraction(2, 10), "comment_count": Fraction(1, 10)}
DRAFT_FIELDS = {"rank", "video_id", "opening_segment_count", "title", "body", "angle", "new_example",
                "shooting", "first_comment", "checks", "sources"}
REVIEW_SCOPE = {
    "semantic_review_certified": False, "audio_review_certified": False,
    "helper_llm_calls": 0, "helper_computer_use_calls": 0,
    "scope": "deterministic_helper_only",
    "execution_scope": "Only this offline mechanical helper; model writing usage is not counted here.",
}


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def jsonl_bytes(rows):
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows).encode("utf-8")


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object: " + str(path))
    return value


def read_jsonl(path):
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid JSONL at {path}:{number}") from error
        if not isinstance(row, dict):
            raise ValueError(f"Expected a JSONL object at {path}:{number}")
        rows.append(row)
    return rows


def reject_symlinks(path):
    """Check existing parents too, including parents of a not-yet-created file."""
    path = Path(os.path.abspath(path.expanduser()))
    for candidate in (*reversed(path.parents), path):
        if candidate.is_symlink():
            raise ValueError("Symbolic links are not permitted in batch paths: " + str(candidate))
    return path


def resolved_path(path):
    return reject_symlinks(Path(path)).resolve()


def inside(path, root):
    return path == root or root in path.parents


def regular_file(path, root=None):
    path = resolved_path(path)
    if root is not None and (path == root or root not in path.parents):
        raise ValueError("Source/output path leaves its selected directory: " + str(path))
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Expected a regular file: " + str(path))
    return path


def separated_paths(root, work_dir, output_dir):
    paths = [resolved_path(path) for path in (root, work_dir, output_dir)]
    for index, first in enumerate(paths):
        for second in paths[index + 1:]:
            if inside(first, second) or inside(second, first):
                raise ValueError("LIB, WORK, and OUT must be separate directories without mutual containment")
    return paths


def capture_parameters(args):
    root, work_dir, output_dir = separated_paths(args.root, args.work_dir, args.output_dir)
    if not root.is_dir():
        raise ValueError("The existing creator library must be a directory")
    catalog = args.catalog
    if catalog is None:
        browser_catalog = root / "catalog/browser-catalog.json"
        catalog = browser_catalog if browser_catalog.exists() or browser_catalog.is_symlink() else root / "catalog/catalog.json"
    catalog = regular_file(catalog, root)
    if type(args.shards) is not int or args.shards < 1:
        raise ValueError("shards must be a positive integer")
    if not isinstance(args.persona, str) or not args.persona.strip() or any(ord(char) < 32 for char in args.persona):
        raise ValueError("persona must be a nonempty single-line name")
    return {"root": str(root), "catalog": str(catalog), "work_dir": str(work_dir),
            "output_dir": str(output_dir), "shards": args.shards, "persona": args.persona}


def descending_value(value):
    return (value is None, -(value if value is not None else 0))


def like_sort_key(work):
    keys = tuple(descending_value(METRICS.metric_value(work, key)) for key in
                 ("digg_count", "share_count", "collect_count", "comment_count"))
    created = work.get("create_time")
    created = created if type(created) is int and created >= 0 else None
    return (*keys, descending_value(created), work["video_id"])


def reference_scores(works):
    """Use average tie ranks only within the four-counter complete subset."""
    complete = [row for row in works if all(METRICS.metric_value(row, key) is not None for key in SCORE_WEIGHTS)]
    scores = {row["video_id"]: {"reference_score": None, "reference_rank": None,
                              "reference_percentiles": None} for row in works}
    if not complete:
        return scores
    percentiles = {row["video_id"]: {} for row in complete}
    count = len(complete)
    for key in SCORE_WEIGHTS:
        values = sorted(METRICS.metric_value(row, key) for row in complete)
        positions = {}
        for index, value in enumerate(values):
            positions.setdefault(value, []).append(index)
        for row in complete:
            indexes = positions[METRICS.metric_value(row, key)]
            percentile = (Fraction(sum(indexes), len(indexes) * (count - 1))
                          if count > 1 else Fraction(1, 2))
            percentiles[row["video_id"]][key] = percentile
    for row in complete:
        parts = percentiles[row["video_id"]]
        total = sum(parts[key] * weight for key, weight in SCORE_WEIGHTS.items())
        scores[row["video_id"]] = {"reference_score": round(float(100 * total), 6),
                                   "reference_rank": None,
                                   "reference_percentiles": {key: round(float(value), 6) for key, value in parts.items()}}
    ordered = sorted(complete, key=lambda row: (-scores[row["video_id"]]["reference_score"], like_sort_key(row)))
    for rank, row in enumerate(ordered, 1):
        scores[row["video_id"]]["reference_rank"] = rank
    return scores


def compact(text):
    return re.sub(r"\s+", "", text)


def paragraph_mapping(body, segments):
    """Record original MD paragraph/SRT cue correspondence, without rewriting."""
    spans, cursor = [], 0
    for number, segment in enumerate(segments, 1):
        end = cursor + len(compact(segment["text"]))
        spans.append((cursor, end, number))
        cursor = end
    rows, cursor = [], 0
    for text in re.split(r"\n\s*\n", body.strip()):
        length = len(compact(text))
        if not length:
            continue
        end = cursor + length
        rows.append({"paragraph_number": len(rows) + 1, "text": text,
                     "segment_numbers": [number for start, stop, number in spans if start < end and stop > cursor],
                     "compact_character_start": cursor, "compact_character_end": end})
        cursor = end
    if cursor != sum(len(compact(segment["text"])) for segment in segments):
        raise ValueError("Original paragraph/segment coverage mismatch")
    return rows


def snapshot(parameters):
    root = Path(parameters["root"])
    work_dir = Path(parameters["work_dir"])
    output_dir = Path(parameters["output_dir"])
    separated_paths(root, work_dir, output_dir)
    args = SimpleNamespace(root=root, catalog=regular_file(Path(parameters["catalog"]), root), expected_count=None)
    for relative in ("media/download-manifest.json", "local-transcripts/_batch-state.json"):
        regular_file(root / relative, root)
    # Validate the original spellings before the exporter resolves pointers.
    raw_catalog = read_json(args.catalog)
    for page in raw_catalog.get("page_evidence", []):
        if isinstance(page, dict) and isinstance(page.get("file"), str):
            path = Path(page["file"]).expanduser()
            regular_file(path if path.is_absolute() else args.catalog.parent / path, root)
    catalog, videos = EXPORT.catalog_records(args)
    for path in args.evidence_paths:
        regular_file(path, root)
    raw_manifest = read_json(root / "media/download-manifest.json").get("videos", {})
    for video in videos:
        path = raw_manifest.get(video["video_id"], {}).get("path")
        if isinstance(path, str) and path:
            regular_file(Path(path).expanduser(), root)
    readiness, manifest = EXPORT.readiness(args, videos)
    if not readiness["all_ready"]:
        raise ValueError("The complete library is not ready; missing IDs: " + ", ".join(readiness["missing_ids"]))
    records, bodies = EXPORT.collect_existing_text(args, catalog, videos, manifest)
    by_id = {row["video_id"]: (row, body) for row, body in zip(records, bodies)}
    videos = sorted(videos, key=like_sort_key)
    scores = reference_scores(videos)
    source_files = {}

    def add_source(path, role, known_sha=None):
        path = regular_file(path, root)
        entry = source_files.setdefault(str(path), {"path": str(path), "bytes": path.stat().st_size,
                                                   "sha256": known_sha or sha256_file(path), "roles": []})
        if known_sha is not None and entry["sha256"] != known_sha:
            raise ValueError("Conflicting source hashes: " + str(path))
        entry["roles"].append(role)

    add_source(args.catalog, "catalog", args.catalog_sha256)
    for number, path in enumerate(args.evidence_paths, 1):
        add_source(path, f"catalog_page:{number}")
    add_source(root / "media/download-manifest.json", "download_manifest")
    add_source(root / "local-transcripts/_batch-state.json", "transcript_state")
    items = []
    width = max(3, len(str(len(videos))))
    for rank, work in enumerate(videos, 1):
        record, body = by_id[work["video_id"]]
        for field, role, digest in (("original_video_path", "media", record["source_media_sha256"]),
                                    ("transcript_path", "markdown", record["source_markdown_sha256"]),
                                    ("srt_path", "srt", record["source_srt_sha256"]),
                                    ("asr_evidence_path", "asr_evidence", None)):
            add_source(Path(record[field]), role + ":" + work["video_id"], digest)
        evidence = read_json(Path(record["asr_evidence_path"]))
        segments = evidence["segments"]
        if any(not isinstance(segment.get("text"), str) for segment in segments):
            raise ValueError("All source segment texts must be strings")
        metrics, availability = METRICS.normalized_statistics(work.get("statistics"), work.get("statistics_availability"))
        title = record["title"]
        folder = f"{rank:0{width}d}_" + METRICS.metric_filename_stem({**work, "title": title})
        item = {"rank": rank, "video_id": work["video_id"], "title": title, "source_author": record["author"],
                "official_url": record["official_url"], "statistics": metrics,
                "statistics_availability": availability,
                "statistics_captured_at": METRICS.validated_capture_time(work.get("statistics_captured_at")),
                "create_time": work.get("create_time"), **scores[work["video_id"]],
                "folder": folder, "output_path": str(output_dir / folder / SCRIPT_NAME),
                "original_video_path": record["original_video_path"], "transcript_path": record["transcript_path"],
                "srt_path": record["srt_path"], "asr_evidence_path": record["asr_evidence_path"],
                "source_catalog_sha256": args.catalog_sha256, "source_media_sha256": record["source_media_sha256"],
                "source_markdown_sha256": record["source_markdown_sha256"], "source_srt_sha256": record["source_srt_sha256"],
                "source_evidence_sha256": source_files[record["asr_evidence_path"]]["sha256"],
                "raw_text": record["machine_text"], "segments": segments,
                "source_paragraphs": paragraph_mapping(body, segments),
                "paragraph_correspondence_scope": "Whitespace-normalized original MD characters to consecutive SRT cue numbers",
                "transcript_status": "machine_output_not_audio_proofread", "asr_warnings": record["asr_warnings"]}
        items.append(item)
    for entry in source_files.values():
        entry["roles"].sort()
    if sha256_file(args.catalog) != args.catalog_sha256:
        raise ValueError("The source catalog changed during preparation")
    base = {"schema_version": SCHEMA_VERSION, "parameters": parameters, "expected_count": len(items),
            "expected_ids": [row["video_id"] for row in items], "source_catalog_sha256": args.catalog_sha256,
            "source_files": sorted(source_files.values(), key=lambda row: row["path"]),
            "metrics_summary": METRICS.metrics_summary(videos),
            "reference_score_complete_subset_count": sum(row["reference_score"] is not None for row in items),
            "shard_count": min(parameters["shards"], len(items)),
            "writing_guidance": "Read references/batch-rewrite.md once per writer; each writer reads only its assigned inputs shard.",
            "media_hash_validation_scope": "Every original media file was rehashed against ASR evidence and the verified manifest.",
            **REVIEW_SCOPE}
    return base, items


def writing_input(item, persona, work_dir):
    opening_segments = []
    for number, segment in enumerate(item["segments"], 1):
        if float(segment["start"]) >= 30:
            break
        opening_segments.append({"segment_number": number, "start": segment["start"],
                                 "end": segment["end"], "text": segment["text"]})
    return {key: item[key] for key in ("rank", "video_id", "title", "raw_text", "transcript_path",
                                      "srt_path", "asr_evidence_path", "asr_warnings")} | {
        "persona": persona, "opening_window_seconds": 30, "opening_segments": opening_segments,
        "full_segments_path": str(work_dir / "work-items.jsonl")}


def prepared_plan(base, items):
    work_dir = Path(base["parameters"]["work_dir"])
    plan = {work_dir / "work-items.jsonl": jsonl_bytes(items)}
    count = base["shard_count"]
    size, remainder = divmod(len(items), count)
    cursor = 0
    for number in range(1, count + 1):
        take = size + (number <= remainder)
        rows = items[cursor:cursor + take]
        cursor += take
        plan[work_dir / f"inputs-{number}.jsonl"] = jsonl_bytes(
            [writing_input(item, base["parameters"]["persona"], work_dir) for item in rows])
    return plan


def preflight(plan, roots, previous=None):
    previous = previous or {}
    for name, entry in previous.items():
        path = resolved_path(Path(name))
        if not any(root in path.parents for root in roots):
            raise ValueError("Recorded output is outside WORK/OUT: " + name)
        regular_file(path)
        if path.stat().st_size != entry["bytes"] or sha256_file(path) != entry["sha256"]:
            raise ValueError("Previously generated file was modified; refusing to overwrite: " + name)
    for path, content in plan.items():
        checked = resolved_path(path)
        if checked != path or not any(root in checked.parents for root in roots):
            raise ValueError("Output path leaves WORK/OUT: " + str(path))
        if path.exists():
            regular_file(path)
            if str(path) not in previous and path.read_bytes() != content:
                raise ValueError("Unknown existing output cannot be overwritten: " + str(path))


def atomic_write_all(plan, roots, previous=None):
    """Precheck the entire batch, stage every file, then atomically replace each."""
    preflight(plan, roots, previous)
    staged = []
    try:
        for path, content in plan.items():
            resolved_path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            resolved_path(path.parent)
            if path.exists() and path.read_bytes() == content:
                continue
            descriptor, filename = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
            temporary = Path(filename)
            staged.append((temporary, path))
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        preflight(plan, roots, previous)
        for temporary, path in staged:
            resolved_path(path)
            os.replace(temporary, path)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def verify_prepared(run, base, items, work_dir):
    if {key: run.get(key) for key in base} != base:
        raise ValueError("Prepared parameters, source snapshot, or helper plan drifted; preserve drafts and prepare in a new WORK/OUT (no new download/ASR needed)")
    expected = prepared_plan(base, items)
    ledger = run.get("prepared_files")
    wanted = [{"name": path.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
              for path, data in expected.items()]
    if ledger != wanted:
        raise ValueError("Prepared work/input manifest differs from the current helper plan; preserve drafts and prepare in a new WORK/OUT")
    for path, data in expected.items():
        regular_file(path, work_dir)
        if path.read_bytes() != data:
            raise ValueError("Prepared work/input file was modified: " + str(path))
    return run, items


def prepare(args):
    parameters = capture_parameters(args)
    base, items = snapshot(parameters)
    work_dir = Path(parameters["work_dir"])
    output_dir = Path(parameters["output_dir"])
    run_path = work_dir / "run.json"
    if run_path.exists():
        run = read_json(regular_file(run_path, work_dir))
        verify_prepared(run, base, items, work_dir)
        return {"event": "rewrite_prepare_reused", "expected_count": len(items), "shard_count": base["shard_count"],
                "work_dir": str(work_dir), "output_dir": str(output_dir), "read_only_reuse": True, **REVIEW_SCOPE}
    if (work_dir / "render-state.json").exists():
        raise ValueError("A render state without run.json cannot be replaced")
    plan = prepared_plan(base, items)
    run = {**base, "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "prepared_files": [{"name": path.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                              for path, data in plan.items()]}
    plan[run_path] = json_bytes(run)
    atomic_write_all(plan, (work_dir, output_dir))
    return {"event": "rewrite_prepared", "expected_count": len(items), "shard_count": base["shard_count"],
            "work_dir": str(work_dir), "output_dir": str(output_dir), "technical_complete": False, **REVIEW_SCOPE}


def load_prepared(work_dir):
    work_dir = resolved_path(work_dir)
    run = read_json(regular_file(work_dir / "run.json", work_dir))
    parameters = run.get("parameters")
    if not isinstance(parameters, dict) or parameters.get("work_dir") != str(work_dir):
        raise ValueError("run.json does not belong to the selected WORK")
    required = {"root", "catalog", "work_dir", "output_dir", "shards", "persona"}
    if set(parameters) != required:
        raise ValueError("Invalid prepared parameters")
    expected = capture_parameters(SimpleNamespace(**parameters))
    if expected != parameters:
        raise ValueError("Prepared paths/parameters are no longer canonical")
    base, items = snapshot(parameters)
    return verify_prepared(run, base, items, work_dir)


def nonempty_string(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(field + " must be a nonempty string")


def string_list(value, field, minimum=0, maximum=None):
    if not isinstance(value, list) or len(value) < minimum or (maximum is not None and len(value) > maximum):
        raise ValueError(field + " has an invalid list length")
    for entry in value:
        nonempty_string(entry, field)


def opening_for(item, count):
    if type(count) is not int or not 1 <= count <= len(item["segments"]):
        raise ValueError("opening_segment_count must select consecutive source segments")
    opening = "".join(segment["text"] for segment in item["segments"][:count]).strip()
    if not opening or not item["raw_text"].strip().startswith(opening):
        raise ValueError("The selected opening is not an exact raw_text prefix")
    return {"segment_count": count, "text": opening, "sha256": hashlib.sha256(opening.encode("utf-8")).hexdigest(),
            "end_seconds": item["segments"][count - 1]["end"]}


def validate_drafts(rows, items, partial, locks=None):
    expected = {item["video_id"]: item for item in items}
    seen, validated = set(), []
    locks = locks or {}
    for row in rows:
        if not DRAFT_FIELDS <= set(row) or set(row) - DRAFT_FIELDS - {"opening_checks"}:
            raise ValueError("Draft fields must match the documented JSONL schema")
        video_id = row.get("video_id")
        if not isinstance(video_id, str) or video_id not in expected:
            raise ValueError("Draft contains an unknown video ID")
        if video_id in seen:
            raise ValueError("Duplicate draft video ID: " + video_id)
        seen.add(video_id)
        item = expected[video_id]
        if type(row["rank"]) is not int or row["rank"] != item["rank"]:
            raise ValueError("Draft rank differs from the frozen like ranking: " + video_id)
        for field in ("title", "body", "angle", "new_example", "first_comment"):
            nonempty_string(row[field], field)
        if "\n" in row["title"] or "\r" in row["title"]:
            raise ValueError("Draft title must be a single line")
        string_list(row["shooting"], "shooting", 1, 3)
        string_list(row["checks"], "checks", 0, 3)
        string_list(row["sources"], "sources")
        if "opening_checks" in row:
            string_list(row["opening_checks"], "opening_checks", 0, 3)
        notes = check_notes(row)
        if len(notes) > 3:
            raise ValueError("checks and opening_checks must have at most 3 combined notes; merge them explicitly")
        if len(notes) != len(row["checks"]) + len(row.get("opening_checks", [])):
            raise ValueError("checks and opening_checks must not repeat a note")
        opening = opening_for(item, row["opening_segment_count"])
        if video_id in locks and locks[video_id] != opening:
            raise ValueError("The previously locked opening cannot change or be shortened: " + video_id)
        if compact(opening["text"]) in compact(row["body"]):
            raise ValueError("Draft body repeats the retained original opening: " + video_id)
        validated.append(dict(row))
    if not validated:
        raise ValueError("At least one draft is required")
    missing = [item["video_id"] for item in items if item["video_id"] not in seen]
    if missing and not partial:
        raise ValueError("Full render requires every planned ID; missing: " + ", ".join(missing))
    return sorted(validated, key=lambda row: row["rank"]), missing


def load_render_state(work_dir, run):
    path = work_dir / "render-state.json"
    if not path.exists():
        return None
    state = read_json(regular_file(path, work_dir))
    if (state.get("schema_version") != SCHEMA_VERSION
            or state.get("run_sha256") != sha256_file(work_dir / "run.json")
            or state.get("expected_ids") != run["expected_ids"]
            or not isinstance(state.get("artifacts"), dict) or not isinstance(state.get("opening_locks"), dict)
            or type(state.get("partial")) is not bool):
        raise ValueError("render-state.json differs from the frozen run")
    roots = (work_dir, Path(run["parameters"]["output_dir"]))
    preflight({}, roots, state["artifacts"])
    if path.read_bytes() != json_bytes(state):
        raise ValueError("render-state.json was modified outside this helper")
    return state


def md_text(value):
    return str(value).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def link(label, path):
    target = str(path).replace("<", "%3C").replace(">", "%3E").replace("\n", "%0A").replace("\r", "%0D")
    return f"[{md_text(label)}](<{target}>)"


def metrics_line(item):
    return " · ".join(label + " " + METRICS.metric_text(item, key) for label, key in
                      (("赞", "digg_count"), ("评", "comment_count"), ("藏", "collect_count"), ("转", "share_count")))


def seconds_text(value):
    return f"{float(value):.3f}".rstrip("0").rstrip(".")


def check_notes(draft):
    notes = []
    for entry in [*draft.get("opening_checks", []), *draft["checks"]]:
        if entry not in notes:
            notes.append(entry)
    return notes


def article(item, draft, opening, persona):
    lines = [f"# {item['rank']:03d} · {draft['title']}\n\n",
             f"署名：{md_text(persona)} · 视频 ID：{item['video_id']} · 点赞排序：{item['rank']}\n\n",
             "公开指标快照：" + metrics_line(item) + " · 采集时间：" + str(item["statistics_captured_at"] or "未获取") + "\n\n",
             "保留开头：原机器稿前 " + str(opening["segment_count"]) + " 个连续识别段，至 " +
             seconds_text(opening["end_seconds"]) + " 秒。逐字保留不等于已核对准确；仍是未人工听音的机器稿。\n\n",
             "## 口播正文\n\n", opening["text"], "\n\n", draft["body"], "\n\n",
             "## 拍摄提示\n\n", *["- " + note + "\n" for note in draft["shooting"]],
             "\n## 首评\n\n", draft["first_comment"], "\n\n",
             "## 待核对\n\n"]
    notes = check_notes(draft)
    lines += ["- " + note + "\n" for note in notes] if notes else ["未单列具体项，仍需人工核对开头、正文和原音频。\n"]
    lines += ["\n## 改写记录与依据\n\n", "- 表达角度：" + draft["angle"] + "\n",
              "- 新例子：" + draft["new_example"] + "\n"]
    lines += ["- " + source + "\n" for source in draft["sources"]] if draft["sources"] else ["- 未添加外部依据；以对应原稿与下列来源为对照。\n"]
    lines += ["\n" + link("对应原机器稿", item["transcript_path"]) + " · " + link("对应 SRT", item["srt_path"]) +
              " · " + link("识别证据", item["asr_evidence_path"]) + " · " + link("原片", item["original_video_path"]) +
              f" · [官方作品]({item['official_url']})\n"]
    return "".join(lines)


def quality_flags(items, drafts):
    by_id = {row["video_id"]: row for row in items}
    flags, paragraphs = [], []
    for draft in drafts:
        video_id = draft["video_id"]
        item = by_id[video_id]
        body, source = compact(draft["body"]), compact(item["raw_text"])
        match = difflib.SequenceMatcher(None, body, source, autojunk=False).find_longest_match()
        if match.size >= 28:
            flags.append({"type": "source_contiguous_overlap", "video_id": video_id, "length": match.size,
                          "sample": body[match.a:match.a + match.size], "review_only": True})
        opening = opening_for(item, draft["opening_segment_count"])
        ratio = (len(compact(opening["text"])) + len(body)) / max(1, len(source))
        if ratio < 0.45 or ratio > 2.2:
            flags.append({"type": "large_length_difference", "video_id": video_id,
                          "rewritten_to_source_ratio": round(ratio, 4), "review_only": True})
        for paragraph in re.split(r"\n\s*\n", draft["body"]):
            text = compact(paragraph)
            if len(text) >= 45:
                paragraphs.append((video_id, text))
    windows, reported = {}, set()
    for video_id, text in paragraphs:
        for index in range(len(text) - 44):
            token = text[index:index + 45]
            others = windows.setdefault(token, {})
            for other_id, other_text in list(others.items()):
                pair = tuple(sorted((video_id, other_id)))
                if video_id == other_id or pair in reported:
                    continue
                match = difflib.SequenceMatcher(None, text, other_text, autojunk=False).find_longest_match()
                reported.add(pair)
                flags.append({"type": "cross_draft_repeated_paragraph", "video_ids": list(pair),
                              "length": match.size, "sample": text[match.a:match.a + match.size], "review_only": True})
            others.setdefault(video_id, text)
    return flags


def status_intro(run, drafts, missing, partial):
    if partial:
        return (f"状态：部分交付，已渲染 {len(drafts)}/{run['expected_count']} 篇；本次不是完整合集。\n\n"
                + "未完成 ID：" + ("、".join(missing) if missing else "无缺失，但本次显式使用 partial 模式") + "\n\n")
    return f"状态：{run['expected_count']} 篇结构校验完整；语义、原创性、亲历和听音仍需人工复核。\n\n"


def output_indexes(run, items, drafts, locks, partial, missing):
    out = Path(run["parameters"]["output_dir"])
    done = {draft["video_id"]: draft for draft in drafts}
    intro = status_intro(run, drafts, missing, partial)
    by_likes = ["# 按点赞排序索引\n\n", intro,
                "未知指标显示“未获取”；真实 0 保留。原机器稿仍未统一听音。\n\n",
                "| 点赞排名 | 作品 | 赞 | 评 | 藏 | 转 | 参考分 | 交付 |\n",
                "| --- | --- | --- | --- | --- | --- | --- | --- |\n"]
    score_index = ["# 综合参考排序\n\n", intro,
                   "这是同批公开计数的自定义选稿参考分，不能代表真实流量、收益或平台推荐算法。缺任一计数不计分、不排名。\n\n",
                   "| 参考排名 | 参考分 | 点赞排名 | 作品 | 交付 |\n", "| --- | --- | --- | --- | --- |\n"]
    for item in items:
        rendered = item["video_id"] in done
        delivery = link("口播稿", item["output_path"]) if rendered else "待改写 · " + item["video_id"]
        score = str(item["reference_score"]) if item["reference_score"] is not None else "未计算"
        counters = [METRICS.metric_text(item, key) for key in ("digg_count", "comment_count", "collect_count", "share_count")]
        by_likes.append("| " + " | ".join([str(item["rank"]), md_text(item["title"]), *counters, score, delivery]) + " |\n")
    for item in sorted(items, key=lambda row: (row["reference_rank"] is None, row["reference_rank"] or 0, row["rank"])):
        delivery = link("口播稿", item["output_path"]) if item["video_id"] in done else "待改写 · " + item["video_id"]
        score_index.append("| " + " | ".join([str(item["reference_rank"] or "未排名"),
                          str(item["reference_score"]) if item["reference_score"] is not None else "未计算",
                          str(item["rank"]), md_text(item["title"]), delivery]) + " |\n")
    summary = run["metrics_summary"]
    explanation = ("# 排序说明\n\n" + intro +
                   "主排序按点赞降序；未知置末并与真实 0 区分。平分依次按分享、收藏、评论、发布时间降序，再按视频 ID 升序。未知的平分项仍置末。排名与指标采集时间均已冻结。\n\n"
                   "综合参考分仅使用同批四项计数均已获取的子集。每项按数值升序计算分位，相同值用平均秩：n>1 时，分位=(小于该值的作品数+(同值作品数-1)/2)/(n-1)；n=1 时取 0.5。\n\n"
                   "参考分=100×(点赞分位×40%+分享分位×30%+收藏分位×20%+评论分位×10%)。相同参考分按主排序稳定排列；缺任一项的分数和参考排名为空，不补 0、不调整权重。\n\n"
                   "该分数是自定义选稿参考，不是作品真实流量、播放表现、商业价值或平台推荐算法。公开指标只是采集时快照。\n\n"
                   f"四项齐全作品：{run['reference_score_complete_subset_count']}/{run['expected_count']}。\n\n"
                   "指标采集范围：" + str(summary["capture_range"]["earliest"] or "未获取") + " 至 " +
                   str(summary["capture_range"]["latest"] or "未获取") + "。\n\n"
                   "开头由写作步骤选择完整连续识别段；脚本只认证与原机器稿精确一致及续跑锁定，不认证语意完整、同音字准确或本人经历。源文件哈希均重新验证；没有 LLM、CUA、下载或 ASR 调用。\n")
    checklist = ["# 开头核对清单\n\n", intro,
                 "全部保留开头都来自未人工听音的机器稿。没有列出疑点不等于已经核对通过；逐篇比对原音频，确认同音字、专名、完整语意及亲历表达。\n\n"]
    for draft in drafts:
        item = items[draft["rank"] - 1]
        opening = locks[draft["video_id"]]
        checklist += [f"## {item['rank']:03d} · {draft['title']}\n\n",
                      "状态：待人工听音与语义核对。保留前 " + str(opening["segment_count"]) + " 段，至 " +
                      seconds_text(opening["end_seconds"]) + " 秒。\n\n", opening["text"], "\n\n"]
        if "opening_checks" not in draft:
            checklist.append("旧字段未区分开头/正文；以下完整列出 checks，尚未证明开头没有疑点。\n\n")
            notes = draft["checks"]
        else:
            checklist.append("开头专列核对项；为空仍表示待核对。\n\n")
            notes = draft["opening_checks"]
        checklist += ["- " + note + "\n" for note in notes] if notes else ["- 未单列具体疑点，仍需逐篇人工听音。\n"]
        checklist += ["\n" + link("口播稿", item["output_path"]) + " · " + link("原机器稿", item["transcript_path"]) +
                      " · " + link("SRT", item["srt_path"]) + "\n\n"]
    return {out / INDEX_NAMES[0]: "".join(by_likes).encode("utf-8"),
            out / INDEX_NAMES[1]: "".join(score_index).encode("utf-8"),
            out / INDEX_NAMES[2]: explanation.encode("utf-8"),
            out / INDEX_NAMES[3]: "".join(checklist).encode("utf-8")}


def render_plan(run, items, drafts, partial, missing, old_state=None, draft_sources=None):
    work_dir = Path(run["parameters"]["work_dir"])
    out = Path(run["parameters"]["output_dir"])
    locks = dict(old_state["opening_locks"]) if old_state else {}
    by_id = {item["video_id"]: item for item in items}
    plan, manifest, links = {}, [], []
    for draft in drafts:
        item = by_id[draft["video_id"]]
        opening = opening_for(item, draft["opening_segment_count"])
        locks[draft["video_id"]] = opening
        path = Path(item["output_path"])
        content = article(item, draft, opening, run["parameters"]["persona"]).encode("utf-8")
        plan[path] = content
        source_links = [item[key] for key in ("transcript_path", "srt_path", "asr_evidence_path", "original_video_path")]
        links += [{"referrer": str(path), "target": target, "kind": "source"} for target in source_links]
        manifest.append({"rank": item["rank"], "video_id": item["video_id"], "path": str(path),
                         "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                         "reference_score": item["reference_score"], "reference_rank": item["reference_rank"],
                         "opening": opening, "source_links": source_links,
                         "source_catalog_sha256": item["source_catalog_sha256"],
                         "source_markdown_sha256": item["source_markdown_sha256"],
                         "source_srt_sha256": item["source_srt_sha256"], "source_evidence_sha256": item["source_evidence_sha256"]})
    plan.update(output_indexes(run, items, drafts, locks, partial, missing))
    for name in (INDEX_NAMES[0], INDEX_NAMES[1], INDEX_NAMES[3]):
        links += [{"referrer": str(out / name), "target": row["path"], "kind": "generated"} for row in manifest]
    if not partial:
        collection = out / f"00-{run['expected_count']}篇口播稿合集.md"
        lines = [f"# {run['parameters']['persona']} · {run['expected_count']} 篇口播稿合集\n\n",
                 status_intro(run, drafts, missing, partial), "按冻结点赞排名汇总。保留的开头仍是未经人工听音的机器稿。\n\n"]
        for draft in drafts:
            item = by_id[draft["video_id"]]
            lines += ["---\n\n", link("本篇单文件", item["output_path"]) + "\n\n",
                      article(item, draft, locks[draft["video_id"]], run["parameters"]["persona"])]
            links.append({"referrer": str(collection), "target": item["output_path"], "kind": "generated"})
        plan[collection] = "".join(lines).encode("utf-8")
    flags = quality_flags(items, drafts)
    audit = {"schema_version": SCHEMA_VERSION, "expected_count": len(items), "rendered_count": len(drafts),
             "partial": partial, "technical_complete": not partial and not missing, "missing_ids": missing,
             "source_hashes_verified": True, "original_media_rehashed": True,
             "flags": flags, "flag_count": len(flags),
             "flag_scope": "Whitespace-normalized source overlap >=28 characters, cross-draft paragraph overlap >=45 characters, and large length differences are manual-review hints, never automatic violations. Retained openings are excluded from source-overlap scanning.",
             "draft_check_notes": [{"video_id": row["video_id"], "checks": row["checks"],
                                    "opening_checks": row.get("opening_checks", []),
                                    "opening_checks_separately_supplied": "opening_checks" in row} for row in drafts],
             **REVIEW_SCOPE}
    plan[work_dir / "rendered-manifest.jsonl"] = jsonl_bytes(manifest)
    plan[work_dir / "quality-audit.json"] = json_bytes(audit)
    artifacts = dict(old_state["artifacts"]) if old_state else {}
    for path, data in plan.items():
        artifacts[str(path)] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    state = {"schema_version": SCHEMA_VERSION, "run_sha256": sha256_file(work_dir / "run.json"),
             "expected_ids": run["expected_ids"], "rendered_ids": [row["video_id"] for row in drafts],
             "missing_ids": missing, "partial": partial, "technical_complete": not partial and not missing,
             "opening_locks": locks, "drafts": drafts, "artifacts": artifacts, "required_links": links,
             "draft_sources": draft_sources if draft_sources is not None else (old_state or {}).get("draft_sources", []),
             **REVIEW_SCOPE}
    plan[work_dir / "render-state.json"] = json_bytes(state)
    return plan, audit, state


def render(args):
    run, items = load_prepared(args.work_dir)
    work_dir = Path(run["parameters"]["work_dir"])
    out = Path(run["parameters"]["output_dir"])
    old = load_render_state(work_dir, run)
    if old:
        verify_rendered(run, items, old)
    if old and not old["partial"] and args.partial:
        raise ValueError("A complete render cannot be downgraded to partial; use a separate WORK/OUT")
    rows, draft_sources = [], []
    for path in args.drafts:
        path = regular_file(path)
        rows.extend(read_jsonl(path))
        draft_sources.append({"path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size})
    draft_sources.sort(key=lambda entry: entry["path"])
    incoming, _ = validate_drafts(rows, items, True, old["opening_locks"] if old else None)
    merged = {row["video_id"]: row for row in old["drafts"]} if old else {}
    merged.update({row["video_id"]: row for row in incoming})
    drafts, missing = validate_drafts(list(merged.values()), items, args.partial, old["opening_locks"] if old else None)
    if old:
        prior_sources = {entry["path"]: entry for entry in old.get("draft_sources", [])}
        prior_sources.update({entry["path"]: entry for entry in draft_sources})
        draft_sources = sorted(prior_sources.values(), key=lambda entry: entry["path"])
    plan, audit, state = render_plan(run, items, drafts, args.partial, missing, old, draft_sources)
    previous = dict(old["artifacts"]) if old else {}
    if old:
        state_path = work_dir / "render-state.json"
        previous[str(state_path)] = {"bytes": state_path.stat().st_size, "sha256": sha256_file(state_path)}
    atomic_write_all(plan, (work_dir, out), previous)
    return {"event": "rewrite_rendered", "expected_count": len(items), "rendered_count": len(drafts),
            "partial": args.partial, "technical_complete": state["technical_complete"], "missing_ids": missing,
            "flag_count": audit["flag_count"], "source_hashes_verified": True, "original_media_rehashed": True, **REVIEW_SCOPE}


def verify_rendered(run, items, state):
    work_dir = Path(run["parameters"]["work_dir"])
    by_id = {item["video_id"]: item for item in items}
    for video_id, opening in state["opening_locks"].items():
        if video_id not in by_id or not isinstance(opening, dict) or opening != opening_for(by_id[video_id], opening.get("segment_count")):
            raise ValueError("A saved opening lock differs from its original source")
    drafts, missing = validate_drafts(state.get("drafts", []), items, state["partial"], state["opening_locks"])
    if (state.get("rendered_ids") != [row["video_id"] for row in drafts]
            or state.get("missing_ids") != missing
            or state.get("technical_complete") != (not state["partial"] and not missing)
            or set(state["opening_locks"]) - set(run["expected_ids"])):
        raise ValueError("Render coverage/opening state is inconsistent")
    plan, audit, rebuilt = render_plan(run, items, drafts, state["partial"], missing, state)
    for path, data in plan.items():
        regular_file(path)
        if path.read_bytes() != data:
            raise ValueError("Rendered bytes/ranking/links differ from the frozen render state: " + str(path))
    if rebuilt["required_links"] != state["required_links"]:
        raise ValueError("Rendered link manifest is inconsistent")
    for entry in state["required_links"]:
        regular_file(Path(entry["target"]))
        regular_file(Path(entry["referrer"]))
    return audit


def check(args):
    run, items = load_prepared(args.work_dir)
    work_dir = Path(run["parameters"]["work_dir"])
    state = load_render_state(work_dir, run)
    if state is None:
        return {"event": "rewrite_checked", "expected_count": len(items), "rendered_count": 0,
                "technical_complete": False, "missing_ids": run["expected_ids"],
                "status": "prepared_only", "source_hashes_verified": True, "original_media_rehashed": True, **REVIEW_SCOPE}
    audit = verify_rendered(run, items, state)
    return {"event": "rewrite_checked", "expected_count": len(items), "rendered_count": len(state["drafts"]),
            "partial": state["partial"], "technical_complete": state["technical_complete"], "missing_ids": state["missing_ids"],
            "source_hashes_verified": True, "original_media_rehashed": True,
            "rendered_bytes_verified": True, "rankings_and_local_links_verified": True,
            "opening_locks_verified": True, "flag_count": audit["flag_count"], **REVIEW_SCOPE}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preparation = commands.add_parser("prepare", help="verify existing library and freeze balanced model inputs")
    preparation.add_argument("--root", type=Path, required=True)
    preparation.add_argument("--catalog", type=Path)
    preparation.add_argument("--work-dir", type=Path, required=True)
    preparation.add_argument("--output-dir", type=Path, required=True)
    preparation.add_argument("--shards", type=int, default=3)
    preparation.add_argument("--persona", default="阿杭")
    rendering = commands.add_parser("render", help="validate JSONL drafts before writing any outputs")
    rendering.add_argument("--work-dir", type=Path, required=True)
    rendering.add_argument("--drafts", type=Path, nargs="+", required=True)
    rendering.add_argument("--partial", action="store_true", help="explicit incomplete run; no full collection")
    checking = commands.add_parser("check", help="read-only source/byte/coverage/opening/link validation")
    checking.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    result = {"prepare": prepare, "render": render, "check": check}[args.command](args)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({"event": "batch_rewrite_failed", "reason": str(error)}, ensure_ascii=False), flush=True)
        raise SystemExit(3)
