"""坐标几何：把「GTK 事件坐标 → 抓图像素」这件事变成可测量、可自证的量。

## 为什么需要这个模块

HiDPI 下同一块屏幕有四套坐标，而且**互不相等**（本机实测）:

===========================  =========================  ==============
量                            本机实测                    来源
===========================  =========================  ==============
X11 根窗口 / 事件坐标          2880 × 1800                Xlib get_geometry
GTK 显示器逻辑几何             1440 × 900                 Gdk.Monitor
抓图（设备像素）               5760 × 3600                Gdk.pixbuf_get_from_window
覆盖层窗口分配                 2880 × 1800（会被缩放时不等于根尺寸）
===========================  =========================  ==============

早期版本用「GTK scale factor」和「抓图宽 ÷ 根宽」去**反推**这些关系，
一旦某个环节的缩放行为和假设不一致（窗口有没有被 GDK 缩放、显示器是不是
分数缩放），框选位置和实际截取位置就会整体错开。

这里的原则改成：

1. **实测**，不反推：窗口在根里的真实位置/尺寸、抓图尺寸、指针在根里的位置
   全部现场量出来。
2. **自证**：第一次鼠标移动时，把 GTK 给的事件坐标与 Xlib 读到的根指针坐标
   对一次；不一致就现场解出修正（平移 + 缩放）并记日志。
3. **一致性优先**：事件坐标 → 画布 → 抓图像素是一条仿射链，导出时直接用它
   求像素矩形，不做整数倍猜测。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Geometry:
    """一次截图会话的坐标标定结果。"""

    # ---- 实测输入 ----
    img_w: int                 # 抓图像素宽
    img_h: int
    root_w: int                # X11 根窗口像素宽（= 事件坐标范围）
    root_h: int
    win_w: int = 0             # 覆盖层窗口在根里的实际宽（0 = 未知，用 root）
    win_h: int = 0
    win_x: int = 0             # 覆盖层窗口在根里的实际原点
    win_y: int = 0
    gdk_scale: int = 0         # GTK 自己报的缩放（仅记录，不参与换算）

    # ---- 自证结果：指针事件坐标 → 根坐标 ----
    ptr_scale_x: float = 1.0
    ptr_scale_y: float = 1.0
    ptr_offset_x: float = 0.0
    ptr_offset_y: float = 0.0
    verified: bool = False
    notes: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- 基本量

    @property
    def rx(self) -> int:
        """窗口在根里的宽（未知时退回根宽）。"""
        return self.win_w if self.win_w > 0 else self.root_w

    @property
    def ry(self) -> int:
        return self.win_h if self.win_h > 0 else self.root_h

    @property
    def zoom_x(self) -> float:
        """根像素 → 抓图像素 的倍数（机型不同可能是 1 / 2 / 1.5 …）。"""
        return self.img_w / float(self.root_w) if self.root_w else 1.0

    @property
    def zoom_y(self) -> float:
        return self.img_h / float(self.root_h) if self.root_h else 1.0

    @property
    def integer_zoom(self) -> int | None:
        """两轴都是同一个整数倍时返回它，否则 None（分数缩放）。"""
        zx, zy = self.zoom_x, self.zoom_y
        ix = int(round(zx))
        if ix >= 1 and abs(zx - ix) < 0.02 and abs(zy - ix) < 0.02:
            return ix
        return None

    # ---------------------------------------------------------------- 换算

    def ptr_to_root(self, x: float, y: float) -> tuple[float, float]:
        """GTK 事件坐标 → 根坐标（自证修正就在这一步生效）。"""
        return (x * self.ptr_scale_x + self.ptr_offset_x,
                y * self.ptr_scale_y + self.ptr_offset_y)

    def root_to_img(self, x: float, y: float) -> tuple[float, float]:
        """根坐标 → 抓图像素。"""
        return (x * self.zoom_x, y * self.zoom_y)

    def ptr_to_img(self, x: float, y: float) -> tuple[float, float]:
        rx, ry = self.ptr_to_root(x, y)
        return self.root_to_img(rx, ry)

    def rect_root_to_img(self, rect) -> tuple[int, int, int, int]:
        """根坐标矩形 → 抓图像素矩形（含边界钳制，保证至少 1×1）。"""
        x0, y0, x1, y1 = rect
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        ix0 = max(0, min(int(round(x0 * self.zoom_x)), self.img_w - 1))
        iy0 = max(0, min(int(round(y0 * self.zoom_y)), self.img_h - 1))
        ix1 = max(ix0 + 1, min(int(round(x1 * self.zoom_x)), self.img_w))
        iy1 = max(iy0 + 1, min(int(round(y1 * self.zoom_y)), self.img_h))
        return ix0, iy0, ix1, iy1

    # ---------------------------------------------------------------- 自证

    def apply_pointer_check(self, event_xy, root_xy, tol: float = 2.0) -> bool:
        """用「GTK 事件坐标 vs Xlib 根指针坐标」这对实测值修正映射。

        模型：``root = event * scale + offset``。修正策略必须**保守**，否则一次
        异常采样就会把好的标定带坏：

        * 单个样本之间的差只当作读数噪声（窗口还没定下来时为 0），
          **不据此改任何东西**，只记录；
        * 只有当两个样本在 x / y 上跨度都超过 120 像素、且都能被同一个比例
          解释时，才认定存在缩放并采用拟合结果；
        * 比例被修正过之后，偏移必须重新拟合（``offset = mean(root - e*scale)``），
          而不是照抄某一点，避免比例与平移互相污染。
        """
        ex, ey = float(event_xy[0]), float(event_xy[1])
        gx, gy = root_xy
        if gx is None or gy is None:
            return False
        if not hasattr(self, "_samples"):
            self._samples: list[tuple[float, float, float, float]] = []
        self._samples.append((ex, ey, float(gx), float(gy)))
        del self._samples[:-10]

        changed = False
        sx = sy = None
        for px, py, pgx, pgy in self._samples[:-1]:
            if sx is None and abs(ex - px) > 120 and abs(gx - pgx) > 120:
                sx = (gx - pgx) / (ex - px)
            if sy is None and abs(ey - py) > 120 and abs(gy - pgy) > 120:
                sy = (gy - pgy) / (ey - py)

        def _usable(k) -> bool:
            """配对样本给出的比例是否可信 *且确实不等于 1*。

            1.0 不算「有缩放证据」：事件坐标本来就是根坐标时，配对样本必然
            得到 1.0，这时候应当去修正平移，而不是把 1.0 当成缩放结论。
            """
            return k is not None and 0.5 <= k <= 3.0 and abs(k - 1.0) > 0.02

        # --- 缩放：只有跨距足够大的配对样本才能定比例 ---
        if _usable(sx) and abs(sx - self.ptr_scale_x) > 0.005:
            self.ptr_scale_x = sx
            self.verified = True
            changed = True
        if _usable(sy) and abs(sy - self.ptr_scale_y) > 0.005:
            self.ptr_scale_y = sy
            self.verified = True
            changed = True
        scale_trusted = self.verified or not (_usable(sx) or _usable(sy))

        matched = abs((ex * self.ptr_scale_x + self.ptr_offset_x) - gx) <= tol and \
            abs((ey * self.ptr_scale_y + self.ptr_offset_y) - gy) <= tol

        # --- 平移：单点即可判定，但必须有两个以上样本、且当前比例可信时才改，
        #     否则测试脚手架里的一次异常读数就会把标定带坏 ---
        if scale_trusted and not matched and len(self._samples) >= 2:
            ox = sum(g - e * self.ptr_scale_x
                     for e, _y, g, _gy in self._samples) / len(self._samples)
            oy = sum(gy2 - ey2 * self.ptr_scale_y
                     for _x, ey2, _gx, gy2 in self._samples) / len(self._samples)
            if abs(ox - self.ptr_offset_x) > tol or abs(oy - self.ptr_offset_y) > tol:
                self.ptr_offset_x, self.ptr_offset_y = ox, oy
                changed = True
                self.verified = True
        elif not matched and len(self._samples) < 2:
            self.notes.append(
                f"指针读数差异：事件({ex:.0f},{ey:.0f}) vs 根({gx},{gy})（样本不足，暂不修正）")

        if changed:
            self.notes.append(
                f"指针自证修正：scale=({self.ptr_scale_x:.4f},{self.ptr_scale_y:.4f}) "
                f"offset=({self.ptr_offset_x:.2f},{self.ptr_offset_y:.2f})"
                f"（样本 {len(self._samples)} 个）")
        return changed

    # ---------------------------------------------------------------- 报告

    def describe(self) -> str:
        z = self.integer_zoom
        zoom = f"x{z}" if z else f"x{self.zoom_x:.3f}/{self.zoom_y:.3f}"
        lines = [
            f"抓图={self.img_w}×{self.img_h}  X11根={self.root_w}×{self.root_h}  "
            f"覆盖层窗口={self.rx}×{self.ry}+{self.win_x}+{self.win_y}  "
            f"缩放={zoom}  GDK_scale={self.gdk_scale or '?'}",
            f"指针映射：scale=({self.ptr_scale_x:.4f},{self.ptr_scale_y:.4f}) "
            f"offset=({self.ptr_offset_x:.2f},{self.ptr_offset_y:.2f}) "
            f"{'已自证' if self.verified else '未自证（单点）'}",
        ]
        lines.extend(f"  · {n}" for n in self.notes)
        return "\n".join(lines)

    def check(self) -> list[str]:
        """返回可疑之处（空列表 = 看起来正常）。"""
        problems: list[str] = []
        if self.win_w > 0 and self.win_w != self.root_w:
            problems.append(
                f"覆盖层窗口宽 {self.win_w} ≠ X11 根宽 {self.root_w}："
                "窗口没有铺满根窗口，点击位置会整体错开")
        if abs(self.zoom_x - self.zoom_y) > 0.02:
            problems.append(f"两轴缩放不一致（x{self.zoom_x:.3f} vs y{self.zoom_y:.3f}）")
        if self.zoom_x < 0.999:
            problems.append(f"抓图比屏幕小（zoom={self.zoom_x:.3f}），导出会被放大")
        if abs(self.ptr_scale_x - 1.0) > 0.01 or abs(self.ptr_offset_x) > 1:
            problems.append(
                f"指针映射被修正过：scale_x={self.ptr_scale_x:.4f} "
                f"offset_x={self.ptr_offset_x:.1f}（说明 GDK 事件坐标与根坐标不同源）")
        return problems


# ---------------------------------------------------------------- 标定


def measure(img_size, gdk_scale: int = 0) -> Geometry:
    """现场量出根几何，构造标定结果（窗口几何由覆盖层 realize 后补上）。"""
    from . import capture_linux as capture

    root_w, root_h = capture.root_geometry()
    geo = Geometry(
        img_w=int(img_size[0]), img_h=int(img_size[1]),
        root_w=int(root_w), root_h=int(root_h),
        gdk_scale=int(gdk_scale or 0),
    )
    # 环境变量逃生阀（用户机器上出现异常缩放时可直接指定）
    _apply_env_overrides(geo)
    geo.win_w, geo.win_h = geo.root_w, geo.root_h
    return geo


def _apply_env_overrides(geo: Geometry) -> None:
    env_zoom = os.environ.get("SNAP_SCALE", "").strip()
    if env_zoom:
        try:
            z = float(env_zoom)
            if z > 0:
                geo.img_w = int(round(geo.root_w * z))
                geo.img_h = int(round(geo.root_h * z))
                geo.notes.append(f"SNAP_SCALE={z} 覆盖了缩放")
        except ValueError:
            pass
    for key, attr in (("SNAP_OFFSET_X", "ptr_offset_x"), ("SNAP_OFFSET_Y", "ptr_offset_y")):
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        try:
            setattr(geo, attr, float(raw))
            geo.notes.append(f"{key}={raw} 覆盖了偏移")
        except ValueError:
            pass


def root_pointer() -> tuple[int, int] | None:
    """Xlib 直接读根指针位置（与 GTK 事件坐标对账用的地面真值）。"""
    try:
        from Xlib import display as xdisplay

        d = xdisplay.Display()
        try:
            q = d.screen().root.query_pointer()
            return int(q.root_x), int(q.root_y)
        finally:
            d.close()
    except Exception:  # noqa: BLE001
        return None


def window_geometry(xid: int) -> tuple[int, int, int, int] | None:
    """读取某个 X11 窗口在根坐标里的 (x, y, w, h)。"""
    try:
        from Xlib import display as xdisplay

        d = xdisplay.Display()
        try:
            win = d.create_resource_object("window", xid)
            g = win.get_geometry()
            coords = win.translate_coords(d.screen().root, 0, 0)
            return int(coords.x), int(coords.y), int(g.width), int(g.height)
        finally:
            d.close()
    except Exception:  # noqa: BLE001
        return None
