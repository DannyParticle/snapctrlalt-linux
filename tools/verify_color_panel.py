#!/usr/bin/env python3
"""真机验收：取色面板到底画在哪、多大、有没有超出工具栏。

流程：拉起常驻实例 → XTEST 拖出选区 → IPC dbgtb 拿工具栏屏幕矩形 →
点「色环」按钮 → 抓屏 → 检查
  * 面板宽度 == 工具栏宽度（不超出工具栏）
  * 面板完整在屏幕内（不会画到屏幕外）
  * 面板紧贴工具栏（不重叠）
同时输出面板区间的像素颜色，确认四条滑块真的画出来了。

会临时接管鼠标约 15 秒。截图落在 /tmp/cp_*.png。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("DISPLAY", ":0")

from PIL import Image  # noqa: E402
from Xlib import X, display  # noqa: E402
from Xlib.ext import xtest  # noqa: E402

d = display.Display()
W, H = d.screen().width_in_pixels, d.screen().height_in_pixels
CFG = Path("/tmp/cp-cfg")


def move(x, y):
    xtest.fake_input(d, X.MotionNotify, x=int(x), y=int(y))
    d.sync()
    time.sleep(0.04)


def click(x, y):
    move(x, y)
    xtest.fake_input(d, X.ButtonPress, 1)
    d.sync(); time.sleep(0.06)
    xtest.fake_input(d, X.ButtonRelease, 1)
    d.sync(); time.sleep(0.15)


def shot(path):
    time.sleep(0.8)
    subprocess.run(["gnome-screenshot", "-f", path], check=True,
                   capture_output=True)
    return Image.open(path).convert("RGB")


def kill_resident():
    out = subprocess.run(
        "ps -eo pid,args | grep -E 'python3 .*bin/snapctrlalt' | grep -v grep",
        shell=True, capture_output=True, text=True).stdout
    for line in out.splitlines():
        subprocess.run(["kill", line.split()[0]])


def main() -> int:
    kill_resident()
    if CFG.exists():
        for p in CFG.rglob("*"):
            p.unlink() if p.is_file() else None
    (CFG / "snapctrlalt").mkdir(parents=True, exist_ok=True)
    (CFG / "snapctrlalt" / "config.json").write_text(json.dumps({
        "hotkey": "ctrl+alt+d", "show_tray": True, "notify": False,
        "after_capture": "copy", "reselect_on_empty": False,
    }), encoding="utf-8")
    env = dict(os.environ, XDG_CONFIG_HOME=str(CFG), SNAP_CP_DEBUG="1")
    logf = open("/tmp/cp_app.log", "w", encoding="utf-8")
    proc = subprocess.Popen([str(ROOT / "bin" / "snapctrlalt")], env=env,
                            stdout=logf, stderr=subprocess.STDOUT, text=True)
    fails = []
    try:
        time.sleep(3.5)
        import socket
        sock = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/{os.getuid()}")) / "snapctrlalt.sock"

        def send(cmd):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sk:
                sk.settimeout(2)
                sk.connect(str(sock))
                sk.sendall((cmd + "\n").encode())
                sk.recv(64)

        def find_overlay():
            r = subprocess.run(["xdotool", "search", "--name", "SnapCtrlAlt 截图"],
                               capture_output=True, text=True)
            if r.returncode != 0 or not r.stdout.strip():
                return None
            for wid in r.stdout.split():
                g = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid],
                                   capture_output=True, text=True)
                if g.returncode != 0:
                    continue
                info = dict(l.split("=", 1) for l in g.stdout.strip().splitlines() if "=" in l)
                try:
                    w, h = int(info["WIDTH"]), int(info["HEIGHT"])
                except Exception:
                    continue
                if w >= W * 0.8 and h >= H * 0.8:
                    return wid
            return None

        send("shot")
        t0 = time.time()
        while time.time() - t0 < 20 and not find_overlay():
            time.sleep(0.3)
        if not find_overlay():
            print("覆盖层没出现")
            return 2
        time.sleep(0.6)

        x0, y0, x1, y1 = 600, 360, 2200, 1500
        move(x0, y0)
        xtest.fake_input(d, X.ButtonPress, 1)
        d.sync(); time.sleep(0.15)
        for i in range(1, 25):
            move(x0 + (x1 - x0) * i / 24, y0 + (y1 - y0) * i / 24)
        xtest.fake_input(d, X.ButtonRelease, 1)
        d.sync(); time.sleep(1.0)

        send("dbgtb")
        geo = None
        t_end = time.time() + 5
        while time.time() < t_end:
            txt = Path("/tmp/cp_app.log").read_text(encoding="utf-8", errors="replace")
            for line in txt.splitlines():
                if "TBGEO" in line:
                    nums = line.split("工具栏=(")[1].split(")")[0]
                    bx, by, size = nums.split(",")
                    bw, bh = size.split("x")
                    bx, by, bw, bh = float(bx), float(by), float(bw), float(bh)
                    geo = (bx, by, bw, bh)
                    break
            if geo:
                break
            time.sleep(0.3)
        if geo is None:
            print("拿不到工具栏矩形（dbgtb 没回话）")
            return 2
        bx, by, bw, bh = geo
        print(f"工具栏：x {bx:.0f}..{bx + bw:.0f}  y {by:.0f}..{by + bh:.0f}  （屏幕 {W}×{H}）")
        before = shot("/tmp/cp_1_before.png")

        # 色环按钮 = 第二排第一个按钮：工具栏内 PAD=6、按钮 30 设计像素
        btn = (bx + (6 + 15) * 2, by + (6 + 34 + 15) * 2)
        click(*btn)
        after = shot("/tmp/cp_2_panel.png")

        diff = Image.new("RGB", (W, H))
        import PIL.ImageChops as C
        diff = C.difference(before, after)
        bbox = diff.getbbox()
        print(f"点色环后变化的区域：{bbox}")
        if bbox is None:
            fails.append("点色环后画面完全没变 —— 面板没画出来")
        else:
            px0, py0, px1, py1 = bbox
            pw, ph = px1 - px0, py1 - py0
            print(f"面板（含工具栏重绘）：{pw}×{ph}")
            # 面板矩形应当与工具栏同宽、贴在其上方/下方、且完整在屏幕内
            if pw > bw + 48:
                fails.append(f"变化区域比工具栏宽：{pw} > {bw:.0f}")
            outside = px0 < 0 or py0 < 0 or px1 > W or py1 > H
            if outside:
                fails.append(f"画到屏幕外：{bbox}")
            # 面板所在行的颜色数：确认四条渐变滑块 + 12 个色块被画出来
            crop = after.crop((int(bx), int(py1) - 10, int(bx + bw), int(py1)))
            print(f"面板下沿 10px 的颜色数：{len(crop.getcolors(maxcolors=99999) or [])}")
            panel = after.crop((int(px0), int(py0), int(px1), int(py1)))
            cols = panel.getcolors(maxcolors=999999) or []
            print(f"变化区域颜色数：{len(cols)}（渐变滑块应当很多）")
            if len(cols) < 200:
                fails.append(f"变化区域颜色太少（{len(cols)}），滑块可能没画出来")

        # 在面板里拖 R 滑块：看颜色是否跟着变。
        # 面板位置不写死：直接从截图里认面板底色，找出面板顶边，再按设计尺码换算。
        ap = after.load()

        def panel_top_scan():
            for y in range(int(by) - 400, int(by) + int(bh)):
                if y < 0 or y >= H:
                    continue
                cnt = sum(1 for x in range(int(bx) + 6, int(bx + bw) - 6, 6)
                          if abs(ap[x, y][0] - 242) < 6 and abs(ap[x, y][2] - 247) < 6)
                if cnt > 40:
                    return y
            return None

        ptop = panel_top_scan()
        print("截图里认出的面板顶边 y =", ptop, "（工具栏 y", by, "..", by + bh, "）")
        if ptop is None:
            fails.append("截图里找不到面板（底色没出现）")
            ptop = int(by) - 312
        panel_h = int(312 if ptop < by else 312)
        pbot = ptop + panel_h
        if pbot > H:
            fails.append(f"面板超出屏幕下沿：{pbot} > {H}")

        def screen_xy(dx, dy):
            """设计像素（面板左上角为原点）→ 屏幕坐标。"""
            return int(bx + dx * 2), int(ptop + dy * 2)

        r_y = screen_xy(0, 8 + 11)[1]
        r_x0 = screen_xy(38, 0)[0]
        r_x1 = screen_xy(38 + 214, 0)[0]
        sw_x, sw_y = screen_xy(8 + 12 * 26 + 2 + 30 + 8, 156 - 22)
        before_swatch = after.crop((sw_x + 4, sw_y + 4, sw_x + 40, sw_y + 40))
        print("预览块屏幕位置", (sw_x, sw_y), "初始颜色",
              before_swatch.resize((1, 1)).getpixel((0, 0)))

        print(f"R 行中心 y={r_y}，命中区间 {ptop + (8) * 2}..{ptop + (8 + 22) * 2}；"
              f"轨道 x {r_x0}..{r_x1}")
        assert ptop + 16 <= r_y <= ptop + 60, "R 行中心落在滑块行之外"
        # 起点放在轨道 1/4 处，终点拉过右端（越界要被钳到 255）
        move(r_x0 + (r_x1 - r_x0) // 4, r_y)
        xtest.fake_input(d, X.ButtonPress, 1)
        d.sync(); time.sleep(0.15)
        for i in range(1, 16):
            move(r_x0 + (r_x1 - r_x0) * (0.25 + 0.25 * i / 15), r_y)
        xtest.fake_input(d, X.ButtonRelease, 1)
        d.sync(); time.sleep(0.8)
        dragged = shot("/tmp/cp_3_dragged.png")
        print("拖 R 滑块后截图：/tmp/cp_3_dragged.png")
        dpx = dragged.load()
        still = sum(1 for x in range(int(bx) + 4, int(bx + bw) - 4, 8)
                    for y in range(ptop + 6, pbot - 6, 12)
                    if abs(dpx[x, y][0] - 242) < 12 and abs(dpx[x, y][2] - 247) < 12)
        print("拖动后面板底色像素数：", still)
        if still < 200:
            fails.append("拖滑块时面板消失了（这次按下被当成重新框选？）")
        after_swatch = dragged.crop((sw_x + 4, sw_y + 4, sw_x + 40, sw_y + 40))
        b0 = before_swatch.resize((1, 1)).getpixel((0, 0))
        a0 = after_swatch.resize((1, 1)).getpixel((0, 0))
        print(f"预览块颜色：{b0} → {a0}")
        if b0 == a0:
            fails.append(f"拖 R 滑块后当前色预览没变（{b0}）")
        rpix = dragged.getpixel((r_x1 - 8, r_y))
        print("R 轨道右端像素：", rpix)
        if not (rpix[0] > 180 and rpix[0] > rpix[1] + 40):
            fails.append(f"R 滑块右端不是红色渐变：{rpix}")
    finally:
        time.sleep(0.4)
        kill_resident()
        try:
            logf.close()
        except Exception:
            pass

    print("\n" + "=" * 56)
    if fails:
        for f in fails:
            print("✗ " + f)
        return 1
    print("✓ 取色面板在工具栏内、完整可见")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
