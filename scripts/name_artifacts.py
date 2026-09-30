#!/usr/bin/env python3
"""Offline, checkpointed naming of existing verified creator artifacts.

The controller calls reconcile_names while holding its pipeline lock. The CLI
also takes that lock. Both routes exclude downloader and ASR writers. A durable
journal records verified bytes before any new filename is exposed. Same-directory
hard links provide a no-overwrite move: commit pointers, then remove old names.
No downloading, decoding, ASR, or machine-text rewriting is performed here.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any


JOURNAL_NAME = "_artifact-name-journal.json"
SCHEMA_VERSION = 1
ID_PATTERN = re.compile(r"\d{16,22}")
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
METRIC_KEYS = ("digg_count", "comment_count", "collect_count", "share_count")
METADATA_KEYS = ("statistics", "statistics_captured_at", "statistics_availability", "create_time")


def _helper(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / filename)
    if not spec or not spec.loader:
        raise RuntimeError("Local naming helper is unavailable: " + filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PATHS = _helper("artifact_paths.py", "ah_artifact_naming_paths")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def atomic_json(path: Path, payload: dict) -> None:
    _check_regular(path, missing=True)
    descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        _fsync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def _check_regular(path: Path, missing: bool = False) -> None:
    if path.is_symlink():
        raise ValueError("Naming paths may not be symbolic links: " + str(path))
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if missing:
            return
        raise ValueError("Naming source file is missing: " + str(path)) from None
    if not stat.S_ISREG(mode):
        raise ValueError("Naming paths must be regular files: " + str(path))


def _safe_path(value: Any, boundary: Path, suffix: str, *, missing: bool = False) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("Naming metadata needs a nonempty local file path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = boundary / path
    path = Path(os.path.abspath(path))
    try:
        relative = path.relative_to(boundary)
    except ValueError:
        raise ValueError("Naming file path leaves its artifact directory") from None
    current = boundary
    if current.is_symlink():
        raise ValueError("Naming directories may not be symbolic links")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Naming paths may not traverse symbolic links")
    if path.suffix.lower() != suffix:
        raise ValueError("Naming artifact has an unexpected extension")
    _check_regular(path, missing=missing)
    return path


def _json(path: Path, *, missing: bool = False) -> dict:
    _check_regular(path, missing=missing)
    if not path.exists() and missing:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("Naming metadata JSON is corrupt: " + str(path)) from exc
    if not isinstance(value, dict):
        raise ValueError("Naming metadata must be an object: " + str(path))
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_file(path: Path, digest: Any, size: Any = None) -> None:
    _check_regular(path)
    if not isinstance(digest, str) or not HASH_PATTERN.fullmatch(digest):
        raise ValueError("Naming source checksum is missing")
    before = path.stat()
    if before.st_size <= 0 or (size is not None and before.st_size != size) or sha256_file(path) != digest:
        raise ValueError("Naming source size/hash mismatch: " + str(path))
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("Naming source changed during verification")


def _immutable_evidence_hash(evidence: dict) -> str:
    value = copy.deepcopy(evidence)
    value.pop("public_statistics", None)
    source = value.get("source_media")
    if isinstance(source, dict):
        source.pop("path", None)
    outputs = value.get("outputs")
    if isinstance(outputs, dict):
        for item in outputs.values():
            if isinstance(item, dict):
                item.pop("path", None)
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


@contextmanager
def _file_lock(path: Path, message: str):
    if path.parent.is_symlink():
        raise ValueError("Naming lock directory may not be a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    _check_regular(path, missing=True)
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(message) from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _library_root(root: Path) -> Path:
    root = Path(root).expanduser()
    if root.is_symlink():
        raise ValueError("Naming library root may not be a symbolic link")
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("Naming requires an existing creator library")
    for name in ("media", "local-transcripts"):
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError("Naming artifact directory is unsafe")
    return root


def _catalog(catalog_path: Path) -> tuple[dict, str]:
    _check_regular(catalog_path)
    raw = catalog_path.read_bytes()
    value = json.loads(raw)
    videos = value.get("videos") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("catalog_complete") is not True
            or not isinstance(videos, list) or not videos):
        raise ValueError("Naming requires a complete official creator catalog")
    ids = [row.get("video_id") if isinstance(row, dict) else None for row in videos]
    if (any(not isinstance(video_id, str) or not ID_PATTERN.fullmatch(video_id) for video_id in ids)
            or len(ids) != len(set(ids))):
        raise ValueError("Naming catalog IDs must be unique verified video identities")
    exporter = _helper("export_transcripts.py", "ah_naming_catalog_validation")
    exporter.validated_pagination(value, catalog_path)
    if catalog_path.read_bytes() != raw:
        raise ValueError("Naming catalog changed during pagination verification")
    return value, hashlib.sha256(raw).hexdigest()


def _inputs(root: Path) -> tuple[dict, dict]:
    manifest = _json(root / "media" / "download-manifest.json", missing=True)
    state = _json(root / "local-transcripts" / "_batch-state.json", missing=True)
    if not isinstance(manifest.get("videos", {}), dict) or not isinstance(state.get("jobs", {}), dict):
        raise ValueError("Naming manifest/state has an invalid entries object")
    return manifest, state


def _checked_stem(stem: Any, video_id: str) -> str:
    if (not isinstance(stem, str) or not stem or len(stem.encode("utf-8")) > 240
            or any(character in stem for character in '/\\\x00')
            or stem in (".", "..") or not stem.endswith(("-" + video_id, "_" + video_id))):
        raise ValueError("Metric filename stem is unsafe or lacks the stable video ID suffix")
    return stem


def _file_plan(kind: str, path: Path, target: Path, digest: str) -> dict:
    _verify_file(path, digest)
    if target.parent != path.parent:
        raise ValueError("Naming only moves files within their existing directory")
    if target != path and (target.exists() or target.is_symlink()):
        raise FileExistsError("Naming target already exists; preserved without overwrite: " + str(target))
    return {"kind": kind, "source": str(path), "target": str(target),
            "sha256": digest, "bytes": path.stat().st_size}


def _build_journal(root: Path, catalog_path: Path, catalog: dict, digest: str) -> dict:
    manifest, state = _inputs(root)
    metrics = _helper("work_metrics.py", "ah_artifact_filename_metrics")
    summary = {"catalog_video_count": len(catalog["videos"]), "renamed": 0, "renamed_videos": 0,
               "metadata_updated": 0, "skipped": 0, "unavailable": 0, "unknown_videos": 0,
               "unknown_metric_values": 0}
    items = []
    for work in catalog["videos"]:
        video_id = work["video_id"]
        if "statistics" not in work:
            summary["unavailable"] += 1
            summary["skipped"] += 1
            continue
        statistics = work["statistics"]
        if not isinstance(statistics, dict):
            raise ValueError("Verified catalog statistics must be an object")
        unknown = sum(statistics.get(key) is None for key in METRIC_KEYS)
        summary["unknown_metric_values"] += unknown
        summary["unknown_videos"] += bool(unknown)
        entry = manifest.get("videos", {}).get(video_id, {})
        if not isinstance(entry, dict) or entry.get("status") != "verified":
            summary["skipped"] += 1
            continue
        stem = _checked_stem(metrics.metric_filename_stem(work), video_id)
        media = _safe_path(entry.get("path"), root / "media", ".mp4")
        files = [_file_plan("media", media, media.with_name(stem + ".mp4"), entry.get("sha256"))]
        directory = root / "local-transcripts" / video_id
        if directory.is_symlink():
            raise ValueError("ASR video ID directory may not be a symbolic link")
        evidence_path = directory / PATHS.OUTPUT_NAMES["evidence"]
        evidence = _json(evidence_path, missing=True)
        immutable_hash = None
        if evidence:
            if evidence.get("video_id") != video_id or evidence.get("status") != "machine_draft_saved":
                raise ValueError("Naming ASR evidence identity/status mismatch for " + video_id)
            source = evidence.get("source_media", {})
            if (not isinstance(source, dict) or source.get("sha256") != entry.get("sha256")
                    or source.get("bytes") != files[0]["bytes"]
                    or not isinstance(source.get("path"), str)
                    or _safe_path(source["path"], root / "media", ".mp4") != media):
                raise ValueError("Naming ASR media evidence differs from the verified manifest")
            paths = PATHS.resolve_artifact_paths(directory, evidence)
            for kind, suffix in (("markdown", ".md"), ("srt", ".srt")):
                output = evidence.get("outputs", {}).get(kind, {})
                files.append(_file_plan(kind, paths[kind], directory / (stem + suffix), output.get("sha256")))
            immutable_hash = _immutable_evidence_hash(evidence)
        public_statistics = {key: copy.deepcopy(work[key]) for key in METADATA_KEYS if key in work}
        public_statistics["source_catalog_sha256"] = digest
        naming = {"schema_version": SCHEMA_VERSION, "stem": stem, "source_catalog_sha256": digest}
        patch = {key: copy.deepcopy(work[key]) for key in METADATA_KEYS if key in work}
        patch.update({"path": files[0]["target"], "artifact_naming": naming})
        changed_files = sum(file["source"] != file["target"] for file in files)
        changed_metadata = any(entry.get(key) != value for key, value in patch.items())
        if evidence:
            changed_metadata = changed_metadata or evidence.get("public_statistics") != public_statistics
            changed_metadata = changed_metadata or evidence["source_media"].get("path") != files[0]["target"]
            changed_metadata = changed_metadata or any(evidence.get("outputs", {}).get(file["kind"], {}).get("path")
                                                        != file["target"] for file in files[1:])
        job = state.get("jobs", {}).get(video_id, {})
        if isinstance(job, dict) and "media_path" in job:
            changed_metadata = changed_metadata or job["media_path"] != files[0]["target"]
        if not changed_files and not changed_metadata:
            summary["skipped"] += 1
            continue
        summary["renamed"] += changed_files
        summary["renamed_videos"] += bool(changed_files)
        summary["metadata_updated"] += 1
        items.append({"video_id": video_id, "stem": stem, "files": files, "manifest_patch": patch,
                      "evidence_path": str(evidence_path) if evidence else None,
                      "evidence_immutable_sha256": immutable_hash, "public_statistics": public_statistics})
    return {"schema_version": SCHEMA_VERSION, "library_root": str(root), "created_at": _now(),
            "catalog_path": str(catalog_path), "catalog_sha256": digest, "summary": summary, "items": items}


def _checked_journal(root: Path, value: dict) -> dict:
    if (value.get("schema_version") != SCHEMA_VERSION or value.get("library_root") != str(root)
            or not isinstance(value.get("items"), list) or not isinstance(value.get("summary"), dict)
            or not isinstance(value.get("catalog_sha256"), str) or not HASH_PATTERN.fullmatch(value["catalog_sha256"])):
        raise ValueError("Naming journal belongs to a different or unsupported library")
    seen_ids, seen_targets = set(), set()
    for item in value["items"]:
        video_id = item.get("video_id") if isinstance(item, dict) else None
        if not isinstance(video_id, str) or not ID_PATTERN.fullmatch(video_id) or video_id in seen_ids:
            raise ValueError("Naming journal has an invalid video identity")
        seen_ids.add(video_id)
        stem = _checked_stem(item.get("stem"), video_id)
        files = item.get("files")
        kinds = [file.get("kind") for file in files] if isinstance(files, list) and all(isinstance(file, dict) for file in files) else []
        if kinds not in (["media"], ["media", "markdown", "srt"]):
            raise ValueError("Naming journal has an invalid artifact set")
        for file in files:
            kind = file["kind"]
            suffix = ".mp4" if kind == "media" else PATHS.SUFFIXES[kind]
            boundary = root / "media" if kind == "media" else root / "local-transcripts" / video_id
            source = _safe_path(file.get("source"), boundary, suffix, missing=True)
            target = _safe_path(file.get("target"), boundary, suffix, missing=True)
            if target.parent != source.parent or target.name != stem + suffix or target in seen_targets:
                raise ValueError("Naming journal has an unsafe or duplicate target")
            if not isinstance(file.get("sha256"), str) or not HASH_PATTERN.fullmatch(file["sha256"]):
                raise ValueError("Naming journal lacks an artifact checksum")
            if not isinstance(file.get("bytes"), int) or isinstance(file["bytes"], bool) or file["bytes"] <= 0:
                raise ValueError("Naming journal has an invalid artifact size")
            seen_targets.add(target)
        patch = item.get("manifest_patch")
        if (not isinstance(patch, dict) or set(patch) - {*METADATA_KEYS, "path", "artifact_naming"}
                or patch.get("path") != files[0]["target"]):
            raise ValueError("Naming journal has an invalid manifest patch")
        expected_evidence = root / "local-transcripts" / video_id / PATHS.OUTPUT_NAMES["evidence"]
        if (len(files) == 3 and (item.get("evidence_path") != str(expected_evidence)
                               or not isinstance(item.get("evidence_immutable_sha256"), str)
                               or not HASH_PATTERN.fullmatch(item["evidence_immutable_sha256"]))) \
                or (len(files) == 1 and item.get("evidence_path") is not None):
            raise ValueError("Naming journal has an invalid evidence pointer")
        if not isinstance(item.get("public_statistics"), dict):
            raise ValueError("Naming journal has an invalid metrics snapshot")
    return value


def _metadata_inputs(root: Path, items: list[dict]) -> tuple[dict, dict, dict[str, dict]]:
    manifest, state = _inputs(root)
    evidence_by_id = {}
    for item in items:
        video_id, media = item["video_id"], item["files"][0]
        entry = manifest.get("videos", {}).get(video_id, {})
        if (not isinstance(entry, dict) or entry.get("status") != "verified"
                or entry.get("sha256") != media["sha256"]):
            raise ValueError("Naming journal no longer matches the verified download manifest")
        entry_path = _safe_path(entry.get("path"), root / "media", ".mp4", missing=True)
        if str(entry_path) not in (media["source"], media["target"]):
            raise ValueError("Naming journal no longer matches the verified media pointer")
        if item["evidence_path"]:
            evidence = _json(Path(item["evidence_path"]))
            if _immutable_evidence_hash(evidence) != item["evidence_immutable_sha256"]:
                raise ValueError("ASR evidence changed after the naming plan; preserved unchanged")
            source_path = _safe_path(evidence.get("source_media", {}).get("path"), root / "media", ".mp4", missing=True)
            if str(source_path) not in (media["source"], media["target"]):
                raise ValueError("Naming journal no longer matches the ASR media pointer")
            output_paths = PATHS.resolve_artifact_paths(Path(item["evidence_path"]).parent, evidence)
            for file in item["files"][1:]:
                if str(output_paths[file["kind"]]) not in (file["source"], file["target"]):
                    raise ValueError("Naming journal no longer matches the ASR output pointer")
            evidence_by_id[video_id] = evidence
    return manifest, state, evidence_by_id


def _check_planned_files(items: list[dict]) -> None:
    for item in items:
        for file in item["files"]:
            source, target = Path(file["source"]), Path(file["target"])
            existing = list(dict.fromkeys(path for path in (source, target) if path.exists() or path.is_symlink()))
            if not existing:
                raise ValueError("Both names of a journaled artifact are missing")
            for path in existing:
                _verify_file(path, file["sha256"], file["bytes"])


def _link_targets(items: list[dict]) -> None:
    for item in items:
        for file in item["files"]:
            source, target = Path(file["source"]), Path(file["target"])
            if source == target or target.exists() or target.is_symlink():
                _verify_file(target, file["sha256"], file["bytes"])
                continue
            _verify_file(source, file["sha256"], file["bytes"])
            # Same-directory links cannot cross filesystems and cannot overwrite.
            os.link(source, target, follow_symlinks=False)
            _fsync_directory(target.parent)
            _verify_file(target, file["sha256"], file["bytes"])


def _commit_metadata(root: Path, items: list[dict]) -> None:
    manifest, state, evidence_by_id = _metadata_inputs(root, items)
    state_changed = False
    for item in items:
        video_id, media = item["video_id"], item["files"][0]
        manifest["videos"][video_id].update(copy.deepcopy(item["manifest_patch"]))
        evidence = evidence_by_id.get(video_id)
        if evidence is not None:
            evidence["source_media"]["path"] = media["target"]
            for file in item["files"][1:]:
                evidence["outputs"][file["kind"]]["path"] = file["target"]
            evidence["public_statistics"] = copy.deepcopy(item["public_statistics"])
        job = state.get("jobs", {}).get(video_id, {})
        if isinstance(job, dict) and "media_path" in job and job["media_path"] != media["target"]:
            job["media_path"] = media["target"]
            state_changed = True
    atomic_json(root / "media" / "download-manifest.json", manifest)
    for item in items:
        if item["evidence_path"]:
            atomic_json(Path(item["evidence_path"]), evidence_by_id[item["video_id"]])
    if state_changed:
        atomic_json(root / "local-transcripts" / "_batch-state.json", state)


def _remove_old_names(items: list[dict]) -> None:
    for item in items:
        for file in item["files"]:
            source, target = Path(file["source"]), Path(file["target"])
            _verify_file(target, file["sha256"], file["bytes"])
            if source != target and source.exists():
                _verify_file(source, file["sha256"], file["bytes"])
                source.unlink()
                _fsync_directory(source.parent)


def _finish_journal(root: Path, journal: dict, *, recovered: bool) -> dict:
    _checked_journal(root, journal)
    _metadata_inputs(root, journal["items"])
    _check_planned_files(journal["items"])
    _link_targets(journal["items"])
    _commit_metadata(root, journal["items"])
    _check_planned_files(journal["items"])
    _remove_old_names(journal["items"])
    (root / JOURNAL_NAME).unlink()
    _fsync_directory(root)
    return {"status": "journal_recovered" if recovered else "names_reconciled", "root": str(root),
            "catalog_sha256": journal["catalog_sha256"], "recovered": recovered, **journal["summary"]}


def reconcile_names(root: Path, catalog_path: Path) -> dict:
    """Name existing bytes using a complete catalog; caller holds pipeline lock.

    A pending journal is always completed first and returns immediately. Recovery
    needs neither the current catalog nor the metric formatter. Fresh plans skip
    legacy catalog rows that never recorded a statistics observation.
    """
    root = _library_root(root)
    with _file_lock(root / "media" / "download-manifest.json.lock", "Another downloader holds the manifest lock"), \
            _file_lock(root / "local-transcripts" / ".transcribe.lock", "Another ASR writer holds the transcript lock"):
        journal_path = root / JOURNAL_NAME
        if journal_path.exists() or journal_path.is_symlink():
            return _finish_journal(root, _json(journal_path), recovered=True)
        catalog_path = Path(catalog_path).expanduser().resolve()
        catalog, digest = _catalog(catalog_path)
        journal = _build_journal(root, catalog_path, catalog, digest)
        if not journal["items"]:
            return {"status": "names_unchanged", "root": str(root), "catalog_sha256": digest,
                    "recovered": False, **journal["summary"]}
        atomic_json(journal_path, journal)
        return _finish_journal(root, journal, recovered=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, help="Default: ROOT/catalog/catalog.json")
    args = parser.parse_args(argv)
    root = _library_root(args.root)
    with _file_lock(root / ".pipeline.lock", "Another pipeline holds this creator library"):
        report = reconcile_names(root, args.catalog or root / "catalog" / "catalog.json")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "naming_failed", "message": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
