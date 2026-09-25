"""离屏帧耗时基准（对标 Windows 版测试里 create / resize / move / annotate 四路径）。

不需要真实显示器：直接在一个假屏幕上构造 ShotOverlay，然后反复调 ``_on_draw``
把每一帧画进内存 Cairo surface，测的就是真实的绘制代码路径。

用法：
    python3 tools/perf.py                 # 默认 1440×900 @1x
    python3 tools/perf.py 5760 3600 4     # 物理像素尺寸 + 缩放系数
    python3 tools/perf.py 5760 3600 4 1440 900   # 再指定屏幕逻辑尺寸
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import cairo  # noqa: E402
import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402,F401

from PIL import Image  # noqa: E402

from snapctrlalt import overlay as ov  # noqa: E402


class HeadlessApp:
    cfg = {"after_capture": "none"}

    def save_image(self, img, silent=True):
        return None

    def notify(self, msg):
        pass

    def ask_save_path(self, parent=None):
        return None

    def write_image(self, img, path):
        pass

    def pin_image(self, img):
        pass


class HeadlessOverlay(ov.ShotOverlay):
    """把「可见画布逻辑尺寸」钉死，免得受未 realize 的 widget 分配尺寸影响。"""

    def __init__(self, *a, disp=(1440, 900), **kw):
        self._disp = disp
        super().__init__(*a, **kw)


    @property
    def disp_w(self) -> float:
        return float(self._disp[0])

    @property
    def disp_h(self) -> float:
        return float(self._disp[1])


def busy_image(w: int, h: int) -> Image.Image:
    """有点内容的假截图：纯色会让某些绘制路径走捷径，测不准。"""
    img = Image.new("RGB", (w, h), (28, 48, 72))
    px = img.load()
    for y in range(0, h, 7):
        shade = 40 + (y * 7) % 180
        for x in range(w):
            px[x, y] = (shade, (shade * 2) % 255, (shade * 3) % 255)
    for y in range(0, h, 60):
        for x in range(0, w, 60):
            for dy in range(20):
                for dx in range(20):
                    if x + dx < w and y + dy < h:
                        px[x + dx, y + dy] = (235, 235, 240)
    return img


def measure(overlay: ov.ShotOverlay, surface: cairo.Surface, frames: int = 40) -> float:
    """跑 ``frames`` 帧绘制，返回平均毫秒。"""
    cr = cairo.Context(surface)
    overlay._on_draw(overlay.area, cr)  # 预热（字体、图标缓存等）
    times = []
    for _ in range(frames):
        cr2 = cairo.Context(surface)
        t = time.perf_counter()
        overlay._on_draw(overlay.area, cr2)
        times.append((time.perf_counter() - t) * 1000.0)
    return statistics.mean(times)


def main(argv: list[str]) -> int:
    # 参数：物理像素尺寸 + 缩放系数 [+ 屏幕逻辑尺寸]
    phys_w = int(argv[1]) if len(argv) > 1 else 1440
    phys_h = int(argv[2]) if len(argv) > 2 else 900
    scale = int(argv[3]) if len(argv) > 3 else 1
    disp_w = int(argv[4]) if len(argv) > 4 else max(1, phys_w // scale)
    disp_h = int(argv[5]) if len(argv) > 5 else max(1, phys_h // scale)

    img = busy_image(phys_w, phys_h)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, disp_w, disp_h)
    overlay = HeadlessOverlay(HeadlessApp(), img, (0, 0, img.width, img.height),
                              on_close=lambda r: None, scale=scale,
                              disp=(disp_w, disp_h))

    results: list[tuple[str, float, str]] = []

    # 路径 1：未框选（整屏压暗 + 跟随光标的放大镜）
    overlay.mode = "select"
    overlay.sel = None
    overlay._cursor = (disp_w * 0.5, disp_h * 0.5)
    results.append(("悬停/放大镜", measure(overlay, surface), "整屏压暗 + 放大镜"))

    # 路径 2：拖框创建选区（选区每帧变大）
    overlay.mode = "select"
    overlay._drag = {"kind": "select", "start": (0, 0), "cur": (0, 0), "preview": True}
    total = 0.0
    worst = 0.0
    for i in range(40):
        f = (i + 1) / 40.0
        overlay.sel = (0.0, 0.0, disp_w * f, disp_h * f)
        overlay._drag["cur"] = (disp_w * f, disp_h * f)
        cr = cairo.Context(surface)
        t = time.perf_counter()
        overlay._on_draw(overlay.area, cr)
        dt = (time.perf_counter() - t) * 1000.0
        total += dt
        worst = max(worst, dt)
    results.append(("拖框创建", total / 40.0, f"最慢一帧 {worst:.1f} ms"))
    overlay._drag = None

    # 路径 3：整屏选区（底图走快路径，无压暗边带）
    overlay.mode = "draw"
    overlay.sel = (0.0, 0.0, float(disp_w), float(disp_h))
    results.append(("整屏选区(底图快路径)", measure(overlay, surface), "无压暗带"))

    # 路径 4：拖动选区（每帧换位置）
    total = 0.0
    for i in range(40):
        off = (i % 10) * 3
        overlay.sel = (off, off, disp_w * 0.8 + off, disp_h * 0.8 + off)
        cr = cairo.Context(surface)
        t = time.perf_counter()
        overlay._on_draw(overlay.area, cr)
        total += (time.perf_counter() - t) * 1000.0
    results.append(("拖动选区", total / 40.0, "选区整体平移"))

    # 路径 5：标注态（选区 + 工具栏 + 8 个标注）
    overlay.sel = (disp_w * 0.1, disp_h * 0.1, disp_w * 0.9, disp_h * 0.9)
    sc = float(scale)
    overlay.shapes = [
        {"tool": ov.T_RECT, "box": (disp_w * .2 * sc, disp_h * .2 * sc,
                                    disp_w * .5 * sc, disp_h * .5 * sc),
         "color": "#ff3b30", "width": 4},
        {"tool": ov.T_ELLIPSE, "box": (disp_w * .3 * sc, disp_h * .3 * sc,
                                       disp_w * .6 * sc, disp_h * .6 * sc),
         "color": "#ffcc00", "width": 4},
        {"tool": ov.T_ARROW, "p0": (disp_w * .2 * sc, disp_h * .7 * sc),
         "p1": (disp_w * .7 * sc, disp_h * .3 * sc), "color": "#34c759", "width": 4},
        {"tool": ov.T_PEN, "width": 4, "color": "#007aff",
         "points": [(disp_w * .1 * sc + i * 4 * sc, disp_h * .5 * sc + (i % 20) * 3 * sc)
                    for i in range(120)]},
        {"tool": ov.T_TEXT, "xy": (disp_w * .25 * sc, disp_h * .25 * sc),
         "text": "标注文字", "color": "#ffffff", "size": 32.0 * sc},
        {"tool": ov.T_SEQ, "xy": (disp_w * .6 * sc, disp_h * .2 * sc), "n": 1,
         "color": "#ff3b30", "size": 24.0 * sc},
        {"tool": ov.T_MOSAIC, "box": (disp_w * .5 * sc, disp_h * .5 * sc,
                                      disp_w * .7 * sc, disp_h * .6 * sc),
         "color": "#000000", "width": 8},
        {"tool": ov.T_BLUR, "box": (disp_w * .6 * sc, disp_h * .6 * sc,
                                    disp_w * .8 * sc, disp_h * .7 * sc),
         "color": "#000000", "width": 8},
    ]
    results.append(("标注态(8 个标注+工具栏)", measure(overlay, surface), "含马赛克/模糊"))

    # 路径 6：形状预览中（拖动画矩形）
    overlay._drag = {"kind": "draw", "tool": ov.T_RECT,
                     "start": (disp_w * .2 * sc, disp_h * .2 * sc),
                     "cur": (disp_w * .6 * sc, disp_h * .6 * sc),
                     "points": [(disp_w * .2 * sc, disp_h * .2 * sc)], "preview": True,
                     "color": "#ff3b30", "width": 4}
    results.append(("绘制预览", measure(overlay, surface), "矩形拖拽中"))
    overlay._drag = None

    print(f"抓图 {img.width}×{img.height}，屏幕逻辑尺寸 {disp_w}×{disp_h}（缩放 x{scale}）")
    print(f"{'路径':<26}{'平均':>9}{'约合 fps':>11}   备注")
    print("-" * 72)
    worst_all = 0.0
    for name, ms, note in results:
        fps = 1000.0 / ms if ms > 0 else float("inf")
        worst_all = max(worst_all, ms)
        print(f"{name:<26}{ms:>7.2f} ms{fps:>10.0f}   {note}")
    print("-" * 72)
    print(f"最慢路径 {worst_all:.2f} ms/帧（60fps 预算 16.7 ms）"
          f" -> {'达标' if worst_all < 16.7 else '偏慢'}")
    del overlay
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
