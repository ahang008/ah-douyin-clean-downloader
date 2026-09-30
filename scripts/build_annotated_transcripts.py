#!/usr/bin/env python3
"""Build source-preserving reading copies with caption, hashtags and cover OCR.

This is a local projection of an official creator catalog, cover metadata and
already verified machine Markdown. It never downloads media, runs OCR, calls a
model, or changes ASR outputs/evidence. Existing hand-edited copies are never
overwritten. A creator library with no machine transcripts is a valid no-op.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any


SCHEMA_VERSION = 1
METADATA_NAME = "作品标题标签封面.jsonl"
STATE_NAME = "_generated-state.json"
REPORT_NAME = "annotation-report.json"
START_MARKER = "<!-- ah-douyin-annotation:start v=1 -->"
END_MARKER = "<!-- ah-douyin-annotation:end -->"
VIDEO_ID = re.compile(r"^[0-9]{16,22}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SUCCESS_COVER = frozenset({"ok", "ocr_low_confidence", "ocr_empty"})


def load_paths_helper():
    path = Path(__file__).resolve().with_name("artifact_paths.py")
    spec = importlib.util.spec_from_file_location("annotation_artifact_paths", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PATHS = load_paths_helper()


def load_asr_helper():
    path = Path(__file__).resolve().with_name("transcribe_local.py")
    spec = importlib.util.spec_from_file_location("annotation_asr_quality", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ASR = load_asr_helper()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def regular_file(path: Path, boundary: Path | None = None) -> Path:
    path = Path(os.path.abspath(path.expanduser()))
    canonical = path.resolve()
    if boundary is not None:
        boundary = boundary.resolve()
        if boundary not in canonical.parents:
            raise ValueError("Input path leaves its creator library: " + str(path))
    # macOS /var is itself a system symlink. Reject links *inside* the selected
    # library while accepting that normal OS alias above the library boundary.
    node = path
    while boundary is None or node.resolve() != boundary:
        if node.is_symlink():
            raise ValueError("Symbolic links are not allowed in annotation paths: " + str(node))
        if node == node.parent or (boundary is None and node == path):
            break
        node = node.parent
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Expected a regular file: " + str(path))
    return canonical


def safe_output_path(root: Path, video_id: str, name: str) -> Path:
    if not VIDEO_ID.fullmatch(video_id) or Path(name).name != name or not name.endswith(".md"):
        raise ValueError("Unsafe annotated transcript identity or filename")
    base = root / "annotated-transcripts"
    directory = base / video_id
    for candidate in (base, directory, directory / name):
        if candidate.is_symlink():
            raise ValueError("Annotated output path is a symbolic link: " + str(candidate))
    return directory / name


def catalog_rows(path: Path) -> tuple[list[dict], str, bool]:
    raw = regular_file(path).read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("videos"), list):
        raise ValueError("Creator catalog needs a videos array")
    rows, seen = [], set()
    for item in value["videos"]:
        if not isinstance(item, dict):
            raise ValueError("Creator catalog video must be an object")
        video_id = str(item.get("video_id") or "")
        if not VIDEO_ID.fullmatch(video_id) or video_id in seen:
            raise ValueError("Creator catalog IDs must be unique and valid")
        seen.add(video_id)
        rows.append(item)
    if not rows:
        raise ValueError("Creator catalog has no videos")
    return rows, digest(raw), value.get("catalog_complete") is True


def metadata_rows(path: Path, catalog: dict[str, dict], catalog_sha: str) -> dict[str, dict]:
    records = {}
    for number, line in enumerate(regular_file(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid cover metadata JSONL at line {number}") from error
        if not isinstance(row, dict):
            raise ValueError(f"Cover metadata line {number} must be an object")
        video_id = str(row.get("video_id") or "")
        if not VIDEO_ID.fullmatch(video_id) or video_id in records or video_id not in catalog:
            raise ValueError(f"Unknown or duplicate cover metadata ID at line {number}")
        if row.get("catalog_sha256") != catalog_sha:
            raise ValueError(f"Cover metadata catalog SHA differs at line {number}")
        official = "https://www.douyin.com/video/" + video_id
        if row.get("official_url") != official:
            raise ValueError(f"Cover metadata official URL differs for {video_id}")
        if row.get("published_caption") != (catalog[video_id].get("title") or ""):
            raise ValueError(f"Cover metadata published caption differs for {video_id}")
        if row.get("title_candidate") is not None and not isinstance(row.get("title_candidate"), str):
            raise ValueError(f"Cover metadata title candidate is invalid for {video_id}")
        tags = row.get("hashtags")
        if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.startswith("#") for tag in tags):
            raise ValueError(f"Cover metadata hashtags are invalid for {video_id}")
        if not isinstance(row.get("cover_status"), str) or not row["cover_status"]:
            raise ValueError(f"Cover metadata status is missing for {video_id}")
        if not isinstance(row.get("cover_text_raw"), str):
            raise ValueError(f"Cover metadata OCR text is invalid for {video_id}")
        records[video_id] = row
    return records


def read_machine_markdown(root: Path, video_id: str) -> tuple[Path, bytes] | None:
    directory = root / "local-transcripts" / video_id
    evidence_path = directory / PATHS.OUTPUT_NAMES["evidence"]
    if not evidence_path.exists():
        return None
    evidence = json.loads(regular_file(evidence_path, root).read_text(encoding="utf-8"))
    if not isinstance(evidence, dict) or evidence.get("video_id") != video_id:
        raise ValueError("Machine evidence identity differs for " + video_id)
    if evidence.get("status") != "machine_draft_saved":
        return None
    if ASR.machine_quality_flags({"text": evidence.get("raw_text"),
                                  "segments": evidence.get("segments")},
                                 float((evidence.get("source_audio") or {}).get("duration_seconds", 0))):
        return None
    paths = PATHS.resolve_artifact_paths(directory, evidence)
    markdown = paths["markdown"]
    if not markdown.exists():
        return None
    raw = regular_file(markdown, root).read_bytes()
    expected = (evidence.get("outputs") or {}).get("markdown", {}).get("sha256")
    if not isinstance(expected, str) or not SHA256.fullmatch(expected) or digest(raw) != expected:
        raise ValueError("Machine Markdown SHA differs from ASR evidence for " + video_id)
    try:
        original = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Machine Markdown is not UTF-8 for " + video_id) from error
    _, marker, body = original.partition("## 机器识别原文\n\n")
    machine_text = evidence.get("raw_text")
    compact = lambda text: re.sub(r"\s+", "", text)
    if not marker or not isinstance(machine_text, str) or compact(body) != compact(machine_text):
        raise ValueError("Machine Markdown body differs from ASR evidence for " + video_id)
    if evidence.get("source_url") != "https://www.douyin.com/video/" + video_id:
        raise ValueError("Machine Markdown source identity differs for " + video_id)
    return markdown, raw


def inline(value: str | None, limit: int) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    shortened = len(text) > limit
    text = text[:limit]
    text = html.escape(text, quote=False)
    text = re.sub(r"([\\`*_{}\[\]|])", r"\\\1", text)
    return text + ("…（已截断，完整字段见元数据 JSONL）" if shortened else "")


def cover_inline(value: str, limit: int = 900) -> str:
    """Keep the selected cover's line breaks visible without allowing Markdown markup."""
    lines = [inline(line, 180) for line in value.splitlines() if line.strip()]
    result = "<br>".join(lines)
    if len(result) > limit:
        return result[:limit] + "…（已截断，完整字段见元数据 JSONL）"
    return result


def image_link(root: Path, video_id: str, row: dict) -> str:
    value = row.get("cover_image_path")
    if value is None or value == "":
        return "未取得"
    if not isinstance(value, str):
        raise ValueError("Cover image path must be a string")
    path = Path(value).expanduser()
    expected = root / "covers" / f"{video_id}.jpg"
    if path != expected:
        raise ValueError("Cover image path differs from its video ID cache: " + video_id)
    if not path.exists():
        return "原图缓存缺失"
    regular_file(path, root)
    return f"[查看原图](<{path}>)"


def cover_label(status: str, raw_text: str) -> tuple[str, str]:
    if status == "ocr_low_confidence":
        return "低置信度，待对照原图", raw_text or "未识别到文字"
    if status == "ok":
        return "机器识别，未人工核对", raw_text or "未识别到文字"
    if status == "ocr_empty":
        return "未识别到文字，不能据此断定封面无字", "未识别到文字"
    return "封面未取得或识别未完成（" + inline(status, 80) + "）", "未取得"


def annotated_bytes(root: Path, video_id: str, row: dict, original: bytes) -> bytes:
    candidate = inline(row.get("title_candidate"), 360) or "未单独提取（发布文案可能从井号标签开始）"
    hashtags = " ".join(row["hashtags"])
    hashtags = inline(hashtags, 600) or "无"
    cover_status, cover_text = cover_label(row["cover_status"], row["cover_text_raw"])
    cover_text = cover_inline(cover_text) or "未取得"
    cover = image_link(root, video_id, row)
    block = (
        f"{START_MARKER}\n"
        "# 作品标题、井号标签与封面文字\n\n"
        f"- 官方作品：[打开抖音视频]({row['official_url']})\n"
        f"- 发布文案开头（标题候选）：{candidate}\n"
        f"- 井号标签：{hashtags}\n"
        f"- 官方选定封面：{cover}\n"
        f"- 封面识字状态：{cover_status}\n"
        f"- 封面文字（本地原始识字，未人工核对）：{cover_text}\n"
        f"{END_MARKER}\n\n"
    )
    return block.encode("utf-8") + original


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "videos": {}}
    value = json.loads(regular_file(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION or not isinstance(value.get("videos"), dict):
        raise ValueError("Annotated transcript state is invalid")
    return value


def preflight_target(root: Path, video_id: str, target: Path, expected: bytes, previous: dict | None) -> tuple[str, Path | None]:
    if previous is not None and not isinstance(previous, dict):
        raise ValueError("Annotated state entry is invalid for " + video_id)
    previous = previous or {}
    old = None
    if previous:
        old_value = previous.get("path")
        old_hash = previous.get("generated_sha256")
        if not isinstance(old_value, str) or not isinstance(old_hash, str) or not SHA256.fullmatch(old_hash):
            raise ValueError("Annotated state entry is invalid for " + video_id)
        old = Path(old_value)
        if old.parent != root / "annotated-transcripts" / video_id or old.suffix != ".md":
            raise ValueError("Annotated state path is unsafe for " + video_id)
        if old.is_symlink():
            raise ValueError("Annotated state path is a symbolic link for " + video_id)
        if old.exists() and digest(regular_file(old, root).read_bytes()) != old_hash:
            raise ValueError("Hand-edited annotated copy preserved for " + video_id)
    if target.exists():
        actual = regular_file(target, root).read_bytes()
        if actual == expected:
            return "unchanged", old if old != target else None
        if previous and old == target and digest(actual) == previous["generated_sha256"]:
            return "updated", None
        raise ValueError("Hand-edited or unknown annotated copy preserved for " + video_id)
    return "created" if not previous else "renamed", old if old != target else None


def build(root: Path, catalog_path: Path, metadata_path: Path) -> dict:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Creator library root does not exist")
    catalog_path = regular_file(catalog_path, root)
    metadata_path = regular_file(metadata_path, root)
    works, catalog_sha, complete = catalog_rows(catalog_path)
    catalog = {row["video_id"]: row for row in works}
    metadata = metadata_rows(metadata_path, catalog, catalog_sha)
    output_root = root / "annotated-transcripts"
    if output_root.is_symlink():
        raise ValueError("Annotated transcript directory is a symbolic link")
    state_path = output_root / STATE_NAME
    state = load_state(state_path)
    plans = []
    counts = Counter({key: 0 for key in (
        "created", "updated", "unchanged", "renamed", "no_verified_transcript",
        "available_verified_transcripts", "metadata_missing", "metadata_not_attempted", "annotated_copies",
    )})
    statuses = Counter()
    for row in works:
        video_id = row["video_id"]
        machine = read_machine_markdown(root, video_id)
        if machine is None:
            counts["no_verified_transcript"] += 1
            continue
        counts["available_verified_transcripts"] += 1
        cover = metadata.get(video_id)
        if cover is None:
            counts["metadata_missing"] += 1
            continue
        if cover["cover_status"] == "not_attempted":
            counts["metadata_not_attempted"] += 1
            continue
        source, original = machine
        target = safe_output_path(root, video_id, source.name)
        output = annotated_bytes(root, video_id, cover, original)
        operation, old = preflight_target(root, video_id, target, output, state["videos"].get(video_id))
        plans.append((video_id, target, output, operation, old, digest(original)))
        statuses[cover["cover_status"]] += 1
    # Preflight the entire batch before writing any file. Mutations after this
    # point are limited to generated copies and their own state/report.
    for video_id, target, output, operation, old, source_sha in plans:
        if operation != "unchanged":
            atomic_bytes(target, output)
        if old is not None and old.exists():
            old.unlink()
        state["videos"][video_id] = {
            "path": str(target), "generated_sha256": digest(output),
            "source_markdown_sha256": source_sha, "catalog_sha256": catalog_sha,
        }
        atomic_bytes(state_path, json_bytes(state))
        counts[operation] += 1
    counts["annotated_copies"] = len(plans)
    report = {
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": str(root), "catalog_path": str(catalog_path), "catalog_sha256": catalog_sha,
        "catalog_complete": complete, "catalog_video_count": len(works),
        "cover_metadata_path": str(metadata_path), "cover_metadata_rows": len(metadata),
        "output_dir": str(output_root), "counts": dict(sorted(counts.items())),
        "cover_status_counts": dict(sorted(statuses.items())),
        "raw_machine_transcripts_modified": False,
        "ocr_review_status": "raw_machine_output_not_human_verified",
        "all_available_transcripts_annotated": counts["metadata_missing"] == 0 and counts["metadata_not_attempted"] == 0,
    }
    atomic_bytes(output_root / REPORT_NAME, json_bytes(report))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="existing creator library")
    parser.add_argument("--catalog", type=Path, help="official creator catalog; default in ROOT/catalog")
    parser.add_argument("--cover-metadata", type=Path, help=f"default ROOT/catalog/{METADATA_NAME}")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    browser_catalog = root / "catalog/browser-catalog.json"
    catalog = args.catalog or (browser_catalog if browser_catalog.exists() else root / "catalog/catalog.json")
    metadata = args.cover_metadata or root / "catalog" / METADATA_NAME
    try:
        report = build(root, catalog, metadata)
    except (OSError, ValueError, json.JSONDecodeError, UnicodeError) as error:
        print(json.dumps({"event": "annotation_failed", "reason": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
