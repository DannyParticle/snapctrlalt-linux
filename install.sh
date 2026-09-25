#!/usr/bin/env bash
# SnapCtrlAlt Linux 安装脚本（用户级安装，不需要 root）。
#
#   ./install.sh              安装到 ~/.local（图标 / 启动器 / 命令行）
#   ./install.sh --autostart  同时开启开机自启
#   ./install.sh --uninstall  卸载（保留 ~/.config/snapctrlalt 里的配置）
#
# 安装内容：
#   ~/.local/share/snapctrlalt/           程序本体（仓库拷贝）
#   ~/.local/bin/snapctrlalt              命令行入口（软链）
#   ~/.local/share/applications/snapctrlalt.desktop
#   ~/.local/share/icons/hicolor/*/apps/snapctrlalt.png
set -euo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
ROOT="$(cd -P "$(dirname "$SOURCE")" && pwd)"

PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/snapctrlalt"
BIN_DIR="$HOME/.local/bin"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m警告:\033[0m %s\n' "$*" >&2; }

uninstall() {
  say "卸载 SnapCtrlAlt"
  rm -f "$BIN_DIR/snapctrlalt"
  rm -f "$APP_DIR/snapctrlalt.desktop"
  rm -f "$AUTOSTART/snapctrlalt.desktop"
  for size in 16 22 24 32 48 64 128 256 512; do
    rm -f "$ICON_ROOT/${size}x${size}/apps/snapctrlalt.png"
  done
  if [ -d "$PREFIX" ]; then
    case "$PREFIX" in
      */snapctrlalt) rm -rf "$PREFIX" ;;
      *) warn "拒绝删除意外的路径：$PREFIX" ;;
    esac
  fi
  say "已卸载（配置与截图保留在 ~/.config/snapctrlalt 和图片目录）"
  exit 0
}

[ "${1:-}" = "--uninstall" ] && uninstall

say "检查依赖"
MISSING=()
python3 - <<'EOF' || MISSING+=(python3)
import sys
sys.exit(0 if sys.version_info >= (3, 9) else 1)
EOF
for mod in gi cairo PIL Xlib; do
  python3 -c "import $mod" 2>/dev/null || MISSING+=("python3-$( [ "$mod" = gi ] && echo gi || echo "$mod" )")
done
python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null \
  || MISSING+=("gir1.2-gtk-3.0")

if [ ${#MISSING[@]} -gt 0 ]; then
  warn "缺少依赖：${MISSING[*]}"
  cat <<'EOF'
请先安装（Linux Mint / Ubuntu / Debian）：
  sudo apt install python3-gi python3-gi-cairo python3-cairo python3-pil \
                   gir1.2-gtk-3.0 python3-xlib gir1.2-ayatanaappindicator3-0.1
EOF
  exit 1
fi
say "依赖齐全"

say "生成图标"
python3 "$ROOT/tools/make_icons.py" >/dev/null

say "复制程序到 $PREFIX"
mkdir -p "$PREFIX"
for item in snap.py snapctrlalt snapctrlalt.sh assets snapctrlalt.desktop README.md LICENSE; do
  [ -e "$ROOT/$item" ] && cp -r "$ROOT/$item" "$PREFIX/"
done

say "安装图标"
for size in 16 22 24 32 48 64 128 256 512; do
  src="$ROOT/assets/snapctrlalt-$size.png"
  [ -f "$src" ] || continue
  dst="$ICON_ROOT/${size}x${size}/apps"
  mkdir -p "$dst"
  cp "$src" "$dst/snapctrlalt.png"
done
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$ICON_ROOT" >/dev/null 2>&1 || true
fi

say "安装命令行入口 $BIN_DIR/snapctrlalt"
mkdir -p "$BIN_DIR"
ln -sfn "$PREFIX/snapctrlalt.sh" "$BIN_DIR/snapctrlalt"
chmod +x "$PREFIX/snapctrlalt.sh" "$ROOT/snapctrlalt.sh" 2>/dev/null || true

say "安装启动器"
mkdir -p "$APP_DIR"
sed "s|^Exec=.*|Exec=$PREFIX/snapctrlalt.sh|; s|^TryExec=.*|TryExec=$PREFIX/snapctrlalt.sh|" \
  "$ROOT/snapctrlalt.desktop" > "$APP_DIR/snapctrlalt.desktop"
command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true

if [ "${1:-}" = "--autostart" ]; then
  say "开启开机自启"
  mkdir -p "$AUTOSTART"
  sed "s|^Exec=.*|Exec=$PREFIX/snapctrlalt.sh --tray|; s|^TryExec=.*|TryExec=$PREFIX/snapctrlalt.sh|" \
    "$ROOT/snapctrlalt.desktop" > "$AUTOSTART/snapctrlalt.desktop"
fi

say "自检"
"$PREFIX/snapctrlalt.sh" --selftest || warn "自检未全部通过，请运行 python3 tools/diagnose.py 查看详情"

cat <<EOF

安装完成 🎉

  启动（托盘常驻）   : snapctrlalt
  只截一次           : snapctrlalt --once
  设置               : snapctrlalt --settings
  开机自启           : snapctrlalt 托盘菜单里勾选，或 ./install.sh --autostart
  卸载               : ./install.sh --uninstall

如果命令找不到，把 ~/.local/bin 加进 PATH：
  echo 'export PATH="\$HOME/.local/bin:\$PATH"' >> ~/.bashrc && source ~/.bashrc

EOF
