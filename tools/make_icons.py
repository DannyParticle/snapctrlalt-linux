#!/usr/bin/env python3
"""生成图标资源（不需要任何外部素材：图标本身就是代码画出来的）。

产出（hicolor 主题结构，deb / install.sh 直接可用）：
  share/icons/hicolor/scalable/apps/snapctrlalt.svg
  share/icons/hicolor/{16..512}x{...}/apps/snapctrlalt.png
  share/icons/hicolor/22x22/apps/snapctrlalt-tray.png（含 @2x）

用法：
    python3 tools/make_icons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cairo  # noqa: E402

ASSETS = ROOT / "share" / "icons" / "hicolor"

# 图标设计：深色圆角底 + 白色虚线选区角标 + 蓝色画笔点
BG = (0.16, 0.17, 0.20, 1.0)
FG = (1.0, 1.0, 1.0, 1.0)
ACCENT = (0.04, 0.52, 1.0, 1.0)


def draw_icon(cr: cairo.Context, size: float) -> None:
    u = size / 64.0
    r = 14 * u
    # 圆角方形底
    cr.move_to(r, 0)
    cr.line_to(size - r, 0)
    cr.curve_to(size, 0, size, 0, size, r)
    cr.line_to(size, size - r)
    cr.curve_to(size, size, size, size, size - r, size)
    cr.line_to(r, size)
    cr.curve_to(0, size, 0, size, 0, size - r)
    cr.line_to(0, r)
    cr.curve_to(0, 0, 0, 0, r, 0)
    cr.close_path()
    cr.set_source_rgba(*BG)
    cr.fill()

    # 虚线选区四角
    cr.set_line_width(5 * u)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_source_rgba(*FG)
    inset = 15 * u
    seg = 7 * u
    for hx, hy, dx, dy in ((inset, inset, 1, 0), (inset, inset, 0, 1),
                           (size - inset, inset, -1, 0), (size - inset, inset, 0, 1),
                           (inset, size - inset, 1, 0), (inset, size - inset, 0, -1),
                           (size - inset, size - inset, -1, 0), (size - inset, size - inset, 0, -1)):
        cr.move_to(hx, hy)
        cr.line_to(hx + dx * seg, hy + dy * seg)
        cr.stroke()

    # 中间的对勾
    cr.set_line_width(6 * u)
    cr.set_source_rgba(*ACCENT)
    cr.move_to(24 * u, 33 * u)
    cr.line_to(30 * u, 40 * u)
    cr.line_to(41 * u, 26 * u)
    cr.stroke()


SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 64 64">
  <rect x="0" y="0" width="64" height="64" rx="14" fill="#292b33"/>
  <g stroke="#ffffff" stroke-width="5" stroke-linecap="round" fill="none">
    <path d="M15 15 H22"/><path d="M15 15 V22"/>
    <path d="M49 15 H42"/><path d="M49 15 V22"/>
    <path d="M15 49 H22"/><path d="M15 49 V42"/>
    <path d="M49 49 H42"/><path d="M49 49 V42"/>
  </g>
  <path d="M24 33 L30 40 L41 26" stroke="#0a84ff" stroke-width="6"
        stroke-linecap="round" stroke-linejoin="round" fill="none"/>
</svg>
"""


def _render(size: int, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surf)
    cr.set_source_rgba(0, 0, 0, 0)
    cr.paint()
    draw_icon(cr, float(size))
    surf.write_to_png(str(path))


def main() -> int:
    scalable = ASSETS / "scalable" / "apps"
    scalable.mkdir(parents=True, exist_ok=True)
    (scalable / "snapctrlalt.svg").write_text(SVG, encoding="utf-8")

    written: list[Path] = [scalable / "snapctrlalt.svg"]
    for size in (16, 22, 24, 32, 48, 64, 128, 256, 512):
        p = ASSETS / f"{size}x{size}" / "apps" / "snapctrlalt.png"
        _render(size, p)
        written.append(p)

    # 托盘图标：22×22 为主，另存一份 48 供高分屏（与主图标同目录）
    tray_dir = ASSETS / "22x22" / "apps"
    for size, name in ((22, "snapctrlalt-tray.png"), (48, "snapctrlalt-tray@2x.png")):
        p = tray_dir / name
        _render(size, p)
        written.append(p)

    print(f"已生成图标：{ASSETS}")
    for p in written:
        print(f"  {p.relative_to(ASSETS.parent.parent.parent)}  {p.stat().st_size} 字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
