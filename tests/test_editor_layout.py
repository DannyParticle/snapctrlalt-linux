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
