#!/usr/bin/env python3
"""真机验收「正在跑的那个实例」：取色面板 + 临时切换方案按钮。

为什么单独写这个：`tests/*.py` 和 `tools/verify_color_panel.py` 都会自己新起进程，
所以永远跑的是新代码。而用户按热键用的是**常驻实例**——改完代码不重启它，看到的
还是旧界面。这个脚本先核实常驻实例不陈旧，再去点它的界面。

    python3 tools/verify_live_ui.py
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOCK = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/{os.getuid()}")) / "snapctrlalt.sock"
os.environ.setdefault("DISPLAY", ":0")

from PIL import Image  # noqa: E402
from Xlib import X, display  # noqa: E402
from Xlib.ext import xtest  # noqa: E402

d = display.Display()
W, H = d.screen().width_in_pixels, d.screen().height_in_pixels
fails: list[str] = []


def send(cmd: str) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(str(SOCK))
            s.sendall((cmd + "\n").encode())
            s.recv(64)
        return True
    except OSError:
        return False


def move(x, y):
    xtest.fake_input(d, X.MotionNotify, x=int(x), y=int(y))
    d.sync()
    time.sleep(0.05)


def click(x, y):
    move(x, y)
    xtest.fake_input(d, X.ButtonPress, 1)
    d.sync(); time.sleep(0.08)
    xtest.fake_input(d, X.ButtonRelease, 1)
    d.sync(); time.sleep(0.2)


def shot(path):
    time.sleep(0.7)
    subprocess.run(["gnome-screenshot", "-f", path], check=True, capture_output=True)
    return Image.open(path).convert("RGB")


def windows(name: str) -> list[str]:
    r = subprocess.run(["xdotool", "search", "--name", name], capture_output=True, text=True)
    return r.stdout.split() if r.returncode == 0 else []


def main() -> int:
    # ---- 0. 常驻实例必须跑的是当前代码 ----
    chk = subprocess.run([sys.executable, str(ROOT / "tools" / "restart_resident.py")],
                         capture_output=True, text=True)
    print(chk.stdout.strip())
    if chk.returncode != 0:
        fails.append("常驻实例比磁盘代码旧 —— 用户按热键看到的会是旧界面")
        print("先跑：python3 tools/restart_resident.py --restart")
        return 2

    def applog() -> str:
        """读常驻实例的日志。实例可能是从 /usr/bin/snapctrlalt 起的（日志在
        ~/.local/state/snapctrlalt/），也可能是本工具重启的（/tmp/snapctrlalt_run.log），
        两处都看。"""
        out = []
        for p in (Path("/tmp/snapctrlalt_run.log"),
                  Path.home() / ".local/state/snapctrlalt/snapctrlalt.log",
                  Path.home() / ".cache/snapctrlalt/snapctrlalt.log"):
            if p.exists():
                out.append(p.read_text(errors="replace")[-8000:])
        return "\n".join(out)

    # ---- 1. 用常驻实例截图并框选 ----
    if not send("shot"):
        fails.append("命令通道没反应（常驻实例没在跑？）")
        print("\n".join(fails))
        return 2
    t0 = time.time()
    while time.time() - t0 < 20 and not windows("SnapCtrlAlt 截图"):
        time.sleep(0.3)
    if not windows("SnapCtrlAlt 截图"):
        fails.append("覆盖层没出现")
        print("\n".join(fails))
        return 2
    time.sleep(0.6)
    x0, y0, x1, y1 = 600, 360, 2200, 1500
    move(x0, y0)
    xtest.fake_input(d, X.ButtonPress, 1)
    d.sync(); time.sleep(0.15)
    for i in range(1, 25):
        move(x0 + (x1 - x0) * i / 24, y0 + (y1 - y0) * i / 24)
    xtest.fake_input(d, X.ButtonRelease, 1)
    d.sync(); time.sleep(1.2)

    # ---- 2. 色环 → 取色面板 ----
    send("dbgtb")
    geo = None
    t0 = time.time()
    while time.time() - t0 < 5 and geo is None:
        txt = applog()
        for line in txt.splitlines():
            if "TBGEO" in line and "取色面板" not in line:
                nums = line.split("工具栏=(")[1].split(")")[0]
                bx, by, size = nums.split(",")
                bw, bh = size.split("x")
                geo = (float(bx), float(by), float(bw), float(bh))
                break
        time.sleep(0.3)
    if geo is None:
        fails.append("dbgtb 没回话（常驻实例是不是旧代码？）")
        print("\n".join(fails))
        return 2
    bx, by, bw, bh = geo
    print(f"工具栏：x {bx:.0f}..{bx + bw:.0f}  y {by:.0f}..{by + bh:.0f}")
    click(bx + (6 + 15) * 2, by + (6 + 34 + 15) * 2)      # 第二排第一个 = 色环
    img = shot("/tmp/live_1_panel.png")
    if "拖 R/G/B 或灰度滑块" not in applog():
        fails.append("点色环后没有出现取色面板的日志 —— 常驻实例可能还是旧的浮动面板")
    # 面板底色出现在工具栏的哪一侧？（贴底边时会翻到上方）
    px = img.load()
    step = 4
    probe = range(int(bx), int(bx + bw), step)

    def light_ratio(y):
        if y < 0 or y >= img.height:
            return 0.0
        n = sum(1 for x in probe
                if abs(px[x, y][0] - 242) < 10 and abs(px[x, y][2] - 247) < 10)
        return n / max(1, len(list(probe)))

    below, above = light_ratio(int(by + bh + 80)), light_ratio(int(by - 80))
    print(f"面板底色比例：工具栏下方 {below:.2f}，上方 {above:.2f}")
    panel_top = int(by + bh) if below >= above else int(by - 312)
    if max(below, above) < 0.8:
        fails.append(f"取色面板没贴着工具栏（下方 {below:.2f} / 上方 {above:.2f}）")

    # ---- 3. 工具栏上的「临时切换方案」按钮 ----
    # 不手算坐标：直接用应用自己的布局代码（ShotOverlay._layout_toolbar）算出
    # 按钮矩形。项目的坐标约定是"ui_scale 只在布局里乘一次、矩形即最终画布
    # 矩形 (= X11 根像素)"，所以把它加上工具栏左上角就是屏幕坐标。
    sys.path.insert(0, str(ROOT / "src"))
    import snapctrlalt.overlay as ov          # noqa: PLC0415
    from snapctrlalt.geometry import Geometry  # noqa: PLC0415

    proxy = ov.ShotOverlay.__new__(ov.ShotOverlay)
    proxy.ui_scale = 2.0
    proxy.geo = Geometry(img_w=W, img_h=H, root_w=W, root_h=H, win_w=W, win_h=H)
    proxy.sel = (0.0, 0.0, 100.0, 100.0)
    proxy.mode = "select"
    proxy.cr_w, proxy.cr_h = W, H
    proxy._tb_pos = (0.0, 0.0, 0.0, 0.0)
    proxy._tb_window = None
    proxy._tb_rows = []
    proxy._buttons = []
    proxy._tb_surf = None
    proxy._tb_key = None
    proxy._palette_open = False
    proxy._layout_toolbar()
    btn = [b for b in proxy._buttons if b.action == "editor"]
    if not btn:
        fails.append("工具栏上没有「临时切换」按钮（运行的是旧代码？）")
        cands = []
    else:
        b = btn[0]
        sw_x = bx - proxy._tb_pos[0] + b.x + b.w / 2.0
        sw_y = by - proxy._tb_pos[1] + b.y + b.h / 2.0
        cands = [(sw_x, sw_y)]
        print(f"「临时切换」按钮屏幕坐标 ({sw_x:.0f},{sw_y:.0f})  tip={b.tip}")

    n_after, switched = 0, False
    n_before = len(windows("SnapCtrlAlt 标注"))
    prev = applog()
    for (cx, cy) in cands:
        print(f"点按钮 ({cx:.0f},{cy:.0f}) …")
        click(cx, cy)
        time.sleep(2.5)
        n_after = len(windows("SnapCtrlAlt 标注"))
        now = applog()
        if "临时切换工具栏形态 → editor" in now and \
                "临时切换工具栏形态 → editor" not in prev:
            switched = True
            break
        # 点错了（例如点到撤销/重做）：清掉编辑器窗口，重新框选后继续
        for wid in windows("SnapCtrlAlt 标注"):
            subprocess.run(["xdotool", "windowkill", wid], capture_output=True)
        if not windows("SnapCtrlAlt 截图"):
            send("shot")
            t0 = time.time()
            while time.time() - t0 < 15 and not windows("SnapCtrlAlt 截图"):
                time.sleep(0.3)
            time.sleep(0.8)
            move(x0, y0)
            xtest.fake_input(d, X.ButtonPress, 1)
            d.sync(); time.sleep(0.15)
            for i in range(1, 13):
                move(x0 + (x1 - x0) * i / 12, y0 + (y1 - y0) * i / 12)
            xtest.fake_input(d, X.ButtonRelease, 1)
            d.sync(); time.sleep(1.0)
    print(f"编辑器窗口：{n_before} → {n_after}；日志切换：{switched}")
    if not switched and n_after == n_before:
        fails.append("点「临时切换」按钮没有切到方案③")
    else:
        cfg = (Path.home() / ".config" / "snapctrlalt" / "config.json").read_text(
            errors="replace")
        if '"toolbar_mode"' in cfg:
            fails.append("默认设置被改动了（临时切换不应该写配置）")
        else:
            print("默认设置未被写入 ✓")

    # ---- 收尾：撤掉这次截图 ----
    for wid in windows("SnapCtrlAlt 标注"):
        subprocess.run(["xdotool", "windowkill", wid], capture_output=True)
    for wid in windows("SnapCtrlAlt 截图"):
        subprocess.run(["xdotool", "windowkill", wid], capture_output=True)
    send("quit")
    time.sleep(1)
    subprocess.Popen(["setsid", "/usr/bin/snapctrlalt"],
                     stdout=open("/tmp/snapctrlalt_run.log", "a"),
                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     start_new_session=True)

    print("\n" + "=" * 56)
    if fails:
        for f in fails:
            print("✗ " + f)
        return 1
    print("✓ 常驻实例跑的是当前代码：取色面板在工具栏内、临时切换按钮生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
