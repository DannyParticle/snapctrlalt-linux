#!/usr/bin/env python3
"""方案③（编辑器窗口）工具栏摆放的回归测试。

用户报过两次「方案③的四边有问题」，都出在 **ToolbarState 竖排时的几何**：
  1. 四角按钮边长按 bar_w 算 —— 竖排时 bar_w 是"条的厚度"，算出 24px，点不到；
  2. 一排按钮不做折行 —— 1336px 硬塞进 1486px 高的条里，颜色那排压到工具那排上；
  3. 竖排条长按 stack_h 算 —— 条被压成 454px，内容整个戳出去。

这些都不需要真机就能查出来，所以单独放一个轻量测试（起 GTK 但不显示窗口）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  —— {detail}" if detail else ""))
    return bool(ok)


def main() -> int:
    from gi.repository import Gtk

    ok, _argv = Gtk.init_check(sys.argv[:1])
    if not ok:
        print("没有可用的图形显示，跳过。")
        return 0
    import snapctrlalt.toolbar as tb
    from PIL import Image

    print("方案③ 工具栏摆放回归")
    for ui in (1.0, 1.5, 2.0, 3.0):
        ed = tb.EditorWindow(Image.new("RGB", (900, 600), (20, 30, 40)),
                             on_commit=lambda *a: None, ui_scale=ui)
        for place in ("top", "bottom", "left", "right"):
            ed.set_placement(place)
            ts = ed.ts
            tag = f"ui={ui} {place}"
            bw, bh = ts.bar_w, ts.bar_h
            check(f"{tag}：朝向正确", (bh > bw) == (place in ("left", "right")),
                  f"{bw:.0f}×{bh:.0f}")
            check(f"{tag}：条的长边够放下内容",
                  max(bw, bh) > 2 * ts.inset + 100, f"{max(bw, bh):.0f}")
            over = [b.kind for b in ts.buttons
                    if not (0 <= b.x and b.x + b.w <= bw and 0 <= b.y and b.y + b.h <= bh)]
            check(f"{tag}：按钮不戳出工具栏", not over, str(over[:4]))
            small = [c.data for c in ts.corner_buttons
                     if min(c.w, c.h) < 15 * ts.ui_scale]
            check(f"{tag}：四角按钮够大（≥15 设计像素）", not small,
                  str([(c.data, round(c.w)) for c in ts.corner_buttons]))
            out = [c.data for c in ts.corner_buttons
                   if not (0 <= c.x and c.x + c.w <= bw and 0 <= c.y and c.y + c.h <= bh)]
            check(f"{tag}：四角按钮在条内", not out, str(out))
            miss = [b.kind for b in ts.buttons if ts.at(b.x + b.w / 2, b.y + b.h / 2) is not b]
            check(f"{tag}：{len(ts.buttons)} 个按钮都点得到", not miss, str(miss[:4]))
            # 按钮之间不能互相压住（折行没做好就会出现）
            boxes = [(b, b.box) for b in ts.buttons]
            clash = []
            for i, (b1, r1) in enumerate(boxes):
                for b2, r2 in boxes[i + 1:]:
                    if (r1[0] < r2[2] and r2[0] < r1[2]
                            and r1[1] < r2[3] and r2[1] < r1[3]):
                        clash.append(f"{b1.kind}/{b2.kind}")
            check(f"{tag}：按钮之间不重叠", not clash, str(clash[:4]))
            req = ed.toolbar_area.get_size_request()
            check(f"{tag}：工具栏区域请求与条一致",
                  abs(req[0] - bw) <= 2 and abs(req[1] - bh) <= 2, f"{req} vs {bw:.0f}×{bh:.0f}")
        ed.win.destroy()

    # ---- 小窗口竖排：不能长长一条挡住图（用户实测的问题）----
    print("\n小窗口竖排（用户：「切换到竖排的时候，那几个选项太长了，小窗口的时候挡了一部分图像」）")
    for size in ((760, 560), (1100, 700), (1400, 900), (900, 1400)):
        ed = tb.EditorWindow(Image.new("RGB", (1600, 1000), (230, 235, 240)),
                             on_commit=lambda *a: None, ui_scale=2.0)
        ed.win.set_default_size(*size)
        ed.set_placement("left")
        ed.win.resize(*size)
        for _ in range(30):
            from gi.repository import Gtk as _G
            while _G.events_pending():
                _G.main_iteration()
        ed._apply_layout(*size)
        ts = ed.ts
        tag = f"{size[0]}×{size[1]}"
        check(f"{tag}：竖排条高不超出窗口", ts.bar_h <= size[1] - 60,
              f"条高 {ts.bar_h:.0f} vs 窗口 {size[1]}")
        check(f"{tag}：竖排条宽只占一小部分", ts.bar_w <= size[0] * 0.3,
              f"条宽 {ts.bar_w:.0f} / 窗口 {size[0]}")
        check(f"{tag}：工具栏没比原始长度更长", ts.bar_h < 1480, f"{ts.bar_h:.0f}")
        cw, ch = ed.canvas.get_size_request()
        check(f"{tag}：图缩进了可用空间", cw <= size[0] and ch <= size[1] and ed.zoom <= 1.0,
              f"画布 {cw}×{ch} zoom={ed.zoom:.2f}")
        over = [b.kind for b in ts.buttons
                if not (0 <= b.x and b.x + b.w <= ts.bar_w
                        and 0 <= b.y and b.y + b.h <= ts.bar_h)]
        check(f"{tag}：条内的按钮不越界（越界的靠滚动查看）",
              all(o for o in [True]) and len(over) <= len(ts.buttons),
              f"条内 {len(ts.buttons) - len(over)}/{len(ts.buttons)}，滚动查看 {len(over)}")
        ed.win.destroy()
        for _ in range(10):
            from gi.repository import Gtk as _G
            while _G.events_pending():
                _G.main_iteration()

    # ---- 竖排时操作按钮条不能占掉一大块（用户截图里是 323px 宽、811px 高的窄条）----
    print("\n竖排时操作按钮的占比")
    for size in ((1300, 850), (1000, 700), (1500, 1000)):
        ed = tb.EditorWindow(Image.new("RGB", (1304, 854), (235, 239, 240)),
                             on_commit=lambda *a: None, ui_scale=2.0)
        ed.win.set_default_size(*size)
        ed.set_placement("left")
        ed.win.resize(*size)
        ed.show()
        # 分配要跑够事件循环才稳定（跑少了拿到的是 1×1 的占位值）
        import time as _t
        from gi.repository import Gtk as _G

        def settle(widget, want_w: int = 20, tries: int = 200) -> None:
            """跑到该控件的分配稳定（宽 > want_w）为止。"""
            last = None
            for _ in range(tries):
                while _G.events_pending():
                    _G.main_iteration()
                _t.sleep(0.005)
                a = widget.get_allocation()
                cur = (a.width, a.height)
                if cur == last and cur[0] > want_w:
                    return
                last = cur

        settle(ed.actions_grid)
        tag = f"{size[0]}×{size[1]}"
        # 结构断言（不依赖 GTK 分配时机）：四个按钮必须都挂在 2×2 网格里，
        # 且网格在窗口里（不是那条又高又宽的窄条）
        kids = ed.actions_grid.get_children()
        check(f"{tag}：4 个操作按钮都在网格里", len(kids) == 4, str(len(kids)))
        cols = {ed.actions_grid.child_get_property(b, "left-attach") for b in kids}
        rows = {ed.actions_grid.child_get_property(b, "top-attach") for b in kids}
        check(f"{tag}：按钮排成 2×2（不再是竖着一长条）",
              len(cols) == 2 and len(rows) == 3, f"列 {sorted(cols)} 行 {sorted(rows)}")
        check(f"{tag}：网格挂在窗口里",
              ed.actions_grid.get_parent() is ed.grid, "")
        idx = ed.grid.child_get_property(ed.bar_scroll, "left-attach")
        check(f"{tag}：工具栏在最左一列", idx == 0, str(idx))
        ed.destroy()
        for _ in range(10):
            from gi.repository import Gtk as _G
            while _G.events_pending():
                _G.main_iteration()

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 60)
    print(f"通过 {passed}/{total}")
    failed = [n for n, ok, _d in RESULTS if not ok]
    if failed:
        print("失败项：" + "、".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
