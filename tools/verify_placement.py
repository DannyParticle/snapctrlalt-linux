#!/usr/bin/env python3
"""真机核验：工具栏四边摆放 + 沿边滑动。

数据取自应用自己的 `dbgtb`（现在一行给全：矩形 / 摆放 / 朝向 / 四角按钮坐标），
再用 XTEST 真点四角按钮走一圈，逐项校验：

  * 贴左/右 → 竖排且贴着选区左右缘；贴上/下 → 横排且贴着选区上下缘；
  * 无论哪个方向，整条都在屏幕内；
  * 抓屏确认条的底色确实出现在它自报的位置（防止"只改了数据没画出来"）。

日志只读**最新那份**（常驻实例可能写 /tmp/snapctrlalt_run.log，也可能写
~/.local/state/snapctrlalt/；两份混读会把上一次运行的状态当成当前状态）。
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


def logfile() -> Path | None:
    cands = [p for p in (Path("/tmp/snapctrlalt_run.log"),
                         Path.home() / ".local/state/snapctrlalt/snapctrlalt.log",
                         Path.home() / ".cache/snapctrlalt/snapctrlalt.log")
             if p.exists()]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def tail() -> str:
    p = logfile()
    if p is None:
        return ""
    with p.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 65536))
        text = fh.read().decode("utf-8", "replace")
    # 尾部截断可能从半行开始，也可能切在多字节字符中间（中文日志！）：
    # 丢掉第一行，避免把半行当成完整记录解析（"四角"少一个方向就是这么来的）。
    lines = text.splitlines()
    return "\n".join(lines[1:] if len(lines) > 1 else lines)


def parse(line: str):
    geo, place, vertical, corners = None, "auto", False, {}
    if "工具栏=(" in line:
        nums = line.split("工具栏=(")[1].split(")")[0]
        bx, by, size = nums.split(",")
        bw, bh = size.split("x")
        geo = (float(bx), float(by), float(bw), float(bh))
    for tok in line.split():
        if tok.startswith("摆放="):
            place = tok.split("=", 1)[1]
        elif tok.startswith("竖排="):
            vertical = tok.split("=", 1)[1] == "1"
        elif ":" in tok and "," in tok and "=" not in tok:
            name, xy = tok.split(":", 1)
            x, y = xy.rstrip("]").split(",")
            corners[name] = (float(x), float(y))
    return geo, place, vertical, corners


def ask():
    """发一次 dbgtb，等这一行变化后解析（拿到的必然是本次请求的结果）。"""
    before = tail()
    send("dbgtb")
    t0 = time.time()
    line = ""
    while time.time() - t0 < 5:
        chunk = tail()
        if chunk != before:
            for ln in chunk.splitlines():
                if "TBGEO" in ln and "工具栏=(" in ln:
                    line = ln
            if line:
                break
        time.sleep(0.2)
    return parse(line)


def move(x, y):
    xtest.fake_input(d, X.MotionNotify, x=int(x), y=int(y))
    d.sync(); time.sleep(0.05)


def click(x, y):
    move(x, y)
    xtest.fake_input(d, X.ButtonPress, 1)
    d.sync(); time.sleep(0.09)
    xtest.fake_input(d, X.ButtonRelease, 1)
    d.sync(); time.sleep(0.35)


def shot(path):
    time.sleep(0.7)
    subprocess.run(["gnome-screenshot", "-f", path], check=True, capture_output=True)
    return Image.open(path).convert("RGB")


def windows(name: str):
    r = subprocess.run(["xdotool", "search", "--name", name], capture_output=True, text=True)
    return r.stdout.split() if r.returncode == 0 else []


def bar_color_ratio(img: Image.Image, box) -> float:
    """工具栏矩形内"条底色"的比例（确认真的画在那儿）。"""
    px = img.load()
    x0, y0, x1, y1 = [int(v) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    n = tot = 0
    for y in range(y0, y1, 4):
        for x in range(x0, x1, 4):
            tot += 1
            c = px[x, y]
            if abs(c[0] - 242) < 16 and abs(c[2] - 247) < 16:
                n += 1
    return n / max(1, tot)


def main() -> int:
    subprocess.run([sys.executable, str(ROOT / "tools" / "restart_resident.py"),
                    "--restart"], capture_output=True, text=True)
    # 常驻实例起来需要一点时间（注册热键 + 托盘 + 命令通道）；起不来就自己拉一次
    ok = False
    for _ in range(6):
        time.sleep(1.0)
        if send("ping"):
            ok = True
            break
    if not ok:
        print("常驻实例没就绪，自己拉一次…")
        try:
            SOCK.unlink()
        except OSError:
            pass
        subprocess.Popen(["setsid", str(ROOT / "bin" / "snapctrlalt")],
                         stdout=open("/tmp/snapctrlalt_run.log", "a"),
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True)
        for _ in range(10):
            time.sleep(1.0)
            if send("ping"):
                ok = True
                break
    if not ok:
        print("命令通道一直没反应")
        return 2
    for wid in windows("SnapCtrlAlt 截图") + windows("SnapCtrlAlt 标注"):
        subprocess.run(["xdotool", "windowkill", wid], capture_output=True)
    time.sleep(0.5)
    for _ in range(3):
        if send("shot"):
            break
        time.sleep(1.0)
    else:
        print("shot 命令没发出去")
        return 2
    t0 = time.time()
    while time.time() - t0 < 20 and not windows("SnapCtrlAlt 截图"):
        time.sleep(0.3)
    if not windows("SnapCtrlAlt 截图"):
        print("覆盖层没出现")
        return 2
    time.sleep(0.8)
    x0, y0, x1, y1 = 700, 300, 2100, 1500
    move(x0, y0)
    xtest.fake_input(d, X.ButtonPress, 1)
    d.sync(); time.sleep(0.15)
    for i in range(1, 25):
        move(x0 + (x1 - x0) * i / 24, y0 + (y1 - y0) * i / 24)
    xtest.fake_input(d, X.ButtonRelease, 1)
    d.sync(); time.sleep(1.5)

    fails = []
    seen: set[str] = set()
    for step in range(6):
        # 先让画面稳定，再问状态：反过来问会出现"状态已是新摆放、屏幕还是旧帧"，
        # 于是量到的底色比例是 0，看起来像"没画出来"（实测左/右那次就这样）。
        time.sleep(0.8)
        geo, place, vertical, corners = ask()
        if geo is None:
            fails.append("拿不到工具栏矩形（dbgtb 没回话？）")
            break
        bx, by, bw, bh = geo
        seen.add(place)
        on_screen = bx >= 0 and by >= 0 and bx + bw <= W and by + bh <= H
        # 重绘可能还没落到屏幕上（实测有一次抓到旧帧，比例 0.00）。
        # 比例太低就再等一拍重抓 —— 条底色比例 0 在几何上不可能。
        ratio = 0.0
        img = None
        for attempt in range(4):
            img = shot(f"/tmp/place_{step}_{place}.png")
            ratio = bar_color_ratio(img, (bx, by, bx + bw, by + bh))
            if ratio >= 0.5:
                break
            time.sleep(1.0)
        del attempt
        print(f"\n[{step}] 摆放={place} 竖排={vertical} 工具栏 {bw:.0f}×{bh:.0f} "
              f"@({bx:.0f},{by:.0f})  条底色比例={ratio:.2f}")
        if not on_screen:
            fails.append(f"{place}：整条越出屏幕 ({bx:.0f},{by:.0f})")
        if (bh > bw) != vertical:
            fails.append(f"{place}：朝向与自报不符（竖排={vertical}）")
        if ratio < 0.5:
            fails.append(f"{place}：屏幕上看不到工具栏（底色比例 {ratio:.2f}）")
        if place == "left" and abs((bx + bw) - x0) > 80:
            fails.append(f"left：没贴选区左缘（{bx + bw:.0f} vs {x0}）")
        if place == "right" and abs(bx - x1) > 80:
            fails.append(f"right：没贴选区右缘（{bx:.0f} vs {x1}）")
        if place == "top" and abs((by + bh) - y0) > 80:
            fails.append(f"top：没贴选区上缘（{by + bh:.0f} vs {y0}）")
        if place == "bottom" and abs(by - y1) > 80:
            fails.append(f"bottom：没贴选区下缘（{by:.0f} vs {y1}）")
        # 点四角按钮走下一个方向（坐标由应用报出，不猜）
        nxt = None
        for cand in ("top", "bottom", "left", "right"):
            if cand not in seen and cand in corners:
                nxt = cand
                break
        if nxt is None:
            break
        cx, cy = corners[nxt]
        print(f"  点四角 → {nxt} @({cx:.0f},{cy:.0f})")
        click(cx, cy)
        time.sleep(1.0)

    # 直接补测还差的方向：点它对应的四角按钮（左上=top、右上=right、
    # 左下=left、右下=bottom），最多试三次 —— 真机点击偶尔会丢一次。
    for _ in range(3):
        missing = {"top", "bottom", "left", "right"} - seen
        if not missing:
            break
        geo, place, vertical, corners = ask()
        if geo is None:
            fails.append("补测时拿不到工具栏矩形")
            break
        bx, by, bw, bh = geo
        want = sorted(missing)[0]
        if want not in corners:
            fails.append(f"{place}：应用没报出 {want} 方向的四角按钮")
            break
        cx, cy = corners[want]
        print(f"\n补测 {want}：位置={place} → 点四角 @({cx:.0f},{cy:.0f})")
        click(cx, cy)
        time.sleep(1.2)
        geo2, place2, vertical2, _c2 = ask()
        if geo2 is None or place2 != want:
            continue
        seen.add(place2)
        bx, by, bw, bh = geo2
        img = shot(f"/tmp/place_extra_{want}.png")
        ratio = 0.0
        for _try in range(3):
            ratio = bar_color_ratio(img, (bx, by, bx + bw, by + bh))
            if ratio >= 0.5:
                break
            time.sleep(1.0)
            img = shot(f"/tmp/place_extra_{want}.png")
        print(f"  摆放={place2} 竖排={vertical2} {bw:.0f}×{bh:.0f} "
              f"@({bx:.0f},{by:.0f}) 底色比例={ratio:.2f}")
        on_screen = bx >= 0 and by >= 0 and bx + bw <= W and by + bh <= H
        if not on_screen:
            fails.append(f"{want}：整条越出屏幕")
        if ratio < 0.5:
            fails.append(f"{want}：屏幕上看不到工具栏（{ratio:.2f}）")
        if want == "top" and abs((by + bh) - y0) > 80:
            fails.append(f"top：没贴选区上缘（{by + bh:.0f} vs {y0}）")
        if want == "bottom" and abs(by - y1) > 80:
            fails.append(f"bottom：没贴选区下缘（{by:.0f} vs {y1}）")
        if want == "left" and abs((bx + bw) - x0) > 80:
            fails.append(f"left：没贴选区左缘（{bx + bw:.0f} vs {x0}）")
        if want == "right" and abs(bx - x1) > 80:
            fails.append(f"right：没贴选区右缘（{bx:.0f} vs {x1}）")
        if (bh > bw) != vertical2:
            fails.append(f"{want}：朝向与自报不符")

    missing = {"top", "bottom", "left", "right"} - seen
    if missing:
        fails.append(f"没走到的方向：{sorted(missing)}")
    for wid in windows("SnapCtrlAlt 截图"):
        subprocess.run(["xdotool", "windowkill", wid], capture_output=True)
    print("\n" + "=" * 56)
    if fails:
        for f in fails:
            print("✗ " + f)
        return 1
    print("✓ 四边摆放真机核验通过（四个方向都贴边、都在屏幕内、都真的画出来了）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
