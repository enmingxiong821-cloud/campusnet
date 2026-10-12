#!/bin/sh
# 校园网自连 - 一键配置（Linux / macOS）
set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "[错误] 没找到 python3。请先安装 Python 3.10+：https://www.python.org/downloads/"
  exit 1
fi

exec "$PY" utils/run.py setup "$@"
