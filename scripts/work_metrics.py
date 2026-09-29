#!/usr/bin/env python3
"""Small public-counter and filename helpers; no network or file mutations.

Counters are observations returned by the public browser SDK, not owner-side
analytics. In particular, a zero play counter is commonly unavailable metadata
and is never presented here as a measured zero-view result.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import unicodedata
from typing import Any, Iterable, Mapping, Optional


COUNT_FIELDS = ("digg_count", "comment_count", "collect_count", "share_count",
                "forward_count", "download_count", "play_count")
STATISTICS_EXTENSION_KEYS = ("statistics", "statistics_captured_at", "statistics_availability")
MAX_COUNT = (1 << 63) - 1
DEFAULT_STEM_BYTES = 200
VIDEO_ID = re.compile(r"^\d{16,22}$", re.ASCII)
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$", re.ASCII)
_NULL_REASONS = {"not_returned", "invalid"}
_ILLEGAL_FILENAME_CHARACTERS = set('/\\:*?"<>|')
_RESERVED_FILENAME = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def _capture_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not UTC_TIMESTAMP.fullmatch(value):
        return None
    try:
        body = value[:-1] if value.endswith("Z") else value[:-6]
        base, _, fraction = body.partition(".")
        # Python 3.9's fromisoformat only accepts 3 or 6 fractional digits.
        parsed = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(
            microsecond=int(fraction.ljust(6, "0")) if fraction else 0, tzinfo=timezone.utc)
    except ValueError:
        return None
    return parsed


def validated_capture_time(value: Any) -> Optional[str]:
    """Validate a UTC observation time without rewriting its exact spelling."""
    return value if _capture_datetime(value) is not None else None


def normalized_statistics(raw: Any, availability: Any = None) -> tuple[dict, dict]:
    """Whitelist observed integer counters and distinguish missing data from 0.

    A projected null may retain one of our fixed unavailable reasons so saved
    pages round-trip exactly. Arbitrary upstream fields or reason strings are
    never copied. Actual integer observations always determine availability.
    """
    source = raw if isinstance(raw, Mapping) else {}
    malformed = raw is not None and not isinstance(raw, Mapping)
    prior = availability if isinstance(availability, Mapping) else {}
    statistics, states = {}, {}
    for key in COUNT_FIELDS:
        value = source.get(key)
        count, reason = None, "invalid" if malformed else "not_returned"
        if value is not None:
            if type(value) is int and 0 <= value <= MAX_COUNT:
                if key == "play_count" and value == 0:
                    reason = "zero_unverified"
                else:
                    count, reason = value, "available"
            else:
                reason = "invalid"
        elif not malformed:
            allowed = _NULL_REASONS | ({"zero_unverified"} if key == "play_count" else set())
            if isinstance(prior.get(key), str) and prior[key] in allowed:
                reason = prior[key]
        statistics[key], states[key] = count, reason
    return statistics, states


def statistics_extension(work: Mapping[str, Any], captured_at: Any = None) -> dict:
    """Return our three optional fields, preserving legacy rows without them.

    New browser captures pass their one page timestamp explicitly. Rebuilding
    saved projections reads their exact saved timestamp and never calls a clock.
    """
    if captured_at is None and not any(key in work for key in STATISTICS_EXTENSION_KEYS):
        return {}
    if captured_at is not None:
        timestamp = validated_capture_time(captured_at)
        if timestamp is None:
            raise ValueError("captured_at must be a UTC ISO8601 timestamp")
    else:
        timestamp = validated_capture_time(work.get("statistics_captured_at"))
    statistics, availability = normalized_statistics(work.get("statistics"), work.get("statistics_availability"))
    return {"statistics": statistics, "statistics_captured_at": timestamp,
            "statistics_availability": availability}


def metric_value(work: Mapping[str, Any], key: str) -> Optional[int]:
    if key not in COUNT_FIELDS or not isinstance(work, Mapping):
        return None
    statistics, _ = normalized_statistics(work.get("statistics"), work.get("statistics_availability"))
    return statistics[key]


def metric_text(work: Mapping[str, Any], key: str) -> str:
    value = metric_value(work, key)
    return "未获取" if value is None else str(value)


def metrics_summary(works: Iterable[Mapping[str, Any]]) -> dict:
    """Summarize availability separately from catalog pagination completeness."""
    rows = list(works)
    fields = {key: {"available": 0, "unknown": 0} for key in COUNT_FIELDS}
    times = []
    for row in rows:
        row = row if isinstance(row, Mapping) else {}
        statistics, _ = normalized_statistics(row.get("statistics"), row.get("statistics_availability"))
        for key, value in statistics.items():
            fields[key]["unknown" if value is None else "available"] += 1
        timestamp = validated_capture_time(row.get("statistics_captured_at"))
        if timestamp is not None:
            times.append((_capture_datetime(timestamp), timestamp))
    return {"total": len(rows), "statistics_present_total": sum(isinstance(row, Mapping) and "statistics" in row for row in rows),
            "fields": fields, "capture_time_available": len(times), "capture_time_unknown": len(rows) - len(times),
            "capture_range": {"earliest": min(times)[1] if times else None, "latest": max(times)[1] if times else None},
            "source": "public_sdk_observation"}


def safe_filename_title(value: Any) -> str:
    value = unicodedata.normalize("NFKC", value if isinstance(value, str) else "")
    value = "".join(" " if char in _ILLEGAL_FILENAME_CHARACTERS else char
                    for char in value if not unicodedata.category(char).startswith("C"))
    value = re.sub(r"\s+", " ", value).strip(" .-_#")
    if not value:
        return "作品"
    if _RESERVED_FILENAME.match(value):
        value = "作品-" + value
    return value


def metric_filename_stem(work: Mapping[str, Any], max_bytes: int = DEFAULT_STEM_BYTES) -> str:
    """Create a cross-platform metric title ending in its unchanged video ID."""
    if not isinstance(work, Mapping):
        raise ValueError("A public work object is required")
    video_id = str(work.get("video_id") or work.get("aweme_id") or "")
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("A valid video ID is required for metric filenames")
    if type(max_bytes) is not int or not 1 <= max_bytes <= DEFAULT_STEM_BYTES:
        raise ValueError("max_bytes must be a positive integer no larger than 200")
    prefix = "_".join(label + metric_text(work, key) for label, key in
                      (("赞", "digg_count"), ("评", "comment_count"), ("藏", "collect_count"), ("转", "share_count"))) + "_"
    suffix = "-" + video_id
    budget = max_bytes - len((prefix + suffix).encode("utf-8"))
    if budget < len("作品".encode("utf-8")):
        raise ValueError("Filename byte budget cannot preserve the counters and video ID")
    title = next((work[key] for key in ("title", "catalog_title", "desc")
                  if isinstance(work.get(key), str) and work[key].strip()), "")
    title = safe_filename_title(title).encode("utf-8")[:budget].decode("utf-8", errors="ignore").rstrip(" .-_#") or "作品"
    return prefix + title + suffix
