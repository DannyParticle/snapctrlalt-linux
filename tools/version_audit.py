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
    import hashlib  # noqa: F401
    src = ver_of_text(SRC.read_text(encoding="utf-8"))
    print(f"源码 __version__            : {src}")

    # deb：既要版本号对，也要**内容**和仓库一致
    # （只比版本号是不够的 —— 1.3.12 的包就出现过"版本对、snap.py 里少一个
    #   --which 参数"的情况，因为打完包之后源码又改了却没重打）
    import hashlib

    def md5(p: Path) -> str:
        return hashlib.md5(p.read_bytes()).hexdigest()

    debs = sorted(ROOT.glob("dist/*.deb"))
    for d in debs:
        v = sh("dpkg-deb", "-f", str(d), "Version").stdout.strip()
        with tempfile.TemporaryDirectory() as td:
            sh("dpkg-deb", "-x", str(d), td)
            pkg = Path(td) / "usr/lib/python3/dist-packages/snapctrlalt"
            init = pkg / "__init__.py"
            inner = ver_of_text(init.read_text(encoding="utf-8")) if init.exists() else None
            # 当前版本这一份要求逐文件与仓库一致；历史版本的包不参与内容比对
            drift = []
            if v == src:
                for f in sorted((ROOT / "src" / "snapctrlalt").glob("*.py")):
                    tgt = pkg / f.name
                    if not tgt.exists() or md5(f) != md5(tgt):
                        drift.append(f.name)
                for f, sub in ((ROOT / "README.md", "usr/share/doc/snapctrlalt/README.md"),
                               (ROOT / "CHANGELOG.md", "usr/share/doc/snapctrlalt/CHANGELOG.md")):
                    tgt = Path(td) / sub
                    if not tgt.exists() or md5(f) != md5(tgt):
                        drift.append(f.name)
        ok = v == inner and not drift
        note = ""
        if v != inner:
            note = f"  包内 __version__={inner} 与包版本不符！"
        elif drift:
            note = f"  包内容与仓库不一致：{', '.join(drift)}（打完包之后源码又改了？重打）"
        print(f"deb  {d.name:38s} 声明 {v:8s} 包内 {inner or '?':8s} "
              f"{'✓' if ok else '✗'}{note}")
        if v != inner:
            bad.append(f"{d.name}: 声明 {v} 但包内是 {inner}")
        elif drift:
            bad.append(f"{d.name}: 内容与仓库不一致（{', '.join(drift)}）")

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

    # 发布清单：同一版本只应有一份产物，sha256 必须对得上
    import json
    rel = ROOT / "dist" / f"RELEASE-{src}.json"
    if rel.exists():
        try:
            data = json.loads(rel.read_text(encoding="utf-8"))
        except ValueError as e:
            bad.append(f"{rel.name} 不是合法 JSON：{e}")
            data = {}
        for key in ("deb", "tarball"):
            item = data.get(key) or {}
            f = ROOT / "dist" / str(item.get("file", ""))
            if not item.get("file"):
                continue
            if not f.exists():
                bad.append(f"清单里的 {item['file']} 不存在")
                print(f"清单 {key:8s} {item.get('file')}  → 文件不见了 ✗")
                continue
            sha = hashlib.sha256(f.read_bytes()).hexdigest()
            ok = sha == item.get("sha256")
            print(f"清单 {key:8s} {item.get('file'):38s} "
                  f"入包代码来自 {str(data.get('packed_code_from'))[:8]} "
                  f"{'✓' if ok else '✗ sha256 不符'}")
            if not ok:
                bad.append(f"{item['file']} 与发布清单不符（重打过？）")
    else:
        print(f"发布清单                     : 缺少 dist/RELEASE-{src}.json")

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
