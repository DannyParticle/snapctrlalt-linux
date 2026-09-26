#!/usr/bin/env python3
"""环境诊断：一条命令看清这台机器上截图工具能用哪些能力。

    python3 tools/diagnose.py

不修改任何配置，也不弹截图界面（托盘/通知只探测后端，不注册热键）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

OK = "✓"
NO = "✗"
WARN = "!"


def line(name: str, status: str, detail: str = "") -> None:
    print(f"  {status} {name:<26}{detail}")


def main() -> int:
    print("SnapCtrlAlt Linux 环境诊断")
    print(f"  项目目录：{ROOT}")
    print(f"  Python：{sys.version.split()[0]}  可执行：{sys.executable}")
    print()

    print("[会话]")
    line("XDG_SESSION_TYPE", OK if os.environ.get("XDG_SESSION_TYPE") else WARN,
         os.environ.get("XDG_SESSION_TYPE", "未设置"))
    line("DISPLAY", OK if os.environ.get("DISPLAY") else NO,
         os.environ.get("DISPLAY", "无（纯 Wayland？）"))
    line("WAYLAND_DISPLAY", OK if os.environ.get("WAYLAND_DISPLAY") else "-",
         os.environ.get("WAYLAND_DISPLAY", "无"))
    line("桌面环境", OK, os.environ.get("XDG_CURRENT_DESKTOP", "未知"))
    line("XDG_RUNTIME_DIR", OK if os.environ.get("XDG_RUNTIME_DIR") else WARN,
         os.environ.get("XDG_RUNTIME_DIR", "未设置（会退回 /tmp）"))
    print()

    print("[依赖]")
    for mod, pretty in (("gi", "PyGObject"), ("cairo", "pycairo"),
                        ("PIL", "Pillow"), ("Xlib", "python-xlib")):
        try:
            __import__(mod)
            line(pretty, OK, "已安装")
        except ImportError as e:
            line(pretty, NO, f"缺失（{e}）")

    for ns, ver, pretty in (("Gtk", "3.0", "GTK 3"),
                            ("Gdk", "3.0", "GDK 3"),
                            ("GdkPixbuf", "2.0", "GdkPixbuf"),
                            ("Pango", "1.0", "Pango"),
                            ("PangoCairo", "1.0", "PangoCairo"),
                            ("Notify", "0.7", "桌面通知"),
                            ("AyatanaAppIndicator3", "0.1", "Ayatana 托盘")):
        try:
            import gi

            gi.require_version(ns, ver)
            __import__("gi.repository", fromlist=[ns])
            line(pretty, OK, f"{ns}-{ver}")
        except Exception as e:  # noqa: BLE001
            line(pretty, WARN, f"不可用（{type(e).__name__}）")
    print()

    print("[屏幕与抓图]")
    try:
        from snapctrlalt import capture_linux as capture

        info = capture.virtual_screen_bounds()
        line("虚拟屏几何", OK, str(info))
        line("缩放系数", OK, f"x{capture.screen_scale()}")
        try:
            img, real = capture.grab_full_screen(info)
            line("抓图", OK, f"{img.width}×{img.height}（几何 {real}）")
        except Exception as e:  # noqa: BLE001
            line("抓图", NO, str(e))
        for tool in capture.EXTERNAL_CANDIDATES:
            exe = shutil.which(tool)
            if exe:
                line(f"外部工具 {tool}", OK, exe)
        cmd = capture.external_command()
        line("将调用的外部命令", OK if cmd else "-",
             " ".join(cmd) if cmd else "无（用内置覆盖层）")
    except Exception as e:  # noqa: BLE001
        line("抓图模块", NO, f"{type(e).__name__}: {e}")
    print()

    print("[剪贴板]")
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk

        Gtk.init_check(sys.argv[:1])
        from snapctrlalt import clipboard_linux as cb

        rc = cb.selftest()
        line("写读往返", OK if rc == 0 else NO, "通过" if rc == 0 else "失败")
        line("剪贴板管理器", OK if shutil.which("klipper") or shutil.which("copyq") else "-",
             "klipper/copyq 已安装" if (shutil.which("klipper") or shutil.which("copyq"))
             else "未检测到（退出后内容不留存属正常）")
    except Exception as e:  # noqa: BLE001
        line("剪贴板", NO, f"{type(e).__name__}: {e}")
    print()

    print("[全局热键]")
    try:
        from snapctrlalt import hotkey_linux as hk

        mods, key = hk.parse_hotkey("ctrl+alt+d")
        from Xlib import display as xdisplay

        d = xdisplay.Display()
        code = hk._keycode_of(d, hk.keysym_of(key))
        d.close()
        line("X11 连接与键位映射", OK, f"ctrl+alt+d -> keycode {code}")
        line("修饰键掩码", OK, f"0x{mods:x}")
    except Exception as e:  # noqa: BLE001
        line("全局热键", WARN, f"不可用（{type(e).__name__}: {e}）"
                               " —— 可用 --once 绑桌面快捷键")
    print()

    print("[坐标标定]")
    try:
        import gi as _gi

        _gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk as _Gtk

        _Gtk.init_check(sys.argv[:1])
        from snapctrlalt import geometry as geo_mod

        info = capture.virtual_screen_bounds()
        img, real = capture.grab_full_screen(info)
        ge = geo_mod.measure((img.width, img.height), gdk_scale=0)
        for ln in ge.describe().splitlines():
            print(f"    {ln}")
        problems = ge.check()
        if problems:
            for pr in problems:
                line(f"警告：{pr}", NO)
        else:
            line("标定自检", OK, "无异常")
        gdk_scale = os.environ.get("GDK_SCALE", "未设置")
        line("GDK_SCALE", OK if gdk_scale == "1" else WARN,
             f"{gdk_scale}（建议为 1：覆盖层坐标依赖它固定在设备像素）")
    except Exception as e:  # noqa: BLE001
        line("坐标标定", NO, f"{type(e).__name__}: {e}")
    print()

    print("[配置与自启]")
    from snapctrlalt import settings

    line("配置文件", OK, str(settings.config_path()))
    line("可写", OK if os.access(settings.config_path().parent, os.W_OK) else NO,
         str(settings.config_path().parent))
    line("自启文件", OK if settings.autostart_enabled() else "-",
         str(settings.autostart_path()))
    line("图片目录", OK, str(settings.pictures_dir()))
    print()

    print("[桌面集成]")
    for rel in ("share/applications/snapctrlalt.desktop",
                "share/icons/hicolor/scalable/apps/snapctrlalt.svg",
                "share/icons/hicolor/22x22/apps/snapctrlalt.png",
                "bin/snapctrlalt", "snapctrlalt.sh",
                "packaging/build-deb.sh"):
        p = ROOT / rel
        line(rel, OK if p.is_file() else WARN, "存在" if p.is_file() else "缺失")
    print()
    print("提示：`./snapctrlalt.sh --selftest` 做完整自检，"
          "`./snapctrlalt.sh --perf` 测帧耗时，"
          "`./packaging/build-deb.sh` 构建安装包。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
