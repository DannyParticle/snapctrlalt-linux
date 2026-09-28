#!/usr/bin/env python3
"""检查/重启常驻实例，确保它跑的是**磁盘上现在这份代码**。

为什么需要它：Python 进程启动时就把源码读进内存了。改完代码、装完新 deb 之后，
**已经在跑的常驻实例仍然是旧代码** —— 按热键截图看到的是旧界面（本项目的
「取色栏还是老的浮动面板」就是这么来的）。测试脚本每次都新起进程，所以永远
发现不了这个问题；诊断必须针对"正在跑的那个进程"。

    python3 tools/restart_resident.py            只检查，不重启
    python3 tools/restart_resident.py --restart  需要时重启
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 会被记进"进程是否陈旧"判断的文件（真正决定运行时行为的那些）
WATCH = [
    ROOT / "src" / "snapctrlalt" / "overlay.py",
    ROOT / "src" / "snapctrlalt" / "toolbar.py",
    ROOT / "src" / "snapctrlalt" / "snap.py",
    ROOT / "src" / "snapctrlalt" / "settings.py",
]
INSTALLED = Path("/usr/lib/python3/dist-packages/snapctrlalt/overlay.py")


def _boot_time() -> float:
    """开机时刻（/proc/stat 的 btime）。"""
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    return 0.0


def _proc_info(pid: int) -> tuple[str, float] | None:
    """(cmdline, 启动时刻)。启动时刻用 /proc/<pid>/stat 的 starttime 算，
    比解析 ``ps -o lstart`` 稳（后者按空格切列，日期里带空格就会错位）。"""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", "replace").strip()
        stat = Path(f"/proc/{pid}/stat").read_text()
        # 第 22 个字段是 starttime（进程启动后的时钟滴答数）；comm 可能含空格/括号，
        # 所以从最后一个 ')' 之后开始数。
        rest = stat[stat.rindex(")") + 2:].split()
        ticks = float(rest[19])                     # 22 - 3
        hz = os.sysconf("SC_CLK_TCK") or 100
        return cmd, _boot_time() + ticks / hz
    except (OSError, ValueError, IndexError):
        return None


def find_instances() -> list[tuple[int, str, float]]:
    """返回 [(pid, cmdline, 启动时刻), ...]，只认真正的入口进程。"""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        info = _proc_info(pid)
        if not info:
            continue
        cmd, ts = info
        if "captain" in cmd or "restart_resident" in cmd or "grep" in cmd:
            continue
        if "snapctrlalt" not in cmd:
            continue
        if not (cmd.endswith("/bin/snapctrlalt") or cmd.endswith("/snapctrlalt")
                or "snapctrlalt/snap.py" in cmd):
            continue
        found.append((pid, cmd, ts))
    return sorted(found)


def newest_change() -> tuple[float, str]:
    """最晚被改动的受监视文件（时间戳, 路径）。"""
    newest, which = 0.0, ""
    for f in WATCH + [INSTALLED]:
        if not f.is_file():
            continue
        m = f.stat().st_mtime
        if m > newest:
            newest, which = m, str(f)
    return newest, which


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--restart", action="store_true", help="陈旧就重启")
    args = ap.parse_args()

    inst = find_instances()
    newest, which = newest_change()
    print(f"磁盘上最新的代码：{time.strftime('%H:%M:%S', time.localtime(newest))}  {which}")
    if not inst:
        print("没有在跑的常驻实例。")
        if args.restart:
            launcher = _launcher()
            print(f"启动：{launcher}")
            subprocess.Popen(["setsid", launcher],
                             stdout=open("/tmp/snapctrlalt_run.log", "a"),
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
            time.sleep(3)
            print("已启动。")
        return 0

    # 已安装包 vs 源码：deb 里的文件 mtime 是固定的（可复现构建），
    # 所以只要源码里有文件比它新，就说明安装版落后 —— 那台机器上"重启常驻"
    # 也修不好，必须重新装 deb。
    # 已安装包 vs 源码：deb 是**可复现构建**，里面的 mtime 固定在 SOURCE_DATE_EPOCH，
    # 拿时间戳比新旧只会误报；比版本号才靠谱。
    def _ver(path: Path) -> str:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("__version__"):
                    return line.split('"')[1]
        except OSError:
            pass
        return ""

    src_ver = _ver(ROOT / "src" / "snapctrlalt" / "__init__.py")
    inst_ver = _ver(Path("/usr/lib/python3/dist-packages/snapctrlalt/__init__.py"))
    print(f"版本：源码 {src_ver or '?'} / 已安装 {inst_ver or '（未安装）'}")
    if inst_ver and inst_ver != src_ver:
        print(f"⚠ 已安装的是 {inst_ver}，源码已是 {src_ver} —— "
              "这台机器上重启常驻也修不好，要重新装：")
        print("  ./packaging/build-deb.sh && sudo apt install "
              "dist/snapctrlalt_%s_all.deb" % src_ver)

    stale = []
    for pid, cmd, ts in inst:
        age = ("早于" if ts < newest else "晚于") + "当前代码"
        flag = "陈旧 ✗" if ts < newest else "最新 ✓"
        print(f"PID {pid}  启动 {time.strftime('%H:%M:%S', time.localtime(ts))}  "
              f"{flag}（{age}）  {cmd}")
        if ts < newest:
            stale.append(pid)

    want = _launcher()
    cur_exe = inst[0][1].split()[-1] if inst else ""
    wrong_entry = bool(cur_exe) and Path(cur_exe).name == "snapctrlalt" and \
        str(Path(cur_exe).resolve()) != str(Path(want).resolve())
    if not stale:
        if not wrong_entry:
            print("常驻实例就是当前代码，无需重启。")
            return 0
        print(f"代码是当前这份，但不是从 {want} 起的 —— 换成它，以后就跟仓库同步。")
        if not args.restart:
            return 0
        stale = [pid for pid, _c, _t in inst]
    if not args.restart:
        print(f"\n有 {len(stale)} 个陈旧实例 —— 它们仍在使用旧代码。"
              f"加 --restart 重启。")
        return 1

    for pid in stale:
        try:
            os.kill(pid, 15)          # SIGTERM：让它自己收尾（托盘/热键）
        except OSError as e:
            print(f"  结束 PID {pid} 失败：{e}")
    for _ in range(20):
        time.sleep(0.25)
        if not any(p in [i[0] for i in find_instances()] for p in stale):
            break
    launcher = _launcher()
    print(f"重新启动：{launcher}")
    subprocess.Popen(["setsid", launcher],
                     stdout=open("/tmp/snapctrlalt_run.log", "a"),
                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     start_new_session=True)
    time.sleep(3)
    for pid, _cmd, ts in find_instances():
        ok = "✓" if ts >= newest else "✗ 仍然陈旧"
        print(f"现在：PID {pid} 启动 {time.strftime('%H:%M:%S', time.localtime(ts))} {ok}")
    return 0


def _launcher() -> str:
    """挑一个能 import 到**本仓库源码**的入口。

    顺序很重要：``~/.local/bin/snapctrlalt`` 与仓库的 ``bin/snapctrlalt`` 都会
    沿着自身路径找到 ``../src``，所以它们跑的是刚改过的代码；而
    ``/usr/bin/snapctrlalt`` 只能跑 deb 安装的那份 —— 开发时用它重启，等于
    把「陈旧实例」换成了「另一份旧代码」。安装版存在只作为最后兜底。
    """
    for cand in (ROOT / "bin" / "snapctrlalt",
                 Path.home() / ".local/bin/snapctrlalt",
                 Path("/usr/bin/snapctrlalt")):
        if Path(cand).exists():
            return str(cand)
    return str(ROOT / "bin" / "snapctrlalt")


if __name__ == "__main__":
    raise SystemExit(main())
