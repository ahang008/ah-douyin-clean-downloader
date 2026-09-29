#!/bin/bash
set -euo pipefail
skill_root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  printf '%s\n' '本地 MLX 转写需要 Apple Silicon Mac；仅下载视频不需要运行此安装脚本。' >&2
  exit 1
fi
task_macos_version="$(/usr/bin/sw_vers -productVersion)"
if [ "${task_macos_version%%.*}" -lt 15 ]; then
  printf '%s\n' '此固定 MLX 依赖需要 macOS 15 或更高版本。' >&2
  exit 1
fi
for executable in curl ffmpeg ffprobe; do
  if ! command -v "$executable" >/dev/null 2>&1; then
    printf '缺少依赖：%s\n' "$executable" >&2
    exit 1
  fi
done
task_python=""
for candidate in python3.12 /opt/homebrew/bin/python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys;raise SystemExit(0 if sys.version_info[:2] == (3,12) else 1)'; then
    task_python="$(command -v "$candidate")"
    break
  fi
done
if [ -z "$task_python" ]; then
  printf '%s\n' '固定依赖使用 Python 3.12；请准备 python3.12 后重试。' >&2
  exit 1
fi
if [ -e "$skill_root/.venv" ]; then
  if [ ! -x "$skill_root/.venv/bin/python" ] || ! "$skill_root/.venv/bin/python" -c 'import sys;raise SystemExit(0 if sys.version_info[:2] == (3,12) else 1)'; then
    printf '%s\n' '现有 .venv 的 Python 版本不匹配；请先保留或迁走该环境后重试。' >&2
    exit 1
  fi
  if [ -f "$skill_root/.venv/pyvenv.cfg" ] && /usr/bin/grep -iq '^include-system-site-packages *= *true' "$skill_root/.venv/pyvenv.cfg"; then
    printf '%s\n' '现有 .venv 使用系统共享包；请保留或迁走后，以此脚本建立独立环境。' >&2
    exit 1
  fi
else
  "$task_python" -m venv "$skill_root/.venv"
fi
"$skill_root/.venv/bin/python" -m pip install -r "$skill_root/scripts/requirements-lock.txt"
printf '%s\n' '转写依赖已准备。正常识别只读本地模型缓存；首次模型准备需显式运行 --download-model。'
