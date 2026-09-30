#!/usr/bin/env python3
"""Collect published captions, hashtags and selected-cover OCR for a creator.

The input is this Skill's saved official public creator catalog. No MP4 is
downloaded. Cover OCR uses local Apple Vision and is never marked as human
verified. Short-lived signed CDN URLs are used only in memory.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


APP_USER_AGENT = "Aweme 350101 rv:350101 (iPhone; iOS 17.0; zh_CN) Cronet"
IMAGE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 Mobile/15E148"
)
FEED_URL = "https://aweme.snssdk.com/aweme/v1/feed/?aweme_id={video_id}&aid=1128"
# Cover hosts returned by the official Aweme feed. Do not fetch arbitrary hosts
# supplied through a manifest or a redirect.
OFFICIAL_IMAGE_HOSTS = (
    "douyinpic.com",
    "byteimg.com",
    "pstatp.com",
    "snssdk.com",
    "bytecdn.cn",
)
VIDEO_ID_PATTERN = re.compile(r"^[0-9]{16,22}$")
HASHTAG_PATTERN = re.compile(r"[#＃]([\w]+)", re.UNICODE)
LOW_CONFIDENCE = 0.65
SUCCESS_STATUSES = frozenset({"ok", "ocr_low_confidence", "ocr_empty"})
WEB_DETAIL_SOURCE = "official_aweme_web_detail.video.cover"
RECORD_NAME = "作品标题标签封面.jsonl"
REPORT_NAME = "cover-metadata-report.json"
INDEX_NAME = "标题标签封面索引.md"


def official_cover_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(host)
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and any(host == suffix or host.endswith("." + suffix) for suffix in OFFICIAL_IMAGE_HOSTS)
    )


def parse_caption(value: object) -> tuple[str, str | None, list[str]]:
    """A catalog title is the full published caption, not an independent title."""
    caption = value if isinstance(value, str) else ""
    first_tag = HASHTAG_PATTERN.search(caption)
    prefix = caption[:first_tag.start()] if first_tag else caption
    title_candidate = re.sub(r"\s+", " ", prefix).strip() or None
    tags = []
    seen = set()
    for match in HASHTAG_PATTERN.finditer(caption):
        tag = "#" + match.group(1)
        if tag not in seen:
            tags.append(tag)
            seen.add(tag)
    return caption, title_candidate, tags


def load_catalog(path: Path) -> tuple[list[dict], str, bool]:
    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("videos"), list):
        raise ValueError("catalog must contain a videos list")
    if (payload.get("method") != "official_browser_sdk_response_capture"
            or payload.get("catalog_visibility") != "public"):
        raise ValueError("cover metadata requires a saved official public browser catalog")
    expected_count = payload.get("video_count")
    if expected_count is not None and (isinstance(expected_count, bool) or expected_count != len(payload["videos"])):
        raise ValueError("catalog video_count differs from videos list")
    digest = hashlib.sha256(raw).hexdigest()
    works = []
    seen = set()
    for number, row in enumerate(payload["videos"], 1):
        if not isinstance(row, dict):
            raise ValueError(f"catalog video {number} is not an object")
        video_id = str(row.get("video_id") or "")
        if not VIDEO_ID_PATTERN.fullmatch(video_id) or video_id in seen:
            raise ValueError(f"catalog video {number} has an invalid or duplicate ID")
        seen.add(video_id)
        caption, title_candidate, tags = parse_caption(row.get("title"))
        works.append({
            "video_id": video_id,
            "official_url": "https://www.douyin.com/video/" + video_id,
            "published_caption": caption,
            "title_candidate": title_candidate,
            "hashtags": tags,
            "caption_source": "official_creator_catalog.title",
            "catalog_sha256": digest,
            "human_verified": False,
        })
    if not works:
        raise ValueError("catalog has no videos")
    return works, digest, payload.get("catalog_complete") is True


def load_records(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    records: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and VIDEO_ID_PATTERN.fullmatch(str(row.get("video_id") or "")):
            records[str(row["video_id"])] = row
    return records


def record(video_id: str, image: Path | None, status: str, lines: list[dict] | None = None,
           captured_at: str | None = None, source: str = "official_aweme_feed.video.cover") -> dict:
    lines = lines or []
    return {
        "video_id": video_id,
        "cover_image_path": str(image.resolve()) if image else None,
        "cover_source": source if image else None,
        "cover_captured_at_utc": captured_at,
        "cover_text_raw": "\n".join(str(line.get("text") or "") for line in lines).strip(),
        "cover_status": status,
        "ocr_lines": lines,
    }


def verified_web_detail_marker(video_id: str, image: Path) -> bool:
    marker = image.with_suffix(".source.json")
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        saved = json.loads(marker.read_text(encoding="utf-8"))
        return (isinstance(saved, dict) and saved.get("video_id") == video_id
                and saved.get("cover_source") == WEB_DETAIL_SOURCE
                and saved.get("cover_image_sha256") == hashlib.sha256(image.read_bytes()).hexdigest())
    except (OSError, ValueError):
        return False


def reusable_cover(video_id: str, saved: dict, image: Path) -> bool:
    return (saved.get("cover_status") in SUCCESS_STATUSES and not image.is_symlink()
            and valid_jpeg(image)
            and (saved.get("cover_source") != WEB_DETAIL_SOURCE
                 or verified_web_detail_marker(video_id, image)))


def merged_record(work: dict, cover: dict | None) -> dict:
    video_id = work["video_id"]
    if not cover:
        cover = record(video_id, None, "not_attempted")
    return {**record(video_id, None, "not_attempted"), **cover, **work}


def save_records(path: Path, works: list[dict], records: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".records-", suffix=".tmp", delete=False
    ) as handle:
        temporary = Path(handle.name)
        for work in works:
            handle.write(json.dumps(merged_record(work, records.get(work["video_id"])),
                                    ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def curl_to(url: str, output: Path, user_agent: str, proxy: str | None, seconds: int) -> bool:
    # No --location: a redirect to a non-official host must not be followed.
    args = [
        "curl", "--silent", "--show-error", "--fail", "--compressed",
        "--proto", "=https", "--max-redirs", "0",
        "--connect-timeout", "8", "--max-time", str(seconds),
        "--max-filesize", "12000000", "--user-agent", user_agent,
        "--output", str(output),
    ]
    if proxy:
        args.extend(["--proxy", proxy])
    args.append(url)
    result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0 and output.exists() and output.stat().st_size > 0


def routes(proxy: str | None) -> list[str | None]:
    configured = proxy or os.environ.get("AH_DOUYIN_PROXY")
    return [configured, None] if configured else [None]


def valid_jpeg(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 128:
        return False
    with path.open("rb") as handle:
        if handle.read(3) != b"\xff\xd8\xff":
            return False
    checked = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
        capture_output=True, text=True, check=False,
    )
    return checked.returncode == 0 and "pixelWidth:" in checked.stdout and "pixelHeight:" in checked.stdout


def convert_to_jpeg(source: Path, destination: Path) -> bool:
    temporary = destination.with_suffix(".jpg.part")
    temporary.unlink(missing_ok=True)
    result = subprocess.run(
        ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "90",
         "--out", str(temporary), str(source)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if result.returncode == 0 and valid_jpeg(temporary):
        os.replace(temporary, destination)
        return True
    temporary.unlink(missing_ok=True)
    return False


def fetch_one(video_id: str, image_dir: Path, proxy: str | None) -> dict:
    destination = image_dir / f"{video_id}.jpg"
    if destination.is_symlink():
        raise ValueError("cover destination must not be a symlink")
    if valid_jpeg(destination):
        cached_at = datetime.fromtimestamp(destination.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        # Existing JPEG bytes alone cannot prove whether the feed or web detail supplied them.
        source = WEB_DETAIL_SOURCE if verified_web_detail_marker(video_id, destination) else "cached_cover_source_unverified"
        return record(video_id, destination, "ocr_pending", captured_at=cached_at, source=source)
    destination.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"cover-{video_id}-") as temp_name:
        temp_dir = Path(temp_name)
        metadata = temp_dir / "feed.json"
        payload = None
        for route in routes(proxy):
            metadata.unlink(missing_ok=True)
            if not curl_to(FEED_URL.format(video_id=video_id), metadata, APP_USER_AGENT, route, 25):
                continue
            try:
                payload = json.loads(metadata.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return record(video_id, None, "metadata_invalid_json")
            if not isinstance(payload, dict):
                return record(video_id, None, "metadata_invalid_json")
            if payload.get("status_code") == 0:
                break
        if payload is None:
            return record(video_id, None, "metadata_request_failed")
        if payload.get("status_code") != 0:
            code = payload.get("status_code")
            return record(video_id, None, f"metadata_status_{code}" if isinstance(code, int) else "metadata_status_error")
        items = payload.get("aweme_list") or []
        if not isinstance(items, list):
            return record(video_id, None, "metadata_invalid_json")
        item = next((row for row in items if isinstance(row, dict) and str(row.get("aweme_id")) == video_id), None)
        if item is None:
            return record(video_id, None, "metadata_id_not_returned")
        video = item.get("video") or {}
        if not isinstance(video, dict):
            return record(video_id, None, "cover_url_missing")
        cover = video.get("cover") or {}
        if not isinstance(cover, dict):
            return record(video_id, None, "cover_url_missing")
        candidates = cover.get("url_list") or []
        if not isinstance(candidates, list) or not candidates:
            return record(video_id, None, "cover_url_missing")
        allowed = [url for url in candidates if official_cover_url(url)]
        if not allowed:
            return record(video_id, None, "cover_url_untrusted")

        saw_download = False
        for url in allowed:
            for route in routes(proxy):
                source = temp_dir / "cover.img"
                source.unlink(missing_ok=True)
                if not curl_to(url, source, IMAGE_USER_AGENT, route, 30):
                    continue
                saw_download = True
                if convert_to_jpeg(source, destination):
                    return record(video_id, destination, "ocr_pending",
                                  captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return record(video_id, None, "cover_decode_failed" if saw_download else "cover_download_failed")


def ocr_status(lines: object, error: object = None) -> str:
    if error:
        return "ocr_" + error if isinstance(error, str) and error in {
            "image_decode_failed", "request_failed"
        } else "ocr_process_failed"
    if not isinstance(lines, list) or not lines:
        return "ocr_empty"
    try:
        for row in lines:
            if not isinstance(row, dict) or not str(row.get("text") or "").strip():
                return "ocr_low_confidence"
            confidence = float(row.get("confidence"))
            if not math.isfinite(confidence) or confidence < LOW_CONFIDENCE or confidence > 1:
                return "ocr_low_confidence"
    except (TypeError, ValueError):
        return "ocr_low_confidence"
    return "ok"


def run_ocr(works: list[dict], images: dict[str, Path], script: Path,
            records: dict[str, dict], output: Path) -> None:
    if not images:
        return
    with tempfile.TemporaryDirectory(prefix="cover-ocr-") as temp_name:
        input_path = Path(temp_name) / "ocr-input.jsonl"
        with input_path.open("w", encoding="utf-8") as handle:
            for video_id in (work["video_id"] for work in works):
                if video_id in images:
                    handle.write(json.dumps({"video_id": video_id, "image_path": str(images[video_id])}) + "\n")
        with (Path(temp_name) / "swift-stderr.txt").open("w", encoding="utf-8") as error_file:
            process = subprocess.Popen(
                ["swift", str(script), str(input_path)], stdout=subprocess.PIPE,
                stderr=error_file, text=True, encoding="utf-8",
            )
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    result = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(result, dict):
                    continue
                video_id = str(result.get("video_id") or "")
                if video_id not in images:
                    continue
                lines = result.get("ocr_lines") or []
                if not isinstance(lines, list):
                    lines = []
                status = ocr_status(lines, result.get("error"))
                lines = [row for row in lines if isinstance(row, dict)]
                captured_at = records.get(video_id, {}).get("cover_captured_at_utc")
                source = records.get(video_id, {}).get("cover_source") or "official_aweme_feed.video.cover"
                records[video_id] = record(video_id, images[video_id], status, lines, captured_at, source)
                save_records(output, works, records)
            exit_code = process.wait()
    for video_id, image in images.items():
        if records.get(video_id, {}).get("cover_status") == "ocr_pending":
            captured_at = records.get(video_id, {}).get("cover_captured_at_utc")
            source = records.get(video_id, {}).get("cover_source") or "official_aweme_feed.video.cover"
            records[video_id] = record(video_id, image, "ocr_process_failed" if exit_code else "ocr_no_result",
                                       captured_at=captured_at, source=source)
    save_records(output, works, records)


def markdown_cell(value: object) -> str:
    return html.escape(str(value or "")).replace("|", "&#124;").replace("\n", "<br>")


def write_index(path: Path, works: list[dict], records: dict[str, dict], complete: bool,
                limit: int) -> None:
    """Write a readable projection without changing any transcript or source file."""
    lines = [
        "# 标题、井号标签与封面文字索引\n\n",
        "发布文案来自官方公开作品目录；“标题候选”仅取首个井号标签前的文字。",
        "封面文字为官方选定封面图的本地 Apple Vision 原始识字，未逐张人工复核，",
        "低置信度结果需对照原图。此步骤不下载视频。\n\n",
        f"目录完整：{'是' if complete else '否'}；本次处理范围：{'前 ' + str(limit) + ' 条样本' if limit else '目录中全部 ' + str(len(works)) + ' 条'}。\n\n",
        "| 作品 | 标题候选 | 井号标签 | 封面文字原始识别 | 识字状态 | 官方封面 |\n",
        "| --- | --- | --- | --- | --- | --- |\n",
    ]
    for work in works:
        video_id = work["video_id"]
        row = merged_record(work, records.get(video_id))
        candidate = markdown_cell(row["title_candidate"]) if row["title_candidate"] else "未单独提取"
        tags = markdown_cell(" ".join(row["hashtags"])) or "无"
        cover = row.get("cover_image_path")
        cover_link = f"[查看原图](<{cover}>)" if cover and Path(cover).is_file() else "未取得"
        cover_text = markdown_cell(row.get("cover_text_raw")) or (
            "未识别到文字" if row.get("cover_status") == "ocr_empty" else "待获取或待识别"
        )
        lines.append(
            f"| [{video_id}]({row['official_url']}) | {candidate} | {tags} | {cover_text} | "
            f"{markdown_cell(row.get('cover_status'))} | {cover_link} |\n"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".cover-index-", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        handle.writelines(lines)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_report(path: Path, works: list[dict], records: dict[str, dict], digest: str,
                 complete: bool, selected: list[str], limit: int) -> dict:
    counts: dict[str, int] = {}
    for video_id in selected:
        status = records.get(video_id, {}).get("cover_status", "not_attempted")
        counts[status] = counts.get(status, 0) + 1
    result = {
        "catalog_sha256": digest,
        "catalog_complete": complete,
        "catalog_video_count": len(works),
        "selected_video_count": len(selected),
        "sample_limit": limit,
        "published_caption_count": sum(bool(work["published_caption"].strip()) for work in works),
        "title_candidate_count": sum(bool(work["title_candidate"]) for work in works),
        "hashtagged_work_count": sum(bool(work["hashtags"]) for work in works),
        "records_path": str(path.with_name(RECORD_NAME)),
        "cover_dir": str(path.parent.parent / "covers"),
        "index_path": str(path.parent.parent / INDEX_NAME),
        "status_counts": dict(sorted(counts.items())),
        "low_confidence_count": counts.get("ocr_low_confidence", 0),
        "all_selected_covers_processed": bool(selected) and all(
            records.get(video_id, {}).get("cover_status") in SUCCESS_STATUSES for video_id in selected
        ),
        "all_catalog_covers_processed": bool(works) and complete and not limit and all(
            records.get(work["video_id"], {}).get("cover_status") in SUCCESS_STATUSES for work in works
        ),
        "ocr_review_status": "raw_machine_output_not_human_verified",
        "video_downloads": 0,
    }
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".cover-report-", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="抖音官方选定封面与本地 Apple Vision 原始识字")
    parser.add_argument("--catalog", required=True, type=Path, help="官方公开作者目录 catalog.json")
    parser.add_argument("--root", required=True, type=Path, help="该博主的持久化资料库目录")
    parser.add_argument("--limit", type=int, default=0, help="0 处理全目录，正数只做样本")
    parser.add_argument("--proxy", help="可选代理；也可使用 AH_DOUYIN_PROXY")
    args = parser.parse_args()
    if (not shutil.which("curl") or not shutil.which("sips") or not shutil.which("swift")
            or not Path(__file__).with_name("ocr_covers.swift").is_file()):
        print("需要 macOS 的 curl、sips 和 swift", file=sys.stderr)
        return 2
    try:
        if args.limit < 0:
            raise ValueError("limit must be nonnegative")
        works, digest, complete = load_catalog(args.catalog.expanduser())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    root = args.root.expanduser().resolve()
    image_dir = root / "covers"
    if image_dir.is_symlink():
        print("covers directory must not be a symlink", file=sys.stderr)
        return 2
    image_dir.mkdir(parents=True, exist_ok=True)
    if not image_dir.is_dir() or image_dir.resolve().parent != root:
        print("covers directory is outside the creator library", file=sys.stderr)
        return 2
    output = root / "catalog" / RECORD_NAME
    output.parent.mkdir(parents=True, exist_ok=True)
    records = load_records(output)
    ids = [work["video_id"] for work in (works[:args.limit] if args.limit else works)]
    todo = [video_id for video_id in ids if not reusable_cover(
        video_id, records.get(video_id, {}), image_dir / f"{video_id}.jpg")]
    # Limit feed traffic to at most three concurrent videos and space requests.
    if todo:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {}
            for video_id in todo:
                futures[pool.submit(fetch_one, video_id, image_dir, args.proxy)] = video_id
                time.sleep(0.35)
            for future in as_completed(futures):
                video_id = futures[future]
                try:
                    records[video_id] = future.result()
                except Exception:
                    records[video_id] = record(video_id, None, "cover_fetch_failed")
                save_records(output, works, records)
        images = {
            video_id: image_dir / f"{video_id}.jpg"
            for video_id in todo if records[video_id]["cover_status"] == "ocr_pending"
        }
        run_ocr(works, images, Path(__file__).with_name("ocr_covers.swift"), records, output)
    save_records(output, works, records)
    report = write_report(root / "catalog" / REPORT_NAME, works, records, digest, complete, ids, args.limit)
    write_index(root / INDEX_NAME, works, records, complete, args.limit)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["all_selected_covers_processed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
