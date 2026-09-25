#!/usr/bin/env python3
"""端到端 GUI 冒烟测试：真窗口 + 真抓图 + 真热键 + 真剪贴板。

会用 XTEST 模拟鼠标（xdotool）走完「框选 → Enter 复制」的完整链路，
所以运行期间会短暂接管整个桌面（约 10 秒）。

    python3 tests/test_gui_e2e.py            # 完整流程
    python3 tests/test_gui_e2e.py --keep     # 结束后保留进程，方便手动看界面

退出码 0 表示全通过。
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

SOCK = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/{os.getuid()}")) / "snapctrlalt.sock"
WINDOW_TITLE = "SnapCtrlAlt 截图"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def send(cmd: str) -> bool:
    import socket

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(str(SOCK))
            s.sendall((cmd + "\n").encode())
            s.recv(64)
        return True
    except OSError:
        return False


def find_overlay() -> tuple[str, tuple[int, int, int, int]] | None:
    """用 xdotool 找到覆盖层窗口，返回 (window_id, 几何)。"""
    r = sh(["xdotool", "search", "--name", WINDOW_TITLE])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    for wid in r.stdout.split():
        g = sh(["xdotool", "getwindowgeometry", "--shell", wid])
        if g.returncode != 0:
            continue
        info = dict(
            line.split("=", 1) for line in g.stdout.strip().splitlines() if "=" in line
        )
        try:
            geom = (int(info["X"]), int(info["Y"]),
                    int(info["WIDTH"]), int(info["HEIGHT"]))
        except (KeyError, ValueError):
            continue
        if geom[2] > 100 and geom[3] > 100:
            return wid, geom
    return None


def active_window_name() -> str:
    r = sh(["xdotool", "getactivewindow", "getwindowname"])
    return r.stdout.strip()


def screenshot(path: Path) -> bool:
    for cmd in (["gnome-screenshot", "-f", str(path)],
                ["import", "-window", "root", str(path)],
                ["scrot", str(path)]):
        if shutil.which(cmd[0]) and sh(["sh", "-c", f"{cmd[0]} --version"]).returncode in (0, 1):
            r = sh(cmd)
            if r.returncode == 0 and path.is_file():
                return True
    return False


def main() -> int:
    keep = "--keep" in sys.argv
    tmp = Path(tempfile.mkdtemp(prefix="snapctrlalt-e2e-"))
    print(f"端到端 GUI 测试（临时目录 {tmp}）")
    print("提示：测试期间鼠标会被程序控制，请勿操作键鼠\n")

    if not shutil.which("xdotool"):
        print("缺少 xdotool，无法模拟鼠标 —— 跳过")
        return 0

    # 先清掉可能在跑的旧实例
    send("quit")
    time.sleep(0.8)
    if SOCK.exists():
        SOCK.unlink()

    # 用隔离的配置目录，并且关掉「优先外部工具」——测的是内置覆盖层本身
    cfg_home = tmp / "config"
    app_cfg_dir = cfg_home / "snapctrlalt"
    app_cfg_dir.mkdir(parents=True)
    (app_cfg_dir / "config.json").write_text(
        '{"prefer_external": false, "notify": false, "show_tray": true,'
        ' "hotkey": "ctrl+alt+d", "quit_hotkey": "ctrl+alt+shift+q",'
        ' "after_capture": "copy"}\n',
        encoding="utf-8",
    )

    log = (tmp / "app.log").open("w")
    env = dict(os.environ, SNAP_SCALE="", XDG_CONFIG_HOME=str(cfg_home))
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "bin" / "snapctrlalt")],
        stdout=log, stderr=subprocess.STDOUT, env=env, cwd=str(ROOT),
    )
    print(f"已启动 snap.py（pid {proc.pid}），日志 {tmp / 'app.log'}")

    try:
        # 1. 常驻实例 + 热键
        t0 = time.time()
        ready = False
        while time.time() - t0 < 25:
            time.sleep(0.4)
            if proc.poll() is not None:
                break
            txt = (tmp / "app.log").read_text(errors="replace")
            if "全局热键已就绪" in txt or "热键" in txt:
                ready = True
                break
        log_txt = (tmp / "app.log").read_text(errors="replace")
        check("进程常驻未退出", proc.poll() is None,
              "" if proc.poll() is None else f"退出码 {proc.poll()}")
        check("命令通道已建立", SOCK.exists(), str(SOCK))
        check("全局热键已注册", "全局热键已就绪" in log_txt,
              _pick(log_txt, "全局热键"))
        check("托盘已就绪", "托盘已就绪" in log_txt, _pick(log_txt, "托盘"))
        del ready

        # 2. 触发截图（走真实的 IPC → 抓图 → 覆盖层）
        before = active_window_name()
        print(f"  触发前活动窗口：{before!r}")
        check("IPC 触发截图成功", send("shot"))
        found = None
        t0 = time.time()
        while time.time() - t0 < 20:
            time.sleep(0.35)
            found = find_overlay()
            if found:
                break
        check("覆盖层窗口出现在屏幕上", found is not None,
              f"{found[0]} 几何 {found[1]}" if found else "未找到")
        if not found:
            print((tmp / "app.log").read_text(errors="replace")[-3000:])
            return 1

        wid, geom = found
        check("覆盖层占满整屏", geom[2] > 1000 and geom[3] > 700, str(geom))
        # 焦点是异步拿到的（show → 全屏映射 → present/_NET_ACTIVE_WINDOW），
        # 给它最多 6 秒
        focused = ""
        t0 = time.time()
        while time.time() - t0 < 6:
            focused = active_window_name()
            if WINDOW_TITLE in focused:
                break
            time.sleep(0.25)
        check("覆盖层获得键盘焦点", WINDOW_TITLE in focused, focused)

        # 3. 抓一张屏幕照片，确认覆盖层真的被画出来了（不是黑窗）
        shot1 = tmp / "overlay.png"
        got = screenshot(shot1)
        check("能对覆盖层截图取证", got, str(shot1))
        if got:
            from PIL import Image, ImageStat

            im = Image.open(shot1).convert("RGB")
            st = ImageStat.Stat(im)
            brightness = sum(st.mean) / 3
            check("覆盖层画面不是全黑", brightness > 8, f"平均亮度 {brightness:.1f}")
            im.save(tmp / "overlay_evidence.png")

        # 4. 用 XTEST 真拖框：从 (300,250) 拖到 (900,650)
        sh(["xdotool", "mousemove", "300", "250"])
        time.sleep(0.3)
        sh(["xdotool", "mousedown", "1"])
        for i in range(1, 13):
            x = 300 + int(600 * i / 12)
            y = 250 + int(400 * i / 12)
            sh(["xdotool", "mousemove", str(x), str(y)])
            time.sleep(0.04)
        sh(["xdotool", "mouseup", "1"])
        time.sleep(0.6)
        shot2 = tmp / "selected.png"
        if screenshot(shot2):
            from PIL import Image, ImageChops

            a = Image.open(shot1).convert("RGB")
            b = Image.open(shot2).convert("RGB")
            if a.size == b.size:
                diff = ImageChops.difference(a, b).getbbox()
                check("拖框后画面发生变化（选区/工具栏已绘出）", diff is not None,
                      f"变化区域 {diff}")
                b.save(tmp / "selected_evidence.png")
        check("拖框后进程仍存活", proc.poll() is None)

        # 5. Enter 完成 → 复制到剪贴板（走真实的 Gtk 剪贴板）
        sh(["xdotool", "key", "--clearmodifiers", "Return"])
        time.sleep(1.2)
        log_txt = (tmp / "app.log").read_text(errors="replace")
        check("完成动作有记录", "复制到剪贴板" in log_txt or "已保存" in log_txt,
              _pick(log_txt, "已复制") or _pick(log_txt, "已保存"))
        check("覆盖层已关闭", find_overlay() is None)

        # 6. 剪贴板：用一个独立进程读回来（跨进程验证）
        reader = tmp / "read_clip.py"
        reader.write_text(
            "import gi\n"
            "gi.require_version('Gtk','3.0')\n"
            "from gi.repository import Gtk, Gdk\n"
            "Gtk.init_check(['x'])\n"
            "for _ in range(60):\n"
            "    while Gtk.events_pending(): Gtk.main_iteration_do(False)\n"
            "    pb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_image()\n"
            "    if pb is not None:\n"
            "        print(f'{pb.get_width()}x{pb.get_height()}')\n"
            "        break\n"
            "    import time; time.sleep(0.1)\n"
            "else:\n"
            "    print('NONE')\n",
            encoding="utf-8",
        )
        r = sh([sys.executable, str(reader)], timeout=30)
        out = r.stdout.strip().splitlines()
        val = out[-1] if out else "NONE"
        check("独立进程能从剪贴板读回图像", val != "NONE" and "x" in val, val)
        if "x" in val:
            w, h = (int(v) for v in val.split("x"))
            # xdotool 的坐标是 X11 根像素；输出是抓图像素，
            # 两者之间差一个「坐标缩放」（本机为 2），所以期望 1200×800
            scale = _coord_scale()
            exp_w, exp_h = 600 * scale, 400 * scale
            check("剪贴板图像尺寸 = 拖框区域×坐标缩放",
                  abs(w - exp_w) <= 8 and abs(h - exp_h) <= 8,
                  f"{w}×{h}（期望约 {exp_w}×{exp_h}，坐标缩放 x{scale}）")

        # 7. 第二次触发：这次用 Esc 取消，验证不会留下残留窗口
        check("再次触发截图", send("shot"))
        t0 = time.time()
        while time.time() - t0 < 15 and not find_overlay():
            time.sleep(0.3)
        check("第二次覆盖层出现", find_overlay() is not None)
        t0 = time.time()
        while time.time() - t0 < 4 and WINDOW_TITLE not in active_window_name():
            time.sleep(0.25)
        sh(["xdotool", "key", "--clearmodifiers", "Escape"])
        t0 = time.time()
        while time.time() - t0 < 8 and find_overlay() is not None:
            time.sleep(0.3)
        check("Esc 取消后覆盖层关闭", find_overlay() is None)
        log_txt = (tmp / "app.log").read_text(errors="replace")
        check("取消有记录", "已取消截图" in log_txt, _pick(log_txt, "已取消"))

        # 8. 退出
        check("IPC 退出成功", send("quit"))
        t0 = time.time()
        while time.time() - t0 < 10 and proc.poll() is None:
            time.sleep(0.3)
        check("主进程已退出", proc.poll() is not None, f"退出码 {proc.poll()}")
    finally:
        if proc.poll() is None:
            if keep:
                print(f"  --keep：进程 {proc.pid} 仍在运行（结束请 `snapctrlalt` 退出或 kill）")
            else:
                send("quit")
                time.sleep(0.6)
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
        log.close()

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 62)
    print(f"通过 {passed}/{total}   证据保存在 {tmp}")
    for n, ok, _d in RESULTS:
        if not ok:
            print(f"  失败：{n}")
    return 0 if passed == total else 1


def _coord_scale() -> int:
    """抓图像素 / X11 根像素（HiDPI 下为 2）。"""
    from Xlib import display as xd

    from PIL import Image

    d = xd.Display()
    g = d.screen().root.get_geometry()
    root_w, root_h = g.width, g.height
    d.close()
    try:
        from snapctrlalt import capture_linux as capture

        info = capture.virtual_screen_bounds()
        img, _real = capture.grab_full_screen(info)
        ratio = img.width / float(root_w)
        cand = int(round(ratio))
        return cand if cand >= 1 and abs(ratio - cand) < 0.15 else 1
    except Exception:  # noqa: BLE001
        del Image
        return max(1, round(root_h / root_h))


def _pick(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            return line.strip()[:110]
    return ""


if __name__ == "__main__":
    sys.exit(main())
