#!/usr/bin/env python3
"""Resolve completed machine artifacts without deriving names from live metrics."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from typing import Any


OUTPUT_NAMES = {
    "markdown": "01-本地ASR机器逐字稿.md",
    "srt": "01-本地ASR机器逐字稿.srt",
    "evidence": "01-本地ASR识别证据.json",
}
SUFFIXES = {"markdown": ".md", "srt": ".srt", "evidence": ".json"}


def _checked_path(directory: Path, candidate: Path, kind: str) -> Path:
    if directory.is_symlink() or candidate.is_symlink():
        raise ValueError("ASR artifact paths may not be symbolic links")
    if Path(os.path.abspath(candidate)).parent != Path(os.path.abspath(directory)):
        raise ValueError("ASR artifact path must directly belong to its video ID directory")
    base = directory.resolve()
    path = candidate.resolve()
    if path.parent != base or path.suffix.lower() != SUFFIXES[kind]:
        raise ValueError("ASR artifact path must be a matching file in its own video ID directory")
    if candidate.exists() and not stat.S_ISREG(candidate.lstat().st_mode):
        raise ValueError("ASR artifact path must name a regular file")
    return path


def resolve_artifact_paths(directory: Path, evidence: dict[str, Any] | None = None) -> dict[str, Path]:
    """Return markdown, SRT, and fixed evidence paths, including legacy layouts.

    Missing evidence or an absent/empty output path keeps the original filename.
    A recorded nonempty path is authoritative and is never replaced by a guess.
    Missing files are returned so callers can distinguish pending work from an
    unsafe pointer. Existing paths must be regular files, never symlinks.
    """
    directory = Path(directory).expanduser()
    evidence_path = _checked_path(directory, directory / OUTPUT_NAMES["evidence"], "evidence")
    if evidence is None:
        if evidence_path.is_file():
            try:
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise ValueError("ASR evidence JSON is corrupt") from exc
        else:
            evidence = {}
    if not isinstance(evidence, dict):
        raise ValueError("ASR evidence must be a JSON object")
    outputs = evidence.get("outputs", {})
    if not isinstance(outputs, dict):
        raise ValueError("ASR evidence outputs must be an object")
    paths = {"evidence": evidence_path}
    for kind in ("markdown", "srt"):
        item = outputs.get(kind, {})
        if not isinstance(item, dict):
            raise ValueError("ASR evidence output metadata must be an object")
        value = item.get("path")
        if value is None or value == "":
            candidate = directory / OUTPUT_NAMES[kind]
        else:
            if not isinstance(value, str):
                raise ValueError("ASR evidence output path must be a string")
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = directory / candidate
        paths[kind] = _checked_path(directory, candidate, kind)
    return paths
