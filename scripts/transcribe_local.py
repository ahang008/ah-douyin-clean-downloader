#!/usr/bin/env python3
"""Batch local MLX Whisper ASR. No browser, paid API, or online model calls.

Python 3.9+, ffmpeg/ffprobe and mlx-whisper are required. The default operation
only resolves already cached model files; --download-model is an explicit,
one-time Hugging Face model preparation action, not an inference API call.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
SCHEMA_VERSION = 1
OUTPUT_NAMES = {
    "markdown": "01-本地ASR机器逐字稿.md",
    "srt": "01-本地ASR机器逐字稿.srt",
    "evidence": "01-本地ASR识别证据.json",
}
_PATH_SPEC = importlib.util.spec_from_file_location("local_asr_artifact_paths", Path(__file__).resolve().parent / "artifact_paths.py")
_PATH_HELPER = importlib.util.module_from_spec(_PATH_SPEC)
_PATH_SPEC.loader.exec_module(_PATH_HELPER)
MEDIA_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".flac"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_default(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=json_default)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, "time": utc_now(), **fields}, ensure_ascii=False, default=json_default), flush=True)


def safe_video_id(value: Any) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[0-9]{8,25}", text):
        raise ValueError("video_id must be 8–25 digits; do not use a title as a directory name")
    return text


def infer_video_id(path: Path) -> str:
    matches = re.findall(r"(?<![0-9])([0-9]{15,25})(?![0-9])", path.stem)
    if len(matches) != 1:
        raise ValueError(f"cannot infer a unique video_id from {path.name}; provide --video-id")
    return matches[0]


def manifest_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        raise ValueError("manifest must be a JSON object or array")
    if any(key in value for key in ("video_id", "aweme_id")):
        return [value]
    for key in ("videos", "items", "downloads", "results", "entries", "completed", "jobs"):
        rows = value.get(key)
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
        if isinstance(rows, dict):
            return [dict(item, video_id=item.get("video_id") or video_id) for video_id, item in rows.items() if isinstance(item, dict)]
    if isinstance(value.get("data"), (list, dict)):
        return manifest_rows(value["data"])
    raise ValueError("no supported entries list found in manifest")


def read_manifest(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        rows = []
        for line in text.splitlines():
            if line.strip():
                rows.extend(manifest_rows(json.loads(line)))
        return rows
    return manifest_rows(json.loads(text))


def path_from_row(row: dict[str, Any], manifest: Path) -> Path | None:
    containers = [row]
    for key in ("download", "downloaded", "result", "media", "output"):
        if isinstance(row.get(key), dict):
            containers.append(row[key])
    for container in containers:
        for key in ("media_path", "local_path", "file_path", "path", "output_path", "video_path"):
            text = container.get(key)
            if not isinstance(text, str) or not text or "://" in text:
                continue
            path = Path(text).expanduser()
            if path.is_absolute():
                return path.resolve()
            candidates = [(manifest.parent / path).resolve(), (Path.cwd() / path).resolve()]
            return next((item for item in candidates if item.is_file()), candidates[0])
    return None


def jobs_from_manifest(path: Path, filters: set[str] | None = None) -> tuple[list[dict[str, Any]], int]:
    jobs, seen, pending = [], set(), 0
    for row in read_manifest(path):
        video_id = safe_video_id(row.get("video_id") or row.get("aweme_id") or row.get("id"))
        if video_id in seen or (filters and video_id not in filters):
            continue
        seen.add(video_id)
        if row.get("status") in {"failed", "error", "pending", "downloading", "processing", "queued"}:
            pending += 1
            continue
        media_path = path_from_row(row, path)
        # .part downloads and files without a committed media extension are not ready.
        if media_path is None or not media_path.is_file() or media_path.suffix.lower() not in MEDIA_EXTENSIONS:
            pending += 1
            continue
        jobs.append({
            "video_id": video_id, "media_path": str(media_path),
            "expected_media_sha256": row.get("sha256"),
            "title": row.get("title") or row.get("desc") or "",
            "author": row.get("author") or row.get("nickname") or "",
            "source_url": row.get("source_url") or row.get("canonical_url") or row.get("url") or f"https://www.douyin.com/video/{video_id}",
        })
    return jobs, pending


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def resolve_model(model: str, allow_download: bool = False) -> dict[str, Any]:
    local = Path(model).expanduser()
    if local.is_dir():
        resolved = local.resolve()
    else:
        from huggingface_hub import snapshot_download
        resolved = Path(snapshot_download(repo_id=model, local_files_only=not allow_download)).resolve()
    config = resolved / "config.json"
    weights = next((resolved / name for name in ("weights.safetensors", "weights.npz") if (resolved / name).is_file()), None)
    if not config.is_file() or weights is None:
        raise RuntimeError("model cache is incomplete; need config.json and weights.safetensors/weights.npz")
    # The snapshot path from Hugging Face has a revision directory before resolving symlinks.
    snapshot = Path(snapshot_download(repo_id=model, local_files_only=True)) if not local.is_dir() else local
    revision = snapshot.name if snapshot.parent.name == "snapshots" else None
    return {
        "repository": model if not local.is_dir() else None,
        "local_path": str(resolved), "revision": revision,
        "config_sha256": sha256_file(config), "weights_sha256": sha256_file(weights),
        "weights_bytes": weights.stat().st_size,
        "mlx_whisper_version": package_version("mlx-whisper"), "mlx_version": package_version("mlx"),
        "model_download_permitted": allow_download,
        "inference_network_used": False,
    }


def model_key(model: dict[str, Any], settings: dict[str, Any]) -> str:
    stable = {"weights_sha256": model["weights_sha256"], "config_sha256": model["config_sha256"],
              "mlx_whisper_version": model["mlx_whisper_version"], "settings": settings}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def inspect_media(path: Path) -> dict[str, Any]:
    command = ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=index,codec_type,codec_name,duration,sample_rate,channels", "-of", "json", str(path)]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    value = json.loads(result.stdout)
    streams = value.get("streams", [])
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not audio_streams:
        raise ValueError("media has no audio stream")
    durations = [float(item["duration"]) for item in [value.get("format", {}), *audio_streams] if item.get("duration") not in (None, "N/A")]
    if not durations or max(durations) <= 0:
        raise ValueError("media has no usable duration")
    return {"duration_seconds": max(durations), "streams": streams}


def extract_audio(media: Path, output: Path) -> dict[str, Any]:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(media), "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", str(output)], capture_output=True, text=True, check=True)
    import wave
    with wave.open(str(output), "rb") as stream:
        frames = stream.readframes(stream.getnframes())
        result = {"sample_rate": stream.getframerate(), "channels": stream.getnchannels(),
                  "sample_width_bytes": stream.getsampwidth(), "frame_count": stream.getnframes(),
                  "duration_seconds": stream.getnframes() / stream.getframerate()}
    result.update({"wav_sha256": sha256_file(output), "pcm_s16le_sha256": hashlib.sha256(frames).hexdigest(), "temporary_wav_retained": False})
    return result


def srt_time(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("invalid SRT timestamp")
    total = round(seconds * 1000)
    hours, rest = divmod(total, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def machine_quality_flags(result: dict[str, Any], duration: float) -> list[str]:
    """Flag obvious short-clip hallucination patterns without claiming silence."""
    if not 0 < duration <= 120:
        return []
    segments = result.get("segments") or []
    compact = lambda value: re.sub(r"[\s,.!?。，！？、]+", "", str(value or ""))
    text = compact(result.get("text"))
    phrases = [compact(segment.get("text")) for segment in segments if compact(segment.get("text"))]
    flags = []
    if duration >= 12 and len(text) <= 2:
        flags.append("short_clip_has_only_one_or_two_machine_characters")
    if len(phrases) >= 6:
        phrase, count = Counter(phrases).most_common(1)[0]
        if len(phrase) <= 20 and count >= 6 and count / len(phrases) >= 0.75:
            flags.append("short_phrase_repeated_across_clip")
    if len(text) >= 24:
        for size in range(2, min(20, len(text) // 4) + 1):
            unit = text[:size]
            repeats = 0
            while text.startswith(unit, repeats * size):
                repeats += 1
            if repeats >= 4 and repeats * size / len(text) >= 0.8:
                flags.append("short_phrase_repeated_within_machine_text")
                break
    if duration >= 12 and len(text) <= 12 and segments:
        try:
            last_end = max(float(segment["end"]) for segment in segments)
        except (KeyError, TypeError, ValueError):
            last_end = duration
        if last_end <= min(3.0, duration * 0.2):
            flags.append("very_sparse_machine_text_at_clip_start")
    for segment in segments:
        try:
            segment_duration = float(segment["end"]) - float(segment["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if duration >= 15 and segment_duration >= max(12.0, duration * 0.6) and len(compact(segment.get("text"))) <= 20:
            flags.append("long_sparse_machine_segment_in_short_clip")
            break
    return flags


def validate_result(result: dict[str, Any], duration: float) -> dict[str, Any]:
    text = str(result.get("text") or "").strip()
    if not text:
        raise ValueError("ASR returned empty text")
    segments = result.get("segments") or []
    if not segments:
        raise ValueError("ASR returned no timecoded segments")
    previous_start = -1.0
    gaps, low_confidence, max_end = [], [], 0.0
    for index, segment in enumerate(segments):
        start, end = float(segment["start"]), float(segment["end"])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start or start < previous_start:
            raise ValueError(f"invalid/unordered timestamps at segment {index}")
        if end > duration + 1.0:
            raise ValueError(f"segment {index} exceeds decoded audio duration")
        if not str(segment.get("text") or "").strip():
            raise ValueError(f"empty text at segment {index}")
        if start - max_end > 10:
            gaps.append({"before_segment": index, "seconds": round(start - max_end, 3)})
        if float(segment.get("avg_logprob", 0)) < -1.0 or float(segment.get("compression_ratio", 0)) > 2.4:
            low_confidence.append(index)
        max_end = max(max_end, end)
        previous_start = start
    compact = lambda value: re.sub(r"\s+", "", value)
    if compact(text) != compact("".join(str(segment["text"]) for segment in segments)):
        raise ValueError("full text differs from concatenated segments; refusing a partial export")
    tail = max(0.0, duration - max_end)
    warnings = []
    if tail > max(8.0, duration * 0.12):
        warnings.append("long_untranscribed_tail_may_be_silence_or_missed_speech")
    if gaps:
        warnings.append("long_gaps_may_be_silence_or_missed_speech")
    if low_confidence:
        warnings.append("low_confidence_or_repetitive_segments")
    quality_flags = machine_quality_flags(result, duration)
    warnings.extend("machine_quality_review:" + flag for flag in quality_flags)
    return {
        "nonempty": True, "segment_count": len(segments), "character_count": len(text),
        "full_text_matches_segments": True, "timestamps_valid": True,
        "first_segment_start_seconds": float(segments[0]["start"]),
        "last_segment_end_seconds": max_end,
        "audio_duration_seconds": duration, "untranscribed_tail_seconds": round(tail, 3),
        "last_segment_end_fraction": round(min(1.0, max_end / duration), 6),
        "long_gaps": gaps, "low_confidence_segment_indices": low_confidence,
        "warnings": warnings, "quality_flags": quality_flags,
        "machine_quality_review_required": bool(quality_flags),
        "semantic_proofread": False, "human_audio_reviewed": False,
        "completeness_scope": "nonempty, segment/text equality and timestamp bounds; speech accuracy requires review",
    }


def repair_timestamp_from_window(result: dict[str, Any], window: dict[str, Any],
                                 index: int, offset: float, duration: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replace three faulty machine timestamps only when local words match exactly."""
    segments = result.get("segments") or []
    local = window.get("segments") or []
    if index < 1 or index + 1 >= len(segments):
        raise ValueError("timestamp repair needs preceding and following machine segments")
    compact = lambda value: re.sub(r"\s+", "", str(value or ""))
    expected = tuple(compact(segments[index + shift].get("text")) for shift in (-1, 0, 1))
    if any(not text for text in expected):
        raise ValueError("timestamp repair requires three nonempty original machine segments")
    matches = [start for start in range(max(0, len(local) - 2))
               if tuple(compact(local[start + shift].get("text")) for shift in range(3)) == expected]
    if not matches:
        raise ValueError("local word alignment did not reproduce the three original machine phrases")
    nearest = min(matches, key=lambda start: abs(offset + float(local[start]["start"])
                                                 - float(segments[index - 1]["start"])))
    selected = local[nearest:nearest + 3]
    starts = [offset + float(part["start"]) for part in selected]
    ends = [offset + float(part["end"]) for part in selected]
    original = segments[index - 1:index + 2]
    replacements = (("end", ends[0], original[0]),
                    ("start", starts[1], original[1]), ("end", ends[1], original[1]),
                    ("start", starts[2], original[2]))
    if any(abs(value - float(segment[key])) > 1.0 for key, value, segment in replacements):
        raise ValueError("local word alignment differs from original timecodes by more than one second")
    repaired = copy.deepcopy(result)
    target = repaired["segments"]
    target[index - 1]["end"] = ends[0]
    target[index]["start"], target[index]["end"] = starts[1], ends[1]
    target[index + 1]["start"] = starts[2]
    validate_result(repaired, duration)
    return repaired, {"segment_indices": [index - 1, index, index + 1],
                      "original_segments": original, "local_window_segments": selected,
                      "repaired_segments": target[index - 1:index + 2],
                      "machine_words_edited": False, "human_audio_reviewed": False}


def local_timing_retry(result: dict[str, Any], index: int, audio: Path,
                       model: dict[str, Any], settings: dict[str, Any], duration: float,
                       diagnostic: Path) -> dict[str, Any]:
    segments = result.get("segments") or []
    if index < 1 or index + 1 >= len(segments):
        raise ValueError("local timing retry needs a three-segment neighborhood")
    offset = max(0.0, float(segments[index - 1]["start"]) - 7.0)
    end = min(duration, float(segments[index + 1]["end"]) + 7.0)
    if not 5.0 <= end - offset <= 30.0:
        raise ValueError("local timing retry window is outside safe length bounds")
    with tempfile.TemporaryDirectory(prefix="asr-timing-") as temporary:
        clip = Path(temporary) / "source-window.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(offset),
                        "-i", str(audio), "-t", str(end - offset), "-c:a", "pcm_s16le",
                        "-y", str(clip)], capture_output=True, text=True, check=True)
        import mlx_whisper
        window = mlx_whisper.transcribe(str(clip), path_or_hf_repo=model["local_path"],
                                        language=settings["language"], task="transcribe", verbose=None,
                                        condition_on_previous_text=False, temperature=0.0,
                                        initial_prompt=settings["initial_prompt"], word_timestamps=True)
        validate_result(window, end - offset)
        repaired, details = repair_timestamp_from_window(result, window, index, offset, duration)
        atomic_json(diagnostic, {"status": "local_machine_timing_alignment",
                                 "window_start_seconds": offset, "window_duration_seconds": end - offset,
                                 "window_audio_sha256": sha256_file(clip),
                                 "window_machine_result": window, **details})
        return repaired


def matching_preserved_invalid(root: Path, video_id: str, media_hash: str,
                               key: str, audio_info: dict[str, Any]) -> tuple[Path | None, dict[str, Any] | None]:
    """Reuse the exact preserved machine result when only its validation failed."""
    for path in sorted((root / "_failed" / video_id).glob("*-raw-invalid.json"), reverse=True):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if (value.get("status") == "invalid_machine_result"
                    and value.get("video_id") == video_id
                    and value.get("source_media_sha256") == media_hash
                    and model_key(value["model"], value["settings"]) == key
                    and value.get("source_audio", {}).get("pcm_s16le_sha256") == audio_info["pcm_s16le_sha256"]
                    and isinstance(value.get("raw_result"), dict)):
                return path, value
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None, None


def render_srt(segments: list[dict[str, Any]]) -> str:
    blocks = []
    for number, segment in enumerate(segments, 1):
        text = str(segment["text"]).strip().replace("\n", " ")
        blocks.append(f"{number}\n{srt_time(float(segment['start']))} --> {srt_time(float(segment['end']))}\n{text}\n")
    return "\n".join(blocks)


def render_markdown(job: dict[str, Any], result: dict[str, Any], evidence: dict[str, Any]) -> str:
    title = str(job.get("title") or job["video_id"]).replace("\n", " ")
    paragraphs = "\n\n".join(str(segment["text"]).strip() for segment in result["segments"])
    return (f"# {title}\n\n"
            f"- 视频 ID：{job['video_id']}\n- 来源：{job['source_url']}\n"
            f"- 作者：{job.get('author') or '未提供'}\n- 生成时间：{evidence['completed_at']}\n"
            f"- 类型：本地 ASR 机器逐字稿；未做语义校对，未人工听核\n"
            f"- 模型：{evidence['model'].get('repository') or evidence['model']['local_path']}\n"
            f"- 音频时长：{evidence['source_audio']['duration_seconds']:.3f} 秒\n"
            "- 原稿与字幕保留机器识别内容；同音词、专名和口误可能需要核对\n\n"
            f"## 机器识别原文\n\n{paragraphs}\n")


def resume_matches(directory: Path, media_hash: str, key: str) -> bool:
    evidence_path = directory / OUTPUT_NAMES["evidence"]
    if not evidence_path.is_file():
        return False
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if evidence.get("source_media", {}).get("sha256") != media_hash or evidence.get("model_settings_key") != key:
        return False
    if evidence.get("status") not in ("machine_draft_saved", "machine_draft_requires_review"):
        return False
    files = _PATH_HELPER.resolve_artifact_paths(directory, evidence)
    for kind in ("markdown", "srt"):
        path = files[kind]
        if not path.is_file() or sha256_file(path) != evidence.get("outputs", {}).get(kind, {}).get("sha256"):
            return False
    return True


@contextmanager
def output_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".transcribe.lock").open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"another ASR process is writing {root}; run only one local ASR worker")
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def transcribe_job(job: dict[str, Any], root: Path, model: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    video_id = safe_video_id(job["video_id"])
    media = Path(job["media_path"]).resolve()
    directory = root / video_id
    media_hash = sha256_file(media)
    if job.get("expected_media_sha256") and job["expected_media_sha256"] != media_hash:
        raise ValueError("media SHA256 differs from the verified downloader manifest; preserve file and redownload before ASR")
    key = model_key(model, settings)
    if resume_matches(directory, media_hash, key):
        previous = json.loads((directory / OUTPUT_NAMES["evidence"]).read_text(encoding="utf-8"))
        validation = previous["validation"]
        flags = machine_quality_flags({"text": previous.get("raw_text"),
                                       "segments": previous.get("segments")},
                                      float((previous.get("source_audio") or {}).get("duration_seconds", 0)))
        needs_review = bool(flags) or previous.get("status") == "machine_draft_requires_review"
        return {"status": "skipped_requires_review" if needs_review else "skipped_verified",
                "video_id": video_id, "directory": str(directory),
                "character_count": validation["character_count"], "segment_count": validation["segment_count"],
                "warnings": validation["warnings"], "quality_flags": flags,
                "original_completed_at": previous["completed_at"]}
    if directory.exists():
        raise RuntimeError(f"existing ASR outputs do not match media/model/hash: {directory}; preserved unchanged, choose a new --output-dir")
    info = inspect_media(media)
    started_at, start = utc_now(), time.monotonic()
    work_root = root / "_work"
    work_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{video_id}-", dir=str(work_root)) as temporary:
        temporary_path = Path(temporary)
        audio = temporary_path / "source-16k.wav"
        audio_info = extract_audio(media, audio)
        import mlx_whisper
        import mlx.core as mx
        mx.reset_peak_memory()
        def decode(word_timestamps: bool) -> dict[str, Any]:
            return mlx_whisper.transcribe(
                str(audio), path_or_hf_repo=model["local_path"], language=settings["language"],
                task="transcribe", verbose=None, condition_on_previous_text=False,
                temperature=0.0, initial_prompt=settings["initial_prompt"], word_timestamps=word_timestamps,
            )
        preserved_path, preserved = matching_preserved_invalid(root, video_id, media_hash, key, audio_info)
        result = preserved["raw_result"] if preserved else decode(False)
        retry_used = False
        local_timing_repair_used = False
        local_timing_diagnostic = None
        first_validation_error = None
        try:
            validation = validate_result(result, audio_info["duration_seconds"])
        except ValueError as exc:
            first_validation_error = str(exc)
            failed_path = preserved_path or root / "_failed" / video_id / (started_at.replace(":", "-") + "-raw-invalid.json")
            if not preserved:
                atomic_json(failed_path, {"status": "invalid_machine_result", "error": first_validation_error,
                                          "video_id": video_id, "source_media_sha256": media_hash, "source_audio": audio_info,
                                          "model": model, "settings": settings, "raw_result": result})
            if "exceeds decoded audio duration" in first_validation_error:
                # A single full alignment retry can resolve a last-window overshoot.
                emit("local_timestamp_retry", video_id=video_id, diagnostic_path=str(failed_path))
                result = decode(True)
                validation = validate_result(result, audio_info["duration_seconds"])
                retry_used = True
                validation["warnings"].append("timestamp_alignment_retry_required_review_of_last_window")
            elif first_validation_error.startswith("invalid/unordered timestamps at segment "):
                index = int(first_validation_error.rsplit(" ", 1)[-1])
                local_timing_diagnostic = failed_path.with_name(failed_path.stem + "-local-alignment.json")
                result = local_timing_retry(result, index, audio, model, settings,
                                            audio_info["duration_seconds"], local_timing_diagnostic)
                validation = validate_result(result, audio_info["duration_seconds"])
                local_timing_repair_used = True
                validation["warnings"].append("three_segment_local_machine_timing_repair_requires_review")
            else:
                raise
        validation["local_word_alignment_retry_used"] = retry_used
        validation["local_timing_repair_used"] = local_timing_repair_used
        stage = temporary_path / "outputs"
        stage.mkdir()
        elapsed = time.monotonic() - start
        evidence = {
            "schema_version": SCHEMA_VERSION,
            "status": "machine_draft_requires_review" if validation["quality_flags"] else "machine_draft_saved",
            "video_id": video_id, "source_url": job["source_url"], "title": job.get("title"), "author": job.get("author"),
            "started_at": started_at, "completed_at": utc_now(),
            "source_media": {"path": str(media), "sha256": media_hash, "bytes": media.stat().st_size, **info},
            "source_audio": audio_info, "model": model, "settings": settings, "model_settings_key": key,
            "effective_decoding_settings": {**settings, "word_timestamps": retry_used,
                                            "local_window_word_timestamps": local_timing_repair_used},
            "local_retry": {"count": int(retry_used or local_timing_repair_used),
                            "first_validation_error": first_validation_error,
                            "policy": "one machine word-alignment retry, full for audio overshoot or local for inverted timestamps; original words retained",
                            "local_timing_diagnostic_path": str(local_timing_diagnostic) if local_timing_diagnostic else None,
                            "reused_preserved_invalid_machine_result": bool(preserved),
                            "preserved_invalid_result_path": str(preserved_path) if preserved_path else None},
            "runtime": {"elapsed_seconds": round(elapsed, 3), "realtime_factor": round(elapsed / audio_info["duration_seconds"], 4),
                        "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                        "mlx_peak_memory_bytes_since_reset": mx.get_peak_memory(), "python": sys.version.split()[0]},
            "paid_api_used": False, "computer_use_used": False, "inference_network_used": False,
            "validation": validation,
            "raw_text": str(result["text"]).strip(), "language": result.get("language"), "segments": result["segments"],
            "outputs": {},
        }
        md = stage / OUTPUT_NAMES["markdown"]
        srt = stage / OUTPUT_NAMES["srt"]
        markdown = render_markdown(job, result, evidence)
        if validation["quality_flags"]:
            markdown = markdown.replace("## 机器识别原文\n\n",
                                        "- 质量状态：机器结果疑似重复或稀疏，需听音复核；当前不计入完整逐字稿。\n\n"
                                        "## 机器识别原文\n\n", 1)
        md.write_text(markdown, encoding="utf-8")
        srt.write_text(render_srt(result["segments"]), encoding="utf-8")
        for kind, path in (("markdown", md), ("srt", srt)):
            evidence["outputs"][kind] = {"path": str(directory / path.name), "sha256": sha256_file(path), "bytes": path.stat().st_size}
        atomic_json(stage / OUTPUT_NAMES["evidence"], evidence)
        # Directory commit is atomic; no half-written finished transcript is exposed.
        os.rename(stage, directory)
    return {"status": evidence["status"], "video_id": video_id, "directory": str(directory),
            "character_count": validation["character_count"], "segment_count": validation["segment_count"],
            "warnings": validation["warnings"], "quality_flags": validation["quality_flags"],
            "elapsed_seconds": evidence["runtime"]["elapsed_seconds"]}


def doctor(model: str) -> dict[str, Any]:
    report = {"python": sys.version.split()[0], "ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe"),
              "mlx_whisper_version": package_version("mlx-whisper"), "mlx_version": package_version("mlx"),
              "free_disk_bytes": shutil.disk_usage(Path.cwd()).free, "model": None}
    try:
        report["model"] = resolve_model(model)
        report["status"] = "ready" if report["ffmpeg"] and report["ffprobe"] and report["mlx_whisper_version"] else "dependencies_missing"
    except Exception as exc:
        report.update({"status": "model_unavailable_offline", "error": str(exc)})
    return report


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--manifest", type=Path, help="download manifest JSON or JSONL; only existing committed media are selected")
    value.add_argument("--media", type=Path, help="single already downloaded video/audio file")
    value.add_argument("--video-id", nargs="+", help="one or more IDs to select; one ID is required when --media filename lacks an ID")
    value.add_argument("--title", default="", help="title for --media")
    value.add_argument("--author", default="", help="author for --media; omitted authors are marked 未提供")
    value.add_argument("--output-dir", type=Path, default=Path.cwd() / "local-transcripts")
    value.add_argument("--model", default=DEFAULT_MODEL, help="cached Hugging Face repository or local model directory")
    value.add_argument("--initial-prompt", default=None, help="optional vocabulary hint; logged verbatim, no automatic rewriting")
    value.add_argument("--language", default="zh")
    value.add_argument("--limit", type=int, help="limit completed/new jobs in this invocation")
    value.add_argument("--watch", action="store_true", help="rescan manifest while downloader is running; stop with Ctrl-C")
    value.add_argument("--done-file", type=Path, help="in watch mode, stop after this downloader completion marker exists and ready media were attempted")
    value.add_argument("--poll-seconds", type=float, default=30)
    value.add_argument("--max-idle-polls", type=int, default=0, help="0 waits indefinitely in watch mode")
    value.add_argument("--doctor", action="store_true")
    value.add_argument("--download-model", action="store_true", help="explicitly prepare public model files then exit; inference always runs offline")
    return value


def main() -> int:
    args = parser().parse_args()
    if args.download_model:
        emit("model_ready", model=resolve_model(args.model, allow_download=True))
        return 0
    # Never silently fall back to downloading or hosted inference while processing.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    if args.doctor:
        report = doctor(args.model)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "ready" else 1
    if bool(args.manifest) == bool(args.media):
        raise ValueError("provide exactly one of --manifest or --media")
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be positive")
    if args.poll_seconds < 1:
        raise ValueError("--poll-seconds must be at least 1")
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            raise RuntimeError(f"missing {command}")
    root = args.output_dir.expanduser().resolve()
    model = resolve_model(args.model)
    settings = {"language": args.language, "task": "transcribe", "temperature": 0.0,
                "condition_on_previous_text": False, "word_timestamps": False, "initial_prompt": args.initial_prompt}
    selected = set(args.video_id or [])
    state_path = root / "_batch-state.json"
    attempted, failed, idle, finished = set(), 0, 0, 0
    with output_lock(root):
        previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        state = {"schema_version": SCHEMA_VERSION, "created_at": previous.get("created_at", utc_now()),
                 "updated_at": utc_now(), "model": model, "jobs": previous.get("jobs", {}),
                 "paid_api_used": False, "computer_use_used": False, "inference_network_used": False}
        emit("batch_started", output_dir=str(root), model=model["repository"] or model["local_path"], offline=True)
        while True:
            if args.media:
                media = args.media.expanduser().resolve()
                if len(selected) > 1:
                    raise ValueError("--media accepts exactly one --video-id")
                video_id = next(iter(selected)) if selected else infer_video_id(media)
                jobs, pending = [{"video_id": safe_video_id(video_id), "media_path": str(media), "title": args.title,
                                  "author": args.author, "source_url": f"https://www.douyin.com/video/{video_id}"}], 0
            else:
                manifest = args.manifest.expanduser().resolve()
                if not manifest.exists() and args.watch:
                    jobs, pending = [], 0
                else:
                    jobs, pending = jobs_from_manifest(manifest, selected)
            new_jobs = [job for job in jobs if job["video_id"] not in attempted]
            if not new_jobs:
                idle += 1
            else:
                idle = 0
            state["pending_media_entries"] = pending
            for job in new_jobs:
                video_id = job["video_id"]
                attempted.add(video_id)
                state["jobs"][video_id] = {"status": "processing", "started_at": utc_now(), "media_path": job["media_path"]}
                state["updated_at"] = utc_now()
                atomic_json(state_path, state)
                emit("transcribing", video_id=video_id)
                try:
                    outcome = transcribe_job(job, root, model, settings)
                    if outcome["status"] in ("machine_draft_requires_review", "skipped_requires_review"):
                        failed += 1
                    else:
                        finished += 1
                    state["jobs"][video_id] = {**outcome, "completed_at": utc_now()}
                    emit("job_finished", **outcome)
                except Exception as exc:
                    failed += 1
                    state["jobs"][video_id] = {"status": "failed", "error": str(exc), "failed_at": utc_now()}
                    emit("job_failed", video_id=video_id, error=str(exc))
                state["updated_at"] = utc_now()
                atomic_json(state_path, state)
                if args.limit is not None and finished + failed >= args.limit:
                    break
            if (args.limit is not None and finished + failed >= args.limit) or not args.watch or args.media:
                break
            if args.done_file and args.done_file.expanduser().exists():
                # The downloader may commit more media while this ASR pass is
                # running. Re-read after its completion marker, rather than
                # deciding from the snapshot taken before recognition began.
                final_jobs, final_pending = jobs_from_manifest(manifest, selected)
                remaining = [job for job in final_jobs if job["video_id"] not in attempted]
                state["pending_media_entries"] = final_pending
                if remaining:
                    state["updated_at"] = utc_now()
                    atomic_json(state_path, state)
                    emit("final_manifest_rescan", new_ready=len(remaining), pending_media_entries=final_pending)
                    continue
                state["download_stage_finished"] = True
                emit("download_stage_finished", pending_media_entries=final_pending)
                break
            if args.max_idle_polls and idle >= args.max_idle_polls:
                break
            state["updated_at"] = utc_now()
            atomic_json(state_path, state)
            emit("waiting_for_media", ready=len(jobs), pending=pending, poll_seconds=args.poll_seconds)
            time.sleep(args.poll_seconds)
        state["updated_at"] = utc_now()
        atomic_json(state_path, state)
    emit("batch_finished", successful_or_verified=finished, failed=failed, output_dir=str(root))
    unfinished_media = state.get("download_stage_finished") and state.get("pending_media_entries", 0)
    return 1 if failed or unfinished_media else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        emit("interrupted", message="finished outputs retained; rerun the same command to resume")
        raise SystemExit(130)
    except Exception as exc:
        emit("fatal_error", error=str(exc))
        raise SystemExit(1)
