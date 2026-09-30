#!/usr/bin/env python3
"""Capture creator pagination made by an isolated, normally logged-in browser.

Connects only to an explicitly supplied loopback CDP browser. It never launches
a browser, accesses Keychain, reads/exports cookies, signs HTTP requests, or
downloads videos. The official page's own SDK produces each authorpost request.
Playwright is imported only when the browser command is actually run.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

POST_URL = "https://www.douyin.com/aweme/v1/web/aweme/post/"
_METRICS_SPEC = importlib.util.spec_from_file_location("ah_douyin_capture_metrics", Path(__file__).with_name("work_metrics.py"))
WORK_METRICS = importlib.util.module_from_spec(_METRICS_SPEC)
_METRICS_SPEC.loader.exec_module(WORK_METRICS)


class CaptureValidationError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def safe_response_diagnostics(payload):
    payload = payload if isinstance(payload, dict) else {}
    return {"status_code": payload.get("status_code") if isinstance(payload.get("status_code"), (int, bool)) else None,
            "has_more": payload.get("has_more") if isinstance(payload.get("has_more"), (int, bool)) else None,
            "aweme_list_count": len(payload["aweme_list"]) if isinstance(payload.get("aweme_list"), list) else None}


def browser_error_kind(error):
    # Inspect in memory only. Playwright messages can contain signed request URLs.
    message = str(error).lower()
    if "targetclosed" in type(error).__name__.lower() or "target page, context or browser has been closed" in message:
        return "target_closed"
    if "execution context was destroyed" in message or "navigation is in progress" in message:
        return "navigation_transient"
    return "other"


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def resolve_profile(value):
    if re.fullmatch(r"MS4wLj[A-Za-z0-9_-]+", value):
        return value
    parsed = urlparse(value)
    match = re.fullmatch(r"/user/(MS4wLj[A-Za-z0-9_-]+)/?", parsed.path)
    if parsed.scheme == "https" and parsed.hostname == "www.douyin.com" and match:
        return match.group(1)
    raise ValueError("Provide a canonical official creator profile URL or sec_uid")


def official_request_cursor(url, sec_uid):
    """Only return public pagination fields; the signed URL stays in memory."""
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname != "www.douyin.com" or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.path != urlparse(POST_URL).path):
        return None
    query = parse_qs(parsed.query)
    if query.get("sec_user_id") != [sec_uid]:
        return None
    raw = query.get("max_cursor", [""])
    if len(raw) != 1 or not re.fullmatch(r"\d+", raw[0]):
        return None
    return int(raw[0])


def normalize_work(row, sec_uid, captured_at=None):
    if not isinstance(row, dict):
        raise CaptureValidationError("work_not_object")
    author = row.get("author") or {}
    if not isinstance(author, dict):
        raise CaptureValidationError("author_wrong_shape")
    if author.get("sec_uid") != sec_uid:
        raise CaptureValidationError("author_mismatch")
    video_id = str(row.get("aweme_id") or "")
    if not re.fullmatch(r"\d{16,22}", video_id):
        raise CaptureValidationError("video_id_invalid")
    video = row.get("video") or {}
    if not isinstance(video, dict):
        raise CaptureValidationError("video_wrong_shape")
    is_video = row.get("aweme_type", 0) in (0, 4) and not row.get("images")
    canonical = "https://www.douyin.com/" + ("video/" if is_video else "note/") + video_id
    work = {
        "video_id": video_id, "source_url": canonical, "canonical_url": canonical,
        "title": row.get("desc") or "", "create_time": row.get("create_time"),
        "duration_ms": video.get("duration") or row.get("duration"),
        "aweme_type": row.get("aweme_type", 0), "is_video": is_video,
        "is_pinned": bool(row.get("is_top") or row.get("is_top_video")),
        "author": {key: author.get(key) for key in ("uid", "sec_uid", "nickname", "unique_id", "short_id")},
    }
    work.update(WORK_METRICS.statistics_extension(row, captured_at=captured_at))
    return work


def public_projection(payload, works):
    """Persist only public target-work metadata, never headers or request tokens."""
    module = payload.get("not_login_module") or {}
    rows = []
    for work in works:
        row = {"aweme_id": work["video_id"], "desc": work["title"],
               "create_time": work["create_time"], "aweme_type": work["aweme_type"],
               "is_top": work["is_pinned"], "author": work["author"],
               "video": {"duration": work["duration_ms"]}}
        row.update({key: work[key] for key in WORK_METRICS.STATISTICS_EXTENSION_KEYS if key in work})
        if not work["is_video"]:
            row["images"] = [{"public_image_work": True}]
        rows.append(row)
    return {"status_code": payload["status_code"], "has_more": payload["has_more"],
            "max_cursor": payload["max_cursor"], "aweme_list": rows,
            "not_login_module": {"guide_login_tip_exist": bool(module.get("guide_login_tip_exist"))}}


class BrowserCatalog:
    """Validate response pages independently of browser event arrival order."""
    def __init__(self, sec_uid, output):
        self.sec_uid = sec_uid
        self.output = Path(output).resolve()
        self.started = now()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.pages_dir = self.output.parent / ("browser-pages-" + stamp)
        self.had_existing_output = self.output.exists()
        self.attempt_catalog = self.output.with_name(self.output.stem + "-capture-" + stamp + ".json")
        self.pages = {}
        self.errors = []
        self.revision = 0
        self.last_public_page = 0.0

    def add(self, requested_cursor, payload, raw):
        if not isinstance(payload, dict):
            raise CaptureValidationError("payload_not_object")
        if payload.get("status_code") != 0:
            raise CaptureValidationError("status_code_nonzero")
        if not isinstance(payload.get("aweme_list"), list):
            raise CaptureValidationError("aweme_list_wrong_shape")
        if not isinstance(payload.get("has_more"), (int, bool)) or payload["has_more"] not in (0, 1):
            raise CaptureValidationError("has_more_invalid")
        returned = payload.get("max_cursor")
        if isinstance(returned, bool) or not re.fullmatch(r"\d+", str(returned)):
            raise CaptureValidationError("cursor_invalid")
        returned = int(returned)
        captured_at = now()
        works = [normalize_work(row, self.sec_uid, captured_at=captured_at) for row in payload["aweme_list"]]
        if requested_cursor == 0 and not works:
            raise CaptureValidationError("empty_first_page")
        if payload.get("not_login_module") is not None and not isinstance(payload.get("not_login_module"), dict):
            raise CaptureValidationError("login_module_wrong_shape")
        filtered = bool((payload.get("not_login_module") or {}).get("guide_login_tip_exist"))
        old = self.pages.get(requested_cursor)
        # Pre-login failures do not belong to a newly established valid first page.
        if requested_cursor == 0 and old is None:
            self.errors = []
        # A real login can refresh the first page. Discard the earlier guest chain.
        if requested_cursor == 0 and old and old["filtered"] and not filtered:
            self.pages = {}
            self.errors = []
            old = None
        semantic = ([work["video_id"] for work in works], returned, bool(payload["has_more"]), filtered)
        if old:
            if old["semantic"] == semantic:
                return False
            raise CaptureValidationError("cursor_conflict")
        if payload["has_more"] and returned == requested_cursor:
            raise CaptureValidationError("cursor_did_not_advance")
        projected = public_projection(payload, works)
        page_file = self.pages_dir / (str(requested_cursor) + ".json")
        atomic_json(page_file, projected)
        self.pages[requested_cursor] = {
            "requested_cursor": requested_cursor, "returned_cursor": returned,
            "has_more": bool(payload["has_more"]), "works": works, "filtered": filtered,
            "semantic": semantic, "captured_at": captured_at, "file": str(page_file),
            "payload_sha256": hashlib.sha256(raw).hexdigest(),
            "saved_file_sha256": hashlib.sha256(page_file.read_bytes()).hexdigest(),
        }
        self.revision += 1
        if not filtered:
            self.last_public_page = time.monotonic()
        return True

    def catalog(self):
        cursor, seen_cursors, chain, seen = 0, set(), [], {}
        errors = list(self.errors)
        exhausted = False
        while cursor in self.pages:
            if cursor in seen_cursors:
                errors.append("Pagination cursor cycle")
                break
            seen_cursors.add(cursor)
            page = self.pages[cursor]
            fresh = 0
            for work in page["works"]:
                if work["video_id"] not in seen:
                    seen[work["video_id"]] = {**work, "catalog_page": len(chain) + 1}
                    fresh += 1
            chain.append({key: page[key] for key in ("requested_cursor", "returned_cursor", "has_more",
                         "captured_at", "file", "payload_sha256", "saved_file_sha256")})
            chain[-1].update({"page": len(chain), "status_code": 0,
                            "returned_works": len(page["works"]), "new_unique_works": fresh,
                            "all_authors_match_sec_uid": True,
                            "not_login_module": {"guide_login_tip_exist": page["filtered"]},
                            "endpoint": POST_URL, "saved_payload_kind": "public_metadata_projection"})
            cursor = page["returned_cursor"]
            if not page["has_more"]:
                exhausted = True
                break
            if fresh == 0:
                errors.append("Nonterminal page contains no new unique works")
                break
        filtered = any(page["not_login_module"]["guide_login_tip_exist"] for page in chain)
        videos = [work for work in seen.values() if work["is_video"]]
        others = [work for work in seen.values() if not work["is_video"]]
        complete = bool(videos) and exhausted and not filtered and not errors
        creator = next(iter(seen.values()), {}).get("author", {})
        return {"schema_version": 1, "creator": {"nickname": creator.get("nickname", ""),
                "sec_uid": self.sec_uid, "profile_url": "https://www.douyin.com/user/" + self.sec_uid},
                "method": "official_browser_sdk_response_capture", "authentication": "isolated_browser_session",
                "collected_at": self.started, "updated_at": now(), "catalog_complete": complete,
                "complete": complete, "pagination_exhausted": exhausted,
                "catalog_visibility": "login_filtered" if filtered else "public",
                "videos": videos, "non_video_works": others, "video_count": len(videos),
                "non_video_work_count": len(others), "work_count": len(seen),
                "page_evidence": chain, "has_more": not exhausted, "cursor": cursor,
                "unlinked_response_count": len(self.pages) - len(chain), "errors": errors,
                "cookie_exported": False, "request_headers_saved": False, "signed_urls_saved": False}

    def save(self):
        value = self.catalog()
        # Preserve an existing output if no independently validated first page exists.
        if value["page_evidence"]:
            target = self.attempt_catalog if self.had_existing_output and not value["catalog_complete"] else self.output
            atomic_json(target, value)
            value["active_attempt_catalog"] = str(target)
        return value


def verify_isolated_browser(args):
    parsed = urlparse(args.cdp)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise ValueError("CDP must be an explicit http://127.0.0.1:PORT endpoint")
    if not args.user_data_dir and not args.browser_pid:
        return parsed.port
    if not args.user_data_dir or not args.browser_pid:
        raise ValueError("Provide both isolated browser PID and user-data directory")
    directory = args.user_data_dir.expanduser().resolve()
    regular = [Path.home() / "Library/Application Support/Microsoft Edge",
               Path.home() / "Library/Application Support/Google/Chrome"]
    if any(directory == path or path in directory.parents for path in regular):
        raise ValueError("Refused a regular browser user-data directory")
    if not directory.is_dir():
        raise ValueError("The isolated browser directory must already exist")
    result = subprocess.run(["ps", "-p", str(args.browser_pid), "-o", "command="], capture_output=True, text=True)
    command = result.stdout.strip()
    directory_flag = "--user-data-dir=" + str(directory)
    directory_matches = re.search(re.escape(directory_flag) + r"(?=$| --)", command)
    port_matches = re.search(re.escape("--remote-debugging-port=" + str(parsed.port)) + r"(?=\s|$)", command)
    random_port = re.search(r"--remote-debugging-port=0(?=\s|$)", command)
    if result.returncode or not directory_matches or not (port_matches or random_port):
        raise ValueError("Browser PID does not match the explicit isolated directory and CDP port")
    if random_port:
        try:
            first_line = (directory / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError):
            raise ValueError("The isolated browser has no usable active-port file") from None
        if first_line != str(parsed.port):
            raise ValueError("The isolated browser active-port file does not match the CDP endpoint")
    listeners = subprocess.run(["lsof", "-nP", "-a", "-p", str(args.browser_pid),
                               "-iTCP:" + str(parsed.port), "-sTCP:LISTEN", "-Fn"],
                              capture_output=True, text=True)
    addresses = [line[1:] for line in listeners.stdout.splitlines() if line.startswith("n")]
    allowed = {"127.0.0.1:" + str(parsed.port), "[::1]:" + str(parsed.port)}
    if listeners.returncode or not addresses or any(address not in allowed for address in addresses):
        raise ValueError("The owned browser CDP port is not restricted to loopback listeners")
    return parsed.port


SCROLL = """pixels => {
  const links = Array.from(document.querySelectorAll('a[href*="/video/"]'));
  const candidates = Array.from(document.querySelectorAll('main,section,div')).filter(el => {
    const rect = el.getBoundingClientRect();
    return rect.width > 300 && rect.height > 200 && el.scrollHeight > el.clientHeight + 100 &&
      ['auto', 'scroll'].includes(getComputedStyle(el).overflowY);
  });
  candidates.sort((a,b) => links.filter(x => b.contains(x)).length - links.filter(x => a.contains(x)).length);
  const target = candidates[0] || document.scrollingElement;
  if (target) target.scrollBy({top: pixels, behavior: 'instant'});
  return {scroll_target_found: !!target};
}"""


async def has_visible(locator):
    for index in range(await locator.count()):
        if await locator.nth(index).is_visible():
            return True
    return False


async def login_panel_observed(page):
    """Require visible login contents; a successful button click is insufficient."""
    marker = re.compile(r"^(扫码登录|验证码登录|手机号登录|密码登录|打开抖音.*扫码.*|使用抖音.*扫码.*|.*二维码.*登录.*)$")
    for frame in page.frames:
        if frame != page.main_frame and not await (await frame.frame_element()).is_visible():
            continue
        if not await has_visible(frame.get_by_text(marker, exact=True)):
            continue
        # An actual QR widget or phone/password entry must accompany login text.
        qr = frame.locator('canvas, img[alt*="二维码"], img[src*="qrcode" i], img[src*="qr_code" i]')
        fields = frame.locator('input[type="tel"], input[type="password"], input[autocomplete="tel"], input[placeholder*="手机号"]')
        if await has_visible(qr) or await has_visible(fields):
            return True
    return False


async def show_normal_login(page, output):
    """Open one unambiguous standard login control; the user handles sign-in."""
    await page.wait_for_timeout(2000)
    controls = page.get_by_role("button", name="登录", exact=True).or_(
        page.get_by_role("link", name="登录", exact=True))
    visible = []
    for index in range(await controls.count()):
        control = controls.nth(index)
        if await control.is_visible():
            visible.append(control)
    if len(visible) != 1:
        print(json.dumps({"event": "normal_login_click_required", "visible_exact_controls": len(visible),
                          "instruction": "请在专用抖音窗口手动点击登录，完成正常登录或验证码"}, ensure_ascii=False), flush=True)
        return
    try:
        await visible[0].click(timeout=10000)
        await page.wait_for_timeout(2000)
        observed = False
        # A cold dedicated profile can need 20–30 seconds for login SDK assets.
        for _ in range(60):
            if await login_panel_observed(page):
                observed = True
                break
            await page.wait_for_timeout(500)
        path = output.parent / "browser-login.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create restrictive permissions before screenshot bytes are written.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        await page.screenshot(path=str(path), full_page=False)
        path.chmod(0o600)
        print(json.dumps({"event": "normal_login_panel_shown" if observed else "normal_login_panel_not_observed",
                          "screenshot": str(path)}, ensure_ascii=False), flush=True)
    except Exception:
        print(json.dumps({"event": "normal_login_click_required",
                          "instruction": "请在专用抖音窗口手动点击登录，完成正常登录或验证码"}, ensure_ascii=False), flush=True)


async def run_browser(args):
    verify_isolated_browser(args)
    sec_uid = resolve_profile(args.profile)
    from playwright.async_api import async_playwright
    recorder = BrowserCatalog(sec_uid, args.output)
    tasks = set()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(args.cdp, no_defaults=True)
        if len(browser.contexts) != 1:
            raise ValueError("Expected exactly one isolated browser context")
        context = browser.contexts[0]
        page = next((page for page in context.pages if urlparse(page.url).hostname == "www.douyin.com"), None)
        if page is None:
            page = await context.new_page()

        async def response_received(response):
            cursor = official_request_cursor(response.url, sec_uid)
            if cursor is None or response.request.method != "GET":
                return
            if response.status != 200:
                recorder.errors.append("Official authorpost response was not HTTP 200")
                print(json.dumps({"event": "authorpost_http_failure", "requested_cursor": cursor,
                                  "http_status": response.status}), flush=True)
                return
            payload = None
            try:
                raw = await response.body()
                payload = json.loads(raw)
                if recorder.add(cursor, payload, raw):
                    catalog = recorder.save()
                    print(json.dumps({"event": "browser_page", "requested_cursor": cursor,
                                      "linked_pages": len(catalog["page_evidence"]),
                                      "videos": catalog["video_count"], "catalog_complete": catalog["catalog_complete"],
                                      "filtered": catalog["catalog_visibility"] == "login_filtered"}), flush=True)
            except CaptureValidationError as error:
                recorder.errors.append("authorpost_validation_failed:" + error.reason)
                print(json.dumps({"event": "authorpost_validation_failed", "requested_cursor": cursor,
                                  "reason": error.reason, **safe_response_diagnostics(payload)}), flush=True)
            except json.JSONDecodeError:
                recorder.errors.append("authorpost_validation_failed:body_not_json")
                print(json.dumps({"event": "authorpost_validation_failed", "requested_cursor": cursor,
                                  "reason": "body_not_json"}), flush=True)
            except (ValueError, TypeError, KeyError):
                recorder.errors.append("authorpost_validation_failed:unexpected_shape")
                print(json.dumps({"event": "authorpost_validation_failed", "requested_cursor": cursor,
                                  "reason": "unexpected_shape", **safe_response_diagnostics(payload)}), flush=True)
            except Exception:
                recorder.errors.append("Official authorpost response body was unavailable")

        def schedule(response):
            task = asyncio.create_task(response_received(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        context.on("response", schedule)
        started = time.monotonic()
        navigation_retries = 0
        consecutive_navigation_retries = 0
        try:
            await page.goto("https://www.douyin.com/user/" + sec_uid, wait_until="domcontentloaded", timeout=60000)
            if args.show_login:
                await show_normal_login(page, args.output)
            for _ in range(args.max_scrolls):
                await page.wait_for_timeout(args.interval * 1000)
                value = recorder.catalog()
                if value["catalog_complete"]:
                    break
                if len(recorder.pages) >= args.max_pages:
                    recorder.errors.append("Browser page limit reached before a validated terminal")
                    break
                if time.monotonic() - started > args.timeout:
                    recorder.errors.append("Browser capture duration limit reached")
                    break
                if (recorder.last_public_page and value["catalog_visibility"] != "login_filtered"
                        and time.monotonic() - recorder.last_public_page > args.idle_timeout):
                    recorder.errors.append("No advancing official page before idle timeout; completeness unproven")
                    break
                try:
                    await page.evaluate(SCROLL, args.scroll_pixels)
                    consecutive_navigation_retries = 0
                except Exception as error:
                    kind = browser_error_kind(error)
                    if kind == "navigation_transient":
                        navigation_retries += 1
                        consecutive_navigation_retries += 1
                        if navigation_retries <= 10 and consecutive_navigation_retries <= 3:
                            print(json.dumps({"event": "browser_navigation_wait", "retry": navigation_retries}), flush=True)
                            continue
                        recorder.errors.append("Browser navigation transient retry limit reached")
                    elif kind == "target_closed":
                        recorder.errors.append("The dedicated browser target was closed")
                    else:
                        recorder.errors.append("Official creator page scroll failed")
                    break
            else:
                recorder.errors.append("Browser scroll limit reached")
        except Exception:
            recorder.errors.append("Official creator page could not be navigated or scrolled")
        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)
        context.remove_listener("response", schedule)
        value = recorder.save()
        if not value["page_evidence"]:
            attempt = args.output.with_name(args.output.stem + "-attempt-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
            atomic_json(attempt, {"attempt_status": "failed", "stage": "browser_capture",
                        "at": now(), "errors": value["errors"] or ["No validated official first page"],
                        "retained_existing_catalog": args.output.exists()})
        print(json.dumps({"event": "browser_capture_finished", "catalog": str(args.output),
                          "catalog_complete": value["catalog_complete"], "videos": value["video_count"],
                          "active_attempt_catalog": value.get("active_attempt_catalog"),
                          "errors": value["errors"]}, ensure_ascii=False), flush=True)
        # The parent owns the browser process. Never close it from this collector.
        return 0 if value["catalog_complete"] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="canonical official creator URL or sec_uid")
    parser.add_argument("--cdp", required=True)
    parser.add_argument("--browser-pid", type=int, help="optional launcher-owned isolated browser PID")
    parser.add_argument("--user-data-dir", type=Path, help="optional launcher-owned isolated browser directory; requires --browser-pid")
    parser.add_argument("--output", required=True, type=Path, help="use a staging browser-catalog.json until independently verified")
    parser.add_argument("--timeout", type=float, default=600, help="total duration including ordinary manual login waiting")
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--show-login", action="store_true", help="open one unique standard login control once; user completes login")
    parser.add_argument("--idle-timeout", type=float, default=60)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--scroll-pixels", type=int, default=1500)
    parser.add_argument("--max-scrolls", type=int, default=600)
    args = parser.parse_args()
    args.output = args.output.expanduser().resolve()
    try:
        return asyncio.run(run_browser(args))
    except ImportError:
        print(json.dumps({"error": "Playwright is required for isolated browser collection"}), flush=True)
        return 3
    except Exception:
        print(json.dumps({"error": "Isolated browser configuration or CDP connection failed"}), flush=True)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
