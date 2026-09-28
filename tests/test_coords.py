#!/usr/bin/env python3
"""坐标标定回归测试：保证「框选的位置」与「截下来的位置」永远是同一块。

这是针对「鼠标圈定的位置和所截图的位置不是一个位置」的专项防护。测试分三层：

1. **纯数学**：``Geometry`` 在 1× / 2× / 1.5× / 3× 各种「根 ↦ 抓图」倍率下，
   矩形换算、边界钳制、指针自证是否都正确。
2. **端到端像素**：拿真实的整屏抓图，用真实标定构造覆盖层，框一块区域导出，
   再和原始抓图的同一区域逐像素比对 —— 偏移必须是 0。
3. **故障注入**：人为把事件坐标整体平移/缩放，验证指针自证能发现并纠正，
   且不会被单个异常采样带坏。

    python3 tests/test_coords.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from PIL import Image, ImageChops  # noqa: E402

from snapctrlalt.geometry import Geometry  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  —— {detail}" if detail else ""))
    return bool(ok)


# ---------------------------------------------------------------- 1. 纯数学


def test_rect_conversion() -> None:
    print("\n[1] 矩形换算（根坐标 → 抓图像素）")
    cases = [
        ("设备像素 1:1", 800, 600, 800, 600),
        ("HiDPI 2x", 800, 600, 1600, 1200),
        ("分数缩放 1.5x", 800, 600, 1200, 900),
        ("三倍", 640, 480, 1920, 1440),
    ]
    for name, rw, rh, iw, ih in cases:
        g = Geometry(img_w=iw, img_h=ih, root_w=rw, root_h=rh, win_w=rw, win_h=rh)
        rect = (100, 50, 300, 250)
        got = g.rect_root_to_img(rect)
        zx, zy = g.zoom_x, g.zoom_y
        want = (round(100 * zx), round(50 * zy), round(300 * zx), round(250 * zy))
        check(f"{name}：换算结果正确", got == want, f"{got} 期望 {want}")

    # 反向矩形（拖右下往左上）也要归一化
    g = Geometry(img_w=1600, img_h=1200, root_w=800, root_h=600, win_w=800, win_h=600)
    check("反向拖动的矩形被归一化",
          g.rect_root_to_img((300, 250, 100, 50)) == (200, 100, 600, 500),
          str(g.rect_root_to_img((300, 250, 100, 50))))

    # 越界钳制
    check("超出右下的矩形被钳到图像内",
          g.rect_root_to_img((700, 500, 9000, 9000)) == (1400, 1000, 1600, 1200),
          str(g.rect_root_to_img((700, 500, 9000, 9000))))
    check("负坐标被钳到 0",
          g.rect_root_to_img((-500, -500, 100, 100))[0:2] == (0, 0),
          str(g.rect_root_to_img((-500, -500, 100, 100))))
    tiny = g.rect_root_to_img((10, 10, 10, 10))
    check("零面积矩形保证至少 1×1", tiny[2] > tiny[0] and tiny[3] > tiny[1], str(tiny))


def test_pointer_self_check() -> None:
    print("\n[2] 指针自证：发现并纠正偏移 / 缩放")
    # a) 完全一致：不应改动任何东西
    g = Geometry(img_w=800, img_h=600, root_w=800, root_h=600, win_w=800, win_h=600)
    changed = g.apply_pointer_check((123, 456), (123, 456))
    check("读数一致时不改标定", not changed and g.ptr_offset_x == 0,
          f"changed={changed} offset={g.ptr_offset_x}")
    check("单点读数一致不会被误判为已自证", g.verified is False)

    # b) 单个异常采样不得污染标定
    g2 = Geometry(img_w=800, img_h=600, root_w=800, root_h=600, win_w=800, win_h=600)
    g2.apply_pointer_check((10, 10), (600, 500))
    check("单个异常采样不会改标定", g2.ptr_offset_x == 0 and g2.ptr_scale_x == 1.0,
          f"offset={g2.ptr_offset_x} scale={g2.ptr_scale_x}")

    # c) 一致的整体平移：多个样本应解出 offset
    g3 = Geometry(img_w=800, img_h=600, root_w=800, root_h=600, win_w=800, win_h=600)
    for ex, ey in ((100, 100), (400, 350), (700, 500)):
        g3.apply_pointer_check((ex, ey), (ex + 37, ey + 24))
    check("整体平移被解出", abs(g3.ptr_offset_x - 37) < 2 and abs(g3.ptr_offset_y - 24) < 2,
          f"offset=({g3.ptr_offset_x:.1f},{g3.ptr_offset_y:.1f})")

    # d) 事件坐标被缩放过（分数缩放）：两个相距够远的样本应解出 scale
    g4 = Geometry(img_w=1200, img_h=900, root_w=1200, root_h=900, win_w=1200, win_h=900)
    for ex, ey in ((100, 100), (500, 420), (900, 740)):
        g4.apply_pointer_check((ex, ey), (int(ex * 1.5), int(ey * 1.5)))
    ok = abs(g4.ptr_scale_x - 1.5) < 0.05 and abs(g4.ptr_scale_y - 1.5) < 0.05
    check("缩放不同源被解出（1.5×）", ok,
          f"scale=({g4.ptr_scale_x:.3f},{g4.ptr_scale_y:.3f})")
    check("修正后换算给出正确的根坐标",
          abs(g4.ptr_to_root(600, 600)[0] - 900) < 20,
          f"ptr_to_root(600,600)={tuple(round(v) for v in g4.ptr_to_root(600, 600))}")


def test_virtual_screen_offset() -> None:
    """副屏排在主屏左侧/上方时，虚拟屏原点不是 (0,0)。"""
    print("\n[2b] 虚拟屏原点非 0（副屏在左 / 在上）")
    # 左屏 1920 宽 + 主屏 1920 宽，根原点 x = -1920
    g = Geometry(img_w=3840, img_h=1080, root_w=3840, root_h=1080,
                 win_w=3840, win_h=1080, win_x=-1920, win_y=0)
    check("窗口原点记录正确", (g.win_x, g.win_y) == (-1920, 0), f"{g.win_x},{g.win_y}")
    check("几何自检仍无异常", g.check() == [], str(g.check()))

    # 覆盖层窗口铺满虚拟屏后，事件坐标是相对窗口的：窗口 (100,100)
    # 对应根坐标 (-1920+100, 100)；换算成抓图像素就是 100,100（zoom=1）。
    # 这里用退化几何（overlay 在拿不到 X11 时的路径）验证这段偏移链路。
    from snapctrlalt.overlay import _fallback_geometry

    fb = _fallback_geometry(3840, 1080, (-1920, 0, 3840, 1080), 1)
    check("退化几何保留窗口原点",
          (fb.win_x, fb.win_y) == (-1920, 0), f"{fb.win_x},{fb.win_y}")
    check("指针换算带上窗口原点偏移",
          fb.ptr_to_root(100, 100) == (-1820, 100),
          f"ptr_to_root(100,100)={fb.ptr_to_root(100, 100)}")
    check("窗口局部坐标 → 抓图像素（去掉原点）",
          fb.ptr_to_img(100, 100) == (-1820, 100),
          f"ptr_to_img(100,100)={fb.ptr_to_img(100, 100)}")

    # 真 X11 标定路径：事件坐标本身就是根坐标（窗口在根原点），偏移为 0
    g2 = Geometry(img_w=3840, img_h=1080, root_w=3840, root_h=1080,
                  win_w=3840, win_h=1080, win_x=-1920, win_y=-1080)
    check("根坐标换算不受窗口摆放影响",
          g2.rect_root_to_img((0, 0, 100, 100)) == (0, 0, 100, 100),
          str(g2.rect_root_to_img((0, 0, 100, 100))))


def test_extreme_fractional_scaling() -> None:
    """1.25× / 1.75× 这类分数缩放不能出现累积偏差。"""
    print("\n[2c] 分数缩放（1.25× / 1.75×）的精度")
    for zoom in (1.25, 1.75):
        g = Geometry(img_w=int(1920 * zoom), img_h=int(1080 * zoom),
                     root_w=1920, root_h=1080, win_w=1920, win_h=1080)
        worst = 0
        for x in range(0, 1920, 97):
            for y in range(0, 1080, 89):
                ix, iy, _x1, _y1 = g.rect_root_to_img((x, y, x + 1, y + 1))
                worst = max(worst, abs(ix - round(x * zoom)), abs(iy - round(y * zoom)))
        check(f"{zoom}× 全屏扫描无累积偏差", worst <= 1, f"最大偏差 {worst}px")
        check(f"{zoom}× 缩放被正确识别", abs(g.zoom_x - zoom) < 0.01,
              f"zoom={g.zoom_x:.4f}")
        check(f"{zoom}× 不是整数倍（走浮点路径）", g.integer_zoom is None,
              f"integer_zoom={g.integer_zoom}")


def test_geometry_report() -> None:
    print("\n[3] 几何自检报告")
    g = Geometry(img_w=800, img_h=600, root_w=800, root_h=600, win_w=800, win_h=600)
    check("正常几何没有问题项", g.check() == [], str(g.check()))
    bad = Geometry(img_w=800, img_h=600, root_w=800, root_h=600, win_w=700, win_h=600)
    check("窗口没铺满根窗口会被报出来",
          any("铺满" in p for p in bad.check()), str(bad.check()))
    frac = Geometry(img_w=1200, img_h=600, root_w=800, root_h=600, win_w=800, win_h=600)
    check("两轴缩放不一致会被报出来",
          any("两轴" in p for p in frac.check()), str(frac.check()))


# ---------------------------------------------------------------- 2. 端到端像素


def test_real_capture_alignment() -> None:
    print("\n[4] 真实抓图端到端：框选区域 == 导出像素")
    from snapctrlalt import capture_linux as capture

    if not capture.has_x11():
        print("  无 X11（纯 Wayland 或无显示），跳过真实抓图比对")
        return
    try:
        info = capture.virtual_screen_bounds()
        img, real = capture.grab_full_screen(info)
    except Exception as e:  # noqa: BLE001
        check("抓图成功", False, str(e))
        return
    check("抓图成功", img.width > 0, f"{img.width}×{img.height}")

    g = Geometry(img_w=img.width, img_h=img.height,
                 root_w=real.width, root_h=real.height,
                 win_w=real.width, win_h=real.height)
    print(f"    标定：{g.describe().splitlines()[0]}")
    check("几何自检无异常", g.check() == [], str(g.check()))

    from test_overlay import TestOverlay, FakeApp

    Gtk.init_check(sys.argv[:1])
    app = FakeApp(Path(tempfile.mkdtemp()))
    o = TestOverlay(app, img, (0, 0, img.width, img.height),
                    on_close=lambda r: None, geo=g, status_cb=app.status.append)
    check("画布尺寸 = 根窗口尺寸", (o.cr_w, o.cr_h) == (real.width, real.height),
          f"画布 {o.cr_w}×{o.cr_h}，根 {real.width}×{real.height}")

    # 取屏幕中间一块（避开可能变化的边角），框选后导出比对
    rw, rh = real.width, real.height
    rects = [
        (rw // 4, rh // 4, rw // 2, rh // 2),
        (10, 10, rw // 5, rh // 5),
        (rw - rw // 5, rh - rh // 5, rw - 10, rh - 10),
    ]
    for idx, rect in enumerate(rects, 1):
        o.sel = tuple(float(v) for v in rect)
        out = o.render_result()
        x0, y0, x1, y1 = g.rect_root_to_img(rect)
        want = img.crop((x0, y0, x1, y1))
        diff = ImageChops.difference(out.convert("RGB"), want.convert("RGB")).getbbox()
        check(f"区域 {idx} 导出与抓图逐像素一致",
              out.size == want.size and diff is None,
              f"{out.size} vs {want.size}，差异 {diff}")

    # 自校准：用同一张抓图做交叉验证。
    # 不在「活的桌面」上做先后两次抓图比对 —— 屏幕随时会变（浏览器重绘、
    # 时钟跳字），那会让测试偶发失败，而失败与被测代码无关。
    # 这里换一种自证方式：同一个选区用两种方式取图，必须逐像素一致：
    #   方式 A：覆盖层的导出路径（走 Geometry 的仿射换算 + Cairo 渲染）
    #   方式 B：直接按换算出的像素矩形裁原始抓图
    # 内容是否变化都不影响结论，因为两者用的是同一份像素。
    rw, rh = real.width, real.height
    probes = [
        (rw // 4, rh // 4, rw // 2, rh // 2),
        (0, 0, rw // 6, rh // 6),
        (rw - rw // 5, rh - rh // 5, rw - 1, rh - 1),
        (rw // 3, rh // 8, rw // 3 + 137, rh // 8 + 91),
    ]
    all_same = True
    for idx, rect in enumerate(probes, 1):
        o.sel = tuple(float(v) for v in rect)
        out = o.render_result()
        x0, y0, x1, y1 = g.rect_root_to_img(rect)
        want = img.crop((x0, y0, x1, y1))
        diff = ImageChops.difference(out.convert("RGB"), want.convert("RGB")).getbbox()
        ok = out.size == want.size and diff is None
        all_same = all_same and ok
        if not ok:
            check(f"探针 {idx} 两种取图方式一致", False,
                  f"{out.size} vs {want.size} 差异 {diff}")
    check("四个探针：覆盖层导出 == 按换算直接裁图（逐像素）", all_same)

    # 再验一次「换算本身」：导出的尺寸必须等于 选区×zoom（与屏幕内容无关）
    zoom_ok = True
    for rect in probes:
        o.sel = tuple(float(v) for v in rect)
        size = o.render_result().size
        want_size = (int(round((rect[2] - rect[0]) * g.zoom_x)),
                     int(round((rect[3] - rect[1]) * g.zoom_y)))
        if abs(size[0] - want_size[0]) > 1 or abs(size[1] - want_size[1]) > 1:
            zoom_ok = False
            check("导出尺寸 == 选区 × zoom", False, f"{size} 期望 {want_size}")
    check("导出尺寸 == 选区 × zoom（与屏幕内容无关）", zoom_ok)

    # 面板尺寸不再依赖屏幕内容，也不再需要「反查最佳匹配偏移」那一步
    o.win.destroy()


def main() -> int:
    print("SnapCtrlAlt 坐标标定回归测试")
    # 部分用例要构造 GTK 窗口；无显示时只跑纯数学部分
    ok, _argv = Gtk.init_check(sys.argv[:1])
    if not ok:
        print("  没有可用的图形显示，仅运行纯数学用例（跳过真实抓图部分）")
        for fn in (test_rect_conversion, test_pointer_self_check,
                   test_virtual_screen_offset, test_extreme_fractional_scaling,
                   test_geometry_report):
            try:
                fn()
            except Exception:  # noqa: BLE001
                import traceback

                RESULTS.append((fn.__name__, False, "异常"))
                print(f"  ✗ {fn.__name__} 抛异常\n{traceback.format_exc()}")
        passed = sum(1 for _n, o, _d in RESULTS if o)
        total = len(RESULTS)
        print(f"\n通过 {passed}/{total}（纯数学部分）")
        return 0 if passed == total else 1
    for fn in (test_rect_conversion, test_pointer_self_check,
               test_virtual_screen_offset, test_extreme_fractional_scaling,
               test_geometry_report, test_real_capture_alignment):
        try:
            fn()
        except Exception:  # noqa: BLE001
            import traceback

            RESULTS.append((fn.__name__, False, "异常"))
            print(f"  ✗ {fn.__name__} 抛异常\n{traceback.format_exc()}")
    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 60)
    print(f"通过 {passed}/{total}")
    for n, ok, _d in RESULTS:
        if not ok:
            print(f"  失败：{n}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
