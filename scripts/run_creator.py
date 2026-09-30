#!/usr/bin/env python3
"""One-command creator catalog, original video download, and local Whisper ASR.

This controller calls deterministic local programs. Catalog collection uses
a dedicated normal Edge session. No LLM service, model Computer Use, Getnote
account, or paid API is required.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

SCRIPTS = Path(__file__).resolve().parent
ACTIVE: list[subprocess.Popen] = []
SUCCESS_ASR = {"machine_draft_saved", "skipped_verified"}
COVER_TERMINAL = {"ok", "ocr_low_confidence", "ocr_empty"}
COVER_RECORDS_NAME = "作品标题标签封面.jsonl"


def local_helper(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PATHS = local_helper("artifact_paths.py", "creator_artifact_paths")
METRICS = local_helper("work_metrics.py", "creator_public_metrics")
NAMING = local_helper("name_artifacts.py", "creator_artifact_naming")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def load_json(path: Path) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except FileNotFoundError:
        return {}


def rows(catalog: dict) -> list[dict]:
    value = catalog.get("videos", [])
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def cover_records(root: Path, catalog_path: Path) -> dict[str, dict]:
    """Read only metadata made for the exact current catalog snapshot."""
    path = root / "catalog" / COVER_RECORDS_NAME
    if not path.is_file() or not catalog_path.is_file():
        return {}
    digest = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    records = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("catalog_sha256") != digest:
            continue
        video_id = str(row.get("video_id") or "")
        if re.fullmatch(r"\d{16,22}", video_id):
            records[video_id] = row
    return records


def cover_ready(root: Path, row: dict | None) -> bool:
    if not row or row.get("cover_status") not in COVER_TERMINAL:
        return False
    raw = row.get("cover_image_path")
    if not isinstance(raw, str) or not raw:
        return False
    image = Path(raw).expanduser().resolve()
    return (root / "covers").resolve() in image.parents and image.is_file()


def annotated_copy(root: Path, video_id: str, entry: dict | None, catalog_sha256: str) -> Path | None:
    if not entry or entry.get("catalog_sha256") != catalog_sha256:
        return None
    raw = entry.get("path")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw).expanduser().resolve()
    if path.parent != (root / "annotated-transcripts" / video_id).resolve() or not path.is_file():
        return None
    expected = entry.get("generated_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        return None
    return path if hashlib.sha256(path.read_bytes()).hexdigest() == expected else None


def transcript_files(directory: Path) -> dict[str, Path]:
    return PATHS.resolve_artifact_paths(directory)


def transcript_present(directory: Path) -> bool:
    try:
        return all(path.is_file() and path.stat().st_size for path in transcript_files(directory).values())
    except (OSError, ValueError):
        return False


def snapshot(root: Path, catalog_path: Path, limit: int = 0) -> dict:
    catalog = load_json(catalog_path)
    records = rows(catalog)
    selected = records[:limit] if limit else records
    ids = {str(row.get("video_id") or row.get("aweme_id")) for row in selected}
    downloads = load_json(root / "media" / "download-manifest.json").get("videos", {})
    transcripts = load_json(root / "local-transcripts" / "_batch-state.json").get("jobs", {})
    covers = cover_records(root, catalog_path)
    verified = set()
    for video_id in ids:
        entry = downloads.get(video_id, {})
        if entry.get("status") == "verified" and entry.get("path") and Path(entry["path"]).is_file():
            verified.add(video_id)
    transcribed = set()
    for video_id in ids:
        entry = transcripts.get(video_id, {})
        directory = root / "local-transcripts" / video_id
        if entry.get("status") in SUCCESS_ASR and transcript_present(directory):
            transcribed.add(video_id)
    catalog_complete = catalog.get("catalog_complete", catalog.get("complete")) is True
    library_downloads = {video_id for video_id, entry in downloads.items() if entry.get("status") == "verified"
                         and entry.get("path") and Path(entry["path"]).is_file()}
    library_transcripts = {video_id for video_id, entry in transcripts.items() if entry.get("status") in SUCCESS_ASR
                           and transcript_present(root / "local-transcripts" / video_id)}
    summary = {
        "updated_at": now(), "root": str(root), "catalog_path": str(catalog_path),
        "catalog_complete": catalog_complete, "catalog_video_count": len(records),
        "selected_video_count": len(ids), "limit": limit,
        "library_download_verified_total": len(library_downloads),
        "library_transcript_saved_total": len(library_transcripts),
        "known_downloads_outside_current_catalog": sorted(library_downloads - {str(row.get("video_id") or row.get("aweme_id")) for row in records}),
        "download_verified": len(verified), "download_pending_or_failed": len(ids - verified),
        "download_statuses": dict(Counter(downloads.get(video_id, {}).get("status", "pending") for video_id in ids)),
        "transcript_saved": len(transcribed), "transcript_pending_or_failed": len(ids - transcribed),
        "transcript_statuses": dict(Counter(transcripts.get(video_id, {}).get("status", "pending") for video_id in ids)),
        "missing_download_ids": sorted(ids - verified), "missing_transcript_ids": sorted(ids - transcribed),
        "all_public_videos_downloaded": bool(ids) and catalog_complete and not limit and ids == verified,
        "all_public_videos_transcribed": bool(ids) and catalog_complete and not limit and ids == verified == transcribed,
        "transcript_review_status": "raw_machine_output_not_audio_proofread",
        "public_metrics": METRICS.metrics_summary(records),
        "cover_metadata_rows": len(ids & covers.keys()),
        "cover_metadata_processed": sum(cover_ready(root, covers.get(video_id)) for video_id in ids),
        "cover_metadata_statuses": dict(Counter(covers.get(video_id, {}).get("cover_status", "pending") for video_id in ids)),
        "cover_metadata_missing_ids": sorted(video_id for video_id in ids if not cover_ready(root, covers.get(video_id))),
        "cover_metadata_complete": bool(ids) and catalog_complete and not limit and all(
            cover_ready(root, covers.get(video_id)) for video_id in ids),
        "runtime": {"online_llm_calls": 0, "computer_use_calls": 0, "paid_asr_calls": 0},
    }
    atomic_json(root / "pipeline-summary.json", summary)
    return summary


def write_library_index(root: Path, catalog_path: Path) -> None:
    """Build a readable local index without copying transcript text into an LLM."""
    catalog = load_json(catalog_path)
    metadata = {str(row.get("video_id") or row.get("aweme_id")): row for row in rows(catalog)}
    downloads = load_json(root / "media" / "download-manifest.json").get("videos", {})
    jobs = load_json(root / "local-transcripts" / "_batch-state.json").get("jobs", {})
    covers = cover_records(root, catalog_path)
    generated = load_json(root / "annotated-transcripts" / "_generated-state.json").get("videos", {})
    catalog_digest = hashlib.sha256(catalog_path.read_bytes()).hexdigest() if catalog_path.is_file() else ""
    heading = ("# 视频与机器逐字稿索引\n\n生成时间：" + now() +
               "\n\n逐字稿为本地机器识别，未统一做人工听音校对。目录 complete=" +
               str(catalog.get("catalog_complete", catalog.get("complete", False))).lower() +
               "。已知链接可能多于尚未采集完整的分页目录。\n\n"
               "指标为官方网页采集时的公开计数快照；未获取不等于 0。文件名使用点赞、评论、收藏、分享四项，播放量占位零视为未核实。\n\n")
    cover_index = root / "标题标签封面索引.md"
    if cover_index.is_file() and covers:
        heading += f"发布文案开头、井号标签和封面文字见[标题标签封面索引](<{cover_index}>)；封面机器识字尚未逐字人工核对。\n\n"
    lines = [heading, "| 视频 ID | 标题 | 赞 | 评 | 藏 | 转 | 指标采集时间 | 原视频 | 机器逐字稿 | SRT | 标注阅读稿 |\n"
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"]
    metric_keys = ("digg_count", "comment_count", "collect_count", "share_count", "play_count", "forward_count", "download_count")
    csv_rows = []
    for video_id in dict.fromkeys([*metadata, *downloads]):
        download = downloads.get(video_id, {})
        title = str(download.get("title") or metadata.get(video_id, {}).get("title") or "").replace("|", "\\|").replace("\n", " ")[:160]
        media = Path(download["path"]) if download.get("path") else None
        video_link = f"[查看原片](<{media}>)" if media and media.is_file() else download.get("status", "待下载")
        directory = root / "local-transcripts" / video_id
        path_error = False
        try:
            files = transcript_files(directory)
        except (OSError, ValueError):
            files, path_error = {}, True
        md, srt = files.get("markdown"), files.get("srt")
        transcript_link = ("路径待核验" if path_error else
                           f"[逐字稿](<{md}>)" if md and md.is_file() else jobs.get(video_id, {}).get("status", "待识别"))
        subtitle_link = "路径待核验" if path_error else f"[字幕](<{srt}>)" if srt and srt.is_file() else "—"
        annotated = annotated_copy(root, video_id, generated.get(video_id), catalog_digest)
        annotated_link = f"[带标题标签封面的阅读稿](<{annotated}>)" if annotated else "待生成"
        work = metadata.get(video_id, {})
        cover = covers.get(video_id, {})
        values = [METRICS.metric_text(work, key) for key in metric_keys]
        captured_at = work.get("statistics_captured_at") or "未获取"
        lines.append(f"| [{video_id}](https://www.douyin.com/video/{video_id}) | {title} | " +
                     " | ".join(values[:4]) + f" | {captured_at} | {video_link} | {transcript_link} | {subtitle_link} | {annotated_link} |\n")
        published_at = "未获取"
        created = work.get("create_time")
        if isinstance(created, int) and not isinstance(created, bool) and created > 0:
            try:
                published_at = datetime.fromtimestamp(created, timezone.utc).isoformat(timespec="seconds")
            except (OverflowError, OSError, ValueError):
                pass
        csv_rows.append([video_id, str(work.get("title") or download.get("title") or ""),
                         "https://www.douyin.com/video/" + video_id, published_at, captured_at, *values,
                         *[(work.get("statistics_availability") or {}).get(key, "not_returned") for key in metric_keys],
                         str(media) if media and media.is_file() else "", str(md) if md and md.is_file() else "", str(srt) if srt and srt.is_file() else "", str(annotated) if annotated else "",
                         str(cover.get("title_candidate") or ""), " ".join(cover.get("hashtags") or []),
                         str(cover.get("cover_text_raw") or ""), str(cover.get("cover_status") or "未获取"),
                         str(cover.get("cover_image_path") or "") if cover_ready(root, cover) else ""])
    temporary = root / ".视频与逐字稿索引.md.tmp"
    temporary.write_text("".join(lines), encoding="utf-8")
    os.replace(temporary, root / "视频与逐字稿索引.md")
    csv_path = root / "catalog" / "作品数据指标.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".作品数据指标.", dir=csv_path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["video_id", "title", "official_url", "published_at_utc", "statistics_captured_at", *metric_keys,
                             *[key + "_availability" for key in metric_keys], "original_video_path", "transcript_path", "srt_path", "annotated_transcript_path",
                             "title_candidate", "hashtags", "cover_text_raw", "cover_status", "cover_image_path"])
            writer.writerows(csv_rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, csv_path)
    finally:
        Path(name).unlink(missing_ok=True)


def brief(summary: dict, stage: str) -> None:
    print(json.dumps({"event": "progress", "stage": stage,
                      **{key: summary[key] for key in ("catalog_complete", "catalog_video_count",
                                                     "selected_video_count", "download_verified", "transcript_saved",
                                                     "library_download_verified_total", "library_transcript_saved_total")}},
                     ensure_ascii=False), flush=True)


def start(command: list[str], log_path: Path) -> tuple[subprocess.Popen, object]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8")
    handle.write("\n--- run started " + now() + " ---\n")
    handle.flush()
    process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT)
    ACTIVE.append(process)
    return process, handle


def monitor(process: subprocess.Popen, root: Path, catalog_path: Path, limit: int, stage: str) -> int:
    while process.poll() is None:
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            brief(snapshot(root, catalog_path, limit), stage)
    return int(process.returncode)


def run_logged(command: list[str], log: Path, root: Path, catalog_path: Path, limit: int, stage: str) -> int:
    process, handle = start(command, log)
    try:
        return monitor(process, root, catalog_path, limit, stage)
    finally:
        handle.close()


def stop_children() -> None:
    for process in ACTIVE:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
    for process in ACTIVE:
        if process.poll() is None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("profile", nargs="?", help="official Douyin profile URL, share card, or sec_uid")
    result.add_argument("--root", type=Path, required=True, help="library folder; videos and raw transcripts are retained")
    result.add_argument("--catalog", type=Path, help="existing catalog; default ROOT/catalog/catalog.json")
    result.add_argument("--stage", choices=("all", "collect", "metadata", "download", "transcribe", "status", "doctor"), default="all")
    result.add_argument("--refresh-catalog", action="store_true", help="refetch public works and their observed interaction metrics")
    result.add_argument("--limit", type=int, default=0, help="0 processes all; positive value is a sample, never a full-library success")
    result.add_argument("--workers", type=int, default=2, help="parallel downloads; ASR runs one local model at a time")
    result.add_argument("--dns-mode", choices=("auto", "off", "always"), default="auto")
    result.add_argument("--proxy", help="explicit proxy for public URL resolution and downloads; not saved to reports")
    result.add_argument("--browser-session", action="store_true", help="compatibility flag: dedicated normal Edge collection is already the default")
    result.add_argument("--browser-session-dir", type=Path, help="dedicated profile directory; never the daily Edge profile")
    result.add_argument("--browser-timeout", type=int, default=600, help="seconds for official page collection including ordinary manual login")
    result.add_argument("--backend", type=Path, help="override the bundled download_douyin.py backend")
    result.add_argument("--model", help="cached MLX Whisper model repository or local snapshot directory")
    result.add_argument("--initial-prompt", help="optional local ASR vocabulary hint, recorded in evidence; no online LLM call")
    result.add_argument("--serial", action="store_true", help="finish downloads before starting local ASR")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.limit < 0 or not 1 <= args.workers <= 8 or not 30 <= args.browser_timeout <= 3600:
        raise ValueError("limit must be nonnegative and workers must be 1–8")
    root = args.root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    default_catalog = root / "catalog" / "catalog.json"
    if (args.stage in ("metadata", "status") and not default_catalog.is_file()
            and (root / "catalog" / "browser-catalog.json").is_file()):
        # Older creator libraries stored the same official browser catalog
        # under this name. Metadata backfill should work without a rename.
        default_catalog = root / "catalog" / "browser-catalog.json"
    catalog_path = args.catalog.expanduser().resolve() if args.catalog else default_catalog
    logs = root / "logs"
    commands: dict[str, list[str]] = {}
    python = sys.executable
    if args.stage == "doctor":
        return subprocess.call([python, str(SCRIPTS / "transcribe_local.py"), "--doctor"])
    if args.stage == "status":
        print(json.dumps(snapshot(root, catalog_path, args.limit), ensure_ascii=False, indent=2))
        write_library_index(root, catalog_path)
        return 0
    lock = (root / ".pipeline.lock").open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("This library already has a running pipeline; inspect --stage status")
    started = now()
    stages: dict[str, int] = {}
    try:
        # Finish an interrupted rename before any download or ASR can use paths.
        if (root / NAMING.JOURNAL_NAME).exists() or (root / NAMING.JOURNAL_NAME).is_symlink():
            NAMING.reconcile_names(root, catalog_path)
        cached = load_json(catalog_path)
        requested_uid = None
        if args.profile:
            if re.fullmatch(r"MS4wLj[A-Za-z0-9_-]+", args.profile):
                requested_uid = args.profile
            else:
                parsed = urlparse(args.profile)
                match = re.fullmatch(r"/user/(MS4wLj[A-Za-z0-9_-]+)/?", parsed.path)
                if (parsed.scheme == "https" and parsed.hostname in ("www.douyin.com", "douyin.com")
                        and parsed.port in (None, 443) and not parsed.username and not parsed.password and match):
                    requested_uid = match.group(1)
        cached_uid = cached.get("creator", {}).get("sec_uid")
        if args.profile and cached_uid and not requested_uid and args.stage in ("all", "collect", "download"):
            spec = importlib.util.spec_from_file_location("creator_identity_resolver", SCRIPTS / "collect_creator.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            requested_uid = module.resolve_sec_uid(args.profile, module.HttpClient(30, args.proxy, args.dns_mode))
        if requested_uid and cached_uid and requested_uid != cached_uid:
            raise ValueError("This library belongs to another creator; use a different --root")
        needs_collect = args.stage == "collect" or (args.stage == "all" and
                        (args.refresh_catalog or not cached.get("catalog_complete", cached.get("complete"))))
        if needs_collect:
            if not args.profile:
                raise ValueError("A profile URL or sec_uid is required to collect the creator catalog")
            if not requested_uid:
                spec = importlib.util.spec_from_file_location("browser_identity_resolver", SCRIPTS / "collect_creator.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                requested_uid = module.resolve_sec_uid(args.profile, module.HttpClient(30, args.proxy, args.dns_mode))
            session_dir = args.browser_session_dir.expanduser().resolve() if args.browser_session_dir else root / ".browser-session"
            commands["collect"] = [python, str(SCRIPTS / "launch_browser_collector.py"),
                                   "--profile", "https://www.douyin.com/user/" + requested_uid,
                                   "--output", str(catalog_path), "--session-dir", str(session_dir),
                                   "--timeout", str(args.browser_timeout), "--dns-mode", args.dns_mode]
            print(json.dumps({"event": "official_browser_session_notice",
                              "message": "专用 Edge 将显示官方作者主页；首次如需登录，请正常扫码，随后由脚本继续采集。"},
                             ensure_ascii=False), flush=True)
            stages["collect"] = run_logged(commands["collect"], logs / "collect.log", root, catalog_path, args.limit, "collect")
            if not rows(load_json(catalog_path)):
                raise RuntimeError("No video catalog was collected; inspect logs/collect.log")
        catalog = load_json(catalog_path)
        if not rows(catalog):
            raise ValueError("A nonempty catalog is required for download or transcription")
        selected = rows(catalog)[:args.limit] if args.limit else rows(catalog)
        video_ids = [str(row.get("video_id") or row.get("aweme_id")) for row in selected]
        manifest = root / "media" / "download-manifest.json"
        commands["download"] = [python, str(SCRIPTS / "download_batch.py"), str(catalog_path),
                                "--download-root", str(root / "media"), "--workers", str(args.workers),
                                "--dns-mode", args.dns_mode, "--limit", str(args.limit)]
        if args.proxy:
            commands["download"] += ["--proxy", args.proxy]
        if args.backend:
            commands["download"] += ["--backend", str(args.backend.expanduser().resolve())]
        commands["transcribe"] = [python, str(SCRIPTS / "transcribe_local.py"), "--manifest", str(manifest),
                                  "--output-dir", str(root / "local-transcripts"), "--video-id", *video_ids]
        if args.model:
            commands["transcribe"] += ["--model", args.model]
        if args.initial_prompt:
            commands["transcribe"] += ["--initial-prompt", args.initial_prompt]
        commands["metadata"] = [python, str(SCRIPTS / "collect_cover_metadata.py"),
                                "--catalog", str(catalog_path), "--root", str(root), "--limit", str(args.limit)]
        if args.proxy:
            commands["metadata"] += ["--proxy", args.proxy]
        commands["annotate"] = [python, str(SCRIPTS / "build_annotated_transcripts.py"),
                                "--root", str(root), "--catalog", str(catalog_path)]
        done_file = root / ".downloads-finished.json"
        asr_process = asr_log = None
        if args.stage == "all" and not args.serial:
            done_file.unlink(missing_ok=True)
            watch_command = commands["transcribe"] + ["--watch", "--poll-seconds", "10", "--done-file", str(done_file)]
            asr_process, asr_log = start(watch_command, logs / "transcribe.log")
        if args.stage in ("all", "download"):
            stages["download"] = run_logged(commands["download"], logs / "download.log", root, catalog_path, args.limit, "download")
            if asr_process:
                if stages["download"] in (0, 1):
                    atomic_json(done_file, {"finished_at": now(), "download_exit_code": stages["download"]})
                elif asr_process.poll() is None:
                    asr_process.send_signal(signal.SIGINT)
        if asr_process:
            try:
                stages["transcribe"] = monitor(asr_process, root, catalog_path, args.limit, "transcribe")
            finally:
                asr_log.close()
        elif args.stage in ("all", "transcribe"):
            stages["transcribe"] = run_logged(commands["transcribe"], logs / "transcribe.log", root, catalog_path, args.limit, "transcribe")
        naming_report = None
        if (args.stage in ("all", "collect", "download", "transcribe")
                and catalog.get("catalog_complete") is True and any("statistics" in row for row in rows(catalog))
                and not any(code != 0 for code in stages.values())):
            # All child writers have finished; names can now change safely.
            naming_report = NAMING.reconcile_names(root, catalog_path)
        if args.stage in ("all", "collect", "metadata"):
            stages["metadata"] = run_logged(commands["metadata"], logs / "metadata.log", root, catalog_path,
                                             args.limit, "metadata")
        if args.stage in ("all", "collect", "metadata", "transcribe") and (root / "catalog" / COVER_RECORDS_NAME).is_file():
            stages["annotate"] = run_logged(commands["annotate"], logs / "annotate.log", root, catalog_path,
                                             args.limit, "annotate")
        summary = snapshot(root, catalog_path, args.limit)
        if naming_report is not None:
            summary["artifact_naming"] = naming_report
        write_library_index(root, catalog_path)
        # A sampled or incomplete run must never publish a full-library corpus.
        core_failed = any(stages.get(stage, 0) != 0 for stage in ("collect", "download", "transcribe"))
        if (args.stage in ("all", "collect") and summary["all_public_videos_transcribed"]
                and not core_failed):
            commands["export"] = [python, str(SCRIPTS / "export_transcripts.py"),
                                  "--root", str(root), "--catalog", str(catalog_path)]
            stages["export"] = run_logged(commands["export"], logs / "export.log", root, catalog_path, 0, "export")
        summary["corpus_exported"] = stages.get("export") == 0
        if summary["corpus_exported"]:
            summary["corpus_export"] = load_json(root / "corpus-export.json")
        atomic_json(root / "pipeline-summary.json", summary)
        atomic_json(root / "pipeline-run.json", {"started_at": started, "finished_at": now(),
                    "stage_exit_codes": stages, "summary": summary,
                    "logs": {stage: str(logs / (stage + ".log")) for stage in stages}})
        brief(summary, "finished")
        if any(code != 0 for code in stages.values()):
            return 1
        if args.stage == "all" and (summary["download_pending_or_failed"] or summary["transcript_pending_or_failed"] or
                                     not summary["catalog_complete"]):
            return 1
        return 0
    except KeyboardInterrupt:
        stop_children()
        snapshot(root, catalog_path, args.limit)
        print(json.dumps({"event": "interrupted", "message": "Checkpoints retained; rerun this command"}), flush=True)
        return 130
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        stop_children()
        print(json.dumps({"event": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
