#!/usr/bin/env python3
"""版本一致性审计：源码 / deb / tar.gz / CHANGELOG / 已安装 / git tag 全对上才算过。"""
from __future__ import annotations
import re, subprocess, sys, tarfile, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src/snapctrlalt/__init__.py"

def ver_of_text(t):
    m = re.search(r'__version__ = "([^"]+)"', t)
    return m.group(1) if m else None

def sh(*a):
    return subprocess.run(a, capture_output=True, text=True)

def main() -> int:
    bad = []
    src = ver_of_text(SRC.read_text(encoding="utf-8"))
    print(f"源码 __version__            : {src}")

    # deb
    debs = sorted(ROOT.glob("dist/*.deb"))
    for d in debs:
        v = sh("dpkg-deb", "-f", str(d), "Version").stdout.strip()
        with tempfile.TemporaryDirectory() as td:
            sh("dpkg-deb", "-x", str(d), td)
            init = Path(td) / "usr/lib/python3/dist-packages/snapctrlalt/__init__.py"
            inner = ver_of_text(init.read_text(encoding="utf-8")) if init.exists() else None
        tag = "✓" if (v == inner == (src if d.name.endswith(f"_{src}_all.deb") else v)) else "✗"
        note = "" if v == inner else f"  包内 __version__={inner} 与包版本不符！"
        print(f"deb  {d.name:38s} 声明 {v:8s} 包内 {inner or '?':8s} {tag}{note}")
        if v != inner:
            bad.append(f"{d.name}: 声明 {v} 但包内是 {inner}")

    # tar.gz：文件名里的版本必须等于快照里的 __version__
    for t in sorted(ROOT.glob("dist/*.tar.gz")):
        m = re.search(r"-(\d+\.\d+\.\d+)\.tar\.gz$", t.name)
        want = m.group(1) if m else "?"
        got = None
        try:
            with tarfile.open(t) as tf:
                for name in tf.getnames():
                    if name.endswith("src/snapctrlalt/__init__.py"):
                        got = ver_of_text(tf.extractfile(name).read().decode("utf-8"))
                        break
        except Exception as e:
            got = f"读取失败 {e}"
        ok = "✓" if got == want else "✗"
        print(f"tar  {t.name:38s} 文件名 {want:8s} 快照 {str(got):8s} {ok}")
        if got != want:
            bad.append(f"{t.name}: 文件名 {want} 但快照是 {got}")

    # 已安装
    st = sh("dpkg-query", "-W", "-f=${Version}", "snapctrlalt")
    inst = st.stdout.strip() if st.returncode == 0 else "(未安装)"
    print(f"已安装 deb                  : {inst}")

    # CHANGELOG 是否有对应小节
    cl = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    have = bool(re.search(rf"^## {re.escape(src)}\b", cl, re.M))
    print(f"CHANGELOG 有 {src} 小节     : {'✓' if have else '✗'}")
    if not have:
        bad.append(f"CHANGELOG 缺少 {src} 小节")

    # git tag 覆盖情况
    tags = sh("git", "tag", "--list").stdout.split()
    version_tags = [t for t in tags if re.match(r"v\d+\.\d+\.\d+", t)]
    # 按版本号排，不要用字典序（字典序下 v1.3.9 会排在 v1.3.12 后面）
    tagged = sorted(version_tags, key=lambda t: [int(x) for x in
                    re.match(r"v(\d+)\.(\d+)\.(\d+)", t).groups()])
    print(f"git tag（版本类）           : {len(tagged)} 个，最新 {tagged[-1] if tagged else '—'}")
    # 提交信息里声明的版本 vs tag
    log = sh("git", "log", "--format=%H %s").stdout.splitlines()
    declared = {}
    for line in log:
        h, _, s = line.partition(" ")
        m = re.search(r"\((\d+\.\d+\.\d+)\)", s)
        if m:
            declared.setdefault(m.group(1), h)
    missing = [v for v in declared if not any(t.startswith(f"v{v}") for t in tags)]
    if missing:
        print(f"提交信息声明了版本但没 tag   : {', '.join(sorted(missing))}")
        bad.append(f"未打 tag 的版本：{', '.join(sorted(missing))}")

    print()
    if bad:
        print("✗ 不一致：")
        for b in bad:
            print("  - " + b)
        return 1
    print("✓ 版本号在源码 / deb / tar.gz / CHANGELOG / tag 之间一致")
    return 0

if __name__ == "__main__":
    sys.exit(main())
