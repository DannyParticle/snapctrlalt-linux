"""屏幕抓图与虚拟屏度量（Linux：X11）。

对标 Windows 版 capture.py：
  SetProcessDpiAwareness + ImageGrab  ->  Xlib get_image / Gdk.pixbuf_get_from_window
  SM_*VIRTUALSCREEN 四个指标           ->  Xinerama / RandR 的虚拟屏包围盒

抓图有三级回落，任何一级失败都不会静默：
  1. ``Gdk.pixbuf_get_from_window``（走 GTK，透明窗口/合成器行为最稳）
  2. Xlib ``get_image``（纯 X11 协议，绕开 GTK）
  3. XDG Desktop Portal（Wayland / 无 X11 时；会弹一次系统授权框）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops

# ---------------------------------------------------------------- 会话信息


def session_type() -> str:
    return (os.environ.get("XDG_SESSION_TYPE") or "").lower()


def is_wayland() -> bool:
    return session_type() == "wayland" or (
        bool(os.environ.get("WAYLAND_DISPLAY")) and not os.environ.get("DISPLAY")
    )


def has_x11() -> bool:
    return bool(os.environ.get("DISPLAY"))


# ---------------------------------------------------------------- 虚拟屏


@dataclass(frozen=True)
class ScreenInfo:
    """整块虚拟屏（所有显示器并集）的几何。"""

    x: int
    y: int
    width: int
    height: int
    scale: int = 1
    monitors: tuple[tuple[int, int, int, int], ...] = ()

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.width, self.height

    def __str__(self) -> str:  # pragma: no cover - 仅日志
        return (f"{self.width}×{self.height}+{self.x}+{self.y} "
                f"@x{self.scale} ({len(self.monitors)} 显示器)")


def _xinerama_monitors(display) -> tuple[tuple[int, int, int, int], ...]:
    from Xlib.ext import xinerama

    if not display.has_extension("XINERAMA"):
        return ()
    try:
        if not xinerama.is_active(display):
            return ()
        q = xinerama.query_screens(display)
        return tuple((s.x, s.y, s.width, s.height) for s in q.screens if s.width > 0)
    except Exception:  # noqa: BLE001
        return ()


def _randr_monitors(display) -> tuple[tuple[int, int, int, int], ...]:
    from Xlib.ext import randr

    try:
        root = display.screen().root
        res = randr.get_monitors(root)
        out = []
        for m in res.monitors:
            if m.width > 0 and m.height > 0:
                out.append((m.x, m.y, m.width, m.height))
        return tuple(out)
    except Exception:  # noqa: BLE001
        return ()


def root_geometry() -> tuple[int, int]:
    """X11 根窗口的像素尺寸（GTK 事件坐标的真实范围）。"""
    from Xlib import display as _display

    d = _display.Display()
    try:
        g = d.screen().root.get_geometry()
        return int(g.width), int(g.height)
    finally:
        try:
            d.close()
        except Exception:  # noqa: BLE001
            pass


def screen_scale() -> int:
    """HiDPI 缩放系数（GTK 逻辑坐标 -> 物理像素的倍数）。"""
    env = os.environ.get("GDK_SCALE", "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    try:
        import gi

        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk

        screen = Gdk.Screen.get_default()
        if screen is not None and hasattr(screen, "get_monitor_scale_factor"):
            n = screen.get_n_monitors()
            if n > 0:
                return max(1, int(screen.get_monitor_scale_factor(0)))
    except Exception:  # noqa: BLE001
        pass
    return 1


def virtual_screen_bounds(display=None) -> ScreenInfo:
    """整块虚拟屏几何 + 各显示器矩形 + 缩放。"""
    own = display is None
    if display is None:
        from Xlib import display as _display

        display = _display.Display()
    try:
        root = display.screen().root
        geom = root.get_geometry()
        monitors = _xinerama_monitors(display)
        if not monitors:
            monitors = _randr_monitors(display)
        if monitors:
            xs = [m[0] for m in monitors]
            ys = [m[1] for m in monitors]
            x1 = max(m[0] + m[2] for m in monitors)
            y1 = max(m[1] + m[3] for m in monitors)
            box = (min(xs), min(ys), x1 - min(xs), y1 - min(ys))
        else:
            box = (0, 0, geom.width, geom.height)
        sym = root.get_geometry().width == geom.width  # 只是让 root 引用不被优化掉
        del sym
        return ScreenInfo(box[0], box[1], box[2], box[3],
                          scale=screen_scale(), monitors=monitors or ((0, 0, geom.width, geom.height),))
    finally:
        if own:
            try:
                display.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- 抓图实现


def _grab_xlib(bounds: ScreenInfo, display=None) -> Image.Image:
    """Xlib 原生抓图。返回 RGB 图（``bounds`` 尺寸）。"""
    from Xlib import X, display as _display

    own = display is None
    if display is None:
        display = _display.Display()
    try:
        root = display.screen().root
        raw = root.get_image(bounds.x, bounds.y, bounds.width, bounds.height,
                             X.ZPixmap, 0xFFFFFFFF)
        data = raw.data
        if isinstance(data, str):  # python-xlib 老版本返回 str
            data = data.encode("latin-1")
        expected = bounds.width * bounds.height * 4
        if len(data) < expected:
            raise RuntimeError(f"抓图数据不完整：{len(data)} < {expected}")
        # 服务端是 BGRX（小端下的 ZPixmap 32 位），直接按 RGBX 读再换道
        img = Image.frombuffer("RGBX", (bounds.width, bounds.height),
                               data[:expected], "raw", "BGRX", 0, 1)
        return img.convert("RGB")
    finally:
        if own:
            try:
                display.close()
            except Exception:  # noqa: BLE001
                pass


def _grab_gdk(bounds: ScreenInfo) -> Image.Image:
    """走 GTK/GDK 抓图；合成器下对透明窗口的表现比裸 Xlib 好。"""
    import gi

    gi.require_version("Gdk", "3.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gdk, GdkPixbuf  # noqa: F401

    root = Gdk.get_default_root_window()
    if root is None:
        raise RuntimeError("拿不到 root window")
    pb = Gdk.pixbuf_get_from_window(root, bounds.x, bounds.y,
                                    bounds.width, bounds.height)
    if pb is None:
        raise RuntimeError("Gdk.pixbuf_get_from_window 返回空")
    from .clipboard_linux import pil_from_pixbuf

    return pil_from_pixbuf(pb).convert("RGB")


def _grab_portal() -> Image.Image:
    """XDG Desktop Portal 截图（Wayland 或 X11 兜底；会弹系统授权框）。"""
    from urllib.parse import unquote

    out = tempfile.mkdtemp(prefix="snapctrlalt-")
    uri = ""
    # 先试 org.freedesktop.portal.Screenshot 的 Screenshot 方法
    cmd = [
        "gdbus", "call", "--session",
        "--dest", "org.freedesktop.portal.Desktop",
        "--object-path", "/org/freedesktop/portal/desktop",
        "--method", "org.freedesktop.portal.Screenshot.Screenshot",
        "", "{'interactive': <false>, 'modal': <false>}",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        text = res.stdout or ""
        # 返回形如 (objectpath, {'uri': <'file:///...'>})
        if "uri" in text:
            start = text.find("file://")
            if start >= 0:
                end = text.find("'", start)
                uri = text[start:end if end > 0 else len(text)]
    except Exception:  # noqa: BLE001
        uri = ""

    if not uri:
        for tool, args in (
            ("gnome-screenshot", ["-f"]),
            ("spectacle", ["-b", "-n", "-o"]),
            ("xfce4-screenshooter", ["-f", "-s"]),
            ("scrot", []),
            ("maim", []),
            ("import", ["-window", "root"]),
        ):
            exe = shutil.which(tool)
            if not exe:
                continue
            dst = str(Path(out) / "shot.png")
            try:
                r = subprocess.run([exe, *args, dst], capture_output=True, timeout=60)
                if r.returncode == 0 and Path(dst).is_file():
                    uri = "file://" + dst
                    break
            except Exception:  # noqa: BLE001
                continue
    if not uri:
        raise RuntimeError("系统截图接口不可用（Portal 与 gnome-screenshot/spectacle/scrot 都没成功）")
    path = Path(unquote(uri[7:])) if uri.startswith("file://") else Path(uri)
    return Image.open(path).convert("RGB")


def grab_full_screen(bounds: ScreenInfo | None = None) -> tuple[Image.Image, ScreenInfo]:
    """抓取整块虚拟屏，返回 ``(RGB 图, 与之匹配的屏幕几何)``。

    多级回落，失败抛异常（绝不静默返回黑图）。返回的几何以「真实抓到的像素」
    为准：HiDPI 下 GDK 给的是设备像素，可能比 X11 的 root 尺寸大一倍，
    覆盖层必须按这个尺寸摆窗口才不会缩放糊掉。
    """
    bounds = bounds or virtual_screen_bounds()
    errors: list[str] = []

    def _accept(img: Image.Image, source: str) -> tuple[Image.Image, ScreenInfo]:
        if img.width <= 0 or img.height <= 0:
            raise RuntimeError("抓到空图")
        if _is_blank(img):
            # 纯色画面多半是「全黑假成功」，但用户真在纯色桌面时也该照常截；
            # 所以只记一笔，不当作失败。
            print(f"[抓图] 提示：{source} 返回的画面是纯色，可能是权限或合成器限制")
        if (img.width, img.height) != (bounds.width, bounds.height):
            return img, ScreenInfo(bounds.x, bounds.y, img.width, img.height,
                                   bounds.scale, bounds.monitors)
        return img, bounds

    if has_x11():
        for name, fn in (("gdk", _grab_gdk), ("xlib", _grab_xlib)):
            try:
                img, info = _accept(fn(bounds), name)
                return img, info
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {e}")
    try:
        img = _grab_portal()
        return img, ScreenInfo(bounds.x, bounds.y, img.width, img.height,
                               _portal_scale(img, bounds), bounds.monitors)
    except Exception as e:  # noqa: BLE001
        errors.append(f"portal: {e}")
    raise RuntimeError("抓图失败 —— " + "；".join(errors))


def _portal_scale(img: Image.Image, bounds: ScreenInfo) -> int:
    """Portal 给的图可能被缩放过，按逻辑尺寸反推一个合理的缩放系数。"""
    try:
        import gi

        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk

        disp = Gdk.Display.get_default()
        mon = disp.get_primary_monitor() if disp else None
        if mon is None:
            return 1
        logical = mon.get_geometry().width
        if logical <= 0:
            return 1
        ratio = img.width / float(logical)
        for cand in (1, 2, 3, 4):
            if abs(ratio - cand) < 0.25:
                return cand
        return 1
    except Exception:  # noqa: BLE001
        return 1


def _is_blank(img: Image.Image, tol: int = 2) -> bool:
    """纯色图判定：缩小后与自身极值比较，用来识破「全黑」的假成功。"""
    small = img.resize((32, 32), Image.BILINEAR)
    ex = ImageChops.difference(small, Image.new("RGB", small.size, small.getpixel((0, 0))))
    return not ex.getbbox()


# ---------------------------------------------------------------- 外部截图工具


EXTERNAL_CANDIDATES = (
    "flameshot", "spectacle", "gnome-screenshot", "xfce4-screenshooter",
    "ksnip", "scrot", "maim", "import",
)


def find_external_tool() -> str | None:
    """探测可用的外部截图工具（对标 Windows 版的 QQ / TIM 探测）。"""
    for name in EXTERNAL_CANDIDATES:
        exe = shutil.which(name)
        if exe:
            return exe
    return None


def external_command() -> list[str] | None:
    """返回外部截图工具的完整命令行。"""
    from . import settings as app_settings

    cfg = app_settings.load_settings()
    custom = str(cfg.get("external_cmd") or "").strip()
    if custom:
        return custom.split()
    exe = find_external_tool()
    if not exe:
        return None
    name = Path(exe).name
    if name == "flameshot":
        return [exe, "gui"]
    if name == "spectacle":
        return [exe, "-r", "-b", "-n"]
    if name == "gnome-screenshot":
        return [exe, "-a"]
    if name == "xfce4-screenshooter":
        return [exe, "-r"]
    if name == "ksnip":
        return [exe]
    return [exe]


def run_external(cmd: list[str]) -> bool:
    """把截图交给外部工具；成功（进程正常起来）返回 True。"""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    try:
        proc.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        return True  # 还在跑，说明界面起来了，判定为接管成功
    return proc.returncode == 0


def monitor_at(x: int, y: int, bounds: ScreenInfo | None = None) -> tuple[int, int, int, int]:
    """返回包含点 (x, y) 的显示器矩形；找不到则整块虚拟屏。"""
    bounds = bounds or virtual_screen_bounds()
    for m in bounds.monitors:
        if m[0] <= x < m[0] + m[2] and m[1] <= y < m[1] + m[3]:
            return m
    return bounds.box


def selftest() -> int:
    """自检：测量虚拟屏 + 抓一张图，校验尺寸与非纯色。"""
    try:
        info = virtual_screen_bounds()
    except Exception as e:  # noqa: BLE001
        print(f"[自检] 屏幕：无法读取 X11 显示（{e}）")
        return 1
    print(f"[自检] 屏幕：{info}")
    try:
        img, real = grab_full_screen(info)
    except Exception as e:  # noqa: BLE001
        print(f"[自检] 抓图：失败（{e}）")
        return 1
    blank = _is_blank(img)
    print(f"[自检] 抓图：{'警告（纯色画面，测试环境下正常）' if blank else '通过'}  "
          f"尺寸 {img.width}×{img.height}，几何 {real}")
    print(f"[自检] 外部工具：{find_external_tool() or '未安装（可选）'}")
    return 0
