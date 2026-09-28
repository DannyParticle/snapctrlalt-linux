#!/usr/bin/env python3
"""把 dist/ 里的产物作为 GitHub Release 上传（不需要 gh，只用标准库 + REST API）。

用法：
    export GITHUB_TOKEN=ghp_xxx          # 需要 repo 权限（经典 token 勾 repo，或细粒度 token 勾 Contents: Read and write）
    python3 tools/make_release.py                       # 用源码里的版本号
    python3 tools/make_release.py --version 1.5.2 --draft
    python3 tools/make_release.py --notes-file notes.md

它会做四件事：
  1. 从 src/snapctrlalt/__init__.py 读版本号，从 CHANGELOG.md 抽出该版本的说明；
  2. 校验 dist/ 里的 deb / tar.gz / bundle 都在，并算出 sha256（附在说明末尾）；
  3. 调 REST API 建 release（tag 已存在就复用，不会重复建）；
  4. 逐个上传资产（已存在的同名资产会先删掉再传，方便重跑）。

为什么不用 gh：本机没装（apt 有包但要 sudo），而这个脚本零依赖、可重复执行。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
REPO_SLUG = os.environ.get("SNAP_REPO", "DannyParticle/snapctrlalt-linux")
API = "https://api.github.com"


def die(msg: str, code: int = 1) -> None:
    print(f"错误：{msg}", file=sys.stderr)
    raise SystemExit(code)


def version_of_source() -> str:
    text = (ROOT / "src" / "snapctrlalt" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__ = "([^"]+)"', text)
    if not m:
        die("读不到源码里的 __version__")
    return m.group(1)


def notes_from_changelog(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(version)} — .*?(?=^## |\Z)", text, re.M | re.S)
    if not m:
        die(f"CHANGELOG.md 里没有 {version} 小节")
    return m.group(0).strip()


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def api(method: str, path: str, token: str, data: dict | None = None,
        ctype: str = "application/json"):
    req = urllib.request.Request(f"{API}{path}", method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "snapctrlalt-release-script")
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, body, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        die(f"{method} {path} → HTTP {e.code}：{detail}")


def upload_asset(upload_url: str, path: Path, token: str) -> None:
    """上传一个资产（upload_url 形如 https://uploads.github.com/...{?name,label}）。"""
    url = upload_url.split("{")[0] + f"?name={urllib.parse.quote(path.name)}"
    req = urllib.request.Request(url, method="POST", data=path.read_bytes())
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/octet-stream")
    req.add_header("User-Agent", "snapctrlalt-release-script")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        die(f"上传 {path.name} 失败：HTTP {e.code} {detail}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default="", help="默认取源码里的版本号")
    ap.add_argument("--repo", default=REPO_SLUG, help=f"默认 {REPO_SLUG}")
    ap.add_argument("--notes-file", default="", help="自定义 release 说明（默认取 CHANGELOG）")
    ap.add_argument("--draft", action="store_true", help="建成草稿，不立刻公开")
    ap.add_argument("--prerelease", action="store_true", help="标记为预发布")
    args = ap.parse_args()

    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not token:
        die("没有 GITHUB_TOKEN / GH_TOKEN。请先 export GITHUB_TOKEN=<你的 token>")

    version = args.version or version_of_source()
    tag = f"v{version}"
    notes = (Path(args.notes_file).read_text(encoding="utf-8") if args.notes_file
             else notes_from_changelog(version))

    assets = [
        DIST / f"snapctrlalt_{version}_all.deb",
        DIST / f"snapctrlalt-linux-{version}.tar.gz",
        DIST / "snapctrlalt-linux.bundle",
    ]
    missing = [p.name for p in assets if not p.is_file() or p.stat().st_size == 0]
    if missing:
        die(f"dist/ 里缺少产物：{missing}（先跑 ./packaging/build-deb.sh）")

    sums = "\n".join(f"{sha256(p)}  {p.name}" for p in assets)
    body = f"{notes}\n\n---\n\n### 附件 sha256\n\n```\n{sums}\n```\n"

    print(f"仓库     : {args.repo}")
    print(f"版本/tag : {version} / {tag}")
    for p in assets:
        print(f"资产     : {p.name}  {p.stat().st_size / 1024:.0f} KiB")

    # 已有同名 release 就复用（重跑友好）
    rel = None
    try:
        rel = api("GET", f"/repos/{args.repo}/releases/tags/{tag}", token)
        print("已存在该 tag 的 release，复用它")
    except SystemExit:
        rel = None
    if rel is None:
        rel = api("POST", f"/repos/{args.repo}/releases", token, {
            "tag_name": tag,
            "name": f"SnapCtrlAlt Linux {version}",
            "body": body,
            "draft": bool(args.draft),
            "prerelease": bool(args.prerelease),
        })
        print("已创建 release")
    else:
        rel = api("PATCH", f"/repos/{args.repo}/releases/{rel['id']}", token,
                  {"body": body, "draft": bool(args.draft)})

    existing = {a["name"]: a["id"] for a in rel.get("assets", [])}
    for p in assets:
        if p.name in existing:                       # 重跑时先删旧的同名资产
            api("DELETE", f"/repos/{args.repo}/releases/assets/{existing[p.name]}", token)
        upload_asset(rel["upload_url"], p, token)
        print(f"已上传 {p.name}")

    print(f"\n完成：{rel.get('html_url')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
