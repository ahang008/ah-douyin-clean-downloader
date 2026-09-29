#!/bin/bash
set -euo pipefail
skill_root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
if [ ! -x "$skill_root/.venv/bin/python" ]; then
  "$skill_root/scripts/bootstrap.sh"
fi
if [ ! -x '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge' ]; then
  printf '%s\n' '请先准备官方 Microsoft Edge；此脚本不会下载替代浏览器。' >&2
  exit 1
fi
"$skill_root/.venv/bin/python" -m pip install -r "$skill_root/scripts/requirements-browser.txt"
printf '%s\n' '专用 Edge 采集依赖已准备。首次正常扫码登录由用户完成。'
