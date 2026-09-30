"""Locate this Skill's optional runtime without requiring shell activation."""
from __future__ import annotations

import os
from pathlib import Path
import shutil


def find_command(name: str, skill_root: Path) -> str | None:
    if name == "mlx_whisper":
        folder = "Scripts" if os.name == "nt" else "bin"
        filename = name + (".exe" if os.name == "nt" else "")
        candidate = skill_root / ".venv" / folder / filename
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    return shutil.which(name)
