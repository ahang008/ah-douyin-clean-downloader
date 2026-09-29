#!/usr/bin/env python3
"""Checkpointed authorized Douyin downloads. No LLM, browser, or paid API.

The bundled download_douyin.py supplies the actual metadata parser,
playback source selection, path sanitation, download procedure, and ffprobe.
This file adds a queue, local integrity checks, and a process-local DNS repair.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import quote, urlparse

DEFAULT_BACKEND = Path(__file__).resolve().parent / "download_douyin.py"
VIDEO_ID = re.compile(r"^\d{16,22}$")
VIDEO_PATH = re.compile(r"/(?:video|note)/(\d{16,22})(?:[/?#]|$)")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_url(video_id: str) -> str:
    return "https://www.douyin.com/video/" + video_id


def redact_error(exc: Exception) -> str:
    # Error details can contain credentials in a user-provided proxy URL.
    value = str(exc)
    value = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", value)
    value = re.sub(r"https?://[^\s|]+", "[URL omitted]", value)
    value = re.sub(r"(?i)(cookie|authorization|token|password)\s*[:=]\s*\S+", r"\1=[redacted]", value)
    return value[-800:]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # Persist the directory entry too, where the local filesystem allows it.
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        except OSError:
            pass
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def exclusive_run_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another downloader holds this manifest lock") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def normalize_catalog(payload: Any) -> tuple[list[dict], dict]:
    if isinstance(payload, list):
        rows, meta = payload, {}
    elif isinstance(payload, dict):
        rows = next((payload[key] for key in ("videos", "items", "entries", "aweme_list", "works", "records")
                     if isinstance(payload.get(key), list)), None)
        if rows is None:
            raise ValueError("Catalog needs a videos list (not an error response)")
        meta = {"complete": payload.get("complete", payload.get("catalog_complete")),
                "expected_total": payload.get("expected_total", payload.get("total"))}
    else:
        raise ValueError("Catalog must be an object or list")
    unique = {}
    invalid = 0
    for row in rows:
        if isinstance(row, str):
            row = {"url": row} if row.startswith("https://") else {"video_id": row}
        if not isinstance(row, dict):
            invalid += 1
            continue
        video_id = str(row.get("video_id") or row.get("id") or row.get("aweme_id") or "")
        if not VIDEO_ID.fullmatch(video_id):
            video_id = ""
            for key in ("canonical_url", "source_url", "url"):
                url = str(row.get(key) or "")
                parsed = urlparse(url)
                host = (parsed.hostname or "").lower().rstrip(".")
                if parsed.scheme != "https" or not (host == "douyin.com" or host.endswith(".douyin.com")
                                                     or host == "iesdouyin.com" or host.endswith(".iesdouyin.com")):
                    continue
                match = VIDEO_PATH.search(parsed.path)
                if match:
                    video_id = match.group(1)
                    break
                match = re.search(r"(?:^|&)modal_id=(\d{16,22})(?:&|$)", parsed.query)
                if match:
                    video_id = match.group(1)
                    break
        if not VIDEO_ID.fullmatch(video_id):
            invalid += 1
            continue
        if video_id not in unique:
            # Avoid persisting arbitrary signed links or credentials from input.
            unique[video_id] = {"video_id": video_id, "canonical_url": canonical_url(video_id),
                                "source_url": canonical_url(video_id),
                                "catalog_title": str(row.get("title") or row.get("desc") or "")[:1000]}
    if not unique:
        raise ValueError("Catalog contains no recognized video IDs")
    meta.update({"input_rows": len(rows), "unique_videos": len(unique),
                 "duplicate_rows": len(rows) - invalid - len(unique), "invalid_rows": invalid})
    return list(unique.values()), meta


def load_backend(path: Path):
    if not path.is_file():
        raise RuntimeError("Downloader backend is missing: " + str(path))
    spec = importlib.util.spec_from_file_location("ah_authorized_douyin_backend", path)
    if not spec or not spec.loader:
        raise RuntimeError("Unable to import downloader backend")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProcessDNS:
    """Resolve affected hosts with DoH, without changing any system setting."""
    def __init__(self, mode: str = "auto"):
        self.mode = mode
        self.cache = {}
        self.lock = threading.Lock()

    def needs_repair(self, host: str) -> bool:
        if self.mode == "off":
            return False
        if self.mode == "always":
            return True
        try:
            ips = [ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, 443)]
            return any(ip in ipaddress.ip_network("198.18.0.0/15") for ip in ips if ip.version == 4)
        except socket.gaierror:
            return True

    def addresses(self, host: str) -> list[str]:
        with self.lock:
            if host in self.cache:
                return self.cache[host]
            for resolver in ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve"):
                response = subprocess.run(
                    ["curl", "--silent", "--show-error", "--fail", "--max-time", "15",
                     "--header", "accept: application/dns-json", resolver + "?name=" + quote(host) + "&type=A"],
                    text=True, capture_output=True, check=False)
                try:
                    answers = json.loads(response.stdout).get("Answer", [])
                    ips = []
                    for answer in answers:
                        if answer.get("type") != 1:
                            continue
                        address = ipaddress.ip_address(answer["data"])
                        if address.version == 4 and address.is_global:
                            ips.append(str(address))
                    if response.returncode == 0 and ips:
                        self.cache[host] = ips
                        return ips
                except (ValueError, KeyError, TypeError):
                    pass
            raise RuntimeError("Public DNS resolution failed for " + host)


def install_process_client(backend, dns: ProcessDNS) -> None:
    """Use the original backend with a bounded curl transport adapter.

    A known canonical ID is passed straight to the metadata validation step,
    avoiding an unnecessary public HTML fetch. Short URLs still use HTTPS.
    """
    original = backend.CurlClient

    class BatchCurlClient(original):
        def fetch(self, url, destination, user_agent, timeout=None):
            parsed = urlparse(url)
            if destination.name == "share.html" and VIDEO_PATH.search(parsed.path):
                destination.write_text("", encoding="utf-8")
                return url
            errors = []
            for proxy in self._ordered_proxies():
                resolves = {}
                host = parsed.hostname or ""
                if not proxy and dns.needs_repair(host):
                    try:
                        resolves[host] = dns.addresses(host)
                    except RuntimeError as exc:
                        errors.append(redact_error(exc))
                        continue
                # At most 8 redirect-host discoveries, not a polling loop.
                for redirect_attempt in range(8):
                    destination.unlink(missing_ok=True)
                    command = ["curl", "--location", "--silent", "--show-error", "--fail-with-body",
                               "--compressed", "--proto", "=https", "--proto-redir", "=https",
                               "--connect-timeout", "8", "--max-time", str(timeout or self.timeout),
                               "--retry", "1", "--retry-all-errors", "--user-agent", user_agent,
                               "--output", str(destination), "--write-out", "%{url_effective}"]
                    if proxy:
                        command.extend(["--proxy", proxy])
                    for name, ips in resolves.items():
                        command.extend(["--resolve", name + ":443:" + ",".join(ips)])
                    command.append(url)
                    response = subprocess.run(command, text=True, capture_output=True, check=False)
                    if response.returncode == 0 and destination.exists() and destination.stat().st_size:
                        self.selected_proxy = proxy
                        self.has_selected_proxy = True
                        return response.stdout.strip() or url
                    effective_host = urlparse(response.stdout.strip()).hostname or host
                    if not proxy and effective_host not in resolves and dns.needs_repair(effective_host):
                        try:
                            resolves[effective_host] = dns.addresses(effective_host)
                            continue
                        except RuntimeError as exc:
                            errors.append(redact_error(exc))
                    errors.append("curl exit %s for host %s" % (response.returncode, effective_host))
                    break
            destination.unlink(missing_ok=True)
            raise backend.DownloadError("Network request failed: " + " | ".join(errors[-3:]))

    backend.CurlClient = BatchCurlClient


def under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return not path.is_symlink()
    except ValueError:
        return False


def verify_file(path: Path, root: Path, backend, expected_sha: Optional[str] = None) -> dict:
    if not under_root(path, root) or not path.is_file() or path.stat().st_size < 1024:
        raise RuntimeError("Missing, small, or unsafe media path")
    probe = backend.verify_media(path)
    if probe.get("verification") != "ffprobe" or not probe.get("video_streams"):
        raise RuntimeError("Full ffprobe video verification is required")
    duration = probe.get("duration_seconds")
    if not duration or duration <= 0:
        raise RuntimeError("Media duration is not positive")
    digest = sha256_file(path)
    if expected_sha and digest != expected_sha:
        raise RuntimeError("Saved file checksum no longer matches the manifest")
    if sha256_file(path) != digest:
        raise RuntimeError("Media changed during checksum readback")
    return {"path": str(path.resolve()), "bytes": path.stat().st_size, "sha256": digest,
            "checksum_readback": True, "verified_at": utc_now(), **probe}


class DownloadBatch:
    def __init__(self, catalog: list[dict], catalog_meta: dict, root: Path, manifest_path: Path,
                 backend, args: argparse.Namespace, download_fn: Optional[Callable] = None):
        self.catalog, self.catalog_meta = catalog, catalog_meta
        self.root, self.manifest_path = root.resolve(), manifest_path.resolve()
        self.backend, self.args = backend, args
        self.download_fn = download_fn or backend.download
        self.guard = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if self.manifest.get("schema_version") != 1 or not isinstance(self.manifest.get("videos"), dict):
                raise RuntimeError("Unsupported or corrupt download manifest; preserve it for inspection")
            if Path(self.manifest.get("download_root", "")).resolve() != self.root:
                raise RuntimeError("Manifest belongs to a different download root")
        else:
            self.manifest = {"schema_version": 1, "created_at": utc_now(),
                             "download_root": str(self.root), "videos": {}}
        self.manifest["catalog"] = catalog_meta
        self.manifest["backend_path"] = str(Path(args.backend).resolve())
        self.manifest["runtime"] = {"llm_used": False, "computer_use_used": False, "paid_api_used": False}
        # Build once, so adopting files does not repeatedly scan the directory.
        self.existing = {}
        for file in self.root.rglob("*.mp4"):
            match = re.search(r"-(\d{16,22})(?:-\d+)?\.mp4$", file.name)
            if match:
                self.existing.setdefault(match.group(1), []).append(file)

    def save(self) -> None:
        self.manifest["updated_at"] = utc_now()
        atomic_json(self.manifest_path, self.manifest)

    def update(self, video_id: str, values: dict) -> None:
        with self.guard:
            self.manifest["videos"].setdefault(video_id, {}).update(values)
            self.save()

    def emit(self, event: str, video_id: str, **values) -> None:
        with self.guard:
            print(json.dumps({"event": event, "video_id": video_id, **values}, ensure_ascii=False), flush=True)

    def process(self, row: dict) -> str:
        video_id = row["video_id"]
        entry = dict(self.manifest["videos"].get(video_id, {}))
        self.update(video_id, row)
        if entry.get("status") == "verified" and entry.get("path") and entry.get("sha256"):
            try:
                check = verify_file(Path(entry["path"]), self.root, self.backend, entry["sha256"])
                self.update(video_id, {**check, "last_action": "reverified_existing", "status": "verified"})
                self.emit("already_verified", video_id)
                return "skipped"
            except Exception as exc:
                self.update(video_id, {"status": "stale", "last_error": redact_error(exc),
                                       "previous_path": entry.get("path"), "previous_sha256": entry.get("sha256")})
        # Adopt a completed backend download after a crash before checkpoint.
        for path in self.existing.get(video_id, []):
            recorded_path = entry.get("previous_path") or entry.get("path")
            recorded_sha = entry.get("previous_sha256") or entry.get("sha256")
            try:
                check = verify_file(path, self.root, self.backend,
                                    recorded_sha if str(path.resolve()) == recorded_path else None)
                self.update(video_id, {**check, "status": "verified", "last_action": "adopted_existing",
                                       "transcoded": False, "paid_api_used": False})
                self.emit("adopted_existing", video_id)
                return "adopted"
            except Exception:
                pass
        for attempt in range(self.args.attempts):
            entry = self.manifest["videos"].get(video_id, {})
            self.update(video_id, {"status": "downloading", "started_at": utc_now(),
                                   "attempts": int(entry.get("attempts", 0)) + 1})
            try:
                backend_args = argparse.Namespace(share_text=[row["canonical_url"]], output_dir=str(self.root),
                                                   proxy=self.args.proxy, timeout=self.args.timeout,
                                                   download_timeout=self.args.download_timeout, metadata_only=False)
                result = self.download_fn(backend_args)
                if result.get("status") != "ok" or str(result.get("video_id")) != video_id:
                    raise RuntimeError("Backend result does not match the requested video ID")
                check = verify_file(Path(result["path"]), self.root, self.backend)
                safe_result = {key: result[key] for key in ("title", "author", "source_kind", "transcoded", "paid_api_used")
                               if key in result}
                self.update(video_id, {**safe_result, **check, "status": "verified", "finished_at": utc_now(),
                                       "last_action": "downloaded", "last_error": None})
                self.emit("downloaded", video_id, bytes=check["bytes"], duration_seconds=check["duration_seconds"])
                return "downloaded"
            except Exception as exc:
                self.update(video_id, {"status": "failed", "last_error": redact_error(exc), "failed_at": utc_now()})
                if attempt + 1 < self.args.attempts:
                    time.sleep(min(2 ** attempt, 10))
        self.emit("failed", video_id)
        return "failed"

    def run(self) -> dict:
        rows = self.catalog[:self.args.limit] if self.args.limit else self.catalog
        for row in rows:
            self.update(row["video_id"], row)
        counts = {"downloaded": 0, "skipped": 0, "adopted": 0, "failed": 0}
        with ThreadPoolExecutor(max_workers=self.args.workers) as executor:
            futures = [executor.submit(self.process, row) for row in rows]
            for future in as_completed(futures):
                counts[future.result()] += 1
        counts.update({"selected": len(rows), "catalog_unique": len(self.catalog),
                       "catalog_complete": self.catalog_meta.get("complete"), "manifest_path": str(self.manifest_path),
                       "download_root": str(self.root), "llm_used": False, "computer_use_used": False})
        self.manifest["last_run"] = {"finished_at": utc_now(), **counts}
        self.save()
        return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--download-root", "--output-dir", type=Path, required=True,
                        help="Absolute media library root; original MP4 files are retained")
    parser.add_argument("--manifest", type=Path, help="Default: DOWNLOAD_ROOT/download-manifest.json")
    parser.add_argument("--backend", type=Path, default=DEFAULT_BACKEND)
    parser.add_argument("--limit", type=int, default=0, help="0 means every catalog entry")
    parser.add_argument("--workers", type=int, default=2, help="Download concurrency (1-8)")
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--proxy", help="User-configured proxy; never persisted in the manifest")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--download-timeout", type=int, default=1800)
    parser.add_argument("--dns-mode", choices=("auto", "off", "always"), default="auto",
                        help="auto repairs benchmark-range fake DNS using process-local curl --resolve")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if not args.download_root.is_absolute():
            raise ValueError("--download-root must be an absolute path")
        if args.limit < 0 or not 1 <= args.workers <= 8 or not 1 <= args.attempts <= 5:
            raise ValueError("Invalid limit, workers, or attempts")
        if args.timeout <= 0 or args.download_timeout <= 0:
            raise ValueError("Timeouts must be positive")
        catalog, meta = normalize_catalog(json.loads(args.catalog.read_text(encoding="utf-8")))
        if args.dry_run:
            print(json.dumps({"catalog": meta, "selected": len(catalog[:args.limit] if args.limit else catalog),
                              "video_ids": [row["video_id"] for row in (catalog[:args.limit] if args.limit else catalog)]},
                             ensure_ascii=False, indent=2))
            return 0
        if not shutil.which("ffprobe"):
            raise RuntimeError("ffprobe is required; header-only downloads are not accepted")
        backend = load_backend(args.backend)
        install_process_client(backend, ProcessDNS(args.dns_mode))
        manifest = args.manifest or args.download_root / "download-manifest.json"
        with exclusive_run_lock(manifest.with_suffix(manifest.suffix + ".lock")):
            batch = DownloadBatch(catalog, meta, args.download_root, manifest, backend, args)
            summary = batch.run()
        print(json.dumps({"event": "summary", **summary}, ensure_ascii=False))
        return 1 if summary["failed"] else 0
    except KeyboardInterrupt:
        print(json.dumps({"event": "interrupted", "message": "Checkpoints retained; rerun the same command"}))
        return 130
    except Exception as exc:
        print(json.dumps({"event": "error", "message": redact_error(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
