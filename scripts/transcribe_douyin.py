#!/usr/bin/env python3
"""Prepare an authorized Douyin video for a single, corrected transcript deliverable."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


SCRIPT_DIR = Path(__file__).resolve().parent
DOWNLOAD_SCRIPT = SCRIPT_DIR / "download_douyin.py"
DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_OUTPUT_NAME = "抖音逐字稿"


def load_downloader():
    spec = importlib.util.spec_from_file_location("ah_download_douyin", DOWNLOAD_SCRIPT)
    if not spec or not spec.loader:
        raise RuntimeError("无法加载下载脚本")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DOWNLOADER = load_downloader()


def default_output_dir() -> str:
    return os.environ.get(
        "AH_DOUYIN_TRANSCRIPT_DIR",
        str(Path.home() / "Desktop" / DEFAULT_OUTPUT_NAME),
    )


def require_command(name: str, message: str) -> str:
    path = shutil.which(name)
    if not path:
        raise DOWNLOADER.DownloadError(message)
    return path


def transcript_destination(output_root: Path, author: str, title: str, video_id: str) -> Path:
    author_dir = DOWNLOADER.ensure_author_directory(output_root, author)
    base = f"{DOWNLOADER.safe_title(title, limit=64)}-{video_id}-校对后逐字稿"
    candidate = author_dir / f"{base}.md"
    number = 2
    while candidate.exists():
        candidate = author_dir / f"{base}-{number}.md"
        number += 1
    return candidate


def prepare_transcript(args: argparse.Namespace) -> dict:
    whisper = require_command("mlx_whisper", "系统缺少 mlx_whisper，无法执行本地语音识别")
    require_command("ffmpeg", "系统缺少 ffmpeg，无法从视频读取音频")
    output_root = Path(args.output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    work_dir = Path(tempfile.mkdtemp(prefix="ah-douyin-transcript-"))
    try:
        download_args = argparse.Namespace(
            share_text=args.share_text,
            output_dir=str(work_dir / "media"),
            proxy=args.proxy,
            timeout=args.timeout,
            download_timeout=args.download_timeout,
            metadata_only=False,
        )
        downloaded = DOWNLOADER.download(download_args)
        media_path = Path(downloaded["path"])
        draft_dir = work_dir / "draft"
        draft_dir.mkdir()
        command = [
            whisper,
            str(media_path),
            "--model",
            args.model,
            "--language",
            "zh",
            "--output-dir",
            str(draft_dir),
            "--output-name",
            "机器识别稿",
            "--output-format",
            "txt",
            "--verbose",
            "False",
            "--condition-on-previous-text",
            "False",
        ]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        draft_path = draft_dir / "机器识别稿.txt"
        if result.returncode != 0 or not draft_path.is_file() or not draft_path.read_text(encoding="utf-8").strip():
            detail = result.stderr.strip()[-500:] or result.stdout.strip()[-500:]
            raise DOWNLOADER.DownloadError("本地语音识别失败：" + detail)

        final_path = transcript_destination(
            output_root,
            downloaded.get("author") or "",
            downloaded.get("title") or "未命名视频",
            downloaded["video_id"],
        )
        return {
            "status": "draft_ready",
            "work_dir": str(work_dir),
            "draft_path": str(draft_path),
            "final_path": str(final_path),
            "video_id": downloaded["video_id"],
            "title": downloaded.get("title"),
            "author": downloaded.get("author"),
            "duration_seconds": downloaded.get("duration_seconds"),
            "model": args.model,
            "paid_api_used": False,
            "next_action": "校对机器识别稿并只写入 final_path，验收后删除 work_dir",
        }
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def cleanup_work_dir(path_text: str) -> dict:
    path = Path(path_text).expanduser().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if path.parent != temp_root or not path.name.startswith("ah-douyin-transcript-"):
        raise DOWNLOADER.DownloadError("拒绝清理非本 Skill 创建的临时目录")
    existed = path.exists()
    shutil.rmtree(path, ignore_errors=False)
    return {"status": "cleaned", "work_dir": str(path), "existed": existed}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="下载抖音视频并生成待校对的本地机器识别稿。")
    parser.add_argument("share_text", nargs="*", help="完整抖音口令或官方分享链接")
    parser.add_argument("--output-dir", default=default_output_dir(), help="最终逐字稿根目录")
    parser.add_argument("--proxy", help="可选代理，例如 socks5h://127.0.0.1:1088")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="mlx_whisper 模型")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--download-timeout", type=int, default=1800)
    parser.add_argument("--cleanup-work-dir", help="删除本脚本创建的指定临时目录")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.cleanup_work_dir:
            result = cleanup_work_dir(args.cleanup_work_dir)
        else:
            if not args.share_text:
                raise DOWNLOADER.DownloadError("请提供完整抖音口令或官方分享链接")
            result = prepare_transcript(args)
    except DOWNLOADER.DownloadError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"status": "error", "message": "用户中止处理"}, ensure_ascii=False), file=sys.stderr)
        return 130
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
