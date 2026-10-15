#!/usr/bin/env bash
# 校园网自连 - Linux/macOS 启动脚本
set -e
cd "$(dirname "$0")/.."

export PYTHONIOENCODING=utf-8
export PYTHONPATH="$PWD/project/backend"

exec python3 -m campusnet serve --port 2268