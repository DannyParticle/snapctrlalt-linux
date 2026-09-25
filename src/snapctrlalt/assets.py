"""资源路径解析：让同一份代码在源码目录、本地安装、系统 deb 安装下都能找到图标。

三种落地形态对应三个位置：

============================  ==========================================================
运行方式                       图标所在
============================  ==========================================================
源码目录（git clone）          <repo>/share/icons/hicolor/{22x22,scalable}/apps/
``install.sh``（~/.local）     ~/.local/share/icons/hicolor/{...}/apps/
deb（/usr/lib/python3/...）    /usr/share/icons/hicolor/{...}/apps/
============================  ==========================================================

``SNAP_ASSETS_DIR`` 可以覆盖，方便测试与自定义打包。
"""

from __future__ import annotations

import os
from pathlib import Path

from .settings import project_root

# 托盘图标（22×22 为主，高分屏用 @2x）
TRAY_ICON = "snapctrlalt-tray.png"
TRAY_ICON_2X = "snapctrlalt-tray@2x.png"
APP_ICON = "snapctrlalt.png"
SCALABLE_ICON = "snapctrlalt.svg"


def _candidates() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("SNAP_ASSETS_DIR", "").strip()
    if env:
        dirs.append(Path(env).expanduser())
    root = project_root()
    dirs += [
        root / "share" / "icons" / "hicolor",          # 源码目录
        Path.home() / ".local/share/icons/hicolor",     # install.sh
        Path("/usr/share/icons/hicolor"),               # deb
        Path("/usr/local/share/icons/hicolor"),
    ]
    return dirs


def icon(kind: str = "tray", size: str = "22x22") -> Path | None:
    """返回指定图标的实际路径；找不到返回 None（调用方应回退到主题图标名）。"""
    name = {"tray": TRAY_ICON, "tray2x": TRAY_ICON_2X,
            "app": APP_ICON, "scalable": SCALABLE_ICON}.get(kind, kind)
    sub = "scalable/apps" if name.endswith(".svg") else f"{size}/apps"
    for base in _candidates():
        p = base / sub / name
        if p.is_file():
            return p
    # 退一步：任意尺寸目录里同名文件
    for base in _candidates():
        if not base.is_dir():
            continue
        for hit in sorted(base.glob(f"*/apps/{name}")):
            if hit.is_file():
                return hit
    return None


def tray_icon_path() -> str | None:
    """托盘图标；专用图缺失时退回应用图标（两者本来就是同一张画）。"""
    for kind in ("tray", "app"):
        p = icon(kind)
        if p:
            return str(p)
    return None


def describe() -> str:
    p = tray_icon_path()
    return str(p) if p else "未找到（将回退到主题图标 camera-photo）"
