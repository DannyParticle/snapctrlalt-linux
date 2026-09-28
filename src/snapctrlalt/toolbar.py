"""工具栏的公共定义：按钮表、尺寸、绘制、以及「独立窗口」形态。

同一个工具栏有两种呈现方式，共用这里的定义，避免两套代码走偏：

* **画在覆盖层上**（默认）：``overlay.ShotOverlay`` 用 ``ToolbarState`` 排布、
  用 ``paint_bar`` 绘制，按钮矩形就在画布坐标系里。
* **独立窗口**（可选项）：``ToolbarWindow`` 是一个普通的 GTK 窗口，自己排版、
  自己响应点击，覆盖层只负责把选区和工具状态同步过去。

工具栏是两排的（第一排 11 个标注工具，第二排颜色／线宽／编辑／收尾），
比单排窄得多，窄选区时不会被顶到屏幕边缘。
"""

from __future__ import annotations

import math

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Gtk, Pango, PangoCairo  # noqa: E402

from PIL import Image, ImageFilter  # noqa: E402

# 工具 id（与 overlay 里的常量保持一致，避免循环 import 就重复定义一次）
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

# 调色盘
BAR_BG = (0.949, 0.949, 0.969, 0.98)
BAR_BORDER = (0.78, 0.78, 0.80, 1.0)
SEP_COLOR = (0.78, 0.78, 0.80, 1.0)
ICON_COLOR = (0.11, 0.11, 0.12, 1.0)
ICON_ACTIVE = (1, 1, 1, 1)
ACTIVE_BG = (0.039, 0.518, 1.0, 1.0)
HOVER_BG = (0.82, 0.82, 0.86, 1.0)

# 设计基准尺寸（都要乘 ui_scale）
BS = 30        # 按钮边长
GAP = 2
PAD = 6
SEP = 10       # 分组之间的额外间距
ROW_GAP = 4
SWATCH_W = 22
MARGIN = 8     # 与屏幕边缘留白
GAP_TO_SEL = 10  # 与选区之间的间距


class Button:
    """一个按钮的矩形与语义。

    ``x/y/w/h`` 的单位由持有者决定：画在覆盖层上时是画布坐标，独立窗口里是
    窗口本地坐标。所以两种形态都能用同一套排布代码。
    """

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


def _row_specs():
    """两排按钮的声明：``(kind, tip, action, data[, 宽度])``，``"sep"`` 表示分组。"""
    row1 = [(tid, TOOL_TIPS[tid], "tool", tid) for tid in TOOL_ORDER]

    row2 = [("colorwheel", "自定义颜色…", "custom_color", None)]
    row2 += [("swatch", f"颜色 {c.upper()}", "color", c, SWATCH_W) for c in COLORS]
    row2.append("sep")
    row2 += [(k, f"线宽 {w}px", "width", w)
             for k, w in (("width", WIDTHS[0]), ("width2", WIDTHS[1]),
                          ("width3", WIDTHS[2]))]
    row2.append("sep")
    row2 += [("editor", "临时切到方案①（贴选区工具栏）· 只这一次，默认不变", "editor", None)]
    row2.append("sep")
    row2 += [("undo", "撤销 (Ctrl+Z)", "undo", None),
             ("redo", "重做 (Ctrl+Shift+Z)", "redo", None),
             ("clear", "清空所有标注", "clear", None)]
    row2.append("sep")
    row2 += [("copy", "复制到剪贴板并关闭 (Enter)", "finish", None),
             ("save", "保存为文件… (Ctrl+S)", "save", None),
             ("pin", "贴到桌面 (Ctrl+T)", "pin", None),
             ("cancel", "取消，不保存 (Esc / 右键)", "cancel", None),
             ("finish", "完成并关闭 (Enter)", "finish", None)]
    return [row1, row2]


class ToolbarState:
    """工具栏的排布状态：尺寸 + 按钮矩形 + 悬停项。

    ``ui_scale`` 只在这里乘一次，之后所有坐标都是最终尺寸（画布像素或窗口像素），
    绘制与命中测试都不再缩放 —— 这一条是被两次坐标事故换来的，别改。
    """

    def __init__(self, ui_scale: float = 1.0) -> None:
        self.ui_scale = float(ui_scale)
        self.buttons: list[Button] = []
        self.rows: list[list[Button]] = []
        self.hover: Button | None = None
        self.bar_w = 0.0
        self.bar_h = 0.0
        # 竖排（贴左/右边时用）：排布仍按"横向"算，只是把每个按钮的 x/y 互换、
        # 整条的宽高互换。这样绘制与命中共用同一份矩形，不会出现两套算法打架。
        self.vertical = False
        self._build()
        self.corner_buttons: list[Button] = []
        self._build_corners()

    # ---------------------------------------------------------------- 排布

    def _build(self) -> None:
        us = self.ui_scale
        bs = BS * us
        gap = GAP * us
        sep = SEP * us
        pad = PAD * us
        row_h = (BS + ROW_GAP) * us

        # 先按声明把每排的「组」建出来，并算出每排的宽度
        rows_groups: list[list[list[Button]]] = []
        row_widths: list[float] = []
        for spec in _row_specs():
            groups: list[list[Button]] = []
            cur: list[Button] = []
            for item in spec:
                if item == "sep":
                    if cur:
                        groups.append(cur)
                        cur = []
                    continue
                kind, tip, action, data = item[0], item[1], item[2], item[3]
                w = (item[4] * us) if len(item) > 4 else bs
                cur.append(Button(kind, 0, 0, w, bs, tip, action, data))
            if cur:
                groups.append(cur)
            rows_groups.append(groups)
            row_widths.append(
                sum(sum(b.w for b in g) + gap * (len(g) - 1) for g in groups)
                + sep * (len(groups) - 1))

        self.content_w = max(row_widths or [0.0])
        self.stack_h = pad * 2 + row_h * len(rows_groups) - ROW_GAP * us
        self._row_geom = (rows_groups, row_widths, pad, gap, sep, row_h)
        # 四角按钮的边长按"沿条方向的长度"算。**横排基准**下就是整条宽 ——
        # 早期这里用内容宽算，横排得到 66px、竖排只有 24px，用户点不到就说
        # "边切不过去"。现在统一走 _recompute_inset()。
        self.inset = 0.0
        self._recompute_inset()
        self.bar_w = pad * 2 + 2 * self.inset + self.content_w
        self.bar_h = self.stack_h
        self._place_buttons()

    @staticmethod
    def _wrap(groups, limit: float, gap: float, sep: float) -> list[list]:
        """把一排按钮按长度上限切段（保持分组，组内不被拆开）。

        竖排时"一排"要沿条的方向铺开，而条长有限：塞不下就得折行。
        横排时内容宽度是照着窗口算的，一般不折；窄窗口里同样受益。
        """
        out: list[list] = []
        cur: list = []
        used = 0.0
        for g in groups:
            glen = sum(b.w for b in g) + gap * (len(g) - 1)
            extra = glen + (sep if cur else 0.0)
            if cur and used + extra > limit:
                out.append(cur)
                cur, used = [], 0.0
                extra = glen
            cur.append(list(g))
            used += extra
        if cur:
            out.append(cur)
        return out

    def natural_height(self) -> float:
        """竖排时"内容全装下"需要多高（按当前大小，不含窗口上限）。

        注意：**折行改变不了这个值** —— 竖排条长 ≈ 内容总长，折成几列只是把同样
        的长度横过来放。小窗口里唯一的办法是整体缩小，编辑器用它反推缩放档位。
        """
        rows_groups, row_widths, _pad, gap, sep, _rh = getattr(
            self, "_row_geom", ([], [], 0.0, 0.0, 0.0, 0.0))
        if not rows_groups or not self.vertical:
            return self.bar_h
        widest = max(row_widths or [1.0])
        span = 0.0
        for gi, groups in enumerate(rows_groups):
            for line in self._wrap(groups, widest, gap, sep):
                span += self._line_len(line, gap, sep) + gap
            if gi != len(rows_groups) - 1:
                span += sep
        return max(0.0, span - gap) + 2 * self.inset

    def _line_len(self, line, gap: float, sep: float) -> float:
        return sum(sum(b.w for b in g) + gap * (len(g) - 1) for g in line) \
            + sep * (len(line) - 1)

    # ---------------------------------------------------------------- 尺寸

    def _canonical_len(self) -> float:
        """"沿条方向"的长度：横排=整条宽，竖排=整条高。"""
        return self.bar_w if not self.vertical else self.bar_h

    def _recompute_inset(self) -> None:
        """按当前朝向重算四角按钮边长（= 内容两端给它留出的位置）。

        横排基准值由内容宽推出；竖排时整条变长（高），四角也跟着变大。
        """
        us = self.ui_scale
        base = 2.0 * max(6.0 * us, self.content_w * 0.028)
        if not self.vertical:
            self.inset = base
        else:
            # 竖排后的条长 = 横排的整条宽（含它自己的 inset），不是 stack_h ——
            # stack_h 是"条的厚度"，早期拿它当长度算，竖排条被压成 353px。
            horiz_len = getattr(self, "_h_len", 0.0) or (
                2 * base + self.content_w + 2 * PAD * us)
            self.inset = 2.0 * max(6.0 * us, horiz_len * 0.028)

    def _place_buttons(self) -> None:
        """按当前朝向把按钮摆好，必要时**折行**。

        * 横排：按钮排成若干行（沿 y 叠），一行放不下就往下折；
        * 竖排：按钮排成若干列（沿 x 叠），一列放不下就向右折。

        两排（工具 / 颜色收尾）在横排时是两行、在竖排时是两段，都可能折成多行；
        折出来的所有行统一按"沿条方向"依次排开，行与行之间不重叠。
        早期没做折行：竖排时一排 1336px 硬塞进 1486px 高的条里，颜色那排直接压到
        工具那排上，表现就是"有些按钮点不到、有的被四角盖住"。
        """
        rows_groups, row_widths, pad, gap, sep, row_h = getattr(
            self, "_row_geom", ([], [], 0.0, 0.0, 0.0, 0.0))
        if not rows_groups:
            self.rows, self.buttons = [], []
            return
        vertical = self.vertical
        along = self.bar_h if vertical else self.bar_w
        limit = max(40.0, along - 2 * (pad + self.inset))
        col_step = max((b.h for b in self.buttons), default=row_h)

        self.rows = []
        lines: list[list] = []                   # [(line, kind_axis)]
        for groups in rows_groups:
            for line in self._wrap(groups, limit, gap, sep):
                lines.append(line)

        if not vertical:
            y = pad
            for line in lines:
                x = pad + self.inset + (limit - self._line_len(line, gap, sep)) / 2.0
                for gi, g in enumerate(line):
                    for b in g:
                        b.x, b.y = x, y
                        x += b.w + gap
                    x -= gap
                    if gi != len(line) - 1:
                        x += sep
                self.rows.append([b for g in line for b in g])
                y += row_h
        else:
            x = pad
            for line in lines:
                span = self._line_len(line, gap, sep)
                y = along / 2.0 - span / 2.0
                for gi, g in enumerate(line):
                    for b in g:
                        b.x, b.y = x, y
                        y += b.h + gap
                    y -= gap
                    if gi != len(line) - 1:
                        y += sep
                self.rows.append([b for g in line for b in g])
                x += col_step + gap
        self.buttons = [b for r in self.rows for b in r]

    # ---------------------------------------------------------------- 旋转

    def set_vertical(self, vertical: bool) -> None:
        """切成竖排 / 横排：交换整条宽高、重算四角边长、重摆按钮并重建四角按钮。

        只交换宽高、不重算 inset 是不行的 —— 竖排的条长（高）比横排的条宽短得多，
        四角按钮会跟着缩水（实测横排 75px、竖排 24px，用户点不到）。所以这里
        一律"重算 + 重摆"，而不是就地转置坐标。
        """
        vertical = bool(vertical)
        if vertical == self.vertical or not self.buttons:
            return
        self._h_len = self.bar_w          # 记住横排条长：竖排的条长就是它
        self.vertical = vertical
        self.bar_w, self.bar_h = self.bar_h, self.bar_w
        # 转置：每个按钮的宽高互换（横排的两排 → 竖排的两列）
        for b in self.buttons:
            b.w, b.h = b.h, b.w
        self._recompute_inset()
        # 条长按 inset 调整：横排看宽、竖排看高。
        # 竖排条长 = **横排的整条宽**（内容 + 两倍 inset）—— stack_h 是"条的厚度"，
        # 拿它当长度会把竖排条压成 454px（实测）。
        if not vertical:
            self.bar_w = 2 * self.inset + self.content_w + 2 * PAD * self.ui_scale
        else:
            self.bar_h = self._h_len + 2 * self.inset
        self._place_buttons()
        self._build_corners()
        self.hover = None

    def _build_corners(self) -> None:
        """四个角的「换个边」按钮：点一下就贴到对应的边。

        尺寸取 min(0.34*宽, 0.34*高)，四个角各占一个，互不重叠；吸附到边的那些
        按钮都用不上的角落因此变成了功能入口（教师白板工具栏的常见做法）。
        """
        # 边长直接用布局算好的 inset（与"内容给它留出的位置"必须同一个数）。
        # 早期这里自己拿 bar_w * 0.028 重算：竖排时 bar_w 是"条的厚度"，
        # 算出来只有 24px —— 编辑器窗口里就是这个问题。
        side = float(getattr(self, "inset", 0.0) or
                     2.0 * max(6.0 * self.ui_scale, self.content_w * 0.028))
        w, h = self.bar_w, self.bar_h
        specs = [
            ("place", "把工具栏移到上边（横排）", "place", "top", 0.0, 0.0),
            ("place", "把工具栏移到下边（横排）", "place", "bottom", w - side, h - side),
            ("place", "把工具栏移到左边（竖排）", "place", "left", 0.0, h - side),
            ("place", "把工具栏移到右边（竖排）", "place", "right", w - side, 0.0),
        ]
        self.corner_buttons = [
            Button(kind, x, y, side, side, tip, action, data)
            for kind, tip, action, data, x, y in specs
        ]

    # ---------------------------------------------------------------- 命中

    def at(self, x: float, y: float) -> Button | None:
        # 四角按钮先判：它们只有一小块，不该被主按钮的悬停/点击盖住
        for b in self.corner_buttons:
            if b.hit(x, y):
                return b
        for b in self.buttons:
            if b.hit(x, y):
                return b
        return None

    def in_bar(self, x: float, y: float, bx: float, by: float) -> bool:
        return bx <= x <= bx + self.bar_w and by <= y <= by + self.bar_h

    def move_to(self, bx: float, by: float) -> None:
        """把整条工具栏平移到 ``(bx, by)``（含四角按钮）。"""
        if not self.buttons:
            return
        ox = bx - self.buttons[0].x
        oy = by - self.buttons[0].y
        for b in self.buttons + self.corner_buttons:
            b.x += ox
            b.y += oy


# ---------------------------------------------------------------- 绘制


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


def _rgba_to_hex(c) -> str:
    """Gdk.RGBA -> #rrggbb。"""
    return "#{:02x}{:02x}{:02x}".format(
        max(0, min(255, int(round(c.red * 255)))),
        max(0, min(255, int(round(c.green * 255)))),
        max(0, min(255, int(round(c.blue * 255)))))


def _rounded_rect(cr: cairo.Context, x, y, w, h, r) -> None:
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


def _btn_rect(state: ToolbarState, b: Button):
    """按钮在"当前朝向"下的矩形。

    竖排时 ``set_vertical`` 已经把每个按钮的 x/y、宽高换过了，这里直接用；
    之所以还留一层，是为了让绘制代码有一段统一的读法（以后要再改朝向只动这里）。
    """
    return b.x, b.y, b.w, b.h


def paint_bar(cr: cairo.Context, state: ToolbarState, bx: float, by: float,
              tool: str, color: str, icon_painter) -> None:
    """把工具栏画到当前上下文。

    ``state.buttons`` 的坐标已经是最终坐标；这里只按 ``(bx, by)`` 做一次平移，
    让调用方可以复用同一份排布画在不同位置（覆盖层每帧重排也一样）。

    ``icon_painter(cr, kind, size, color)`` 由调用方提供（覆盖层里是 ``draw_icon``），
    避免这里反向依赖 overlay 模块。
    """
    us = state.ui_scale
    cr.save()
    cr.set_operator(cairo.OPERATOR_OVER)
    _rounded_rect(cr, bx, by, state.bar_w, state.bar_h, 8 * us)
    cr.set_source_rgba(*BAR_BG)
    cr.fill_preserve()
    cr.set_line_width(max(1.0, us))
    cr.set_source_rgba(*BAR_BORDER)
    cr.stroke()
    cr.restore()

    # 分组竖线：按排分别判断
    cr.save()
    cr.set_source_rgba(*SEP_COLOR)
    cr.set_line_width(max(1.0, us))
    for row in state.rows:
        btns = sorted(row, key=lambda b: b.y if state.vertical else b.x)
        for i in range(len(btns) - 1):
            if state.vertical:
                gap_px = btns[i + 1].y - (btns[i].y + btns[i].h)
            else:
                gap_px = btns[i + 1].x - (btns[i].x + btns[i].w)
            if gap_px > 2 * us:
                if state.vertical:
                    sy = btns[i].y + btns[i].h + gap_px / 2.0
                    left = btns[i].x + 6 * us
                    cr.move_to(left, sy)
                    cr.line_to(left + btns[i].w - 12 * us, sy)
                else:
                    sx = btns[i].x + btns[i].w + gap_px / 2.0
                    top = btns[i].y + 6 * us
                    cr.move_to(sx, top)
                    cr.line_to(sx, top + btns[i].h - 12 * us)
    cr.stroke()
    cr.restore()

    for b in list(state.buttons) + list(getattr(state, "corner_buttons", [])):
        bxx, byy, bww, bhh = _btn_rect(state, b)
        active = (b.action == "tool" and b.data == tool)
        hover = state.hover is b
        if active or hover:
            cr.save()
            _rounded_rect(cr, bxx + us, byy + us, bww - 2 * us, bhh - 2 * us, 6 * us)
            cr.set_source_rgba(*(ACTIVE_BG if active else HOVER_BG))
            cr.fill()
            cr.restore()
        if b.kind == "swatch":
            cr.save()
            col = _rgba(b.data)
            cr.rectangle(bxx + 4 * us, byy + 6 * us, bww - 8 * us, bhh - 12 * us)
            cr.set_source_rgba(col[0], col[1], col[2], 1)
            cr.fill_preserve()
            if b.data == color:
                cr.set_source_rgba(0.04, 0.52, 1.0, 1)
                cr.set_line_width(2.4 * us)
            else:
                cr.set_source_rgba(0.55, 0.55, 0.58, 1)
                cr.set_line_width(max(1.0, us))
            cr.stroke()
            cr.restore()
            continue
        if b.kind == "colorwheel":
            cr.save()
            col = _rgba(color)
            cr.set_line_width(3 * us)
            cr.set_source_rgba(col[0], col[1], col[2], 1)
            cr.arc(bxx + bww / 2.0, byy + bhh / 2.0, bww / 2.0 - 5 * us, 0, 6.283185307179586)
            cr.stroke()
            cr.restore()
            continue
        size = bww - 8 * us
        cr.save()
        cr.translate(bxx + (bww - size) / 2.0, byy + (bhh - size) / 2.0)
        icon_painter(cr, b.kind, size, ICON_ACTIVE if active else ICON_COLOR)
        cr.restore()

    # 四角「换个边」按钮：一个朝内的小三角，指哪贴哪
    for b in getattr(state, "corner_buttons", []):
        bxx, byy, bww, bhh = _btn_rect(state, b)
        if state.hover is b:
            cr.save()
            _rounded_rect(cr, bxx + us, byy + us, bww - 2 * us, bhh - 2 * us, 6 * us)
            cr.set_source_rgba(*HOVER_BG)
            cr.fill()
            cr.restore()
        cr.save()
        cr.set_source_rgba(0.35, 0.35, 0.38, 0.95)
        cx, cy = bxx + bww / 2.0, byy + bhh / 2.0
        tri = min(bww, bhh) * 0.22
        which = b.data
        # 指向"要贴的那条边"：上边朝上、下边朝下、左边朝左、右边朝右
        if which == "top":
            pts = ((cx, cy - tri), (cx - tri, cy + tri * 0.7), (cx + tri, cy + tri * 0.7))
        elif which == "bottom":
            pts = ((cx, cy + tri), (cx - tri, cy - tri * 0.7), (cx + tri, cy - tri * 0.7))
        elif which == "left":
            pts = ((cx - tri, cy), (cx + tri * 0.7, cy - tri), (cx + tri * 0.7, cy + tri))
        else:
            pts = ((cx + tri, cy), (cx - tri * 0.7, cy - tri), (cx - tri * 0.7, cy + tri))
        cr.move_to(*pts[0])
        cr.line_to(*pts[1])
        cr.line_to(*pts[2])
        cr.close_path()
        cr.fill()
        cr.restore()


# ---------------------------------------------------------------- 标注绘制
#
# 覆盖层（画在屏幕上）与独立编辑器窗口（画在普通窗口画布里）共用同一套标注
# 绘制与形状语义，所以放在这里，避免两套实现慢慢走偏。
#
# 使用方需要提供：
#   self.screen      PIL 原图（马赛克/模糊从这里采样）
#   self.geo         标定（提供 zoom_x / zoom_y）
#   self.dev_scale   图像像素 → 画布像素 的倍数
#   self.color / self.width   当前描边色与线宽
#   self.img_w / self.img_h


# ①..⑳ 序号字形
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


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


def _font(size: float, bold: bool = False) -> Pango.FontDescription:
    fd = Pango.FontDescription()
    fd.set_family("Noto Sans CJK SC, Source Han Sans SC, WenQuanYi Micro Hei, Sans")
    fd.set_absolute_size(size * Pango.SCALE)
    if bold:
        fd.set_weight(Pango.Weight.BOLD)
    return fd


class AnnotationRenderer:
    """把标注形状画成像素的那部分逻辑。"""

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


def paint_tip(cr: cairo.Context, text: str, x: float, y: float,
              max_w: float, ui_scale: float = 1.0):
    """悬停提示气泡；返回它的矩形以便调用方复用。"""
    if not text:
        return None
    layout = PangoCairo.create_layout(cr)
    fd = Pango.FontDescription()
    fd.set_family("Noto Sans CJK SC, Source Han Sans SC, WenQuanYi Micro Hei, Sans")
    fd.set_absolute_size(11 * ui_scale * Pango.SCALE)
    layout.set_font_description(fd)
    layout.set_text(text, -1)
    tw, th = layout.get_pixel_size()
    pad = 7 * ui_scale
    bw, bh = tw + pad * 2, th + pad
    bx = max(4.0, min(x - bw / 2.0, max_w - bw - 4))
    by = y - bh - 8 * ui_scale
    if by < 4:
        by = y + 22 * ui_scale
    cr.save()
    _rounded_rect(cr, bx, by, bw, bh, 5 * ui_scale)
    cr.set_source_rgba(0.11, 0.11, 0.12, 0.92)
    cr.fill()
    cr.set_source_rgba(1, 1, 1, 1)
    cr.move_to(bx + pad, by + pad / 2.0)
    PangoCairo.show_layout(cr, layout)
    cr.restore()
    return bx, by, bw, bh


# ---------------------------------------------------------------- 独立窗口


class ToolbarWindow:
    """把工具栏放在**独立窗口**里的备选形态。

    与「画在覆盖层上」的区别：

    * 它是普通顶层窗口，不参与覆盖层的绘制，所以完全不会碰到覆盖层那套
      clip / 缩放问题（这也是当初想选它的理由）；
    * 可以自己拖动位置（拖空白处），选区变了会重新贴到选区下方；
    * 键盘焦点仍然留在覆盖层上 —— 窗口设成不接受焦点，点它不会把
      Enter/Esc 抢走。

    动作通过 ``on_action(button)`` 回调交给覆盖层执行，工具栏本身不碰业务。
    """

    def __init__(self, on_action, ui_scale: float = 1.0, icon_painter=None,
                 on_hover=None) -> None:
        self.on_action = on_action
        self.on_hover = on_hover
        self.icon_painter = icon_painter
        self.state = ToolbarState(ui_scale)
        self.tool = T_SELECT
        self.color = COLORS[0]
        self._drag: tuple[float, float, float, float] | None = None
        self._manual_pos: tuple[int, int] | None = None
        self._parent = None

        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win.set_title("SnapCtrlAlt 工具栏")
        self.win.set_role("snapctrlalt-toolbar")
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_keep_above(True)
        self.win.set_accept_focus(False)          # 不抢覆盖层的键盘焦点
        self.win.set_focus_on_map(False)
        self.win.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.win.set_app_paintable(True)

        w = int(round(self.state.bar_w))
        h = int(round(self.state.bar_h))
        self.area = Gtk.DrawingArea()
        self.area.set_size_request(w, h)
        self.area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.area.connect("draw", self._on_draw)
        self.area.connect("button-press-event", self._on_press)
        self.area.connect("button-release-event", self._on_release)
        self.area.connect("motion-notify-event", self._on_motion)
        self.area.connect("leave-notify-event", self._on_leave)
        self.win.add(self.area)

    # ---------------------------------------------------------------- 生命周期

    def show(self, parent=None) -> None:
        """显示并确保压在覆盖层之上。

        这一步是必须的：全屏覆盖层会盖住普通窗口，而一个后台程序想把自己
        「提到最前」会被窗口管理器拦（防抢焦点）。把它设成覆盖层的
        **临时窗口（transient）**，窗口管理器就会保证它始终在父窗口之上，
        同时点击它也不会把键盘焦点从覆盖层抢走。
        """
        if parent is not None and self._parent is not parent:
            try:
                self.win.set_transient_for(parent)
                self._parent = parent
            except Exception:  # noqa: BLE001
                pass
        self.win.show_all()
        self.win.set_keep_above(True)
        try:
            self.win.present()
        except Exception:  # noqa: BLE001
            pass

    def hide(self) -> None:
        self.win.hide()

    def destroy(self) -> None:
        try:
            self.win.destroy()
        except Exception:  # noqa: BLE001
            pass

    @property
    def visible(self) -> bool:
        return self.win.get_visible()

    # ---------------------------------------------------------------- 状态同步

    def set_state(self, tool: str, color: str) -> None:
        if tool != self.tool or color != self.color:
            self.tool = tool
            self.color = color
            self.area.queue_draw()

    def place(self, sel_canvas, screen_origin, screen_size) -> None:
        """贴到选区下方；放不下就上方。用户拖动过之后就不再自动移动。

        ``sel_canvas`` 是画布坐标的选区（(x0, y0, x1, y1)），
        ``screen_origin`` 是画布原点对应的屏幕坐标，``screen_size`` 是屏幕尺寸。
        """
        if self._manual_pos is not None:
            return
        if sel_canvas is None:
            return
        sx0, sy0, sx1, sy1 = sel_canvas
        ox, oy = screen_origin
        sw, sh = screen_size
        bw, bh = int(round(self.state.bar_w)), int(round(self.state.bar_h))
        margin = int(round(MARGIN * self.state.ui_scale))

        x = int(sx0) + ox
        x = min(max(x, ox + margin), ox + sw - bw - margin)
        y = int(sy1) + oy + int(round(GAP_TO_SEL * self.state.ui_scale))
        if y + bh > oy + sh - margin:
            y = int(sy0) + oy - bh - int(round(GAP_TO_SEL * self.state.ui_scale))
        y = min(max(y, oy + margin), oy + sh - bh - margin)
        self.win.move(x, y)

    # ---------------------------------------------------------------- 交互

    def _on_draw(self, _w, cr) -> bool:
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        if self.icon_painter is None:
            return False
        paint_bar(cr, self.state, 0.0, 0.0, self.tool, self.color, self.icon_painter)
        if self.state.hover is not None and self.state.hover.tip:
            b = self.state.hover
            paint_tip(cr, b.tip, b.x + b.w / 2.0, b.y,
                      self.state.bar_w, self.state.ui_scale)
        return False

    def _on_press(self, _w, event) -> bool:
        x, y = float(event.x), float(event.y)
        if event.button == 1:
            b = self.state.at(x, y)
            if b is not None:
                try:
                    self.on_action(b)
                except Exception as e:  # noqa: BLE001
                    print(f"[toolbar] 动作 {b.action} 失败: {e}")
                return True
            # 空白处按下：开始拖动整条工具栏
            self._drag = (event.x_root, event.y_root, event.x, event.y)
            return True
        if event.button == 3:
            self._manual_pos = None          # 右键：恢复自动跟随选区
            return True
        return False

    def _on_release(self, _w, _event) -> bool:
        self._drag = None
        return False

    def _on_motion(self, _w, event) -> bool:
        if self._drag:
            rx, ry, wx, wy = self._drag
            nx = int(event.x_root - (rx - wx))
            ny = int(event.y_root - (ry - wy))
            self.win.move(nx, ny)
            self._manual_pos = (nx, ny)
            return True
        x, y = float(event.x), float(event.y)
        b = self.state.at(x, y)
        if b is not self.state.hover:
            self.state.hover = b
            if self.on_hover is not None and b is not None:
                try:
                    self.on_hover(b.tip)
                except Exception:  # noqa: BLE001
                    pass
            self.area.queue_draw()
        return False

    def _on_leave(self, *_a) -> bool:
        if self.state.hover is not None:
            self.state.hover = None
            self.area.queue_draw()
        return False

    @property
    def position(self) -> tuple[int, int]:
        return self.win.get_position()


# ---------------------------------------------------------------- 编辑器窗口


class EditorWindow(AnnotationRenderer):
    """方案三：选完区域后开一个**普通窗口**（像画图程序）来做标注。

    为什么需要它：覆盖层上的工具栏、以及浮在遮罩上的独立小窗，都要和窗口管理器
    的层级/焦点打交道（实测会遇到「窗口显示了但收不到点击」）。这个方案完全不
    碰覆盖层 —— 就是一个普通窗口：中间是截下来的图，上面是工具栏，有标题栏和
    关闭按钮，窗口管理器正常给它焦点、正常派发鼠标事件。

    绘图复用 ``AnnotationRenderer``，和覆盖层是同一套形状语义。
    """

    def __init__(self, image, on_commit, on_cancel=None, ui_scale: float = 1.0,
                 icon_painter=None, title: str = "SnapCtrlAlt 标注", app=None) -> None:
        # app 用来做「临时切到方案①」：只改本次运行的形态，不动默认设置
        self.app = app
        self.on_commit = on_commit
        self.on_cancel = on_cancel or (lambda: None)
        self.icon_painter = icon_painter
        self.image = image
        self.img_w, self.img_h = image.width, image.height

        # AnnotationRenderer 需要的上下文
        self.screen = image
        self.dev_scale = 1.0
        self.color = COLORS[0]
        self.width = WIDTHS[1]
        from .geometry import Geometry

        self.geo = Geometry(img_w=self.img_w, img_h=self.img_h,
                            root_w=self.img_w, root_h=self.img_h,
                            win_w=self.img_w, win_h=self.img_h)

        # 底图 surface：缓存一份，避免每帧重新编码（编辑器画布每帧都要贴它）
        self._base_surf = pil_to_surface(image.convert("RGB"))
        self.shapes: list[dict] = []
        self.redo_stack: list[dict] = []
        self._seq_no = 1
        self.tool = T_RECT
        self._drag: dict | None = None
        self._hover: Button | None = None
        self._zoom = 1.0
        self._color_before_pick: str | None = None

        self._base_ui_scale = float(ui_scale)
        self._win_alloc: tuple[int, int] | None = None
        # 工具栏尺寸**只由 UI 缩放决定**，与截图尺寸无关。
        # 早期把宽度绑到了图宽上（avail = 图宽 - 8），于是截一张 936 宽的图时
        # 工具栏被压到 scale 1.54，比覆盖层里的小一圈 —— 那是错的。
        # 窗口比工具栏窄时由 ScrolledWindow 横向滚动，不缩工具栏。
        self.ts = ToolbarState(ui_scale)

        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win.set_title(f"{title} · {self.img_w}×{self.img_h}")
        # 默认宽度：至少放得下整条工具栏，再大也不超过屏幕的九成
        need_w = int(self.ts.bar_w) + 40
        screen_w = 1600
        try:
            from gi.repository import Gdk

            disp = Gdk.Display.get_default()
            if disp is not None:
                mon = disp.get_primary_monitor() or disp.get_monitor(0)
                if mon is not None:
                    screen_w = mon.get_geometry().width
        except Exception:  # noqa: BLE001
            pass
        win_w = min(max(need_w, min(self.img_w + 40, 1240)), int(screen_w * 0.92))
        self.win.set_default_size(int(win_w), min(900, self.img_h + 170))
        self.win.set_position(Gtk.WindowPosition.CENTER)
        self.win.set_icon_name("camera-photo")
        self.win.connect("delete-event", lambda *_: (self.cancel(), True)[1])
        self.win.connect("key-press-event", self._on_key)
        self.win.connect("size-allocate", self._on_size_allocate)

        self.outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.win.add(self.outer)

        # ---- 工具栏（真正的 GTK 控件，随窗口布局）----
        self.toolbar_area = Gtk.DrawingArea()
        self.toolbar_area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.toolbar_area.connect("draw", self._on_toolbar_draw)
        self.toolbar_area.connect("button-press-event", self._on_toolbar_press)
        self.toolbar_area.connect("motion-notify-event", self._on_toolbar_motion)
        self.toolbar_area.connect("leave-notify-event", self._on_toolbar_leave)
        self.toolbar_area.set_can_focus(False)
        # 放进滚动容器：窗口比工具栏窄（或矮）时也不会被裁掉两端
        self.bar_scroll = Gtk.ScrolledWindow()
        self.bar_scroll.set_margin_top(6)
        self.bar_scroll.set_margin_bottom(6)
        self.bar_scroll.add_with_viewport(self.toolbar_area)

        # ---- 中间画布 ----
        self.canvas = Gtk.DrawingArea()
        self.canvas.set_size_request(self.img_w, self.img_h)
        self.canvas.set_can_focus(True)
        self.canvas.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.KEY_PRESS_MASK)
        self.canvas.connect("draw", self._on_canvas_draw)
        self.canvas.connect("button-press-event", self._on_canvas_press)
        self.canvas.connect("button-release-event", self._on_canvas_release)
        self.canvas.connect("motion-notify-event", self._on_canvas_motion)
        self.canvas.connect("key-press-event", self._on_key)
        self.canvas_scroll = Gtk.ScrolledWindow()
        self.canvas_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.canvas_scroll.add_with_viewport(self.canvas)

        # ---- 底部按钮（放在工具栏**对面**那条边）----
        self.bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.bottom.set_margin_top(6)
        self.bottom.set_margin_bottom(8)
        self.bottom.set_margin_start(10)
        self.bottom.set_margin_end(10)
        hint = Gtk.Label(label="滚轮缩放 · Ctrl+Z 撤销 · Enter 完成 · 点工具栏四角可换边")
        hint.set_halign(Gtk.Align.START)
        hint.get_style_context().add_class("dim-label")
        self.bottom.pack_start(hint, True, True, 0)
        btn_copy = Gtk.Button(label="复制到剪贴板")
        btn_save = Gtk.Button(label="另存为…")
        btn_cancel = Gtk.Button(label="取消")
        btn_close = Gtk.Button(label="完成")
        btn_close.get_style_context().add_class("suggested-action")
        for b in (btn_copy, btn_save, btn_cancel):
            self.bottom.pack_start(b, False, False, 0)
        self.bottom.pack_end(btn_close, False, False, 0)
        btn_copy.connect("clicked", lambda _b: self.commit("copy"))
        btn_save.connect("clicked", lambda _b: self.commit("save"))
        btn_cancel.connect("clicked", lambda _b: self.cancel())
        btn_close.connect("clicked", lambda _b: self.commit("copy"))

        # ---- 摆放：默认工具栏在上、按钮在下；点四角小三角换边 ----
        self.placement = "top"
        self.grid = Gtk.Grid()
        self.outer.pack_start(self.grid, True, True, 0)
        self._apply_layout()

    # ------------------------------------------------------------ 摆放

    def _fit_canvas(self, win_w: int, win_h: int) -> None:
        """把图缩到"扣掉工具栏/按钮条之后"剩下的空间里。

        竖排工具栏会占掉左右两侧（用户实测"挡了一部分图像"），画布宽度只剩
        一部分，不缩的话图会被裁掉一大块。
        """
        k = self._px_scale()
        dev_w, dev_h = win_w * k, win_h * k      # 逻辑 → 设备像素
        bw, bh = int(self.ts.bar_w), int(self.ts.bar_h)
        vertical = self.placement in ("left", "right")
        if vertical:
            avail_w = dev_w - bw - 220 * k       # 另一侧留给底部按钮条
            avail_h = dev_h - 90 * k
        else:
            avail_w = dev_w - 40 * k
            avail_h = dev_h - bh - 130 * k
        if avail_w <= 20 or avail_h <= 20:
            return
        # 画布尺寸是**设备像素**，而窗口可用空间是**逻辑像素**：GDK 会把画布按
        # 1/k 缩放着画出来（k = 窗口 scale，本机 2）。所以这里要除以 k，
        # 否则算出来的 zoom 只有实际需要的一半（图看起来特别小）。
        z = min(1.0, avail_w / max(1, self.img_w), avail_h / max(1, self.img_h))
        self.set_zoom(max(0.05, z / k))

    def _on_size_allocate(self, _w, alloc) -> None:
        """窗口尺寸变了：重排（竖排的缩放档位要跟着窗口高度走）。"""
        size = (int(alloc.width), int(alloc.height))
        if size == self._win_alloc:
            return
        self._win_alloc = size
        self._apply_layout()


    def set_placement(self, place: str) -> None:
        """把工具栏贴到上/下/左/右某条边（用户点四角的小三角）。

        * 贴上/下：工具栏横排、底部按钮放到对面那条边；
        * 贴左/右：工具栏**竖排**（``ToolbarState.set_vertical`` 就地转置每个按钮的
          矩形，绘制与命中共用同一份），底部按钮挪到另一侧。
        """
        place = (place or "top").lower()
        if place not in ("top", "bottom", "left", "right"):
            place = "top"
        self.placement = place
        self.ts.set_vertical(place in ("left", "right"))
        self._apply_layout()
        if self.app is not None:
            cfg = getattr(self.app, "cfg", None)
            if isinstance(cfg, dict):
                cfg["toolbar_place"] = place

    # 竖排时允许的缩放档位（从大到小试，取第一个能一次装下的）
    VERT_SCALES = (1.0, 0.85, 0.7)

    def _px_scale(self) -> int:
        """窗口逻辑像素 → 工具栏设备像素的倍率（HiDPI 下是 2）。"""
        try:
            win = self.win.get_window()
            if win is not None:
                return max(1, int(win.get_scale_factor()))
        except Exception:  # noqa: BLE001
            pass
        return 1

    def _fit_vertical_toolbar(self, win_h: int) -> None:
        """竖排：选一个"内容能装进窗口高度"的缩放；装不下就用最小的那档。

        用户实测「切换到竖排的时候，那几个选项太长了，小窗口的时候挡了一部分
        图像」：竖排条长 = 内容总长（约 1500px），而窗口可能只有 600px。
        折行解决不了（总长不变），所以整体缩小 —— 图标字号一起缩，仍可读可点。
        """
        # win_h 是逻辑像素，工具栏尺寸是设备像素：先换算到同一套单位
        k = self._px_scale()
        avail = max(160.0, (win_h - 120.0) * k)
        base = self._base_ui_scale
        chosen = None
        for f in self.VERT_SCALES:
            cand = ToolbarState(base * f)
            cand.set_vertical(True)
            cand._build_corners()
            chosen = cand
            if cand.natural_height() <= avail:
                break
        # 换过 ToolbarState 之后要重新接一次绘制/命中（ts 是新对象，
        # 但 DrawingArea 还是同一个，回调里用的是 self.ts，所以只要 queue_draw）
        self.ts = chosen
        self.ts._place_buttons()
        self.ts._build_corners()
        self.toolbar_area.queue_draw()
        if self.ts.natural_height() > avail:
            # 最小档也装不下（窗口太矮）：条**钳进窗口可用高度**，剩下的按钮靠
            # 工具栏自己的滚动条看 —— 关键是条不再长长地竖出去挡住图。
            # 注意：必须在这里钳（set_vertical 会把 bar_h 重置回内容长度）。
            self.ts.bar_h = min(self.ts.bar_h, avail)
            self.ts._place_buttons()
            self.ts._build_corners()
            self._need_bar_scroll = True

    def _apply_layout(self, win_w: int = 0, win_h: int = 0) -> None:
        """按 placement 重摆三块：工具栏 / 画布 / 底部按钮。"""
        if win_w and win_h:
            self._win_alloc = (int(win_w), int(win_h))
        for child in list(self.grid.get_children()):
            self.grid.remove(child)
        vertical = self.placement in ("left", "right")
        self._need_bar_scroll = False
        if vertical and self._win_alloc is not None:
            self._fit_vertical_toolbar(self._win_alloc[1])
        elif self.ts.ui_scale != self._base_ui_scale:
            self.ts = ToolbarState(self._base_ui_scale)       # 横排恢复原大小
        self.toolbar_area.set_size_request(int(self.ts.bar_w), int(self.ts.bar_h))
        # 竖排：工具条比窗口高时给**竖直滚动条**（否则被裁掉的按钮点不到）
        need_scroll = bool(getattr(self, "_need_bar_scroll", False))
        self.bar_scroll.set_policy(
            Gtk.PolicyType.AUTOMATIC if not vertical else Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC if (not vertical or need_scroll)
            else Gtk.PolicyType.NEVER)
        self.bottom.set_orientation(
            Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL)
        if self.placement == "top":
            self.grid.attach(self.bar_scroll, 0, 0, 1, 1)
            self.grid.attach(self.canvas_scroll, 0, 1, 1, 1)
            self.grid.attach(self.bottom, 0, 2, 1, 1)
        elif self.placement == "bottom":
            self.grid.attach(self.bottom, 0, 0, 1, 1)
            self.grid.attach(self.canvas_scroll, 0, 1, 1, 1)
            self.grid.attach(self.bar_scroll, 0, 2, 1, 1)
        elif self.placement == "left":
            self.grid.attach(self.bar_scroll, 0, 0, 1, 1)
            self.grid.attach(self.canvas_scroll, 1, 0, 1, 1)
            self.grid.attach(self.bottom, 2, 0, 1, 1)
        else:                                    # right
            self.grid.attach(self.bottom, 0, 0, 1, 1)
            self.grid.attach(self.canvas_scroll, 1, 0, 1, 1)
            self.grid.attach(self.bar_scroll, 2, 0, 1, 1)
        self.canvas_scroll.set_hexpand(True)
        self.canvas_scroll.set_vexpand(True)
        self.grid.show_all()
        if self._win_alloc is not None:
            self._fit_canvas(*self._win_alloc)

    # ---------------------------------------------------------------- 生命周期

    def show(self) -> None:
        self.win.show_all()
        self.win.present()
        self.canvas.grab_focus()

    def destroy(self) -> None:
        try:
            self.win.destroy()
        except Exception:  # noqa: BLE001
            pass

    @property
    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, z: float) -> None:
        z = max(0.1, min(8.0, z))
        if abs(z - self._zoom) < 1e-3:
            return
        self._zoom = z
        self.canvas.set_size_request(int(self.img_w * z), int(self.img_h * z))
        self.canvas.queue_draw()

    # ---------------------------------------------------------------- 工具栏

    def _on_toolbar_draw(self, _w, cr) -> bool:
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        paint_bar(cr, self.ts, 0, 0, self.tool, self.color,
                  self.icon_painter or (lambda *a: None))
        return False

    def _on_toolbar_press(self, _w, event) -> bool:
        b = self.ts.at(float(event.x), float(event.y))
        if b is None:
            return False
        self._apply_action(b)
        return True

    def _on_toolbar_motion(self, _w, event) -> bool:
        b = self.ts.at(float(event.x), float(event.y))
        if b is not self.ts.hover:
            self.ts.hover = b
            self.toolbar_area.set_tooltip_text(b.tip if b else None)
            self.toolbar_area.queue_draw()
        return False

    def _on_toolbar_leave(self, *_a) -> bool:
        if self.ts.hover is not None:
            self.ts.hover = None
            self.toolbar_area.queue_draw()
        return False

    def _apply_action(self, b: Button) -> None:
        a = b.action
        if a == "tool":
            self.tool = b.data
        elif a == "color":
            self.color = b.data
        elif a == "custom_color":
            self._pick_custom_color()
        elif a == "editor":
            self._switch_mode("canvas")
        elif a == "place":
            self.set_placement(b.data)
        elif a == "place":
            self.set_placement(b.data)
        elif a == "width":
            self.width = b.data
        elif a == "undo":
            self.undo()
        elif a == "redo":
            self.redo()
        elif a == "clear":
            self.clear()
        elif a in ("finish", "copy"):
            self.commit("copy")
            return
        elif a == "save":
            self.commit("save")
            return
        elif a == "cancel":
            self.cancel()
            return
        self.toolbar_area.queue_draw()
        self.canvas.queue_draw()

    def _switch_mode(self, mode: str) -> None:
        """一键切到另一个工具栏形态（方案① 贴选区工具栏）。"""
        fn = getattr(self.app, "switch_toolbar_mode", None)
        if callable(fn):
            fn(mode)
        else:
            self.win.destroy()

    # ---------------------------------------------------------------- 画布

    def _on_canvas_draw(self, _w, cr) -> bool:
        """先贴截图底图，再叠标注。

        底图是必须的：早期版本只画了标注形状，于是窗口中间一片空白
        （用户实测「没有截图显示」）—— 标注是画在截图上的，底图不画就只剩形状。
        """
        z = self._zoom
        cr.save()
        cr.scale(z, z)
        cr.set_source_surface(self._base_surf, 0, 0)
        cr.paint()
        self._draw_shapes(cr)
        cr.restore()
        if self._drag and self._drag.get("preview"):
            cr.save()
            cr.scale(z, z)
            self._draw_preview(cr, self._drag)
            cr.restore()
        return False

    def _to_img(self, x: float, y: float) -> tuple[float, float]:
        """窗口画布坐标 → 图像像素坐标。"""
        z = self._zoom or 1.0
        return x / z, y / z

    def _on_canvas_press(self, _w, event) -> bool:
        if event.button != 1:
            return False
        self.canvas.grab_focus()
        ix, iy = self._to_img(float(event.x), float(event.y))
        ix = max(0.0, min(ix, self.img_w))
        iy = max(0.0, min(iy, self.img_h))
        if self.tool == T_PICKER:
            px = int(max(0, min(ix, self.img_w - 1)))
            py = int(max(0, min(iy, self.img_h - 1)))
            r, g, b_ = self.image.getpixel((px, py))[:3]
            self.color = f"#{r:02x}{g:02x}{b_:02x}"
            self.tool = T_RECT
            self.toolbar_area.queue_draw()
            return True
        if self.tool == T_SEQ:
            self.shapes.append({"tool": T_SEQ, "xy": (ix, iy), "n": self._seq_no,
                                "color": self.color,
                                "size": max(14.0, self.width * 5.0)})
            self._seq_no += 1
            self.redo_stack.clear()
            self.canvas.queue_draw()
            return True
        self._drag = {"kind": "draw", "tool": self.tool, "start": (ix, iy),
                      "cur": (ix, iy), "points": [(ix, iy)], "preview": True,
                      "color": self.color, "width": self.width}
        return True

    def _on_canvas_motion(self, _w, event) -> bool:
        if not self._drag:
            return False
        ix, iy = self._to_img(float(event.x), float(event.y))
        self._drag["cur"] = (ix, iy)
        if self._drag.get("tool") in (T_PEN, T_HIGHLIGHT):
            self._drag.setdefault("points", []).append((ix, iy))
        self.canvas.queue_draw()
        return True

    def _on_canvas_release(self, _w, event) -> bool:
        if not self._drag or event.button != 1:
            return False
        ix, iy = self._to_img(float(event.x), float(event.y))
        d, self._drag = self._drag, None
        d["cur"] = (ix, iy)
        if d.get("tool") in (T_PEN, T_HIGHLIGHT):
            d.setdefault("points", []).append((ix, iy))
        self._commit_shape(d, (ix, iy))
        self.canvas.queue_draw()
        return True

    def _draw_preview(self, cr, d: dict) -> None:
        """拖拽中的预览（与覆盖层同样的形状，只是画在窗口画布上）。"""
        tool = d.get("tool")
        sx, sy = d["start"]
        cx, cy = d.get("cur", (sx, sy))
        col = _rgba(d.get("color", self.color))
        lw = float(d.get("width", self.width))
        cr.save()
        cr.set_line_width(lw)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        if tool in (T_RECT, T_MOSAIC, T_BLUR):
            x0, y0 = min(sx, cx), min(sy, cy)
            x1, y1 = max(sx, cx), max(sy, cy)
            if tool == T_RECT:
                cr.set_source_rgba(*col)
                cr.rectangle(x0, y0, x1 - x0, y1 - y0)
                cr.stroke()
            else:
                cr.set_source_rgba(1, 1, 1, 0.95)
                cr.set_dash([5, 4])
                cr.set_line_width(1.6)
                cr.rectangle(x0, y0, x1 - x0, y1 - y0)
                cr.stroke()
        elif tool == T_ELLIPSE:
            x0, y0 = min(sx, cx), min(sy, cy)
            x1, y1 = max(sx, cx), max(sy, cy)
            cr.set_source_rgba(*col)
            cr.save()
            cr.translate((x0 + x1) / 2.0, (y0 + y1) / 2.0)
            cr.scale(max(0.01, (x1 - x0) / 2.0), max(0.01, (y1 - y0) / 2.0))
            cr.arc(0, 0, 1, 0, 6.283185307179586)
            cr.restore()
            cr.stroke()
        elif tool == T_ARROW:
            cr.set_source_rgba(*col)
            self._arrow_path(cr, (sx, sy), (cx, cy), lw)
            cr.stroke()
        elif tool in (T_PEN, T_HIGHLIGHT):
            pts = list(d.get("points") or [])
            if len(pts) >= 2:
                if tool == T_HIGHLIGHT:
                    cr.set_line_width(max(lw, 10))
                    cr.set_source_rgba(col[0], col[1], col[2], 0.35)
                else:
                    cr.set_source_rgba(*col)
                cr.move_to(*pts[0])
                for p in pts[1:]:
                    cr.line_to(*p)
                cr.stroke()
        cr.restore()

    # ---------------------------------------------------------------- 编辑操作

    def _commit_shape(self, d: dict, end) -> None:
        tool = d.get("tool")
        if tool not in (T_RECT, T_ELLIPSE, T_ARROW, T_PEN, T_HIGHLIGHT, T_MOSAIC, T_BLUR):
            return
        x0, y0 = d["start"]
        x1, y1 = d.get("cur") or end
        if tool in (T_RECT, T_ELLIPSE, T_MOSAIC, T_BLUR):
            bx = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            if bx[2] - bx[0] < 3 or bx[3] - bx[1] < 3:
                return
            shape = {"tool": tool, "box": bx, "color": d.get("color", self.color),
                     "width": d.get("width", self.width)}
        elif tool == T_ARROW:
            if math.hypot(x1 - x0, y1 - y0) < 6:
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

    def _pick_custom_color(self) -> None:
        """选自定义颜色。

        早期方案三**根本没有处理 custom_color 动作**，所以点色环毫无反应；
        方案一虽然接了，但对话框会被全屏覆盖层压在下面。两边现在都：
        设 parent、置顶、present()，并把结果写回。
        """
        self._color_before_pick = self.color
        dlg = Gtk.ColorChooserDialog(title="选择标注颜色", parent=self.win)
        dlg.set_modal(True)
        dlg.set_keep_above(True)
        dlg.set_use_alpha(False)
        rgba = Gdk.RGBA()
        rgba.parse(self.color)
        dlg.set_rgba(rgba)
        # 实时预览：拖动取色时工具栏的色环跟着变，方便判断效果
        dlg.connect("color-activated",
                    lambda _d, c: self._preview_color(c))
        dlg.connect("notify::rgba",
                    lambda d, _p: self._preview_color(d.get_rgba()))
        dlg.present()
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            self.color = _rgba_to_hex(dlg.get_rgba())
        else:
            self.color = self._color_before_pick
        dlg.destroy()
        self.toolbar_area.queue_draw()
        self.canvas.queue_draw()

    def _preview_color(self, c) -> None:
        if getattr(self, "_color_before_pick", None) is None:
            self._color_before_pick = self.color
        self.color = _rgba_to_hex(c)
        self.toolbar_area.queue_draw()

    def undo(self) -> None:
        if not self.shapes:
            return
        s = self.shapes.pop()
        self.redo_stack.append(s)
        if s.get("tool") == T_SEQ:
            self._seq_no = max(1, self._seq_no - 1)
        self.canvas.queue_draw()

    def redo(self) -> None:
        if not self.redo_stack:
            return
        s = self.redo_stack.pop()
        self.shapes.append(s)
        if s.get("tool") == T_SEQ:
            self._seq_no = min(len(_CIRCLED), self._seq_no + 1)
        self.canvas.queue_draw()

    def clear(self) -> None:
        if not self.shapes:
            return
        self.redo_stack = list(reversed(self.shapes)) + self.redo_stack
        self.shapes.clear()
        self._seq_no = 1
        self.canvas.queue_draw()

    def render_result(self):
        """把标注烧进图片，返回最终 PIL 图。"""
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.img_w, self.img_h)
        cr = cairo.Context(surf)
        cr.set_source_surface(self._base_surf, 0, 0)
        cr.paint()
        self._draw_shapes(cr)
        surf.flush()
        data = bytes(surf.get_data())
        img = Image.frombuffer("RGBA", (self.img_w, self.img_h), data, "raw",
                               "BGRA", surf.get_stride(), 1).copy()
        # Cairo 是预乘 alpha，转回 PIL 需要还原
        px = img.load()
        for y in range(self.img_h):
            for x in range(self.img_w):
                r, g, b, a = px[x, y]
                if a not in (0, 255):
                    f = 255.0 / a
                    px[x, y] = (min(255, int(r * f)), min(255, int(g * f)),
                                min(255, int(b * f)), a)
        return img.convert("RGB")

    def commit(self, action: str) -> None:
        try:
            img = self.render_result()
        except Exception as e:  # noqa: BLE001
            import traceback

            print(f"[editor] 渲染失败: {type(e).__name__}: {e}\n"
                  + traceback.format_exc(), flush=True)
            return
        self.destroy()
        self.on_commit(img, action)

    def cancel(self) -> None:
        self.destroy()
        self.on_cancel()

    def _on_key(self, _w, event) -> bool:
        kv = event.keyval
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        if kv == Gdk.KEY_Escape:
            self.cancel()
            return True
        if kv in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.commit("copy")
            return True
        if ctrl and kv in (Gdk.KEY_z, Gdk.KEY_Z):
            self.redo() if shift else self.undo()
            return True
        quick = {Gdk.KEY_r: T_RECT, Gdk.KEY_o: T_ELLIPSE, Gdk.KEY_a: T_ARROW,
                 Gdk.KEY_p: T_PEN, Gdk.KEY_h: T_HIGHLIGHT, Gdk.KEY_t: T_TEXT,
                 Gdk.KEY_m: T_MOSAIC, Gdk.KEY_b: T_BLUR, Gdk.KEY_s: T_SELECT}
        if not ctrl and kv in quick:
            self.tool = quick[kv]
            self.toolbar_area.queue_draw()
            return True
        return False
