"""应用设置：JSON 配置（XDG 目录）+ 开机自启（autostart .desktop）。

对标 Windows 版 settings.py：
  %APPDATA%\\SnapCtrlAlt\\config.json  ->  ~/.config/snapctrlalt/config.json
  HKCU\\...\\Run 注册表项               ->  ~/.config/autostart/snapctrlalt.desktop
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

APP_NAME = "SnapCtrlAlt"
APP_ID = "snapctrlalt"
DESKTOP_FILE = f"{APP_ID}.desktop"

# ---------------------------------------------------------------- 路径


def _xdg(env: str, default: str) -> Path:
    raw = os.environ.get(env, "").strip()
    if raw:
        p = Path(raw).expanduser()
        if p.is_absolute():
            return p
    return Path(default).expanduser()


def config_home() -> Path:
    return _xdg("XDG_CONFIG_HOME", "~/.config")


def data_home() -> Path:
    return _xdg("XDG_DATA_HOME", "~/.local/share")


def project_root() -> Path:
    """项目根目录（放 share/、packaging/、安装脚本的地方）。

    兼容两种布局：``<root>/src/snapctrlalt/settings.py``（源码目录）与
    ``/usr/lib/python3/dist-packages/snapctrlalt/settings.py``（deb 安装）。
    后者取到的 ``/usr/lib/python3`` 不是项目根，靠 ``share/`` 是否存在来判别。
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "share" / "icons" / "hicolor").is_dir():
            return parent
    pkg_parent = here.parent.parent
    return pkg_parent.parent if pkg_parent.name == "src" else pkg_parent


def app_dir() -> Path:
    """可写数据目录。

    优先级：便携目录（仓库根的 ``portable`` 标记文件）> ``~/.config/snapctrlalt``
    > 只读主目录时的本地回退。配置目录不可写不应该让程序起不来。
    """
    portable = project_root() / "portable"
    if portable.is_file() and os.access(project_root(), os.W_OK):
        return project_root()
    candidates = [config_home() / APP_ID, project_root() / ".snapctrlalt-data"]
    last_err: OSError | None = None
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write-test"
            probe.touch()
            probe.unlink()
            return d
        except OSError as e:
            last_err = e
            continue
    # 都写不了：返回第一个路径，让上层在真正写入时报出清楚的错误
    if last_err is not None:
        print(f"[配置] 目录不可写（{last_err}），设置将无法保存")
    return candidates[0]


def config_path() -> Path:
    return app_dir() / "config.json"


def icon_dir() -> Path:
    return data_home() / "icons" / "hicolor"


def pictures_dir() -> Path:
    """系统图片目录，优先读 XDG 用户目录配置。"""
    cfg = config_home() / "user-dirs.dirs"
    if cfg.is_file():
        try:
            for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("XDG_PICTURES_DIR="):
                    val = line.split("=", 1)[1].strip().strip('"')
                    val = val.replace("$HOME", str(Path.home()))
                    if val:
                        p = Path(val)
                        if p.is_dir():
                            return p
        except OSError:
            pass
    for name in ("图片", "Pictures", "pictures"):
        p = Path.home() / name
        if p.is_dir():
            return p
    return Path.home()


# ---------------------------------------------------------------- 配置

DEFAULTS: dict = {
    "hotkey": "ctrl+alt+d",
    "quit_hotkey": "ctrl+alt+shift+q",
    "autostart": False,
    "save_dir": "",
    # 默认用内置覆盖层。原先默认 True（对标 Windows 版的「优先 QQ 截图」），
    # 但只要机器上装了 Flameshot / Spectacle 之类，用户按快捷键看到的就成了
    # **别人的界面**（本该出现的标注工具栏不见了）—— 对交付给别人的工具来说
    # 这个默认值是错的。要交出去就在托盘菜单或设置里打开。
    "prefer_external": False,
    "external_cmd": "",               # 自定义外部截图命令，空则自动探测
    "after_capture": "copy",          # copy / copy_save / save
    "copy_to_primary": False,         # 同时写入 X11 PRIMARY 选区（中键粘贴）
    "show_tray": True,
    "notify": True,
    "delay": 3,                       # 托盘「延时截图」的秒数（与 README 一致）
    "magnifier": True,
    "toolbar_theme": "light",
    "save_format": "png",
    "jpeg_quality": 95,
    "last_save_dir": "",
    "window_pin": True,               # 贴图（钉在桌面）
    "ui_scale": "auto",               # 覆盖层 UI 缩放：auto / 0.5~3.0
    # 工具栏形态：
    #   "canvas"（默认）= 画在覆盖层上，紧贴选区；
    #   "editor"        = 选完区域后开一个普通窗口（像画图程序）标注。
    # 之前尝试过的「浮在遮罩上的独立小窗 / 单独置顶窗口」两种形态需要和窗口管理器
    # 争层级与焦点，实测会出现「窗口显示了但收不到点击」，所以没有作为默认。
    "toolbar_mode": "canvas",
}

# 兼容旧键名（Windows 版 config.json 直接拿来也能跑）
_ALIASES = {"prefer_qq": "prefer_external", "qq_hotkey": "external_cmd"}


def load_settings() -> dict:
    cfg = dict(DEFAULTS)
    p = config_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    cfg[_ALIASES.get(k, k)] = v
        except (OSError, json.JSONDecodeError):
            pass
    cfg.setdefault("save_dir", "")
    return cfg


def save_settings(cfg: dict) -> None:
    p = config_path()
    tmp = p.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        raise RuntimeError(f"写入配置失败（{p} 不可写）: {e}") from e


# ---------------------------------------------------------------- 开机自启


def autostart_path() -> Path:
    return config_home() / "autostart" / DESKTOP_FILE


def autostart_enabled() -> bool:
    return autostart_path().is_file()


def _launch_cmd() -> str:
    """返回自启用的命令行。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    root = project_root()
    launcher = root / "snapctrlalt.sh"
    if launcher.is_file():
        return f'"{launcher}" --tray'
    py = shutil.which("python3") or sys.executable
    script = root / "snap.py"
    return f'"{py}" "{script}" --tray'


def set_autostart(enabled: bool) -> None:
    p = autostart_path()
    try:
        if enabled:
            p.parent.mkdir(parents=True, exist_ok=True)
            from . import assets

            icon = assets.icon("scalable") or (project_root() / "assets" / "snapctrlalt.svg")
            p.write_text(
                "[Desktop Entry]\n"
                "Type=Application\n"
                f"Name={APP_NAME}\n"
                "Name[zh_CN]=截图工具\n"
                "Comment=Press the hotkey to capture, annotate and copy\n"
                "Comment[zh_CN]=按快捷键截图、标注并复制到剪贴板\n"
                f"Exec={_launch_cmd()}\n"
                f"Icon={icon if icon.is_file() else 'camera-photo'}\n"
                "Terminal=false\n"
                "Categories=Utility;Graphics;\n"
                "StartupNotify=false\n"
                "X-GNOME-Autostart-enabled=true\n",
                encoding="utf-8",
            )
        elif p.exists():
            p.unlink()
    except OSError as e:
        raise RuntimeError(f"写入开机自启失败: {e}") from e


def ensure_config() -> dict:
    cfg = load_settings()
    if not config_path().exists():
        try:
            save_settings(cfg)
        except RuntimeError:
            pass
    return cfg
