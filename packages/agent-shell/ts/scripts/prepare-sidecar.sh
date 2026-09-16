#!/usr/bin/env bash
# Build the embedded portable Python runtime that backs the steerable-sidecar.
# agent-shell 打包 helper（3.4 从 deeppath-agent 上提）：产品仓库打包前调用，
# 把 sidecar 运行时产出到产品的 resources/python-runtime/<platform>/，由
# 产品的 electron-builder extraResources 收进安装包。
#
# Usage:
#   prepare-sidecar.sh [--out <dir>] [--target {host|all|<name>}] [--skip-wheels]
#                      [--strip-stdlib] [--aggressive]
#
# --out 缺省 $PWD/resources/python-runtime。框架仓位置用
# STEERABLE_FRAMEWORK_DIR 覆盖；缺省按本脚本在框架仓内的位置解析
#（packages/agent-shell/ts/scripts → 仓根是 ../../../..）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FW="${STEERABLE_FRAMEWORK_DIR:-$(cd "$SCRIPT_DIR/../../../.." && pwd)}"
OUT_DIR=""
TARGET="host"
SKIP_WHEELS=0
EXTRA_BUILD_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)            OUT_DIR="$2"; shift 2 ;;
    --target)         TARGET="$2"; shift 2 ;;
    --skip-wheels)    SKIP_WHEELS=1; shift ;;
    --strip-stdlib|--aggressive)
                      EXTRA_BUILD_ARGS+=("$1"); shift ;;
    # pnpm 10 会把脚本调用里的 `--` 分隔符当成字面参数透传，落到这里。
    # 直接丢掉避免污染 build_sidecar.py argparse（它没有 positional args，
    # `--` 会被当成 end-of-options 标记，让后续 `--strip-stdlib` 报错）。
    --)               shift ;;
    *)                EXTRA_BUILD_ARGS+=("$1"); shift ;;
  esac
done

OUT_DIR="${OUT_DIR:-$PWD/resources/python-runtime}"

if [[ ! -d "$FW/packages/sidecar/build" ]]; then
  echo "ERROR: $FW does not look like a steerable-framework checkout." >&2
  exit 1
fi

if [[ "$SKIP_WHEELS" -eq 0 ]]; then
  bash "$SCRIPT_DIR/prepare-framework-wheels.sh"
else
  echo "[prepare-sidecar] --skip-wheels set, assuming $FW/dist/py/ is current"
fi

WHEELS_ABS="$(cd "$FW/dist/py" && pwd)"
mkdir -p "$OUT_DIR"

echo "[prepare-sidecar] building portable Python runtime (target=$TARGET)"
PYTHON_CMD="python3"
if ! python3 --version &>/dev/null; then
  PYTHON_CMD="python"
fi
"$PYTHON_CMD" "$FW/packages/sidecar/build/build_sidecar.py" \
  --target "$TARGET" \
  --from-wheels "$WHEELS_ABS" \
  "${EXTRA_BUILD_ARGS[@]}"

echo "[prepare-sidecar] copying runtime into $OUT_DIR"
SRC="$FW/packages/sidecar/dist/python-runtime"
rm -rf "$OUT_DIR" && mkdir -p "$OUT_DIR"
# Skip the _cache/ directory — only contains the upstream tar.gz downloads.
for entry in "$SRC"/*; do
  name="$(basename "$entry")"
  if [[ "$name" == "_cache" ]]; then continue; fi
  cp -R "$entry" "$OUT_DIR/$name"
done

echo "[prepare-sidecar] done — runtime at $OUT_DIR/<platform>/"
ls -lh "$OUT_DIR"
