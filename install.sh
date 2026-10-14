#!/bin/sh
# 校园网自连 · 一行命令安装（Linux / macOS）
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/enmingxiong821-cloud/campusnet/main/install.sh | sh
# 也可以下载后 ./install.sh；支持 --dry-run。
# 设计原则：幂等、断点续跑（已有目录走升级）、密码不进命令行、中文输出。

set -e
REPO="https://github.com/enmingxiong821-cloud/campusnet.git"
BRANCH="${CAMPUSNET_BRANCH:-main}"
INSTALL_DIR="${CAMPUSNET_INSTALL_DIR:-}"

# 就地运行（仓库已解压好、直接从 install.sh 所在目录跑）时，不需要下载。
IN_PLACE=0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$SCRIPT_DIR/utils/run.py" ]; then
  IN_PLACE=1
  INSTALL_DIR="$SCRIPT_DIR"
fi

# 只认 --dry-run 这一个开关，其余参数原样透传给 setup（例如 --with-browser）。
SETUP_ARGS=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) SETUP_ARGS="$SETUP_ARGS $arg" ;;
  esac
done

if [ -z "$INSTALL_DIR" ]; then
  if [ "$(uname -s)" = "Darwin" ]; then
    INSTALL_DIR="$HOME/Library/Application Support/campusnet"
  else
    INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/campusnet"
  fi
fi

say() { printf '%s\n' "$*"; }
die() {
  printf '[错误] %s\n' "$*" >&2
  printf '        手动兜底：先装 Python 3.10+，再从 %s 下载仓库，解压后运行 ./install.sh。\n' "$REPO" >&2
  exit 1
}

# ---- 1. 找 Python ----
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  die "没找到 python3。请先安装 Python 3.10+：https://www.python.org/downloads/"
fi
VER_OUT=$("$PY" --version 2>&1 || true)
case "$VER_OUT" in
  "Python 3."*) ;;
  *) die "python --version 异常：$VER_OUT" ;;
esac
MAJOR=$(printf '%s' "$VER_OUT" | sed -E 's/^Python ([0-9]+)\.([0-9]+).*/\1/')
MINOR=$(printf '%s' "$VER_OUT" | sed -E 's/^Python ([0-9]+)\.([0-9]+).*/\2/')
if [ "$MAJOR" -lt 3 ] || { [ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 10 ]; }; then
  die "Python 版本 $VER_OUT 过低，需要 3.10 或更高"
fi
say "[OK]  Python：$VER_OUT"

# ---- 2. 取仓库（已有则升级）----
RUN_PY="$INSTALL_DIR/utils/run.py"
if [ -f "$RUN_PY" ]; then
  say "[信息] 已存在安装目录 $INSTALL_DIR，走升级分支"
  if [ -d "$INSTALL_DIR/.git" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      say "[预演] 会执行：git -C $INSTALL_DIR pull --ff-only"
    else
      git -C "$INSTALL_DIR" pull --ff-only
      say "[OK]  已升级到最新"
    fi
  else
    say "[信息] 目录存在但非 git 仓库，跳过升级，直接复用现有文件"
  fi
elif [ "$IN_PLACE" -eq 0 ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    say "[预演] 会执行：git clone --depth 1 --branch $BRANCH $REPO $INSTALL_DIR"
  else
    if command -v git >/dev/null 2>&1; then
      say "[信息] 正在 git clone --depth 1（约几 MB）……"
      git clone --depth 1 --branch "$BRANCH" "$REPO" "$INSTALL_DIR"
    else
      # 没 git：拉分支归档兜底。raw.githubusercontent.com 只发单个文件，
      # 取分支要用 codeload 的归档地址；这里的归档没有随附校验和，
      # 完整性靠 HTTPS 传输本身（Release 分发包才有 sha256）。
      ARCHIVE_URL="https://github.com/enmingxiong821-cloud/campusnet/archive/refs/heads/$BRANCH.tar.gz"
      TMP_DIR=$(mktemp -d)
      say "[信息] 没找到 git，改用归档下载……"
      if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$ARCHIVE_URL" -o "$TMP_DIR/repo.tar.gz"
      elif command -v wget >/dev/null 2>&1; then
        wget -qO "$TMP_DIR/repo.tar.gz" "$ARCHIVE_URL"
      else
        die "没找到 git / curl / wget，装一个再跑，或手动下载仓库归档"
      fi
      tar -xzf "$TMP_DIR/repo.tar.gz" -C "$TMP_DIR"
      # 归档里套着一层 campusnet-<分支>/，把它里面的东西平铺进安装目录
      INNER=$(find "$TMP_DIR" -maxdepth 1 -type d -name 'campusnet-*' | head -n 1)
      [ -n "$INNER" ] || die "归档解出来的目录结构不认识，请手动下载仓库归档"
      mkdir -p "$INSTALL_DIR"
      cp -R "$INNER"/. "$INSTALL_DIR"/
      rm -rf "$TMP_DIR"
      say "[OK]  已获取仓库文件"
    fi
  fi
fi

# ---- 3. 委托给 setup ----
if [ "$DRY_RUN" -eq 1 ]; then
  say "[预演] 会执行：$PY $RUN_PY setup$SETUP_ARGS"
else
  say "[信息] 正在运行一键配置……"
  # shellcheck disable=SC2086
  exec "$PY" "$RUN_PY" setup $SETUP_ARGS
fi
