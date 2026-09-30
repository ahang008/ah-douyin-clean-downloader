#!/usr/bin/env python3
"""Recover catalog-verified works omitted by the app feed from official web detail.

The dedicated Edge session exposes the target work's public detail response.
Signed playback and cover URLs remain in memory. The saved report contains
only identities, status codes, source kinds, and local paths.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import traceback
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_cover_metadata as covers
import download_batch as batch
import download_douyin as backend
import launch_browser_collector as launch
from browser_dns_proxy import BrowserDNSProxy


VIDEO_ID = re.compile(r"\d{16,22}")
DETAIL_PATH = "/aweme/v1/web/aweme/detail/"
SOURCE = "official_aweme_web_detail.video.cover"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def eligible_failed_ids(manifest: dict, catalog_ids: set[str]) -> list[str]:
    """Only an exact app-feed identity miss triggers the browser fallback."""
    entries = manifest.get("videos") or {}
    return [video_id for video_id, row in entries.items()
            if video_id in catalog_ids and isinstance(row, dict) and row.get("status") == "failed"
            and ("metadata_id_not_returned" in str(row.get("last_error") or "")
                 or "接口没有返回对应作品" in str(row.get("last_error") or ""))]


def public_detail_from_response(response, video_id: str) -> dict | None:
    parsed = urlsplit(response.url)
    if (parsed.scheme != "https" or parsed.hostname != "www.douyin.com"
            or parsed.path != DETAIL_PATH or response.status != 200):
        return None
    try:
        payload = response.json()
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("status_code") != 0:
        return None
    item = payload.get("aweme_detail")
    if not isinstance(item, dict) or str(item.get("aweme_id") or "") != video_id:
        return None
    return item


def web_detail(context, video_id: str) -> dict | None:
    for wait_ms in (5000, 8500):
        page = context.new_page()
        responses = []
        def record(response):
            responses.append(response)
        page.on("response", record)
        try:
            page.goto("https://www.douyin.com/video/" + video_id,
                      wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(wait_ms)
            for response in responses:
                item = public_detail_from_response(response, video_id)
                if item is not None:
                    return item
        finally:
            page.close()
    return None


def author_matches(item: dict, catalog: dict, row: dict) -> bool:
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    creator_uid = str((catalog.get("creator") or {}).get("sec_uid") or "")
    row_author = row.get("author") if isinstance(row.get("author"), dict) else {}
    if not creator_uid or str(row_author.get("sec_uid") or "") != creator_uid:
        return False
    detail_sec_uid, detail_uid = author.get("sec_uid"), author.get("uid")
    row_uid = row_author.get("uid")
    if detail_sec_uid and str(detail_sec_uid) != creator_uid:
        return False
    if detail_uid and row_uid and str(detail_uid) != str(row_uid):
        return False
    return bool(detail_sec_uid) or (bool(detail_uid) and bool(row_uid))


def recover_media(root: Path, item: dict, video_id: str, author: str,
                  proxy: str | None) -> dict:
    media_root = root / "media"
    folder = media_root / backend.safe_component(author, "未知作者")
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"抖音网页播放源-{video_id}.mp4"
    if destination.is_file():
        probe = batch.verify_file(destination, media_root, backend)
        if probe.get("audio_streams"):
            return {"media_saved": True, "media_path": str(destination.resolve()),
                    "source_kind": "existing_official_web_detail", "bytes": probe["bytes"]}
    client = backend.CurlClient(backend.proxy_candidates(proxy), timeout=45)
    partial = destination.with_suffix(".mp4.part")
    failures = []
    for source in backend.media_sources(item):
        partial.unlink(missing_ok=True)
        try:
            client.fetch(source["url"], partial, backend.USER_AGENT, timeout=1200)
            probe = backend.verify_media(partial)
            if probe.get("verification") != "ffprobe" or not probe.get("video_streams") or not probe.get("audio_streams"):
                raise RuntimeError("ffprobe did not find complete video and audio streams")
            os.replace(partial, destination)
            return {"media_saved": True, "media_path": str(destination.resolve()),
                    "source_kind": source["name"], "bytes": destination.stat().st_size}
        except Exception as exc:
            partial.unlink(missing_ok=True)
            failures.append(type(exc).__name__)
    raise RuntimeError("all_official_playback_sources_failed:" + ",".join(failures[-3:]))


def recover_cover(root: Path, item: dict, video_id: str, proxy: str | None) -> dict:
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    cover = video.get("cover") if isinstance(video.get("cover"), dict) else {}
    candidates = [url for url in (cover.get("url_list") or []) if covers.official_cover_url(url)]
    if not candidates:
        return {"cover_saved": False, "cover_reason": "official_cover_url_missing_or_untrusted"}
    destination = root / "covers" / (video_id + ".jpg")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if covers.valid_jpeg(destination):
        return {"cover_saved": True, "cover_image_path": str(destination.resolve()),
                "cover_source": "existing_cached_cover"}
    if not covers.valid_jpeg(destination):
        with tempfile.TemporaryDirectory(prefix="douyin-web-cover-") as temp:
            image = Path(temp) / "image"
            for url in candidates:
                for route in covers.routes(proxy):
                    image.unlink(missing_ok=True)
                    if covers.curl_to(url, image, covers.IMAGE_USER_AGENT, route, 45) and covers.convert_to_jpeg(image, destination):
                        break
                if covers.valid_jpeg(destination):
                    break
    if not covers.valid_jpeg(destination):
        return {"cover_saved": False, "cover_reason": "official_cover_download_failed"}
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    marker = destination.with_suffix(".source.json")
    batch.atomic_json(marker, {"video_id": video_id, "cover_source": SOURCE,
                               "cover_image_sha256": digest, "captured_at_utc": now()})
    return {"cover_saved": True, "cover_image_path": str(destination.resolve()),
            "cover_source": SOURCE}


def run(args: argparse.Namespace) -> int:
    root = args.root.expanduser().resolve()
    catalog = load_json(args.catalog or root / "catalog/catalog.json")
    if catalog.get("catalog_complete") is not True or not isinstance(catalog.get("videos"), list):
        raise ValueError("A complete public creator catalog is required")
    by_id = {str(row.get("video_id")): row for row in catalog["videos"] if isinstance(row, dict)}
    if args.video_id:
        ids = list(dict.fromkeys(args.video_id))
    else:
        if args.cover_only:
            raise ValueError("--cover-only requires explicit --video-id")
        ids = eligible_failed_ids(load_json(root / "media/download-manifest.json"), set(by_id))
    if not ids:
        print(json.dumps({"event": "web_detail_recovery", "selected": 0, "recovered": 0}), flush=True)
        return 0
    if any(not VIDEO_ID.fullmatch(video_id) or video_id not in by_id for video_id in ids):
        raise ValueError("Recovery video ID is not in the selected public creator catalog")
    if not args.cover_only:
        batch.install_process_client(backend, batch.ProcessDNS(args.dns_mode))
    session = (args.session_dir or root / ".browser-session").expanduser().resolve()
    launch.prepare_session(session)
    lock = (session / ".launcher.lock").open("a")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    process = dns_proxy = None
    records = []
    try:
        child_env = dict(os.environ)
        for key in ("DEBUG", "DEBUG_FILE", "PWDEBUG"):
            child_env.pop(key, None)
        child_env["NO_PROXY"] = "127.0.0.1,localhost,::1"
        port = launch.available_loopback_port()
        command = [str(launch.EDGE), "--user-data-dir=" + str(session),
                   "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=" + str(port),
                   "--no-first-run", "about:blank"]
        if launch.needs_dns_tunnel(args.dns_mode):
            dns_proxy = BrowserDNSProxy()
            proxy_port = dns_proxy.start()
            command.insert(-1, "--proxy-server=http://127.0.0.1:" + str(proxy_port))
        (session / "DevToolsActivePort").unlink(missing_ok=True)
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True, env=child_env)
        port = launch.wait_for_port(process, session, expected_port=port)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp("http://127.0.0.1:" + str(port), no_defaults=True)
            context = browser.contexts[0]
            for video_id in ids:
                row = {"video_id": video_id, "at_utc": now(), "web_detail_verified": False}
                stage = "official_web_detail"
                try:
                    item = web_detail(context, video_id)
                    if item is None:
                        raise RuntimeError("target_official_web_detail_not_returned")
                    stage = "author_identity"
                    if not author_matches(item, catalog, by_id[video_id]):
                        raise RuntimeError("official_web_detail_author_mismatch")
                    row["web_detail_verified"] = True
                    if not args.cover_only:
                        stage = "original_media"
                        row.update(recover_media(root, item, video_id,
                                                 catalog["creator"].get("nickname") or "", args.proxy))
                    stage = "selected_cover"
                    row.update(recover_cover(root, item, video_id, args.proxy))
                except Exception as exc:
                    message = str(exc)
                    code = message if message in ("target_official_web_detail_not_returned",
                                                   "official_web_detail_author_mismatch") else (
                        "all_official_playback_sources_failed" if message.startswith("all_official_playback_sources_failed:")
                        else type(exc).__name__)
                    row["error"] = code
                    row["failure_stage"] = stage
                    row["failure_location"] = [{"function": frame.name, "line": frame.lineno}
                                               for frame in traceback.extract_tb(exc.__traceback__)[-3:]]
                records.append(row)
                print(json.dumps({"event": "web_detail_work", "video_id": video_id,
                                  "media_saved": row.get("media_saved", False),
                                  "cover_saved": row.get("cover_saved", False),
                                  "error": row.get("error")}, ensure_ascii=False), flush=True)
            browser.close()
    finally:
        launch.stop_owned(process)
        if dns_proxy:
            dns_proxy.stop()
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    report = root / "catalog" / ("web-detail-cover-recovery-report.json" if args.cover_only
                                  else "web-detail-media-recovery-report.json")
    batch.atomic_json(report, {"captured_at_utc": now(), "source": DETAIL_PATH,
                               "cover_only": args.cover_only, "records": records,
                               "signed_urls_saved": False, "browser_session_exported": False})
    recovered = sum(bool(row.get("cover_saved")) if args.cover_only else bool(row.get("media_saved")) for row in records)
    print(json.dumps({"event": "web_detail_recovery", "selected": len(ids), "recovered": recovered,
                      "report": str(report)}, ensure_ascii=False), flush=True)
    return 0 if recovered == len(ids) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--session-dir", type=Path)
    parser.add_argument("--proxy")
    parser.add_argument("--dns-mode", choices=("auto", "off", "always"), default="auto")
    parser.add_argument("--video-id", nargs="+")
    parser.add_argument("--cover-only", action="store_true")
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
