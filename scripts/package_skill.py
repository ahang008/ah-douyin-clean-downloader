#!/usr/bin/env python3
"""Build a reproducible, code-only Skill archive using an explicit allowlist."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import zipfile

NAME = "ah-douyin-clean-downloader"
ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    "SKILL.md", "README.md", "LICENSE", "PROVENANCE.md", "THIRD_PARTY_NOTICES.md",
    "SECURITY.md", "CHANGELOG.md", "CONTRIBUTING.md",
)
DIRECTORIES = ("agents", "assets", "references", "scripts", "tests")
FORBIDDEN_PARTS = {"__pycache__", ".git", ".browser-session", "media", "local-transcripts", "catalog", "logs", "covers", "annotated-transcripts",
                   "rewrites", "rewrite-work", "rewrite-output"}


def allowed_file(relative: Path) -> bool:
    if any(part in FORBIDDEN_PARTS or part.startswith(".venv") for part in relative.parts):
        return False
    group = relative.parts[0]
    if group == "scripts":
        return relative.suffix in {".py", ".sh", ".swift"} or (
            relative.name.startswith("requirements") and relative.suffix == ".txt")
    if group == "tests":
        return relative.suffix == ".py" or (
            relative.parent == Path("tests/fixtures") and relative.suffix == ".json")
    if group == "agents":
        return relative.name == "openai.yaml" and relative.parent == Path("agents")
    if group == "references":
        return relative.suffix == ".md"
    if group == "assets":
        return relative.suffix.lower() in {".png", ".svg", ".jpg", ".webp"}
    return False


def release_files(root: Path) -> list[Path]:
    root = root.resolve()
    paths = []
    for name in ROOT_FILES:
        path = root / name
        if not path.is_file():
            raise ValueError("Missing release file: " + name)
        if path.is_symlink():
            raise ValueError("Symlinks cannot be packaged: " + name)
        paths.append(path)
    for name in DIRECTORIES:
        folder = root / name
        if folder.is_symlink():
            raise ValueError("Symlinks cannot be packaged: " + name)
        if not folder.is_dir():
            continue
        for directory, subdirs, files in os.walk(folder, followlinks=False):
            directory = Path(directory)
            for child in subdirs:
                if (directory / child).is_symlink():
                    raise ValueError("Symlinks cannot be packaged: " + str((directory / child).relative_to(root)))
            subdirs[:] = [child for child in subdirs if child not in FORBIDDEN_PARTS and not child.startswith(".venv")]
            for child in files:
                path = directory / child
                relative = path.relative_to(root)
                if not allowed_file(relative):
                    continue
                if path.is_symlink():
                    raise ValueError("Symlinks cannot be packaged: " + str(relative))
                paths.append(path)
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def build_package(root: Path, output: Path, version: str) -> dict:
    if not re.fullmatch(r"v?\d+\.\d+\.\d+(?:-[a-z0-9.-]+)?", version):
        raise ValueError("Version must look like v0.4.0")
    root = root.expanduser().resolve()
    paths = release_files(root)
    output = output.expanduser().resolve()
    if output in paths:
        raise ValueError("Output cannot replace a source file")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + output.name + ".", dir=output.parent)
    os.close(fd)
    inventory = []
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in paths:
                relative = path.relative_to(root).as_posix()
                data = path.read_bytes()
                info = zipfile.ZipInfo(NAME + "/" + relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o100755 if os.access(path, os.X_OK) else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
                inventory.append({"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("Archive integrity check failed")
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"skill": NAME, "version": version, "output": str(output), "files": len(inventory),
            "bytes": output.stat().st_size, "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "inventory": inventory, "runtime_and_user_data_included": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--version", default="v0.5.0")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.root / "dist" / f"{NAME}-{args.version}.zip"
    result = build_package(args.root, output, args.version)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
