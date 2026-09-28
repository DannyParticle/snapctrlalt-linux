"""全屏截图覆盖层（Linux / GTK3 + Cairo）。

对标 Windows 版 overlay.py：交互、工具集、工具栏布局、键盘快捷键全部一致，
渲染层从 Tk 画布 + Pillow 换成 Cairo 直接绘制。

渲染策略（对应 Windows 版的三层缓存思路）：
  * ``self._base``   冻结的整屏 Cairo surface，全程复用；
  * 选区压暗用一次整屏 ``paint_with_alpha`` 得到 ``_dim``，之后每帧只重画
    「选区内部的亮图 + 四条压暗边带」，避免整屏重绘；
  * 形状是纯绘图指令列表，每帧直接重画（Cairo 画几十个图元 + 一次裁切贴图，
    比 Pillow 重新合成整屏快得多），所以撤销 / 重做只改列表不碰像素。
"""

from __future__ import annotations

import math
import os
import time
from typing import Callable

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango, PangoCairo  # noqa: E402

from PIL import Image, ImageFilter  # noqa: E402

from . import geometry  # noqa: E402

# ---------------------------------------------------------------- 常量

T_SELECT = "select"
T_RECT = "rect"
T_ELLIPSE = "ellipse"
T_ARROW = "arrow"
T_PEN = "pen"
T_HIGHLIGHT = "highlight"
T_TEXT = "text"
T_SEQ = "seq"
T_MOSAIC = "mosaic"
T_BLUR = "blur"
T_PICKER = "picker"

TOOL_ORDER = (T_SELECT, T_RECT, T_ELLIPSE, T_ARROW, T_PEN, T_HIGHLIGHT,
              T_TEXT, T_SEQ, T_MOSAIC, T_BLUR, T_PICKER)

COLORS = ["#ff3b30", "#ffcc00", "#34c759", "#007aff", "#000000", "#ffffff"]
WIDTHS = [2, 4, 8]

DIM_ALPHA = 0.58          # 选区外的压暗程度（对应 Windows 版的 0.42 亮度乘子）
HANDLE = 8                # 手柄边长（逻辑像素）
MAG_ZOOM = 8              # 放大镜倍数
MAG_SIZE = 152            # 放大镜边长
MAG_BAR = 44              # 放大镜下方读数条高度

TOOL_NAMES = {
    T_SELECT: "选择", T_RECT: "矩形", T_ELLIPSE: "椭圆", T_ARROW: "箭头",
    T_PEN: "画笔", T_HIGHLIGHT: "荧光笔", T_TEXT: "文字", T_SEQ: "序号",
    T_MOSAIC: "马赛克", T_BLUR: "模糊", T_PICKER: "取色",
}

TOOL_TIPS = {
    T_SELECT: "选择 · 拖动边角/内部调整选区",
    T_RECT: "矩形 · 拖动绘制",
    T_ELLIPSE: "椭圆 · 拖动绘制",
    T_ARROW: "箭头 · 拖动绘制",
    T_PEN: "画笔 · 按住自由绘制，滚轮调粗细",
    T_HIGHLIGHT: "荧光笔 · 半透明涂抹重点",
    T_TEXT: "文字 · 点击画面输入",
    T_SEQ: "序号 · 点击添加 ①②③",
    T_MOSAIC: "马赛克 · 拖动涂抹",
    T_BLUR: "模糊 · 拖动涂抹（高斯模糊）",
    T_PICKER: "取色 · 点击画面取色",
}

_HINT_SELECT = "拖动框选区域 · 双击整屏 · Enter 完成 · Esc/右键 取消"

# ①..⑳ 序号字形
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

# 调色盘
BAR_BG = (0.949, 0.949, 0.969, 0.98)
BAR_BORDER = (0.78, 0.78, 0.80, 1.0)
SEP = (0.78, 0.78, 0.80, 1.0)
ICON_COLOR = (0.11, 0.11, 0.12, 1.0)
ICON_ACTIVE = (1, 1, 1, 1)
ACTIVE_BG = (0.039, 0.518, 1.0, 1.0)
HOVER_BG = (0.82, 0.82, 0.86, 1.0)
LABEL_BG = (0.96, 0.96, 0.97, 0.96)
LABEL_FG = (0.11, 0.11, 0.12, 1.0)
LABEL_BORDER = (0.82, 0.82, 0.84, 1.0)
TIP_BG = (0.11, 0.11, 0.12, 0.92)
TIP_FG = (1, 1, 1, 1)


def _font(size: float, bold: bool = False) -> Pango.FontDescription:
    fd = Pango.FontDescription()
    fd.set_family("Noto Sans CJK SC, Source Han Sans SC, WenQuanYi Micro Hei, Sans")
    fd.set_absolute_size(size * Pango.SCALE)
    if bold:
        fd.set_weight(Pango.Weight.BOLD)
    return fd


def _norm_box(x0: float, y0: float, x1: float, y1: float):
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return x0, y0, x1, y1


def _clip_box(box, region):
    if region is None:
        return box
    x0 = max(box[0], region[0])
    y0 = max(box[1], region[1])
    x1 = min(box[2], region[2])
    y1 = min(box[3], region[3])
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return x0, y0, x1, y1


def pil_to_surface(img: Image.Image) -> cairo.ImageSurface:
    """PIL -> Cairo surface。

    Cairo 的 ``ARGB32`` 是预乘 alpha 的，PIL 的 RGBA 不是；不透明图（截屏主体）
    直接按 BGRA 字节序喂给 Cairo 即可，只有真带 alpha 的图才需要预乘（那会走
    Python 逐像素循环，所以要避开）。
    """
    if img.mode == "RGB":
        rgb = img.tobytes()
        bgra = bytearray(len(rgb) // 3 * 4)
        bgra[0::4] = rgb[2::3]
        bgra[1::4] = rgb[1::3]
        bgra[2::4] = rgb[0::3]
        bgra[3::4] = b"\xff" * (len(bgra) // 4)
        return cairo.ImageSurface.create_for_data(
            bgra, cairo.FORMAT_ARGB32, img.width, img.height, img.width * 4
        )
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    data = bytearray(img.tobytes())
    if data[3::4] != b"\xff" * (len(data) // 4):  # 有透明像素时才预乘
        for i in range(0, len(data), 4):
            a = data[i + 3]
            if a == 255:
                continue
            if a == 0:
                data[i] = data[i + 1] = data[i + 2] = 0
            else:
                f = a / 255.0
                data[i] = int(data[i] * f)
                data[i + 1] = int(data[i + 1] * f)
                data[i + 2] = int(data[i + 2] * f)
    return cairo.ImageSurface.create_for_data(
        data, cairo.FORMAT_ARGB32, img.width, img.height, img.width * 4
    )


def surface_to_pil(surface: cairo.ImageSurface) -> Image.Image:
    """Cairo surface -> PIL。

    走 GdkPixbuf（C 实现）最快；拿不到就退回纯 Python 去预乘的慢路径。
    """
    try:
        pb = Gdk.pixbuf_get_from_surface(surface, 0, 0,
                                         surface.get_width(), surface.get_height())
        data = pb.get_pixels()
        img = Image.frombuffer("RGBA", (pb.get_width(), pb.get_height()), data,
                               "raw", "RGBA", pb.get_rowstride(), 1)
        return img.copy()
    except Exception:  # noqa: BLE001
        pass
    surface.flush()
    w, h = surface.get_width(), surface.get_height()
    buf = bytes(surface.get_data())
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA",
                           surface.get_stride(), 1).copy()
    px = img.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a not in (0, 255):
                f = 255.0 / a
                px[x, y] = (min(255, int(r * f)), min(255, int(g * f)),
                            min(255, int(b * f)), a)
    return img


# ---------------------------------------------------------------- 图标（Cairo 线性图标）


def draw_icon(cr: cairo.Context, kind: str, size: float, color=ICON_COLOR) -> None:
    """在 ``size``×``size`` 的方框里画 24×24 设计网格的线性图标。

    对标 Windows 版用 Pillow 超采样画图标再降采样的做法：这里用 Cairo 的
    抗锯齿描边直接画，线宽统一，不依赖任何字体（缺字体时不会变成方框）。
    """
    u = size / 24.0
    lw = max(1.2, 1.85 * u)
    cr.save()
    cr.set_line_width(lw)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.set_source_rgba(*color)

    def line(x0, y0, x1, y1):
        cr.move_to(x0 * u, y0 * u)
        cr.line_to(x1 * u, y1 * u)
        cr.stroke()

    def rect(x0, y0, x1, y1, radius=0.0):
        if radius:
            x0, y0, x1, y1 = x0 * u, y0 * u, x1 * u, y1 * u
            r = radius * u
            cr.move_to(x0 + r, y0)
            cr.line_to(x1 - r, y0)
            cr.curve_to(x1, y0, x1, y0, x1, y0 + r)
            cr.line_to(x1, y1 - r)
            cr.curve_to(x1, y1, x1, y1, x1 - r, y1)
            cr.line_to(x0 + r, y1)
            cr.curve_to(x0, y1, x0, y1, x0, y1 - r)
            cr.line_to(x0, y0 + r)
            cr.curve_to(x0, y0, x0, y0, x0 + r, y0)
        else:
            cr.rectangle(x0 * u, y0 * u, (x1 - x0) * u, (y1 - y0) * u)
        cr.stroke()

    if kind == T_SELECT:
        for a, b in (((4, 4), (9, 4)), ((15, 4), (20, 4)), ((4, 20), (9, 20)),
                     ((15, 20), (20, 20)), ((4, 4), (4, 9)), ((4, 15), (4, 20)),
                     ((20, 4), (20, 9)), ((20, 15), (20, 20))):
            line(*a, *b)
    elif kind == T_RECT:
        rect(4, 5, 20, 19, 1.5)
    elif kind == T_ELLIPSE:
        cr.save()
        cr.translate(12 * u, 12 * u)
        cr.scale(u, u)
        cr.arc(0, 0, 8, 0, 2 * math.pi)
        cr.restore()
        cr.stroke()
    elif kind == T_ARROW:
        line(4, 20, 19, 5)
        line(19, 5, 12, 5)
        line(19, 5, 19, 12)
    elif kind == T_PEN:
        cr.move_to(4 * u, 20 * u)
        cr.line_to(6.5 * u, 13.5 * u)
        cr.line_to(15 * u, 5 * u)
        cr.line_to(19 * u, 9 * u)
        cr.line_to(10.5 * u, 17.5 * u)
        cr.close_path()
        cr.stroke()
        line(6.5, 13.5, 10.5, 17.5)
    elif kind == T_HIGHLIGHT:
        cr.save()
        cr.set_line_width(max(2.0, 5.0 * u))
        cr.set_source_rgba(color[0], color[1], color[2], 0.45 if color[3] > 0.6 else 0.45)
        line(5, 17, 19, 8)
        cr.restore()
        cr.set_source_rgba(*color)
        line(5, 20, 19, 20)
    elif kind == T_TEXT:
        line(5, 5.5, 19, 5.5)
        line(12, 5.5, 12, 19)
        line(9, 19, 15, 19)
    elif kind == T_SEQ:
        cr.save()
        cr.translate(12 * u, 12 * u)
        cr.scale(u, u)
        cr.arc(0, 0, 8, 0, 2 * math.pi)
        cr.restore()
        cr.stroke()
        cr.save()
        cr.set_font_size(11 * u)
        ext = cr.text_extents("1")
        cr.move_to(12 * u - ext.width / 2 - ext.x_bearing, 12 * u - ext.height / 2 - ext.y_bearing)
        cr.show_text("1")
        cr.restore()
    elif kind == T_MOSAIC:
        for x, y in ((4, 4), (13, 4), (4, 13), (13, 13)):
            cr.rectangle(x * u, y * u, 7 * u, 7 * u)
        cr.fill()
    elif kind == T_BLUR:
        cr.save()
        cr.translate(12 * u, 12 * u)
        cr.scale(u, u)
        for r, alpha in ((8.5, 0.35), (5.5, 0.6), (2.5, 1.0)):
            cr.set_source_rgba(color[0], color[1], color[2], alpha)
            cr.arc(0, 0, r, 0, 2 * math.pi)
            cr.fill()
        cr.restore()
    elif kind == T_PICKER:
        line(4, 20, 9, 15)
        cr.save()
        cr.translate(14.5 * u, 9.5 * u)
        cr.rotate(math.pi / 4)
        cr.rectangle(-4 * u, -4 * u, 8 * u, 8 * u)
        cr.restore()
        cr.stroke()
    elif kind == "width":
        cr.set_line_width(max(1.0, 1.2 * u))
        line(4, 12, 20, 12)
    elif kind == "width2":
        cr.set_line_width(max(2.0, 2.4 * u))
        line(4, 12, 20, 12)
    elif kind == "width3":
        cr.set_line_width(max(3.4, 4.2 * u))
        line(4, 12, 20, 12)
    elif kind == "undo":
        cr.move_to(8 * u, 7 * u)
        cr.line_to(4 * u, 11 * u)
        cr.line_to(8 * u, 15 * u)
        cr.stroke()
        cr.move_to(4 * u, 11 * u)
        cr.line_to(14 * u, 11 * u)
        cr.curve_to(19 * u, 11 * u, 19 * u, 19 * u, 13 * u, 19 * u)
        cr.stroke()
    elif kind == "redo":
        cr.move_to(16 * u, 7 * u)
        cr.line_to(20 * u, 11 * u)
        cr.line_to(16 * u, 15 * u)
        cr.stroke()
        cr.move_to(20 * u, 11 * u)
        cr.line_to(10 * u, 11 * u)
        cr.curve_to(5 * u, 11 * u, 5 * u, 19 * u, 11 * u, 19 * u)
        cr.stroke()
    elif kind == "clear":
        rect(6, 7, 18, 20, 1.5)
        line(4, 7, 20, 7)
        line(9.5, 7, 10.5, 4)
        line(14.5, 7, 13.5, 4)
        line(10.5, 4, 13.5, 4)
        line(10, 11, 10, 16)
        line(14, 11, 14, 16)
    elif kind == "save":
        cr.move_to(5 * u, 7 * u)
        cr.line_to(5 * u, 19 * u)
        cr.line_to(19 * u, 19 * u)
        cr.line_to(19 * u, 9.5 * u)
        cr.line_to(16.5 * u, 5 * u)
        cr.line_to(5 * u, 5 * u)
        cr.close_path()
        cr.stroke()
        rect(9, 5, 15, 9.5)
        rect(8.5, 13, 15.5, 19)
    elif kind == "pin":
        line(9, 4, 15, 4)
        line(10, 4, 10, 11)
        line(14, 4, 14, 11)
        cr.move_to(6.5 * u, 11 * u)
        cr.line_to(17.5 * u, 11 * u)
        cr.line_to(12 * u, 20 * u)
        cr.close_path()
        cr.stroke()
    elif kind == "cancel":
        line(6, 6, 18, 18)
        line(18, 6, 6, 18)
    elif kind == "copy":
        # 剪贴板：底板 + 顶部的夹子
        rect(5, 6, 19, 21, 2)
        rect(9, 3.5, 15, 8.5, 1.5)
        line(9, 12, 15, 12)
        line(9, 15.5, 15, 15.5)
    elif kind == "finish":
        cr.move_to(5 * u, 12.5 * u)
        cr.line_to(10 * u, 17.5 * u)
        cr.line_to(19 * u, 6.5 * u)
        cr.stroke()
    elif kind == "colorwheel":
        cr.save()
        cr.translate(12 * u, 12 * u)
        cr.scale(u, u)
        cr.arc(0, 0, 8, 0, 2 * math.pi)
        cr.restore()
        cr.stroke()
    cr.restore()


# ---------------------------------------------------------------- 工具定义


class _Button:
    __slots__ = ("kind", "x", "y", "w", "h", "tip", "action", "data")

    def __init__(self, kind, x, y, w, h, tip, action, data=None):
        self.kind = kind
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.tip = tip
        self.action = action
        self.data = data

    def hit(self, x, y) -> bool:
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h

    @property
    def box(self):
        return self.x, self.y, self.x + self.w, self.y + self.h


def _physical_size_mm() -> tuple[float, float]:
    """显示器物理尺寸（毫米）。

    只有 RandR 提供毫米信息；Xinerama 只有像素尺寸，**不能**拿来当毫米用
    （早期就踩过这个坑：宽 2880 被当成 2880mm，算出的 DPI 荒谬地低）。
    """
    try:
        from Xlib import display as xdisplay
        from Xlib.ext import randr

        d = xdisplay.Display()
        try:
            root = d.screen().root
            res = randr.get_screen_resources(root)
            for out in res.outputs:
                info = randr.get_output_info(d, out, res.config_timestamp)
                if info.crtc and info.mm_width > 0 and info.mm_height > 0:
                    return float(info.mm_width), float(info.mm_height)
        finally:
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return 0.0, 0.0


def _detect_ui_scale(cfg: dict) -> float:
    """UI 缩放系数：配置优先，否则按物理 DPI 自动判定。

    **不能用 Gdk.Monitor.get_scale_factor()**：入口为了让坐标链固定在设备像素
    上会设 ``GDK_SCALE=1``，此时 GDK 会把显示器报成「2880 逻辑宽、scale 1」，
    于是任何基于 GDK 缩放的判断都会把 HiDPI 屏误判成普通屏（实测就是这个原因
    让工具栏又变小了）。

    改用物理尺寸算 DPI —— 显示器有多少毫米是硬件事实，与 GDK 缩放无关。
    这也是 Flameshot 判断 HiDPI 的做法。环境变量 ``SNAP_UI_SCALE`` 可覆盖。
    """
    env = os.environ.get("SNAP_UI_SCALE", "").strip()
    if env:
        try:
            val = float(env)
            if 0.5 <= val <= 3.0:
                return val
        except ValueError:
            pass

    raw = str(cfg.get("ui_scale", "auto") or "auto").strip().lower()
    if raw not in ("auto", ""):
        try:
            val = float(raw)
            if 0.5 <= val <= 3.0:
                return val
        except ValueError:
            pass

    try:
        from . import capture_linux as capture

        width_px, _height_px = capture.root_geometry()
        width_mm, _height_mm = _physical_size_mm()
        if width_px <= 0 or width_mm <= 0:
            return 1.0
        dpi = width_px / (width_mm / 25.4)
        # 96dpi 是「1 个 CSS 像素 = 1 个物理像素」的基准
        ratio = dpi / 96.0
        if ratio >= 1.75:
            return 2.0
        if ratio >= 1.25:
            return 1.5
        return 1.0
    except Exception:  # noqa: BLE001
        return 1.0


def _make_resize_cursors() -> dict:
    """8 个方位的手柄光标；拿不到就返回空字典（不影响功能）。"""
    names = {"nw": "nw-resize", "n": "n-resize", "ne": "ne-resize", "e": "e-resize",
             "se": "se-resize", "s": "s-resize", "sw": "sw-resize", "w": "w-resize"}
    out: dict = {}
    try:
        disp = Gdk.Display.get_default()
    except Exception:  # noqa: BLE001
        return out
    if disp is None:
        return out
    for key, name in names.items():
        try:
            out[key] = Gdk.Cursor.new_from_name(disp, name)
        except Exception:  # noqa: BLE001
            continue
    return out


def _fallback_geometry(img_w: int, img_h: int, box=None, scale: int = 0):
    """拿不到 X11 标定时的退化几何。

    约定：画布（根像素）= ``screen_box`` 的宽高，缩放 = 抓图 / 画布。
    这样：
      * 设备像素模式（GDK_SCALE=1，抓图 = 根）→ 缩放 1.0，1:1；
      * SDK 或测试里传入放大过的抓图（如 2 倍）→ 缩放 2.0，导出仍是放大图；
      * 多显示器且根原点非 (0,0) 时，用 ``ptr_offset`` 把窗口坐标映射回根坐标。
    """
    from .geometry import Geometry

    if box is not None:
        bx, by, bw, bh = box
        root_w, root_h = max(1, int(bw)), max(1, int(bh))
    else:
        bx = by = 0
        root_w, root_h = max(1, int(img_w)), max(1, int(img_h))
    if scale and scale > 1 and (img_w, img_h) == (root_w * scale, root_h * scale):
        pass          # 抓图正好是 root 的整数倍，下面按实测尺寸算即可
    return Geometry(img_w=int(img_w), img_h=int(img_h),
                    root_w=root_w, root_h=root_h,
                    win_x=int(bx), win_y=int(by),
                    win_w=root_w, win_h=root_h,
                    ptr_offset_x=float(bx), ptr_offset_y=float(by))


class ShotOverlay:
    """全屏截图覆盖层。生命周期内独占一个 GTK 窗口。"""

    PRESENT = True        # 测试脚手架会置 False，避免在真实桌面上弹窗

    def __init__(
        self,
        app,
        screen_img: Image.Image,
        screen_box: tuple[int, int, int, int],
        on_close: Callable[[str], None] | None = None,
        status_cb: Callable[[str], None] | None = None,
        scale: int = 1,
        logical_size: tuple[int, int] | None = None,
        geo=None,
    ) -> None:
        self.app = app
        self.screen = screen_img
        self.box = screen_box          # 虚拟屏 (x, y, w, h)
        self.origin = (screen_box[0], screen_box[1])
        self.on_close = on_close or (lambda reason: None)
        self.status_cb = status_cb or (lambda s: None)

        self.img_w, self.img_h = screen_img.width, screen_img.height
        # 坐标一律走 geometry：它给出「事件坐标 → 根坐标 → 抓图像素」这条链，
        # 并能用 Xlib 根指针对账自证。没传进来时按「抓图 = 根像素」退化处理。
        self.geo = (geo if geo is not None
                    else _fallback_geometry(self.img_w, self.img_h, screen_box, scale))
        # UI（工具栏 / 放大镜 / 手柄 / 提示）的显示缩放。
        #
        # 覆盖层固定在设备像素尺度（GDK_SCALE=1）换来的是「坐标绝不偏移」，
        # 代价是在 HiDPI 屏上按 1 个物理像素画 UI 会显得很小。所以 UI 单独按
        # 显示器的缩放系数放大 —— 它只作用于绘制，不参与任何坐标换算，
        # 框选与截取的一致性不受影响。
        self.ui_scale = _detect_ui_scale(getattr(app, "cfg", {}) or {})
        # 画布坐标系 = X11 根像素（与 GTK 事件坐标同源，绘制 1:1 不重采样）
        self.cr_w = max(1, int(self.geo.root_w))
        self.cr_h = max(1, int(self.geo.root_h))
        disp_img = screen_img
        if (self.cr_w, self.cr_h) != (self.img_w, self.img_h):
            disp_img = screen_img.resize((self.cr_w, self.cr_h), Image.LANCZOS)
        self.disp_surface = pil_to_surface(disp_img.convert("RGB"))
        # 全分辨率原图（放大镜取色 / 最终导出用）
        self.screen_surface = pil_to_surface(screen_img.convert("RGB"))

        # 状态
        self.mode = "select"           # select | draw
        self.tool = T_SELECT
        self.color = COLORS[0]
        self.width = WIDTHS[1]
        self.sel: tuple[float, float, float, float] | None = None
        self.shapes: list[dict] = []
        self.redo_stack: list[dict] = []
        self._seq_no = 1
        self._drag: dict | None = None
        self._cursor = (-1.0, -1.0)
        self._hover: _Button | None = None
        self._closed = False
        self._text_entry: Gtk.Entry | None = None
        self._overlay_box: Gtk.Overlay | None = None
        self._text_pos = (0.0, 0.0)
        self._text_size = 24

        self._dim: cairo.Surface | None = None
        self._dim_key = None
        self._buttons: list[_Button] = []
        self._tip_text = ""
        self._tip_pos = (0.0, 0.0)
        self._mag_cache: dict[tuple[int, int], cairo.Surface] = {}
        self._tb_surf: cairo.Surface | None = None
        self._tb_key: tuple | None = None
        # 工具栏位置要在这里就给初值：首次绘制之前若发生点击，_on_press 会读它
        self._tb_pos: tuple[float, float, float, float] | None = None
        self._frames = 0
        self._frame_ms = 0.0
        self._ptr_checked_at = 0.0
        self._focus_tries = 0
        # 兜底自检：覆盖层必须在「设备像素」模式下运行（入口脚本设 GDK_SCALE=1），
        # 否则窗口会被 GDK 再缩放一次，事件坐标与画布就不同源了。真出现这种情况
        # 下面的分配尺寸检查会立刻报出来，并让指针自证去纠正。
        self._scale_warned = False
        # 弹对话框 / 文字输入框时不要抢焦点，否则用户点不了、打不了字
        self._focus_guard = False
        self._finishing = False
        self._saving = False

        # 窗口
        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win.set_title("SnapCtrlAlt 截图")
        self.win.set_role("snapctrlalt-overlay")
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_keep_above(True)
        # 用 NOTIFICATION 而不是 NORMAL：窗口管理器会把它当提示类窗口，
        # 不抢任务栏、不参与平铺，配合 keep_above + 全屏就能盖住所有窗口。
        self.win.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        self.win.set_app_paintable(True)
        try:
            self.win.set_accept_focus(True)
            self.win.set_focus_on_map(True)
        except Exception:  # noqa: BLE001
            pass

        self.area = Gtk.DrawingArea()
        self.area.set_can_focus(True)
        # 未 realize 时也给一个正确的最小尺寸，免得布局/测试拿到 200×200 的默认值
        self.area.set_size_request(self.cr_w, self.cr_h)
        self.area.add_events(
            Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.SMOOTH_SCROLL_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
            | Gdk.EventMask.STRUCTURE_MASK
        )
        self.win.add(self.area)

        self._crosshair = Gdk.Cursor.new_from_name(Gdk.Display.get_default(), "crosshair")
        self._cursor_obj = self._crosshair
        self._active_cursor = self._crosshair
        self._resize_cursors = _make_resize_cursors()
        self.area.connect("draw", self._on_draw)
        self.win.connect("button-press-event", self._on_press)
        self.win.connect("button-release-event", self._on_release)
        self.win.connect("motion-notify-event", self._on_motion)
        self.win.connect("scroll-event", self._on_scroll)
        self.win.connect("key-press-event", self._on_key)
        self.win.connect("delete-event", lambda *_: (self.cancel(), True)[1])
        self.win.connect("destroy", self._on_destroy)
        self.win.connect("realize", self._on_realize)
        self.win.connect("focus-out-event", self._on_focus_out)

        x, y, w, h = self.box
        # 窗口几何用 GTK 逻辑坐标（会再被 GDK scale factor 放大到物理像素）；
        # 画布坐标系则是事件坐标，两者在这里是分开的两套数。
        lw, lh, lx, ly = self._window_logical_geometry(logical_size)
        self.win.move(lx, ly)
        self.win.set_default_size(lw, lh)
        self.win.fullscreen()
        if self.PRESENT:
            self.win.show_all()
        # 必须显式 present()：它会给窗口管理器发 _NET_ACTIVE_WINDOW；
        # 只 show_all() 的话窗口能显示、能收鼠标，但拿不到键盘焦点
        # （实测 Cinnamon/Muffin 下 Enter / Esc 会跑到别的窗口去）。
        if self.PRESENT:
            self.win.present()
        self.area.grab_focus()
        self._focus_tries = 0
        GLib.timeout_add(60, self._ensure_focus)

        self.status_cb(_HINT_SELECT)

    def _window_logical_geometry(self, logical_size) -> tuple[int, int, int, int]:
        """窗口应占的 GTK 逻辑矩形（宽, 高, x, y）。"""
        if logical_size and logical_size[0] > 1 and logical_size[1] > 1:
            return logical_size[0], logical_size[1], 0, 0
        # 退路：用画布尺寸 / 缩放；再不行直接用显示器几何
        lw = max(1, self.cr_w // max(1, self._present_scale))
        lh = max(1, self.cr_h // max(1, self._present_scale))
        return lw, lh, self.box[0], self.box[1]

    @property
    def _present_scale(self) -> int:
        """显示器实际缩放（画布像素 / GTK 逻辑像素），只用来说明与记录。"""
        try:
            import gi

            gi.require_version("Gdk", "3.0")
            from gi.repository import Gdk

            sc = Gdk.Screen.get_default()
            return max(1, int(sc.get_monitor_scale_factor(0))) if sc else 1
        except Exception:  # noqa: BLE001
            return 1

    def _ensure_focus(self) -> bool:
        """确认真的拿到键盘焦点，没拿到就重试（必要时直接走 X11 设输入焦点）。

        只 ``show_all()`` 是不够的：实测 Cinnamon/Muffin 下窗口能显示、能收鼠标，
        但键盘焦点还留在原来的窗口上，Enter / Esc 会跑到别的程序里去。
        """
        if self._closed:
            return False
        if self._focus_guard:
            return False
        try:
            if self.win.is_active() and self.win.has_toplevel_focus():
                self.area.grab_focus()
                return False
            self.win.present()
            self._focus_tries += 1
            # 几次之后改用 X11 直接指定输入焦点：绕过 WM 的「防抢焦点」策略
            if self._focus_tries in (4, 8, 12, 20, 30):
                self._x11_focus()
            self.area.grab_focus()
            if self._focus_tries in (8, 20, 40):
                print(f"[overlay] 第 {self._focus_tries} 次争取键盘焦点"
                      f"（active={self.win.is_active()} "
                      f"focus={self.win.has_toplevel_focus()}）")
            if self._focus_tries >= 60:
                print("[overlay] 警告：一直没拿到键盘焦点，快捷键可能失灵"
                      "（点一下画面即可恢复）")
                return False
            return True
        except Exception:  # noqa: BLE001
            return False

    def _x11_focus(self) -> bool:
        """用 Xlib 直接把输入焦点给覆盖层窗口（不依赖窗口管理器配合）。"""
        if self._focus_guard or self._closed:
            return False
        try:
            gw = self.win.get_window()
            if gw is None:
                return False
            xid = gw.get_xid()
            from Xlib import X, display as xdisplay

            d = xdisplay.Display()
            try:
                d.set_input_focus(xid, X.RevertToParent, X.CurrentTime)
                d.sync()
            finally:
                d.close()
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[overlay] X11 直接设焦点失败：{e}")
            return False

    # ------------------------------------------------------------ 基础

    @property
    def disp_w(self) -> float:
        alloc = self.area.get_allocated_width()
        return float(alloc if alloc > 1 else self.cr_w)

    @property
    def disp_h(self) -> float:
        alloc = self.area.get_allocated_height()
        return float(alloc if alloc > 1 else self.cr_h)

    def _on_realize(self, *_a) -> None:
        try:
            self.area.get_window().set_cursor(self._cursor_obj)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.win.get_window().set_override_redirect(True)
        except Exception:  # noqa: BLE001
            pass
        self._refresh_window_geometry()

    def _refresh_window_geometry(self) -> None:
        """把覆盖层窗口在根里的实测位置/尺寸写回标定。

        窗口没铺满根窗口（被 WM 挪过 / 没真正全屏）时，事件坐标的原点就和根
        原点不一致，这正是「框选位置 ≠ 截取位置」的一种成因，所以这里量一次
        并留下记录。
        """
        try:
            gw = self.win.get_window()
            if gw is None:
                return
            geom = geometry.window_geometry(int(gw.get_xid()))
            if not geom:
                return
            wx, wy, ww, wh = geom
            self.geo.win_x, self.geo.win_y = wx, wy
            self.geo.win_w, self.geo.win_h = ww, wh
            if (wx, wy) != (0, 0) or (ww, wh) != (self.geo.root_w, self.geo.root_h):
                self.geo.notes.append(
                    f"覆盖层窗口实测 {ww}×{wh}+{wx}+{wy}，"
                    f"根 {self.geo.root_w}×{self.geo.root_h}")
                if not self._scale_warned:
                    self._scale_warned = True
                    print("[overlay] 警告：覆盖层窗口没有铺满 X11 根窗口，"
                          "事件坐标与画布可能不同源（请确认以 GDK_SCALE=1 启动）")
                    # 让分配尺寸反推一个更贴近现实的缩放，避免整屏错位
                    if ww > 0 and abs(ww - self.geo.root_w) > 2:
                        self.geo.notes.append(
                            f"画布按窗口分配尺寸 {ww} 与根 {self.geo.root_w} 的比值"
                            f"（{self.geo.root_w / ww:.3f}）留待指针自证修正")
        except Exception as e:  # noqa: BLE001
            print(f"[overlay] 读取窗口几何失败：{e}")

    def _redraw(self) -> None:
        if not self._closed:
            self.area.queue_draw()

    def _ensure_dim(self) -> cairo.Surface:
        """整屏压暗层，只在底图变化时重建（对应 Windows 版 _dimmed 缓存）。"""
        key = id(self.disp_surface)
        if self._dim is not None and self._dim_key == key:
            return self._dim
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.cr_w, self.cr_h)
        cr = cairo.Context(surf)
        cr.set_source_surface(self.disp_surface, 0, 0)
        cr.paint()
        cr.set_source_rgba(0, 0, 0, DIM_ALPHA)
        cr.paint()
        self._dim = surf
        self._dim_key = key
        return surf

    @property
    def canvas_box(self) -> tuple[float, float, float, float]:
        """画布对应的根坐标范围。

        单屏且主屏在原点时就是 (0, 0, w, h)；若虚拟屏原点不是 0（副屏排在主屏
        左侧或上方），下界就是负的 —— 所有钳制都必须用它，不能硬编码 0。
        """
        ox, oy = self.geo.win_x, self.geo.win_y
        return float(ox), float(oy), float(ox + self.cr_w), float(oy + self.cr_h)

    def _clamp_to_canvas(self, x0, y0, x1, y1):
        bx0, by0, bx1, by1 = self.canvas_box
        x0 = max(bx0, min(x0, bx1))
        x1 = max(bx0, min(x1, bx1))
        y0 = max(by0, min(y0, by1))
        y1 = max(by0, min(y1, by1))
        return x0, y0, x1, y1

    @property
    def dev_scale(self) -> float:
        """画布（根像素）→ 抓图像素 的倍数。

        设备像素模式下就是 1.0；只有当抓图分辨率高于根窗口（例如 Xlib 拿到了
        真实面板分辨率）时才不是 1。所有「图像像素」与「画布像素」的换算都走它。
        """
        return self.geo.zoom_x if abs(self.geo.zoom_x - 1.0) > 1e-6 else 1.0

    def _to_img(self, x: float, y: float) -> tuple[float, float]:
        """画布（根像素）→ 抓图像素。"""
        return self.geo.root_to_img(x, y)

    def _to_disp(self, x: float, y: float) -> tuple[float, float]:
        """抓图像素 → 画布（根像素）。"""
        zx = self.geo.zoom_x or 1.0
        zy = self.geo.zoom_y or 1.0
        return x / zx, y / zy

    # ------------------------------------------------------------ 绘制

    def _on_draw(self, _widget, cr) -> bool:
        t0 = time.perf_counter()
        w, h = self.disp_w, self.disp_h
        cr.save()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        cr.restore()

        # 画布坐标系 = GTK 事件坐标；窗口分配尺寸可能更小（HiDPI），
        # 这里统一缩放到画布坐标系后，所有绘制代码都用画布单位。
        k = (w / float(self.cr_w)) if self.cr_w else 1.0
        if abs(k - 1.0) > 1e-6:
            cr.scale(k, k)
        sel = self.sel
        if sel is None:
            cr.set_source_surface(self._ensure_dim(), 0, 0)
            cr.paint()
        else:
            x0, y0, x1, y1 = self._clamp_to_canvas(*[float(v) for v in sel])
            region = (x0 * self.geo.zoom_x, y0 * self.geo.zoom_y,
                      x1 * self.geo.zoom_x, y1 * self.geo.zoom_y)
            # 亮区：直接把冻结底图贴进选区（Cairo 裁切，比 Pillow 裁片快）
            cr.save()
            cr.rectangle(x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))
            cr.clip()
            cr.set_source_surface(self.disp_surface, 0, 0)
            cr.paint()
            self._draw_shapes(cr, region)
            cr.restore()
            # 压暗：只画四条边带，避免整屏重绘
            self._draw_dim_bands(cr, x0, y0, x1, y1)

            self._draw_size_label(cr, x0, y0, x1, y1)
            if not (self._drag and self._drag.get("kind") == "select"):
                self._draw_outline(cr, x0, y0, x1, y1)
            # 手柄常显：任何标注工具下都能拖拽调整选区
            self._draw_handles(cr, x0, y0, x1, y1)

            if self._drag and self._drag.get("preview"):
                self._draw_preview(cr, self._drag)

        # UI 层单独缩放：这里之后一律用 UI 单位绘制（不参与坐标换算）
        us = self.ui_scale
        cr.save()
        if abs(us - 1.0) > 1e-6:
            cr.scale(us, us)
        uw, uh = w / us, h / us
        if sel is not None and self.mode == "draw":
            self._draw_toolbar(cr, uw, uh)
        if sel is None and not self._drag and self._cursor[0] >= 0:
            self._draw_magnifier(cr, uw, uh)
        if self._tip_text:
            self._draw_tip(cr, uw, uh)
        cr.restore()
        self._frames += 1
        self._frame_ms = (time.perf_counter() - t0) * 1000.0
        return False

    def _draw_dim_bands(self, cr, x0, y0, x1, y1) -> None:
        dim = self._ensure_dim()
        W, H = self.cr_w, self.cr_h
        for rect in (
            (0, 0, W, y0),                 # 上
            (0, y1, W, H - y1),            # 下
            (0, y0, x0, y1 - y0),          # 左
            (x1, y0, W - x1, y1 - y0),     # 右
        ):
            rx, ry, rw, rh = rect
            if rw <= 0 or rh <= 0:
                continue
            cr.save()
            cr.rectangle(rx, ry, rw, rh)
            cr.clip()
            cr.set_source_surface(dim, 0, 0)
            cr.paint()
            cr.restore()

    def _draw_outline(self, cr, x0, y0, x1, y1) -> None:
        cr.save()
        cr.set_line_width(2.0)
        cr.set_source_rgba(0, 0.635, 1.0, 1.0)
        cr.rectangle(x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))
        cr.stroke()
        # 内侧白色细线，深色画面上更清楚
        cr.set_line_width(1.0)
        cr.set_source_rgba(1, 1, 1, 0.55)
        cr.rectangle(x0 + 1.6, y0 + 1.6, max(0.0, x1 - x0 - 3.2),
                     max(0.0, y1 - y0 - 3.2))
        cr.stroke()
        cr.restore()

    def _handle_points(self, x0, y0, x1, y1):
        mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        return [(x0, y0), (mx, y0), (x1, y0), (x1, my),
                (x1, y1), (mx, y1), (x0, y1), (x0, my)]

    def _draw_handles(self, cr, x0, y0, x1, y1) -> None:
        r = HANDLE / 2.0
        cr.save()
        cr.set_line_width(1.6)
        for hx, hy in self._handle_points(x0, y0, x1, y1):
            cr.set_source_rgba(1, 1, 1, 1)
            cr.arc(hx, hy, r, 0, 2 * math.pi)
            cr.fill_preserve()
            cr.set_source_rgba(0, 0.635, 1.0, 1)
            cr.stroke()
        cr.restore()

    def _draw_size_label(self, cr, x0, y0, x1, y1) -> None:
        """QQ 风格尺寸标签：选区上方，越界自动落到下方。显示的是真实图像像素。"""
        text = (f"{int(round((x1 - x0) * self.geo.zoom_x))} × "
                f"{int(round((y1 - y0) * self.geo.zoom_y))}")
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(_font(12))
        layout.set_text(text, -1)
        tw, th = layout.get_pixel_size()
        pad = 9
        bw, bh = tw + pad * 2, th + 8
        lx = x0
        ly = y0 - bh - 8
        if ly < 0:
            ly = y1 + 8
        if ly + bh > self.cr_h:
            ly = max(0.0, y0 + 8)
        if lx + bw > self.cr_w:
            lx = max(0.0, self.cr_w - bw)
        cr.save()
        self._rounded_rect(cr, lx, ly, bw, bh, 6)
        cr.set_source_rgba(*LABEL_BG)
        cr.fill_preserve()
        cr.set_line_width(1.0)
        cr.set_source_rgba(*LABEL_BORDER)
        cr.stroke()
        cr.set_source_rgba(*LABEL_FG)
        cr.move_to(lx + pad, ly + (bh - th) / 2.0)
        PangoCairo.show_layout(cr, layout)
        cr.restore()

    def _rounded_rect(self, cr, x, y, w, h, r) -> None:
        r = min(r, w / 2.0, h / 2.0)
        cr.move_to(x + r, y)
        cr.line_to(x + w - r, y)
        cr.curve_to(x + w, y, x + w, y, x + w, y + r)
        cr.line_to(x + w, y + h - r)
        cr.curve_to(x + w, y + h, x + w, y + h, x + w - r, y + h)
        cr.line_to(x + r, y + h)
        cr.curve_to(x, y + h, x, y + h, x, y + h - r)
        cr.line_to(x, y + r)
        cr.curve_to(x, y, x, y, x + r, y)
        cr.close_path()

    # ------------------------------------------------------------ 形状绘制

    def _draw_shapes(self, cr, region=None) -> None:
        """绘制全部标注。``region`` 用图像像素给出（与 shape 里的坐标同一坐标系）。"""
        if region is not None:
            region = (region[0] / self.geo.zoom_x, region[1] / self.geo.zoom_y,
                      region[2] / self.geo.zoom_x, region[3] / self.geo.zoom_y)
        for s in self.shapes:
            try:
                self._draw_shape(cr, s, region)
            except Exception as e:  # noqa: BLE001
                print(f"[overlay] 形状绘制失败 {s.get('tool')}: {e}")

    def _draw_shape(self, cr, s: dict, region=None) -> None:
        tool = s.get("tool")
        color = _rgba(s.get("color", self.color))
        sc = float(self.dev_scale)
        lw = float(s.get("width", self.width)) / sc      # 图像线宽 -> 画布线宽

        if tool in (T_MOSAIC, T_BLUR):
            box = _clip_box(s["box"], region) if region else s["box"]
            if not box:
                return
            ix0, iy0, ix1, iy1 = [int(round(v)) for v in box]
            ix0, iy0 = max(0, ix0), max(0, iy0)
            ix1, iy1 = min(self.img_w, ix1), min(self.img_h, iy1)
            if ix1 - ix0 < 2 or iy1 - iy0 < 2:
                return
            # 效果图只算一次并挂在 shape 上：整屏重绘时不必每帧重跑滤波
            surf = s.get("_surf")
            if surf is None or s.get("_surf_box") != (ix0, iy0, ix1, iy1):
                crop = self.screen.crop((ix0, iy0, ix1, iy1))
                if tool == T_MOSAIC:
                    block = max(6, int(s.get("width", 4)) * 3)
                    cw, ch = crop.size
                    small = crop.resize((max(1, cw // block), max(1, ch // block)),
                                        Image.BILINEAR)
                    crop = small.resize((cw, ch), Image.NEAREST)
                else:
                    crop = crop.filter(
                        ImageFilter.GaussianBlur(radius=4 + int(s.get("width", 4))))
                surf = pil_to_surface(crop.convert("RGB"))
                s["_surf"] = surf
                s["_surf_box"] = (ix0, iy0, ix1, iy1)
                s["_surf_scale"] = self.dev_scale
            cr.save()
            cr.rectangle(ix0 / sc, iy0 / sc, (ix1 - ix0) / sc, (iy1 - iy0) / sc)
            cr.clip()
            cr.scale(1.0 / sc, 1.0 / sc)
            cr.set_source_surface(surf, ix0, iy0)
            cr.paint()
            cr.restore()
            return

        cr.save()
        cr.set_line_width(lw)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_source_rgba(*color)

        if tool == T_RECT:
            x0, y0, x1, y1 = s["box"]
            x0, y0, x1, y1 = x0 / sc, y0 / sc, x1 / sc, y1 / sc
            cr.rectangle(x0, y0, x1 - x0, y1 - y0)
            cr.stroke()
        elif tool == T_ELLIPSE:
            x0, y0, x1, y1 = [v / sc for v in s["box"]]
            cr.save()
            cr.translate((x0 + x1) / 2.0, (y0 + y1) / 2.0)
            cr.scale(max(0.01, (x1 - x0) / 2.0), max(0.01, (y1 - y0) / 2.0))
            cr.arc(0, 0, 1, 0, 2 * math.pi)
            cr.restore()
            cr.stroke()
        elif tool == T_ARROW:
            p0 = (s["p0"][0] / sc, s["p0"][1] / sc)
            p1 = (s["p1"][0] / sc, s["p1"][1] / sc)
            self._arrow_path(cr, p0, p1, lw)
            cr.stroke()
        elif tool in (T_PEN, T_HIGHLIGHT):
            pts = list(s.get("points") or [])
            if region:
                pts = [p for p in pts if region[0] - 4 <= p[0] <= region[2] + 4
                       and region[1] - 4 <= p[1] <= region[3] + 4]
            if len(pts) >= 2:
                if tool == T_HIGHLIGHT:
                    cr.set_line_width(max(lw, 10 / sc))
                    cr.set_source_rgba(color[0], color[1], color[2], 0.35)
                cr.move_to(pts[0][0] / sc, pts[0][1] / sc)
                for p in pts[1:]:
                    cr.line_to(p[0] / sc, p[1] / sc)
                cr.stroke()
        elif tool == T_TEXT:
            x, y = s["xy"]
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(_font(float(s.get("size", 24)) / sc))
            layout.set_text(str(s.get("text", "")), -1)
            cr.move_to(x / sc, y / sc)
            cr.set_source_rgba(*color)
            PangoCairo.show_layout(cr, layout)
        elif tool == T_SEQ:
            x, y = s["xy"][0] / sc, s["xy"][1] / sc
            r = float(s.get("size", 24)) / sc
            n = int(s.get("n", 1))
            cr.set_source_rgba(*color)
            cr.arc(x, y, r, 0, 2 * math.pi)
            cr.fill_preserve()
            cr.set_source_rgba(1, 1, 1, 1)
            cr.set_line_width(2.0)
            cr.stroke()
            txt = _CIRCLED[n - 1] if 1 <= n <= len(_CIRCLED) else str(n)
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(_font(r * 1.15, bold=True))
            layout.set_text(txt, -1)
            tw, th = layout.get_pixel_size()
            cr.set_source_rgba(1, 1, 1, 1)
            cr.move_to(x - tw / 2.0, y - th / 2.0)
            PangoCairo.show_layout(cr, layout)
        cr.restore()

    def _arrow_path(self, cr, p0, p1, lw) -> None:
        x0, y0 = p0
        x1, y1 = p1
        ang = math.atan2(y1 - y0, x1 - x0)
        head = max(12.0, lw * 3.2)
        # 箭杆收在箭头根部，避免线头戳出箭尖
        bx = x1 - head * 0.75 * math.cos(ang)
        by = y1 - head * 0.75 * math.sin(ang)
        cr.move_to(x0, y0)
        cr.line_to(bx, by)
        cr.move_to(x1, y1)
        for da in (math.pi * 0.78, -math.pi * 0.78):
            cr.line_to(x1 + head * math.cos(ang + da), y1 + head * math.sin(ang + da))
        cr.close_path()

    def _draw_preview(self, cr, d: dict) -> None:
        tool = d.get("tool")
        if not tool:
            return
        sc = float(self.dev_scale)
        sx, sy = d["start"][0] / sc, d["start"][1] / sc
        cx, cy = d.get("cur", d["start"])
        cx, cy = cx / sc, cy / sc
        color = _rgba(d.get("color", self.color))
        lw = float(d.get("width", self.width)) / sc
        cr.save()
        cr.set_line_width(lw)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        if tool in (T_RECT, T_MOSAIC, T_BLUR):
            x0, y0, x1, y1 = _norm_box(sx, sy, cx, cy)
            if tool == T_RECT:
                cr.set_source_rgba(*color)
            else:
                cr.set_source_rgba(1, 1, 1, 0.95)
                cr.set_dash([5, 4])
                cr.set_line_width(1.6)
            cr.rectangle(x0, y0, x1 - x0, y1 - y0)
            cr.stroke()
        elif tool == T_ELLIPSE:
            x0, y0, x1, y1 = _norm_box(sx, sy, cx, cy)
            cr.set_source_rgba(*color)
            cr.save()
            cr.translate((x0 + x1) / 2.0, (y0 + y1) / 2.0)
            cr.scale(max(0.01, (x1 - x0) / 2.0), max(0.01, (y1 - y0) / 2.0))
            cr.arc(0, 0, 1, 0, 2 * math.pi)
            cr.restore()
            cr.stroke()
        elif tool == T_ARROW:
            cr.set_source_rgba(*color)
            self._arrow_path(cr, (sx, sy), (cx, cy), lw)
            cr.stroke()
        elif tool in (T_PEN, T_HIGHLIGHT):
            pts = [(p[0] / sc, p[1] / sc) for p in (d.get("points") or [])]
            if len(pts) >= 2:
                if tool == T_HIGHLIGHT:
                    cr.set_line_width(max(lw, 10 / sc))
                    cr.set_source_rgba(color[0], color[1], color[2], 0.35)
                else:
                    cr.set_source_rgba(*color)
                cr.move_to(*pts[0])
                for p in pts[1:]:
                    cr.line_to(*p)
                cr.stroke()
        cr.restore()

    # ------------------------------------------------------------ 放大镜

    def _draw_magnifier(self, cr, dw, dh) -> None:
        # 进来的 dw/dh 与上下文都是 UI 单位，光标先换算过来
        cx, cy = self._cursor[0] / self.ui_scale, self._cursor[1] / self.ui_scale
        ix, iy = self._to_img(*self._cursor)
        px, py = int(ix), int(iy)
        half = MAG_SIZE // MAG_ZOOM // 2
        size = MAG_SIZE

        cr.save()
        # 位置：跟随光标，越界翻到另一侧
        mx = cx + 22
        my = cy + 22
        if mx + size > dw:
            mx = cx - size - 22
        if my + size + MAG_BAR > dh:
            my = cy - size - MAG_BAR - 22
        mx = max(4.0, min(mx, dw - size - 4))
        my = max(4.0, min(my, dh - MAG_BAR - 4))

        # 放大内容：整屏一次性预放大缓存，避免每帧重采样
        surf = self._mag_surface(px, py, half)
        cr.save()
        cr.rectangle(mx, my, size, size)
        cr.clip()
        cr.set_source_surface(surf, mx, my)
        cr.paint()
        cr.restore()

        # 边框
        cr.set_line_width(2)
        cr.set_source_rgba(1, 1, 1, 1)
        cr.rectangle(mx, my, size, size)
        cr.stroke()
        cr.set_line_width(1)
        cr.set_source_rgba(0, 0, 0, 0.85)
        cr.rectangle(mx + 1, my + 1, size - 2, size - 2)
        cr.stroke()

        # 十字准星（落在当前像素上）：光标恒在被放大区域正中
        ox = mx + half * MAG_ZOOM
        oy = my + half * MAG_ZOOM
        cr.set_line_width(1)
        cr.set_source_rgba(1, 0.23, 0.19, 1)
        cr.move_to(mx, oy + MAG_ZOOM / 2.0)
        cr.line_to(mx + size, oy + MAG_ZOOM / 2.0)
        cr.move_to(ox + MAG_ZOOM / 2.0, my)
        cr.line_to(ox + MAG_ZOOM / 2.0, my + size)
        cr.stroke()
        cr.rectangle(ox, oy, MAG_ZOOM, MAG_ZOOM)
        cr.stroke()

        # 读数条：坐标 + HEX 色值
        col = self.screen.getpixel((max(0, min(px, self.img_w - 1)),
                                    max(0, min(py, self.img_h - 1))))
        hexs = "#{:02X}{:02X}{:02X}".format(*col[:3])
        # 原始事件坐标与换算后的根坐标都留着：两者不等时一眼就能看出是偏移
        coord = (f"{int(round(self.box[0] + ix / self.geo.zoom_x))},"
                 f"{int(round(self.box[1] + iy / self.geo.zoom_y))}")
        cr.set_source_rgba(0.11, 0.11, 0.12, 0.92)
        cr.rectangle(mx, my + size, size, MAG_BAR)
        cr.fill()
        # 色块
        cr.set_source_rgba(col[0] / 255.0, col[1] / 255.0, col[2] / 255.0, 1)
        cr.rectangle(mx + 8, my + size + 9, 26, 26)
        cr.fill_preserve()
        cr.set_line_width(1)
        cr.set_source_rgba(1, 1, 1, 0.5)
        cr.stroke()

        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(_font(11))
        layout.set_text(hexs, -1)
        cr.set_source_rgba(1, 1, 1, 1)
        cr.move_to(mx + 42, my + size + 8)
        PangoCairo.show_layout(cr, layout)

        layout.set_font_description(_font(10))
        layout.set_text(coord, -1)
        cr.set_source_rgba(0.7, 0.7, 0.72, 1)
        cr.move_to(mx + 42, my + size + 24)
        PangoCairo.show_layout(cr, layout)
        cr.restore()

    def _mag_surface(self, px: int, py: int, half: int) -> cairo.Surface:
        """以 (px, py) 为中心、边长 2*half+1 的原始像素，最近邻放大到 MAG_SIZE。"""
        key = (px, py)
        hit = self._mag_cache.get(key)
        if hit is not None:
            return hit
        x0 = max(0, px - half)
        y0 = max(0, py - half)
        x1 = min(self.img_w, px + half + 1)
        y1 = min(self.img_h, py + half + 1)
        crop = self.screen.crop((x0, y0, x1, y1))
        crop = crop.resize((crop.width * MAG_ZOOM, crop.height * MAG_ZOOM), Image.NEAREST)
        pad = Image.new("RGB", (MAG_SIZE, MAG_SIZE), (0, 0, 0))
        pad.paste(crop, ((px - half - x0) * MAG_ZOOM, (py - half - y0) * MAG_ZOOM))
        surf = pil_to_surface(pad.convert("RGB"))
        if len(self._mag_cache) > 160:
            self._mag_cache.clear()
        self._mag_cache[key] = surf
        return surf

    # ------------------------------------------------------------ 工具栏

    def _layout_toolbar(self) -> None:
        """计算工具栏位置与每个按钮的矩形，**统一使用画布坐标**。

        绘制时整体乘 ``ui_scale`` 放大（``_draw_toolbar``），命中测试反过来除
        （``_button_at``）。两套坐标不再混用。
        """
        bs = 30           # 按钮边长
        gap = 2
        pad = 6
        row_h = bs + gap
        groups: list[list[_Button]] = []
        cur: list[_Button] = []

        def add(kind, tip, action, data=None, w=bs):
            nonlocal cur
            b = _Button(kind, 0, 0, w, bs, tip, action, data)
            cur.append(b)
            return b

        for tid in TOOL_ORDER:
            add(tid, TOOL_TIPS[tid], "tool", tid)
        groups.append(cur)
        cur = []

        add("colorwheel", "自定义颜色…", "custom_color", None)
        for c in COLORS:
            add("swatch", f"颜色 {c.upper()}", "color", c, w=22)
        groups.append(cur)
        cur = []

        for kind, wv in (("width", WIDTHS[0]), ("width2", WIDTHS[1]), ("width3", WIDTHS[2])):
            add(kind, f"线宽 {wv}px", "width", wv)
        groups.append(cur)
        cur = []

        add("undo", "撤销 (Ctrl+Z)", "undo")
        add("redo", "重做 (Ctrl+Shift+Z)", "redo")
        add("clear", "清空所有标注", "clear")
        groups.append(cur)
        cur = []

        add("copy", "复制到剪贴板并关闭 (Enter)", "finish")
        add("save", "保存为文件… (Ctrl+S)", "save")
        add("pin", "贴到桌面 (Ctrl+T)", "pin")
        add("cancel", "取消，不保存 (Esc / 右键)", "cancel")
        add("finish", "完成并关闭 (Enter)", "finish")
        groups.append(cur)

        total_w = pad * 2 + sum(sum(b.w for b in g) + gap * (len(g) - 1) for g in groups) \
            + 10 * (len(groups) - 1)
        bar_h = pad * 2 + row_h - gap

        # ---- 定位：全部用画布坐标（= X11 根像素）----
        # 早期这里混用了两套单位：dw/dh 是「UI 单位下的窗口尺寸」，而 _to_disp
        # 返回画布坐标，两者直接比较 —— ui_scale=2 时就会拿画布 1300 和 UI 上限
        # 900 比大小，工具栏被丢到屏幕外（真机实测 y=3196，画布高只有 1800）。
        W, H = float(self.cr_w), float(self.cr_h)
        if self.sel is None:
            cx0 = cy0 = cx1 = cy1 = 0.0
        else:
            cx0, cy0 = self._to_disp(self.sel[0], self.sel[1])
            cx1, cy1 = self._to_disp(self.sel[2], self.sel[3])

        # 屏幕上真正占多大：布局尺寸是「设计尺寸」，绘制时整体乘 ui_scale。
        # 注意：绘制阶段是「先画再缩放」，所以缩放同样放大了位置。要让最终
        # 屏幕矩形落在可视区内，得先按放大后的尺寸钳制，再反推回设计坐标：
        #     屏幕矩形 = (bx*us, by*us, total_w*us, bar_h*us)
        #     要求 屏幕上 bx*us + total_w*us <= W
        # 早期忽略了这一点：按 922 宽摆好位置再放大成 1844 画出去，工具栏跑到
        # 屏幕外（实测 x 2056..3900，屏幕只有 2880 宽），用户感觉就是
        # 「窄截图时两端的功能点不到、点了反而重新框选」。
        us = self.ui_scale
        vis_w = total_w * us
        vis_h = bar_h * us
        margin = 8.0

        # 水平：与选区左缘对齐（窄选区时工具栏比选区宽得多，居中会把两端顶出
        # 屏幕），再把最终矩形压回可视范围
        vx = cx0 * us
        if vx + vis_w > W - margin:
            vx = W - margin - vis_w
        if vx < margin:
            vx = margin
        bx = vx / us

        # 竖直：优先选区下方，放不下就上方；都放不下时贴下边缘
        # （此时必然与选区重叠，但至少完整可见、点得到）
        vy = (cy1 + 10) * us
        if vy + vis_h > H:
            vy = (cy0 - 10) * us - vis_h
        # 选区又高又贴边时（比如整屏选中、或贴着屏幕底部），上方也放不下，
        # 此时只能让它压在选区上 —— 但必须保证「整条可见可点」，否则又变成
        # 「按钮点不到」。所以最后统一夹进可视区间。
        vy = min(max(vy, margin), max(margin, H - vis_h - margin))
        by = vy / us

        self._tb_pos = (bx, by, total_w, bar_h)
        x = bx + pad
        for gi, g in enumerate(groups):
            for b in g:
                b.x, b.y = x, by + pad
                x += b.w + gap
            x -= gap
            if gi != len(groups) - 1:
                x += 10
        self._buttons = [b for g in groups for b in g]

    def _draw_toolbar(self, cr, dw, dh) -> None:
        """画工具栏。位置与按钮都是画布坐标，这里整体按 ui_scale 放大绘制。"""
        self._layout_toolbar()
        bx, by, bw, bh = self._tb_pos
        cr.save()
        cr.scale(self.ui_scale, self.ui_scale)
        self._paint_toolbar(cr, bx, by, bw, bh)
        cr.restore()
        del dw, dh

    def _paint_toolbar(self, cr, bx, by, bw, bh) -> None:
        """把工具栏画到当前上下文（坐标单位为 UI 单位）。"""
        cr.save()
        cr.set_operator(cairo.OPERATOR_OVER)
        self._rounded_rect(cr, bx, by, bw, bh, 8)
        cr.set_source_rgba(*BAR_BG)
        cr.fill_preserve()
        cr.set_line_width(1)
        cr.set_source_rgba(*BAR_BORDER)
        cr.stroke()
        cr.restore()

        # 分组竖线
        cr.save()
        cr.set_source_rgba(*SEP)
        cr.set_line_width(1)
        prev_right = None
        for b in self._buttons:
            if prev_right is not None and b.x - prev_right > 4:
                sx = (prev_right + b.x) / 2.0
                cr.move_to(sx, by + 7)
                cr.line_to(sx, by + bh - 7)
            prev_right = b.x + b.w
        cr.stroke()
        cr.restore()

        for b in self._buttons:
            active = (b.action == "tool" and b.data == self.tool)
            hover = self._hover is b
            if active or hover:
                cr.save()
                self._rounded_rect(cr, b.x + 1, b.y + 1, b.w - 2, b.h - 2, 6)
                cr.set_source_rgba(*(ACTIVE_BG if active else HOVER_BG))
                cr.fill()
                cr.restore()
            if b.kind == "swatch":
                cr.save()
                col = _rgba(b.data)
                cr.rectangle(b.x + 4, b.y + 6, b.w - 8, b.h - 12)
                cr.set_source_rgba(col[0], col[1], col[2], 1)
                cr.fill_preserve()
                cr.set_line_width(1)
                if b.data == self.color:
                    cr.set_source_rgba(0.04, 0.52, 1.0, 1)
                    cr.set_line_width(2.4)
                else:
                    cr.set_source_rgba(0.55, 0.55, 0.58, 1)
                cr.stroke()
                cr.restore()
                continue
            if b.kind == "colorwheel":
                # 当前颜色圆环
                cr.save()
                col = _rgba(self.color)
                cr.set_line_width(3)
                cr.set_source_rgba(col[0], col[1], col[2], 1)
                cr.arc(b.x + b.w / 2.0, b.y + b.h / 2.0, b.w / 2.0 - 5, 0, 2 * math.pi)
                cr.stroke()
                cr.restore()
                continue
            size = b.w - 8
            cr.save()
            cr.translate(b.x + (b.w - size) / 2.0, b.y + (b.h - size) / 2.0)
            draw_icon(cr, b.kind, size, ICON_ACTIVE if active else ICON_COLOR)
            cr.restore()

    def _build_toolbar_surface(self, bx, by, bw, bh) -> cairo.Surface:
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32,
                                  int(bx + bw) + 2, int(by + bh) + 2)
        cr = cairo.Context(surf)
        self._paint_toolbar(cr, bx, by, bw, bh)
        return surf

    def _button_at(self, x, y) -> _Button | None:
        """x, y 是画布坐标；按钮矩形是 UI 单位。"""
        us = self.ui_scale
        ux, uy = x / us, y / us
        for b in self._buttons:
            if b.hit(ux, uy):
                return b
        return None

    def _draw_tip(self, cr, dw, dh) -> None:
        tx, ty = self._tip_pos
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(_font(11))
        layout.set_text(self._tip_text, -1)
        tw, th = layout.get_pixel_size()
        pad = 7
        bw, bh = tw + pad * 2, th + pad
        bx = max(4.0, min(tx - bw / 2.0, dw - bw - 4))
        by = ty - bh - 8
        if by < 4:
            by = ty + 22
        cr.save()
        self._rounded_rect(cr, bx, by, bw, bh, 5)
        cr.set_source_rgba(*TIP_BG)
        cr.fill()
        cr.set_source_rgba(*TIP_FG)
        cr.move_to(bx + pad, by + pad / 2.0)
        PangoCairo.show_layout(cr, layout)
        cr.restore()

    # ------------------------------------------------------------ 鼠标

    def _pos(self, event) -> tuple[float, float]:
        """GTK 事件坐标 → 画布（根像素）坐标。

        正常情况下 event.x/y 就是根坐标，这一步是恒等映射；一旦某些窗口管理器
        或分数缩放让两者不同源，geometry 的指针自证会把修正补上。
        """
        return self.geo.ptr_to_root(float(event.x), float(event.y))

    def _verify_pointer(self, event) -> None:
        """用 Xlib 读到的根指针对账 GTK 事件坐标，发现不同源就地修正。

        这是「框选位置 ≠ 截取位置」的兜底：只要两者被观测到不等，后续所有换算
        都按实测修正后的映射走，并在日志里留下证据。
        """
        if self.geo.verified or self._closed:
            return
        now = time.time()
        if now - self._ptr_checked_at < 1.0:
            return
        self._ptr_checked_at = now
        truth = geometry.root_pointer()
        if truth is None:
            return
        if self.geo.apply_pointer_check((float(event.x), float(event.y)), truth):
            self.geo.verified = True
            self.status_cb(f"已自动校正坐标：{self.geo.describe().splitlines()[-1].strip()}")

    def _on_motion(self, _w, event) -> bool:
        if self._closed:
            return False
        self._verify_pointer(event)
        x, y = self._pos(event)
        self._cursor = (x, y)
        if self.sel is not None and self.mode == "draw":
            self._update_resize_cursor(*self._to_img(x, y))
        prev_hover = self._hover
        self._hover = self._button_at(x, y) if (self.sel is not None and self.mode == "draw") else None
        if self._hover is not None:
            self._tip_text = self._hover.tip
            self._tip_pos = (x, y)
        else:
            self._tip_text = ""
        if self._drag:
            self._drag["cur"] = self._to_img(x, y)
            if self._drag["kind"] == "select":
                self.sel = _norm_box(*self._drag["start"], *self._drag["cur"])
            elif self._drag["kind"] == "resize":
                self._apply_resize(*self._drag["cur"])
            elif self._drag["kind"] == "move":
                self._apply_move(*self._drag["cur"])
            elif self._drag["kind"] == "draw" and self._drag.get("tool") in (T_PEN, T_HIGHLIGHT):
                self._drag.setdefault("points", []).append(self._drag["cur"])
        self._redraw()
        del prev_hover
        return False

    def _on_press(self, _w, event) -> bool:
        if self._closed:
            return False
        x, y = self._pos(event)
        ix, iy = self._to_img(x, y)

        # 双击 = 选整屏（QQ 截图行为）
        if event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS and event.button == 1:
            self._drag = None
            self.select_all()
            return True

        if event.button == 3:
            self.cancel()
            return True
        if event.button != 1:
            return False

        if self._text_entry is not None:
            self._commit_text()

        # 工具栏优先：命中按钮就执行；落在按钮之间的空隙上也只是「什么都不做」。
        # 不能让它继续往下走 —— 那会被当成「点在选区外」，于是清掉选区、重新
        # 开始框选，用户的感觉就是「点半天点不中，还得重新截」。
        if self.sel is not None and self.mode == "draw" and self._tb_pos is not None:
            tbx, tby, tbw, tbh = self._tb_pos
            us = self.ui_scale
            if (tbx * us <= x <= (tbx + tbw) * us
                    and tby * us <= y <= (tby + tbh) * us):
                b = self._button_at(x, y)
                if b is not None:
                    self._activate(b)
                return True

        self.area.grab_focus()

        if self.mode == "select" or self.sel is None:
            self.sel = None
            self.mode = "select"
            self._drag = {"kind": "select", "start": (ix, iy), "cur": (ix, iy), "preview": True}
            self._redraw()
            return True

        if self.tool == T_PICKER:
            px = int(max(0, min(ix, self.img_w - 1)))
            py = int(max(0, min(iy, self.img_h - 1)))
            r, g, b_ = self.screen.getpixel((px, py))[:3]
            self._set_color(f"#{r:02x}{g:02x}{b_:02x}")
            self._set_tool(T_RECT)
            self.status_cb(f"已取色 {self.color.upper()}")
            self._redraw()
            return True

        if self.tool == T_SEQ and self._in_sel(ix, iy):
            self.shapes.append({"tool": T_SEQ, "xy": (ix, iy), "n": self._seq_no,
                                "color": self.color,
                                "size": max(14, self.width * 5) * self.dev_scale})
            self._seq_no += 1
            self.redo_stack.clear()
            self.status_cb(f"序号 {self._seq_no - 1}")
            self._redraw()
            return True

        hname = self._handle_at(ix, iy)
        if hname and self.sel:
            self._drag = {"kind": "resize", "handle": hname, "start": (ix, iy), "orig": self.sel}
            return True

        # 点在选区外 -> 重新框选（QQ 行为）
        if not self._in_sel(ix, iy):
            self.sel = None
            self.mode = "select"
            self._drag = {"kind": "select", "start": (ix, iy), "cur": (ix, iy), "preview": True}
            self._redraw()
            return True

        if self.tool == T_SELECT:
            self._drag = {"kind": "move", "start": (ix, iy), "orig": self.sel}
            return True

        if self.tool == T_TEXT:
            self._start_text(x, y)
            return True

        self._drag = {
            "kind": "draw", "tool": self.tool, "start": (ix, iy), "cur": (ix, iy),
            "points": [(ix, iy)], "preview": True,
            "color": self.color, "width": self.width,
        }
        self._redraw()
        return True

    def _on_release(self, _w, event) -> bool:
        if self._closed or event.button != 1 or not self._drag:
            return False
        x, y = self._pos(event)
        ix, iy = self._to_img(x, y)
        d = self._drag
        self._drag = None
        kind = d["kind"]
        if kind == "select":
            self.sel = _norm_box(*d["start"], ix, iy)
            x0, y0, x1, y1 = self.sel
            if (x1 - x0) < 5 and (y1 - y0) < 5:
                self.sel = None
                self.mode = "select"
                self.status_cb(_HINT_SELECT)
            else:
                self.sel = self._clamp_to_canvas(
                    x0, y0, max(x1, x0 + 5), max(y1, y0 + 5))
                self.mode = "draw"
                self.tool = T_RECT if self.tool == T_SELECT else self.tool
                self.status_cb("已选中区域 · 工具栏可标注 · Enter 复制并关闭")
        elif kind == "draw":
            d["cur"] = (ix, iy)
            if d.get("tool") in (T_PEN, T_HIGHLIGHT):
                d.setdefault("points", []).append((ix, iy))
            self._commit_shape(d, (ix, iy))
        self._redraw()
        return True

    def _on_scroll(self, _w, event) -> bool:
        if self.tool in (T_PEN, T_HIGHLIGHT, T_RECT, T_ELLIPSE, T_ARROW, T_MOSAIC, T_BLUR):
            idx = WIDTHS.index(self.width) if self.width in WIDTHS else 1
            idx = min(idx + 1, len(WIDTHS) - 1) if event.direction == Gdk.ScrollDirection.UP \
                else max(idx - 1, 0)
            self.width = WIDTHS[idx]
            self.status_cb(f"线宽 {self.width}px")
            self._redraw()
            return True
        return False

    def _in_sel(self, ix, iy) -> bool:
        if not self.sel:
            return False
        x0, y0, x1, y1 = self.sel
        return x0 <= ix <= x1 and y0 <= iy <= y1

    def select_all(self) -> None:
        """选中整屏（双击 / Ctrl+A）。"""
        self.sel = self.canvas_box
        self.mode = "draw"
        self.status_cb(f"已选整屏 {self.img_w}×{self.img_h} · 工具栏可标注 · Enter 完成")
        self._redraw()

    def _handle_at(self, ix, iy) -> str | None:
        """命中选区的 8 个调整手柄。

        **与当前标注工具无关**：参考 Flameshot / ksnip 的做法，选区边框随时可
        拖拽调整。早期只在「选择工具」下才响应，导致用户选完区域（工具已自动
        切到矩形）想微调边缘时，被当成要画矩形 —— 表现出来就是「点不中、
        还得重截」。
        """
        if not self.sel or self.mode != "draw" or self._drag is not None:
            return None
        names = ["nw", "n", "ne", "e", "se", "s", "sw", "w"]
        tol = float(HANDLE)
        for name, (hx, hy) in zip(names, self._handle_points(*self.sel)):
            if abs(ix - hx) <= tol and abs(iy - hy) <= tol:
                return name
        return None

    def _update_resize_cursor(self, ix, iy) -> None:
        """靠近手柄时把光标换成对应的缩放箭头（Flameshot 同款反馈）。"""
        if self._cursor_obj is None or self._closed:
            return
        name = self._handle_at(ix, iy)
        if name is None:
            # 在选区内侧边缘也算「可拖边」：给一点余量，手感更好
            name = self._edge_at(ix, iy)
        want = self._resize_cursors.get(name or "", self._crosshair)
        if want is not None and want is not self._active_cursor:
            try:
                self.area.get_window().set_cursor(want)
                self._active_cursor = want
            except Exception:  # noqa: BLE001
                pass

    def _edge_at(self, ix, iy) -> str | None:
        """选区边框附近（含内侧一点）的方位，用于放大可拖拽区域。"""
        if not self.sel or self.mode != "draw":
            return None
        x0, y0, x1, y1 = self.sel
        m = float(HANDLE) + 2
        if not (x0 - m <= ix <= x1 + m and y0 - m <= iy <= y1 + m):
            return None
        horiz = "w" if ix <= x0 + m else ("e" if ix >= x1 - m else "")
        vert = "n" if iy <= y0 + m else ("s" if iy >= y1 - m else "")
        if horiz and vert:
            return vert + horiz if vert == "n" else vert + horiz
        return horiz or vert or None

    def _apply_resize(self, ix, iy) -> None:
        if not self._drag or not self.sel:
            return
        x0, y0, x1, y1 = self._drag["orig"]
        h = self._drag["handle"]
        if "n" in h:
            y0 = iy
        if "s" in h:
            y1 = iy
        if "w" in h:
            x0 = ix
        if "e" in h:
            x1 = ix
        x0, y0, x1, y1 = _norm_box(x0, y0, x1, y1)
        self.sel = self._clamp_to_canvas(x0, y0, x1, y1)

    def _apply_move(self, ix, iy) -> None:
        if not self._drag:
            return
        ox0, oy0, ox1, oy1 = self._drag["orig"]
        sx, sy = self._drag["start"]
        dx, dy = ix - sx, iy - sy
        bx0, by0, bx1, by1 = self.canvas_box
        dx = max(dx, bx0 - ox0)
        dy = max(dy, by0 - oy0)
        dx = min(dx, bx1 - ox1)
        dy = min(dy, by1 - oy1)
        self.sel = (ox0 + dx, oy0 + dy, ox1 + dx, oy1 + dy)

    def _commit_shape(self, d: dict, end) -> None:
        tool = d.get("tool")
        if tool not in (T_RECT, T_ELLIPSE, T_ARROW, T_PEN, T_HIGHLIGHT, T_MOSAIC, T_BLUR):
            return
        x0, y0 = d["start"]
        x1, y1 = d.get("cur") or end
        if tool in (T_RECT, T_ELLIPSE, T_MOSAIC, T_BLUR):
            bx = _norm_box(x0, y0, x1, y1)
            if bx[2] - bx[0] < 3 or bx[3] - bx[1] < 3:
                return
            shape = {"tool": tool, "box": bx, "color": d.get("color", self.color),
                     "width": d.get("width", self.width)}
        elif tool == T_ARROW:
            if math.hypot(x1 - x0, y1 - y0) < 6 * self.dev_scale:
                return
            shape = {"tool": tool, "p0": (x0, y0), "p1": (x1, y1),
                     "color": d.get("color", self.color), "width": d.get("width", self.width)}
        else:
            pts = list(d.get("points") or [])
            if len(pts) < 2:
                return
            shape = {"tool": tool, "points": pts, "color": d.get("color", self.color),
                     "width": d.get("width", self.width)}
        self.shapes.append(shape)
        self.redo_stack.clear()
        self._redraw()

    # ------------------------------------------------------------ 工具切换

    def _activate(self, b: _Button) -> None:
        a = b.action
        if a == "tool":
            self._set_tool(b.data)
        elif a == "color":
            self._set_color(b.data)
        elif a == "custom_color":
            self._pick_custom_color()
        elif a == "width":
            self.width = b.data
            self.status_cb(f"线宽 {self.width}px")
        elif a == "undo":
            self.undo()
        elif a == "redo":
            self.redo()
        elif a == "clear":
            self.clear()
        elif a == "save":
            self.save()
        elif a == "pin":
            self.pin()
        elif a == "cancel":
            self.cancel()
        elif a == "finish":
            self.finish()
        self._redraw()

    def _set_tool(self, tool: str) -> None:
        self.tool = tool
        if self.sel:
            self.mode = "draw"
        self.status_cb({
            T_SELECT: "拖动边缘调整选区 · Enter 完成",
            T_RECT: "拖动画矩形", T_ELLIPSE: "拖动画椭圆", T_ARROW: "拖动画箭头",
            T_PEN: "自由画笔 · 滚轮调粗细", T_HIGHLIGHT: "荧光笔 · 滚轮调粗细",
            T_TEXT: "点击输入文字", T_SEQ: "点击添加序号",
            T_MOSAIC: "拖动打马赛克", T_BLUR: "拖动高斯模糊",
            T_PICKER: "点击画面取色",
        }.get(tool, ""))
        self.area.grab_focus()
        self._redraw()

    def _set_color(self, c: str) -> None:
        self.color = c

    def _pick_custom_color(self) -> None:
        dlg = Gtk.ColorChooserDialog(title="选择标注颜色", parent=self.win)
        self._focus_guard = True
        dlg.set_use_alpha(False)
        rgba = Gdk.RGBA()
        rgba.parse(self.color)
        dlg.set_rgba(rgba)
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            c = dlg.get_rgba()
            self._set_color("#{:02x}{:02x}{:02x}".format(
                int(c.red * 255), int(c.green * 255), int(c.blue * 255)))
        dlg.destroy()
        self._focus_guard = False
        self.win.present()
        self.area.grab_focus()
        self._redraw()

    # ------------------------------------------------------------ 文本

    def _start_text(self, x, y) -> None:
        if self._text_entry is not None:
            self._commit_text()
        size = max(13, int(self.width * 5))
        self._text_size = size * self.dev_scale   # 存图像像素，绘制时 /scale
        e = Gtk.Entry()
        e.set_has_frame(True)
        e.override_color(Gtk.StateFlags.NORMAL, _gdk_rgba(self.color))
        try:
            e.override_background_color(Gtk.StateFlags.NORMAL, Gdk.RGBA(1, 1, 1, 0.92))
        except Exception:  # noqa: BLE001
            pass
        e.set_width_chars(14)
        e.connect("activate", lambda *_: self._commit_text())
        e.connect("key-press-event", self._on_entry_key)
        e.connect("focus-out-event", lambda *_: (self._commit_text(), False)[1])
        # 用 Gtk.Overlay 把输入框浮在画布上（先摘下来再挂进去，GTK 不允许一个
        # widget 同时属于两个容器）
        if not isinstance(self._overlay_box, Gtk.Overlay):
            self._overlay_box = Gtk.Overlay()
            parent = self.area.get_parent()
            if parent is not None:
                parent.remove(self.area)
            self._overlay_box.add(self.area)
            self.win.add(self._overlay_box)
            self._overlay_box.show_all()
        self._overlay_box.add_overlay(e)
        e.set_halign(Gtk.Align.START)
        e.set_valign(Gtk.Align.START)
        e.set_margin_start(int(x / self.ui_scale))
        e.set_margin_top(int(y / self.ui_scale))
        self._text_entry = e
        self._text_pos = self._to_img(x, y)
        self.win.show_all()
        self._focus_guard = True     # 让用户能往输入框里打字
        e.grab_focus()
        self.status_cb("输入文字后回车确认 · Esc 取消")

    def _on_entry_key(self, _w, event) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self._cancel_text()
            return True
        return False

    def _cancel_text(self) -> None:
        if self._text_entry is None:
            return
        self._teardown_entry()
        self.area.grab_focus()
        self._redraw()

    def _teardown_entry(self) -> None:
        e, self._text_entry = self._text_entry, None
        self._focus_guard = False
        try:
            self._overlay_box.remove(e)
            self.area.grab_focus()
        except Exception:  # noqa: BLE001
            pass
        del e

    def _commit_text(self) -> None:
        if self._text_entry is None:
            return
        text = self._text_entry.get_text().strip()
        self._teardown_entry()
        if text:
            self.shapes.append({"tool": T_TEXT, "xy": self._text_pos, "text": text,
                                "color": self.color, "size": float(self._text_size)})
            self.redo_stack.clear()
        self._redraw()

    # ------------------------------------------------------------ 键盘

    def _on_key(self, _w, event) -> bool:
        if self._closed:
            return False
        kv = event.keyval
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        if kv == Gdk.KEY_Escape:
            self.cancel()
            return True
        if self._text_entry is not None:
            return False          # 文字输入中，其余按键交给 Entry
        if kv in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.finish()
            return True
        if ctrl and kv in (Gdk.KEY_z, Gdk.KEY_Z):
            self.redo() if shift else self.undo()
            return True
        if ctrl and kv in (Gdk.KEY_y, Gdk.KEY_Y):
            self.redo()
            return True
        if ctrl and kv in (Gdk.KEY_s, Gdk.KEY_S):
            self.save()
            return True
        if ctrl and kv in (Gdk.KEY_c, Gdk.KEY_C):
            self.finish()
            return True
        if ctrl and kv in (Gdk.KEY_t, Gdk.KEY_T):
            self.pin()
            return True
        if ctrl and kv in (Gdk.KEY_a, Gdk.KEY_A):
            self.select_all()
            return True
        # 单键切工具（QQ 截图同款习惯）
        quick = {Gdk.KEY_r: T_RECT, Gdk.KEY_o: T_ELLIPSE, Gdk.KEY_a: T_ARROW,
                 Gdk.KEY_p: T_PEN, Gdk.KEY_t: T_TEXT, Gdk.KEY_m: T_MOSAIC,
                 Gdk.KEY_b: T_BLUR, Gdk.KEY_s: T_SELECT, Gdk.KEY_h: T_HIGHLIGHT}
        if not ctrl and kv in quick:
            self._set_tool(quick[kv])
            return True
        if kv in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
            self.clear()
            return True
        if kv in (Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_Up, Gdk.KEY_Down) and self.sel:
            dx = -1 if kv == Gdk.KEY_Left else (1 if kv == Gdk.KEY_Right else 0)
            dy = -1 if kv == Gdk.KEY_Up else (1 if kv == Gdk.KEY_Down else 0)
            step = 10 if shift else 1
            x0, y0, x1, y1 = self.sel
            nx = min(max(x0 + dx * step, 0.0), max(0.0, self.cr_w - (x1 - x0)))
            ny = min(max(y0 + dy * step, 0.0), max(0.0, self.cr_h - (y1 - y0)))
            self.sel = (nx, ny, nx + (x1 - x0), ny + (y1 - y0))
            self._redraw()
            return True
        return False

    def _on_focus_out(self, _w, _event) -> bool:
        """覆盖层丢了键盘焦点（被别的窗口抢走）时重新抢回来，否则快捷键会失灵。"""
        if self._closed:
            return False
        if self._focus_guard:
            return False
        self._focus_tries = 0

        def refocus() -> bool:
            if not self._closed and not self._focus_guard:
                try:
                    self.win.present()
                    self.area.grab_focus()
                except Exception:  # noqa: BLE001
                    pass
            return False

        GLib.timeout_add(120, refocus)
        GLib.timeout_add(400, self._ensure_focus)
        return False

    # ------------------------------------------------------------ 操作

    def undo(self) -> None:
        if self._text_entry is not None:
            self._commit_text()
        if not self.shapes:
            return
        s = self.shapes.pop()
        self.redo_stack.append(s)
        if s.get("tool") == T_SEQ:
            self._seq_no = max(1, self._seq_no - 1)
        self.status_cb(f"已撤销（剩 {len(self.shapes)} 个标注）")
        self._redraw()

    def redo(self) -> None:
        if not self.redo_stack:
            return
        s = self.redo_stack.pop()
        self.shapes.append(s)
        if s.get("tool") == T_SEQ:
            self._seq_no = min(len(_CIRCLED), self._seq_no + 1)
        self.status_cb("已重做")
        self._redraw()

    def clear(self) -> None:
        if self._text_entry is not None:
            self._cancel_text()
        if not self.shapes:
            return
        self.redo_stack = list(reversed(self.shapes)) + self.redo_stack
        self.shapes.clear()
        self._seq_no = 1
        self.status_cb("已清空标注")
        self._redraw()

    def _sel_in_image_px(self) -> tuple[int, int, int, int]:
        """选区换算成全分辨率图像的像素矩形。"""
        if not self.sel:
            return 0, 0, self.img_w, self.img_h
        return self.geo.rect_root_to_img(self.sel)

    def render_result(self) -> Image.Image:
        """把标注烧进画面，返回选区内的最终图像（全分辨率物理像素）。"""
        if self._text_entry is not None:
            self._commit_text()
        x0, y0, x1, y1 = self._sel_in_image_px()
        w, h = x1 - x0, y1 - y0
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surf)
        cr.translate(-x0, -y0)
        cr.rectangle(x0, y0, w, h)
        cr.clip()
        cr.set_source_surface(self.screen_surface, 0, 0)
        cr.paint()
        self._draw_shapes(cr, (x0, y0, x1, y1))
        surf.flush()
        return surface_to_pil(surf).convert("RGB")

    def current_size(self) -> tuple[int, int]:
        x0, y0, x1, y1 = self._sel_in_image_px()
        return x1 - x0, y1 - y0

    def finish(self, auto_save: bool = False) -> None:
        """Enter / 工具栏对勾：按配置复制（或保存）后关闭。

        必须幂等：一次 Enter 可能同时触发 Gtk.Entry 的 activate 和窗口的
        key-press-event，重复执行会重复写剪贴板、重复存盘。
        """
        if self._closed or self._finishing:
            return
        self._finishing = True
        img = self.render_result()
        action = str(self.app.cfg.get("after_capture", "copy"))
        saved_path = None
        try:
            if action in ("copy", "copy_save"):
                from . import clipboard_linux

                clipboard_linux.copy_image(img, primary=bool(self.app.cfg.get("copy_to_primary")))
                msg = f"已复制到剪贴板 {img.width}×{img.height}"
            else:
                msg = ""
            if action in ("save", "copy_save") or auto_save:
                saved_path = self.app.save_image(img, silent=True)
                msg = f"已保存到 {saved_path}" if saved_path else msg
            # 只报一次：App._status 负责「覆盖层在时写日志、关闭后弹通知」
            self.status_cb(msg or f"已处理 {img.width}×{img.height}")
        except Exception as e:  # noqa: BLE001
            self.status_cb(f"处理失败：{e}")
        self._close("finish")

    def save(self) -> None:
        """另存为…（Ctrl+S）。"""
        if self._closed or self._saving:
            return
        self._saving = True
        img = self.render_result()
        path = self.app.ask_save_path(self.win)
        if not path:
            self._saving = False
            self.status_cb("已取消保存")
            return
        try:
            self.app.write_image(img, path)
            self.status_cb(f"已保存 {path}")
        except Exception as e:  # noqa: BLE001
            self.status_cb(f"保存失败：{e}")
            return
        self._close("save")

    def pin(self) -> None:
        """贴图：把当前结果钉成一个置顶小窗（对标 QQ 截图的贴图）。"""
        img = self.render_result()
        self.app.pin_image(img)
        self.status_cb("已贴到桌面")
        self._close("pin")

    def cancel(self) -> None:
        if self._text_entry is not None:
            self._cancel_text()
        self.status_cb("已取消截图")
        self._close("cancel")

    def _close(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        self._finishing = True
        self._tip_text = ""
        try:
            self.win.destroy()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.on_close(reason)
        except Exception as e:  # noqa: BLE001
            print(f"[overlay] 关闭回调失败: {e}")

    def _on_destroy(self, *_a) -> None:
        if not self._closed:
            self._closed = True
            try:
                self.on_close("destroy")
            except Exception:  # noqa: BLE001
                pass


def _rgba(hex_color: str):
    hex_color = (hex_color or "#000000").lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    try:
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
    except ValueError:
        r = g = b = 0
    return r / 255.0, g / 255.0, b / 255.0, 1.0


def _gdk_rgba(hex_color: str) -> Gdk.RGBA:
    c = _rgba(hex_color)
    return Gdk.RGBA(c[0], c[1], c[2], 1.0)


def icon_pixbuf(kind: str, size: int = 24, color=ICON_COLOR) -> GdkPixbuf.Pixbuf:
    """把 Cairo 图标渲染成 Pixbuf（设置界面 / 托盘用）。"""
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surf)
    cr.set_source_rgba(0, 0, 0, 0)
    cr.paint()
    draw_icon(cr, kind, size, color)
    surf.flush()
    return Gdk.pixbuf_get_from_surface(surf, 0, 0, size, size)
