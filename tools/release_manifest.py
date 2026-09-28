#!/usr/bin/env python3
"""生成发布清单：把"这批产物对应哪份代码"钉死。

    python3 tools/release_manifest.py <version> <deb路径> [tar路径]

两个字段各有分工：
  * ``code_fingerprint``：入包文件的**内容指纹**。仓库里任何一个入包文件变了，
    指纹就变 —— 审计拿它一比对就知道"包是不是打完包之后又改过代码"。
  * ``packed_code_from``：改动过入包文件的**最老那个提交**，说明这份包的代码
    基线在历史里的位置（只动工具/文档的提交不会影响它）。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKED = (sorted((ROOT / "src" / "snapctrlalt").glob("*.py"))
          + [ROOT / "share" / "icons" / "hicolor" / "128x128" / "apps" / "snapctrlalt.png",
             ROOT / "share" / "applications" / "snapctrlalt.desktop",
             ROOT / "README.md", ROOT / "CHANGELOG.md"])


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def fingerprint() -> tuple[str, list[tuple[str, str]]]:
    pairs = []
    for p in PACKED:
        if p.is_file():
            pairs.append((str(p.relative_to(ROOT)), sha256_file(p)))
    h = hashlib.sha256()
    for rel, digest in pairs:
        h.update(f"{rel}\0{digest}\n".encode())
    return h.hexdigest(), pairs


def last_commit(rel: str) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%H", "--", rel],
                       capture_output=True, text=True)
    return r.stdout.strip()


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    version, deb = sys.argv[1], Path(sys.argv[2])
    tarball = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    fp, pairs = fingerprint()
    commits = {rel: last_commit(rel) for rel, _ in pairs}
    commits = {k: v for k, v in commits.items() if v}
    if commits:
        def depth(c: str) -> int:
            r = subprocess.run(["git", "-C", str(ROOT), "rev-list", "--count", c],
                               capture_output=True, text=True)
            return int(r.stdout.strip() or 0)
        base = min(commits.values(), key=depth)
    else:
        base = ""
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    out = {
        "version": version,
        "git_head": head,
        "packed_code_from": base,
        "code_fingerprint": fp,
        "files": len(pairs),
        "deb": {"file": deb.name, "sha256": sha256_file(deb) if deb.is_file() else ""},
    }
    if tarball is not None and tarball.is_file():
        out["tarball"] = {"file": tarball.name, "sha256": sha256_file(tarball)}
    target = ROOT / "dist" / f"RELEASE-{version}.json"
    target.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    print(f"    {target.name}  指纹 {fp[:12]}  入包 {len(pairs)} 个文件  "
          f"基线 {base[:8]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
