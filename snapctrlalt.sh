#!/usr/bin/env bash
# SnapCtrlAlt 本地启动脚本（对标 Windows 版的「启动截图.bat」）。
#
#   ./snapctrlalt.sh              托盘常驻
#   ./snapctrlalt.sh --once       截一次就退出
#   ./snapctrlalt.sh --settings   打开设置
#   ./snapctrlalt.sh --selftest   基础自检
#
# 默认强制用内置覆盖层（QQ 截图那套交互）；想让 Flameshot / Spectacle 接管，
# 设 SNAP_ALLOW_EXTERNAL=1，或在设置里关掉「优先调用外部截图工具」。
#
# 依赖检查与解释器选择都在 bin/snapctrlalt 里，这里只负责定位与转发。
set -euo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
ROOT="$(cd -P "$(dirname "$SOURCE")" && pwd)"

PY="${SNAP_PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[SnapCtrlAlt] 找不到 python3" >&2
  exit 1
fi

# 覆盖层坐标链固定在设备像素上（必须在 GTK 初始化前生效）
export GDK_SCALE="${SNAP_GDK_SCALE:-1}"
export GDK_DPI_SCALE="${SNAP_GDK_DPI_SCALE:-1}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

ARGS=("$@")
if [ "${SNAP_ALLOW_EXTERNAL:-0}" != "1" ]; then
  has_local=0
  for a in ${ARGS[@]+"${ARGS[@]}"}; do
    [ "$a" = "--local" ] && has_local=1
  done
  [ "$has_local" = "0" ] && ARGS+=("--local")
fi

exec "$PY" "$ROOT/bin/snapctrlalt" ${ARGS[@]+"${ARGS[@]}"}
