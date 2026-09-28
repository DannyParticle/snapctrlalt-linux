#!/usr/bin/env python3
"""界面回归测试（对标 Windows 版 test_ui.py）。

不开真窗口：把 ``ShotOverlay.PRESENT`` 关掉后驱动真实的覆盖层对象，跑完整的
「框选 → 标注 → 渲染 → 导出 → 存盘 → 复制」链路，逐项断言。

    python3 tests/test_overlay.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from PIL import Image, ImageChops  # noqa: E402

from snapctrlalt import overlay as ov  # noqa: E402
from snapctrlalt.geometry import Geometry  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  —— {detail}" if detail else ""))
    return bool(ok)


class FakeApp:
    """只实现覆盖层用得到的那几个接口。"""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.cfg = {"after_capture": "copy", "copy_to_primary": False,
                    "save_format": "png", "jpeg_quality": 95}
        self.messages: list[str] = []
        self.status: list[str] = []      # 覆盖层状态栏消息
        self.saved: list[str] = []
        self.pinned: list[Image.Image] = []

    def save_image(self, img, silent=True):
        p = self.tmp / "auto.png"
        img.save(p)
        self.saved.append(str(p))
        return str(p)

    def notify(self, msg):
        self.messages.append(msg)

    def ask_save_path(self, parent=None):
        return str(self.tmp / "asked.png")

    def write_image(self, img, path):
        Image.open  # noqa: B018
        img.save(path)
        self.saved.append(str(path))

    def pin_image(self, img):
        self.pinned.append(img)

    def set_clipboard_hint(self):
        pass


class TestOverlay(ov.ShotOverlay):
    PRESENT = False


def make_screen(w: int, h: int, scale: int = 1) -> Image.Image:
    """有明确色块的假截图：便于断言「画上去的东西确实变了像素」。"""
    img = Image.new("RGB", (w * scale, h * scale), (240, 240, 245))
    px = img.load()
    for y in range(img.height):
        for x in range(img.width):
            if (x // (40 * scale) + y // (40 * scale)) % 2 == 0:
                px[x, y] = (40, 60, 90)
    return img


def diff_bbox(a: Image.Image, b: Image.Image):
    return ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox()


def new_overlay(app: FakeApp, w=800, h=600, scale=1) -> TestOverlay:
    """构造覆盖层。

    ``scale`` 表示抓图相对屏幕（根窗口）的像素倍数：1 = 设备像素模式（抓图 =
    屏幕像素），2 = 抓图分辨率是屏幕的两倍（HiDPI）。这不是「旧版整数缩放」，
    而是明确的「根 ↦ 图像」映射，由 Geometry 承载。
    """
    img = make_screen(w, h, scale)
    geo = Geometry(img_w=img.width, img_h=img.height, root_w=w, root_h=h,
                   win_w=w, win_h=h)
    return TestOverlay(app, img, (0, 0, img.width, img.height),
                       on_close=lambda r: None, scale=scale, geo=geo,
                       status_cb=app.status.append)


# ---------------------------------------------------------------- 用例


def test_selection_flow(app: FakeApp) -> None:
    print("\n[1] 框选流程")
    o = new_overlay(app)
    check("未框选时 sel 为空", o.sel is None)
    check("初始为选择态", o.mode == "select" and o.tool == ov.T_SELECT)

    # 模拟按下 -> 拖动 -> 松开（覆盖层内部把坐标换算成图像像素）
    o._drag = {"kind": "select", "start": (100.0, 100.0), "cur": (100.0, 100.0),
               "preview": True}
    o._on_motion(o.win, _FakeEvent(x=200, y=180))
    check("拖动中选区随之更新", o.sel is not None and o.sel[2] == 200)
    o._on_release(o.win, _FakeEvent(x=400, y=300, button=1))
    check("松开后进入标注态", o.mode == "draw", f"sel={o.sel}")
    check("选区尺寸正确", o.sel == (100.0, 100.0, 400.0, 300.0), str(o.sel))
    o._layout_toolbar()
    check("工具栏已排布", len(o._buttons) > 10, f"{len(o._buttons)} 个按钮")
    check("工具自动切到矩形", o.tool == ov.T_RECT, str(o.tool))

    # 选区外按下 = 重新框选（QQ 行为）
    o._on_press(o.win, _FakeEvent(x=700, y=550, button=1))
    check("选区外按下可重新框选", o.mode == "select" and o.sel is None)
    o._drag = None

    # 双击 = 整屏
    o._on_press(o.win, _FakeEvent(x=10, y=10, button=1, double=True))
    check("双击选中整屏", o.sel == (0.0, 0.0, 800.0, 600.0), str(o.sel))
    o.win.destroy()


def test_result_matches_selection(app: FakeApp) -> None:
    print("\n[2] 导出尺寸与选区一致（含 HiDPI：抓图分辨率是屏幕的两倍）")
    for scale in (1, 2):
        o = new_overlay(app, 800, 600, scale)
        o.sel = (100.0, 50.0, 300.0, 250.0)      # 选区用屏幕坐标
        img = o.render_result()
        want = (200 * scale, 200 * scale)        # 导出按 zoom 放大
        check(f"x{scale} 导出尺寸 = 选区×抓图倍率 {want}", img.size == want, str(img.size))
        base = o.screen.crop((100 * scale, 50 * scale, 300 * scale, 250 * scale))
        check(f"x{scale} 取的是屏幕上同一区域，逐像素一致",
              diff_bbox(img, base) is None, f"差异 {diff_bbox(img, base)}")
        o.win.destroy()


def test_each_tool(app: FakeApp) -> None:
    print("\n[3] 各标注工具都真的画上去了")
    cases = [
        ("矩形", ov.T_RECT, {"tool": ov.T_RECT, "box": (150.0, 120.0, 320.0, 260.0),
                            "color": "#ff3b30", "width": 4}),
        ("椭圆", ov.T_ELLIPSE, {"tool": ov.T_ELLIPSE, "box": (150.0, 120.0, 320.0, 260.0),
                               "color": "#007aff", "width": 4}),
        ("箭头", ov.T_ARROW, {"tool": ov.T_ARROW, "p0": (140.0, 140.0),
                             "p1": (330.0, 270.0), "color": "#34c759", "width": 4}),
        ("画笔", ov.T_PEN, {"tool": ov.T_PEN,
                           "points": [(140.0 + i * 6, 150.0 + (i % 8) * 8) for i in range(30)],
                           "color": "#000000", "width": 4}),
        ("荧光笔", ov.T_HIGHLIGHT, {"tool": ov.T_HIGHLIGHT,
                                   "points": [(140.0 + i * 6, 200.0) for i in range(30)],
                                   "color": "#ffcc00", "width": 6}),
        ("文字", ov.T_TEXT, {"tool": ov.T_TEXT, "xy": (160.0, 200.0), "text": "测试文字 ABC",
                            "color": "#ffffff", "size": 28.0}),
        ("序号", ov.T_SEQ, {"tool": ov.T_SEQ, "xy": (250.0, 180.0), "n": 1,
                           "color": "#ff3b30", "size": 22.0}),
        ("马赛克", ov.T_MOSAIC, {"tool": ov.T_MOSAIC, "box": (160.0, 140.0, 300.0, 240.0),
                                "color": "#000000", "width": 8}),
        ("模糊", ov.T_BLUR, {"tool": ov.T_BLUR, "box": (160.0, 140.0, 300.0, 240.0),
                            "color": "#000000", "width": 8}),
    ]
    for name, _kind, shape in cases:
        o = new_overlay(app, 800, 600, 1)
        o.sel = (100.0, 100.0, 400.0, 320.0)
        base = o.render_result()
        o.shapes = [dict(shape)]
        out = o.render_result()
        bb = diff_bbox(out, base)
        check(f"{name}：像素确实被改动", bb is not None, f"改动区域 {bb}")
        o.win.destroy()


def test_undo_redo_clear(app: FakeApp) -> None:
    print("\n[4] 撤销 / 重做 / 清空")
    o = new_overlay(app)
    o.sel = (100.0, 100.0, 400.0, 320.0)
    clean = o.render_result()
    o.shapes.append({"tool": ov.T_RECT, "box": (150.0, 150.0, 300.0, 250.0),
                     "color": "#ff3b30", "width": 4})
    drawn = o.render_result()
    check("加标注后画面变化", diff_bbox(drawn, clean) is not None)

    o.undo()
    check("撤销后形状清空", o.shapes == [])
    check("撤销结果回到原始画面", diff_bbox(o.render_result(), clean) is None)
    o.redo()
    check("重做后形状回来", len(o.shapes) == 1)
    check("重做结果与标注后一致", diff_bbox(o.render_result(), drawn) is None)
    o.clear()
    check("清空后无残留", o.shapes == [] and diff_bbox(o.render_result(), clean) is None)
    o.win.destroy()


def test_text_entry_flow(app: FakeApp) -> None:
    print("\n[5] 文字工具（真实 Gtk.Entry 落字）")
    o = new_overlay(app)
    o.sel = (100.0, 100.0, 500.0, 400.0)
    before = o.render_result()
    o._set_tool(ov.T_TEXT)
    o._start_text(200.0, 200.0)
    check("输入框已弹出", o._text_entry is not None)
    o._text_entry.set_text("SnapCtrlAlt")
    o._commit_text()
    check("提交后形状入列", len(o.shapes) == 1 and o.shapes[0]["text"] == "SnapCtrlAlt")
    check("输入框已销毁", o._text_entry is None)
    after = o.render_result()
    check("文字渲染到画面上", diff_bbox(after, before) is not None)

    # 空文本不应该产生标注
    o._start_text(220.0, 220.0)
    o._text_entry.set_text("   ")
    o._commit_text()
    check("空白文字被忽略", len(o.shapes) == 1)
    o.win.destroy()


def test_clip_inside_selection(app: FakeApp) -> None:
    print("\n[6] 标注被裁在选区内")
    o = new_overlay(app)
    o.sel = (200.0, 200.0, 400.0, 400.0)
    # 一个远远超出选区的矩形：左上角落在选区内，右下角在选区外
    o.shapes = [{"tool": ov.T_RECT, "box": (250.0, 250.0, 900.0, 900.0),
                 "color": "#ff0000", "width": 6}]
    out = o.render_result()
    check("越界标注不改变导出尺寸", out.size == (200, 200), str(out.size))

    # 参照：同一个矩形提前裁到选区边界
    o2 = new_overlay(app)
    o2.sel = (200.0, 200.0, 400.0, 400.0)
    o2.shapes = [{"tool": ov.T_RECT, "box": (250.0, 250.0, 400.0, 400.0),
                  "color": "#ff0000", "width": 6}]
    want = o2.render_result()
    bb = diff_bbox(out, want)
    # 差异只允许出现在贴着裁切线的那一圈（描边被裁切时的抗锯齿），
    # 内部必须逐像素一致 —— 等价于「选区外的部分没有画进来」。
    inner_same = True
    if bb:
        for y in range(60, 190):
            for x in range(60, 190):
                if out.getpixel((x, y)) != want.getpixel((x, y)):
                    inner_same = False
                    break
            if not inner_same:
                break
    check("选区内部与裁剪后重画逐像素一致", inner_same, f"差异区域 {bb}")
    check("差异只出现在裁切边缘", bb is None or (bb[0] >= 45 and bb[1] >= 45),
          f"差异区域 {bb}")

    # 只画在选区外的东西不应该出现在结果里
    o3 = new_overlay(app)
    o3.sel = (200.0, 200.0, 400.0, 400.0)
    clean = o3.render_result()
    o3.shapes = [{"tool": ov.T_RECT, "box": (500.0, 500.0, 600.0, 600.0),
                  "color": "#ff0000", "width": 6}]
    check("完全在选区外的标注不影响结果",
          diff_bbox(o3.render_result(), clean) is None)
    for x in (o, o2, o3):
        x.win.destroy()


def test_scroll_width_and_tools(app: FakeApp) -> None:
    print("\n[7] 滚轮线宽 / 工具切换")
    o = new_overlay(app)
    o.sel = (100.0, 100.0, 400.0, 300.0)
    o._set_tool(ov.T_PEN)
    w0 = o.width
    o._on_scroll(o.win, _FakeEvent(direction="up"))
    check("滚轮放大线宽", o.width >= w0, f"{w0} -> {o.width}")
    for _ in range(5):
        o._on_scroll(o.win, _FakeEvent(direction="down"))
    check("线宽有下限", o.width == ov.WIDTHS[0], str(o.width))

    o._set_tool(ov.T_PICKER)
    o._on_press(o.win, _FakeEvent(x=120, y=120, button=1))
    check("取色后自动切回矩形", o.tool == ov.T_RECT, str(o.tool))
    check("取到的颜色是合法 hex", o.color.startswith("#") and len(o.color) == 7, o.color)
    o.win.destroy()


def test_handles_and_move(app: FakeApp) -> None:
    print("\n[8] 手柄缩放 / 整体平移")
    o = new_overlay(app)
    o.sel = (100.0, 100.0, 400.0, 300.0)
    o._set_tool(ov.T_SELECT)
    h = o._handle_at(100.0, 100.0)
    check("左上角手柄可命中", h == "nw", str(h))
    o._drag = {"kind": "resize", "handle": "se", "start": (400.0, 300.0),
               "orig": o.sel}
    o._apply_resize(500.0, 400.0)
    o._drag = None
    check("拖手柄改变选区", o.sel == (100.0, 100.0, 500.0, 400.0), str(o.sel))

    o._drag = {"kind": "move", "start": (200.0, 200.0), "orig": o.sel}
    o._apply_move(250.0, 220.0)
    o._drag = None
    check("选区整体平移", o.sel == (150.0, 120.0, 550.0, 420.0), str(o.sel))

    o._drag = {"kind": "move", "start": (200.0, 200.0), "orig": o.sel}
    o._apply_move(-900.0, -900.0)
    o._drag = None
    check("平移被钳制在屏幕内", o.sel[0] >= 0 and o.sel[1] >= 0, str(o.sel))
    o.win.destroy()


def test_persist_and_finish(app: FakeApp) -> None:
    print("\n[9] 保存 / 完成（走真实存盘与剪贴板）")
    o = new_overlay(app)
    o.sel = (100.0, 100.0, 260.0, 220.0)
    o.shapes = [{"tool": ov.T_RECT, "box": (120.0, 120.0, 240.0, 200.0),
                 "color": "#ff3b30", "width": 4}]
    o.save()
    p = Path(app.tmp / "asked.png")
    check("另存为写出了文件", p.is_file(), str(p))
    if p.is_file():
        saved = Image.open(p)
        check("存盘尺寸与选区一致", saved.size == (160, 120), str(saved.size))

    app.cfg["after_capture"] = "copy"
    o2 = new_overlay(app)
    o2.sel = (0.0, 0.0, 120.0, 90.0)
    o2.finish()
    check("完成时给出了状态提示",
          any("复制" in m or "剪贴板" in m for m in app.status),
          str(app.status[-1:]))
    try:
        from snapctrlalt import clipboard_linux

        clipboard_linux.pump(200)
        got = clipboard_linux.read_clipboard_image()
        check("剪贴板里能读回图像", got is not None and got.size == (120, 90),
              str(got.size) if got else "None")
    except Exception as e:  # noqa: BLE001
        check("剪贴板里能读回图像", False, str(e))

    app.cfg["after_capture"] = "copy_save"
    o3 = new_overlay(app)
    o3.sel = (0.0, 0.0, 100.0, 80.0)
    n_before = len(app.saved)
    o3.finish()
    check("完成后自动存了一份", len(app.saved) > n_before, str(app.saved[-1:]))
    o.win.destroy()


def test_pin(app: FakeApp) -> None:
    print("\n[10] 贴图")
    o = new_overlay(app)
    o.sel = (50.0, 50.0, 250.0, 200.0)
    o.pin()
    check("贴图请求把结果交给了应用层", len(app.pinned) == 1,
          str(app.pinned[0].size) if app.pinned else "无")
    if app.pinned:
        check("贴图内容尺寸正确", app.pinned[0].size == (200, 150), str(app.pinned[0].size))
    o.win.destroy()


def test_toolbar_click_does_not_reset_selection(app: FakeApp) -> None:
    """工具栏上的点击（含按钮之间的空隙）绝不能把选区清掉。

    这是用户实际遇到的问题：点在工具栏按钮之间的空隙上，会因为「落在选区外」
    而被判成重新框选，于是刚选好的区域没了、得重截一次。
    """
    print("\n[12] 工具栏点击不毁选区")
    o = new_overlay(app)
    o.ui_scale = 2.0
    o.sel = (100.0, 100.0, 700.0, 500.0)
    o.mode = "draw"
    o._layout_toolbar()
    check("工具栏已排布", o._tb_pos is not None, str(o._tb_pos))

    # 双排布局：按排分别找分组空隙
    rows: dict[float, list] = {}
    for b in o._buttons:
        rows.setdefault(b.y, []).append(b)
    gaps: list[tuple[float, float]] = []
    for ry, btns in rows.items():
        btns.sort(key=lambda b: b.x)
        for i in range(len(btns) - 1):
            gap_px = btns[i + 1].x - (btns[i].x + btns[i].w)
            if gap_px > 4:
                gaps.append((btns[i].x + btns[i].w + gap_px / 2.0, ry + btns[i].h / 2.0))
    check("工具栏里确实存在分组空隙", bool(gaps), f"{len(gaps)} 处，{len(rows)} 排")

    if gaps:
        gx = int(gaps[0][0])          # 按钮矩形已是画布坐标
        gy = int(gaps[0][1])
        before = o.sel
        o._on_press(o.win, _FakeEvent(x=gx, y=gy, button=1))
        check("点空隙：选区保持不变", o.sel == before, f"{before} -> {o.sel}")
        check("点空隙：仍处于标注态", o.mode == "draw", o.mode)

    check("工具栏是双排布局",
          len({round(b.y) for b in o._buttons}) == 2,
          f"排数={len({round(b.y) for b in o._buttons})}")
    row1 = [b for b in o._buttons if round(b.y) == min(round(x.y) for x in o._buttons)]
    row2 = [b for b in o._buttons if b not in row1]
    check("第一排全是标注工具", len(row1) == len(ov.TOOL_ORDER),
          f"{len(row1)} 个")
    check("第二排是颜色/线宽/操作", len(row2) > 10, f"{len(row2)} 个")
    # 单排 29 个按钮在 ui_scale=2 下约 1844 画布像素；双排应显著更窄
    tb_w = o._tb_pos[2]
    check("双排后宽度明显收窄（<1400 画布像素）", tb_w < 1400, f"{tb_w:.0f}")

    # 真实按钮依然生效
    o.sel = (100.0, 100.0, 700.0, 500.0)
    o.mode = "draw"
    o._layout_toolbar()
    b = o._buttons[0]
    o._on_press(o.win, _FakeEvent(
        x=int(b.x + b.w / 2),
        y=int(b.y + b.h / 2), button=1))
    check("点真实按钮：工具切换生效", o.tool == b.data, f"tool={o.tool}")

    # 选区外的空白处仍然可以重新框选（原行为不能被改坏）
    # 注意：要避开工具栏矩形本身 —— 它可能盖住画布一角，落在它上面的点击
    # 本就该被工具栏吃掉。这里选一个明确在工具栏与选区之外的空白点。
    o.sel = (300.0, 600.0, 700.0, 900.0)
    o.mode = "draw"
    o._layout_toolbar()
    tbx, tby, tbw, tbh = o._tb_pos
    px_, py_ = tbx + tbw + 40, tby + tbh + 40      # 工具栏右下方
    inside_tb = tbx <= px_ <= tbx + tbw and tby <= py_ <= tby + tbh
    check("测试点确实在工具栏之外", not inside_tb, f"({px_:.0f},{py_:.0f})")
    o._on_press(o.win, _FakeEvent(x=int(px_), y=int(py_), button=1))
    check("选区外空白处仍可重新框选", o.sel is None and o.mode == "select",
          f"sel={o.sel} mode={o.mode}")
    o._drag = None
    o.win.destroy()


def test_handles_work_with_any_tool(app: FakeApp) -> None:
    """选区手柄必须在任何标注工具下都能用（参考 Flameshot / ksnip）。

    早期只在「选择工具」下才响应手柄，而选完区域后工具会自动切到矩形，
    于是用户想微调边缘时被当成要画矩形 —— 表现为「点不中、还得重截」。
    """
    print("\n[13] 手柄在任何工具下都能调整选区")
    o = new_overlay(app, 1200, 800, 1)
    o.ui_scale = 1.0
    o._drag = {"kind": "select", "start": (200.0, 150.0), "cur": (200.0, 150.0),
               "preview": True}
    o._on_release(o.win, _FakeEvent(x=900, y=650, button=1))
    check("选区建立后工具自动切到矩形", o.tool == ov.T_RECT, o.tool)

    for tool in (ov.T_RECT, ov.T_PEN, ov.T_ELLIPSE, ov.T_ARROW, ov.T_TEXT, ov.T_BLUR):
        o._set_tool(tool)
        o._drag = None
        hit = o._handle_at(900.0, 650.0)
        check(f"{tool}：右下角手柄可命中", hit == "se", str(hit))
        o._on_press(o.win, _FakeEvent(x=900, y=650, button=1))
        kind = (o._drag or {}).get("kind")
        check(f"{tool}：拖手柄是缩放而非画图", kind == "resize", str(kind))
        o._drag = None

    # 选区内部仍然要能画图（不能被手柄逻辑吃掉）
    o._set_tool(ov.T_PEN)
    o._on_press(o.win, _FakeEvent(x=550, y=400, button=1))
    check("选区中间拖动仍是画图", (o._drag or {}).get("kind") == "draw",
          str((o._drag or {}).get("kind")))
    o._drag = None

    # 四角与四边都要能命中
    o._set_tool(ov.T_RECT)
    o._layout_toolbar()
    corners = {"nw": (200.0, 150.0), "ne": (900.0, 150.0),
               "sw": (200.0, 650.0), "se": (900.0, 650.0)}
    for name, (hx, hy) in corners.items():
        check(f"{name} 角手柄可命中", o._handle_at(hx, hy) == name,
              str(o._handle_at(hx, hy)))
    o.win.destroy()


def test_toolbar_reachable_for_any_selection(app: FakeApp) -> None:
    """无论选区什么形状，工具栏都必须完整落在屏幕内、每个按钮都点得到。

    用户报的「窄截图时最左/最右功能点不到」就是这个不变量被破坏了：
    早期布局用「设计尺寸」摆位置、绘制又整体乘 ui_scale，工具栏被放大后跑到
    屏幕外（实测落在 x 2056..3900，而屏幕只有 2880 宽）；后来又在已被 GTK
    缩放过的上下文里二次缩放，整条落到 clip 之外而**完全消失**。现在
    ui_scale 只在布局里乘一次，矩形即最终画布矩形，绘制不再缩放。
    """
    print("\n[15] 工具栏对任意选区都完整可见可点")
    W, H = 2880, 1800
    shapes = [
        ("窄 560×200", (1160, 700, 1720, 900)),
        ("极窄 200×200", (1340, 700, 1540, 900)),
        ("细长 280×900", (1200, 400, 1480, 1300)),
        ("贴右边缘", (2700, 600, 2879, 900)),
        ("贴左边缘", (2, 600, 560, 900)),
        ("贴底边", (1100, 1650, 1780, 1798)),
        ("贴顶边", (1100, 2, 1780, 300)),
        ("常规 1400×300", (740, 700, 2140, 1000)),
        ("整屏", (0, 0, W, H)),
    ]
    from snapctrlalt.geometry import Geometry  # noqa: PLC0415

    for name, sel in shapes:
        img = make_screen(W // 8, H // 8, 1)
        geo = Geometry(img_w=W, img_h=H, root_w=W, root_h=H, win_w=W, win_h=H)
        o = TestOverlay(app, img, (0, 0, W, H), on_close=lambda r: None, geo=geo)
        o.sel = tuple(float(v) for v in sel)
        o.mode = "draw"
        o.tool = ov.T_RECT
        o._layout_toolbar()
        bx, by, bw, bh = o._tb_pos
        # ui_scale 已在布局里算进尺寸，_tb_pos 就是最终画布矩形
        inside = bx >= 0 and by >= 0 and bx + bw <= W and by + bh <= H
        check(f"{name}：工具栏完整在屏幕内", inside,
              f"矩形 {bx:.0f},{by:.0f}..{bx + bw:.0f},{by + bh:.0f}（画布 {W}×{H}）")
        missed = [b.kind for b in o._buttons
                  if o._button_at(b.x + b.w / 2, b.y + b.h / 2) is not b]
        check(f"{name}：{len(o._buttons)} 个按钮全部可点", not missed, str(missed[:4]))
        o.win.destroy()


def test_construction_smoke(app: FakeApp) -> None:
    """覆盖层必须能真的构造出来（真窗口，非离屏）。

    这条是补课：一次重构误删了 _make_resize_cursors，单元测试全绿但程序里
    覆盖层直接起不来（NameError）。这里用真窗口构造一次，专门盯住
    「构造函数里引用的每个符号都存在」。
    """
    print("\n[14] 构造冒烟：真窗口构造覆盖层")
    import gi as _gi

    _gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk as _Gtk

    img = make_screen(320, 200, 1)
    try:
        o = ov.ShotOverlay(app, img, (0, 0, 320, 200),
                           on_close=lambda r: None, scale=1)
    except Exception as e:  # noqa: BLE001
        check("覆盖层可构造", False, f"{type(e).__name__}: {e}")
        return
    check("覆盖层可构造", True, f"ui_scale={o.ui_scale}")
    check("缩放手柄光标已就绪", bool(o._resize_cursors),
          f"{len(o._resize_cursors)} 个方位")
    check("UI 缩放系数是正数", o.ui_scale > 0, str(o.ui_scale))
    for attr in ("_tb_pos", "_buttons", "geo", "cr_w", "cr_h"):
        check(f"属性 {attr} 存在", hasattr(o, attr))
    o.win.destroy()


def test_editor_window(app: FakeApp) -> None:
    """编辑器窗口（方案三）：能构造、能画、能导出、尺寸正确。

    这一形态是为了绕开「独立窗口浮在覆盖层上收不到点击」那类与窗口管理器
    打交道的问题 —— 它就是一个普通窗口，所以这里在真窗口上验证。
    """
    print("\n[16] 编辑器窗口（普通窗口形态）")
    from snapctrlalt import toolbar as tb_mod
    from snapctrlalt.overlay import draw_icon

    img = make_screen(320, 240, 1)
    got: list = []
    ed = tb_mod.EditorWindow(img, on_commit=lambda i, a: got.append((i.size, a)),
                             ui_scale=1.0, icon_painter=draw_icon)
    check("编辑器可构造", ed.win is not None, ed.win.get_title())
    check("工具栏已排布", len(ed.ts.buttons) > 20, f"{len(ed.ts.buttons)} 个按钮")
    check("画布尺寸 = 图片尺寸",
          (ed.canvas.get_size_request().width, ed.canvas.get_size_request().height)
          == (320, 240),
          str(ed.canvas.get_size_request()))
    ed.set_zoom(2.0)
    check("缩放后画布尺寸成倍",
          ed.canvas.get_size_request().width == 640, str(ed.canvas.get_size_request()))
    ed.set_zoom(1.0)

    # 直接提交一个形状，验证导出结果里确实有它
    before = ed.render_result()
    ed.shapes = [{"tool": ov.T_RECT, "box": (60.0, 60.0, 200.0, 160.0),
                  "color": "#ff3b30", "width": 6}]
    after = ed.render_result()
    check("导出尺寸 = 原图尺寸", after.size == (320, 240), str(after.size))
    check("标注被烧进导出结果", diff_bbox(after, before) is not None,
          str(diff_bbox(after, before)))
    ed.undo()
    check("撤销后回到原样", diff_bbox(ed.render_result(), before) is None)
    ed.redo()
    check("重做后标注回来", diff_bbox(ed.render_result(), before) is not None)
    ed.clear()
    check("清空后没有残留", diff_bbox(ed.render_result(), before) is None)

    # commit 会走 on_commit 并销毁窗口
    ed.shapes = [{"tool": ov.T_RECT, "box": (20.0, 20.0, 120.0, 100.0),
                  "color": "#007aff", "width": 4}]
    ed.commit("copy")
    check("提交回传了结果与动作", got == [((320, 240), "copy")], str(got))

    # 工具栏尺寸只由 UI 缩放决定，与截图尺寸无关（截小图时工具栏不该变小）
    sizes = []
    for w in (320, 800, 1600):
        ed2 = tb_mod.EditorWindow(make_screen(w, 300, 1),
                                  on_commit=lambda i, a: None,
                                  ui_scale=2.0, icon_painter=draw_icon)
        sizes.append((round(ed2.ts.bar_w), round(ed2.ts.bar_h)))
        ed2.destroy()
    check("工具栏尺寸与截图尺寸无关（都是 1204×152）",
          len(set(sizes)) == 1 and sizes[0] == (1204, 152), str(sizes))


def test_toolbar_follows_selection(app: FakeApp) -> None:
    """工具栏自动跟随选区：换选区就换位置，永远整条可见可点。

    （早期试过「可拖动的画布工具栏」作为「浮在遮罩上」的替代实现，但它改变了
    默认行为、并非需求，已移除。）
    """
    print("\n[17] 工具栏自动跟随选区")
    W, H = 2880, 1800
    from snapctrlalt.geometry import Geometry  # noqa: PLC0415

    img = make_screen(W // 8, H // 8, 1)
    geo = Geometry(img_w=W, img_h=H, root_w=W, root_h=H, win_w=W, win_h=H)
    o = TestOverlay(app, img, (0, 0, W, H), on_close=lambda r: None, geo=geo)
    o.ui_scale = 2.0
    o.mode = "draw"
    o.tool = ov.T_RECT

    seen = []
    for name, sel in (("居中", (700.0, 300.0, 1900.0, 900.0)),
                      ("贴顶", (700.0, 4.0, 1900.0, 400.0)),
                      ("贴底", (700.0, 1400.0, 1900.0, 1796.0)),
                      ("细长", (1300.0, 300.0, 1600.0, 1200.0))):
        o.sel = sel
        o._layout_toolbar()
        bx, by, bw, bh = o._tb_pos
        seen.append((round(bx), round(by)))
        inside = bx >= 0 and by >= 0 and bx + bw <= W and by + bh <= H
        missed = [b.kind for b in o._buttons
                  if o._button_at(b.x + b.w / 2, b.y + b.h / 2) is not b]
        check(f"{name}：工具栏整条可见", inside,
              f"({bx:.0f},{by:.0f})..({bx + bw:.0f},{by + bh:.0f})")
        check(f"{name}：按钮全部可点", not missed, str(missed[:3]))

    check("换选区后工具栏位置跟着变", len(set(seen)) >= 3, str(seen))
    o.win.destroy()


def test_cancel(app: FakeApp) -> None:
    print("\n[11] 取消")
    reasons: list[str] = []
    img = make_screen(400, 300)
    o = TestOverlay(app, img, (0, 0, img.width, img.height),
                    on_close=reasons.append, scale=1)
    o.sel = (10.0, 10.0, 100.0, 100.0)
    o.cancel()
    check("关闭回调收到 cancel", reasons == ["cancel"], str(reasons))
    check("取消后窗口标记为已关闭", o._closed is True)
    app.cfg["after_capture"] = "copy"


class _FakeEvent:
    """够用的假事件对象（覆盖层只读这几个字段）。"""

    def __init__(self, x: int = 0, y: int = 0, button: int = 0,
                 double: bool = False, direction: str = "up") -> None:
        import gi as _gi

        _gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk

        self.x = float(x)
        self.y = float(y)
        self.x_root = float(x)
        self.y_root = float(y)
        self.button = button
        self.type = (Gdk.EventType.DOUBLE_BUTTON_PRESS if double
                     else Gdk.EventType.BUTTON_PRESS)
        self.direction = (Gdk.ScrollDirection.UP if direction == "up"
                          else Gdk.ScrollDirection.DOWN)
        self.state = Gdk.ModifierType(0)
        self.keyval = 0


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="snapctrlalt-test-"))
    app = FakeApp(tmp)
    print(f"SnapCtrlAlt Linux 界面回归测试（临时目录 {tmp}）")
    # 覆盖层构造需要 GTK 初始化；无显示（CI 未起 Xvfb）时优雅跳过
    ok, _argv = Gtk.init_check(sys.argv[:1])
    if not ok:
        print("\n没有可用的图形显示（DISPLAY 未设置？），跳过界面回归测试。")
        print("提示：CI 里请用 xvfb-run -a python3 tests/test_overlay.py")
        return 0

    for fn in (test_selection_flow, test_result_matches_selection, test_each_tool,
               test_undo_redo_clear, test_text_entry_flow, test_clip_inside_selection,
               test_scroll_width_and_tools, test_handles_and_move,
               test_persist_and_finish, test_pin, test_toolbar_click_does_not_reset_selection,
               test_handles_work_with_any_tool, test_toolbar_reachable_for_any_selection,
               test_construction_smoke, test_editor_window,
               test_toolbar_follows_selection, test_cancel):
        try:
            fn(app)
        except Exception:  # noqa: BLE001
            import traceback

            RESULTS.append((fn.__name__, False, "异常"))
            print(f"  ✗ {fn.__name__} 抛异常\n{traceback.format_exc()}")

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 60)
    print(f"通过 {passed}/{total}")
    failed = [n for n, ok, _d in RESULTS if not ok]
    if failed:
        print("失败项：" + "、".join(failed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
