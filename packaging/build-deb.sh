#!/bin/bash
# Build the SnapCtrlAlt Debian package.
#
# debhelper is *not* required: the payload is staged by hand and packed with
# dpkg-deb, so the same script works on Linux Mint, Debian, Ubuntu and any other
# dpkg based distribution without installing build dependencies.
#
# Usage:
#     packaging/build-deb.sh [--version X.Y.Z] [--no-tests]
#
# The package is written to dist/.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION=""
RUN_TESTS=1
while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --no-tests) RUN_TESTS=0; shift ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ -z "$VERSION" ]; then
    VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/snapctrlalt/__init__.py | head -1)"
fi
[ -n "$VERSION" ] || { echo "cannot determine the version" >&2; exit 1; }

PKG="snapctrlalt"
ARCH="all"
# Reproducible builds: a fixed timestamp keeps the .deb byte-identical across
# rebuilds, so a published checksum can be verified by rebuilding from source.
# 2026-09-25 16:00:00 +0800.
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1789977600}"
export SOURCE_DATE_EPOCH
BUILD="$ROOT/build/${PKG}_${VERSION}_${ARCH}"
STAGE="$BUILD"
DIST="$ROOT/dist"
OUT="$DIST/${PKG}_${VERSION}_${ARCH}.deb"

echo "==> SnapCtrlAlt $VERSION"
rm -rf "$BUILD"
mkdir -p "$DIST" "$STAGE/DEBIAN"

# ------------------------------------------------------------- validation ---
echo "==> syntax check"
python3 -m compileall -q src/snapctrlalt
python3 -c "import ast,sys; [ast.parse(open(p).read(), p) for p in sys.argv[1:]]" \
    bin/snapctrlalt tools/make_icons.py tools/diagnose.py tools/perf.py
for script in packaging/debian/postinst packaging/debian/prerm packaging/debian/postrm; do
    sh -n "$script"
done

if [ "$RUN_TESTS" = "1" ]; then
    echo "==> tests"
    # 界面回归（离屏驱动真实覆盖层对象）
    PYTHONPATH=src python3 tests/test_overlay.py >/dev/null
    # 坐标标定：确保「框选位置 == 截取位置」这条被钉住
    PYTHONPATH=src python3 tests/test_coords.py >/dev/null
    echo "    overlay + coords tests passed"
fi

# ---------------------------------------------------------------- payload ---
echo "==> staging"
install -d "$STAGE/usr/lib/python3/dist-packages"
cp -r src/snapctrlalt "$STAGE/usr/lib/python3/dist-packages/"
find "$STAGE/usr/lib/python3/dist-packages" -name '__pycache__' -type d -prune -exec rm -rf {} +

install -d "$STAGE/usr/bin"
install -m 0755 bin/snapctrlalt "$STAGE/usr/bin/"

install -d "$STAGE/usr/share/applications"
install -m 0644 share/applications/snapctrlalt.desktop "$STAGE/usr/share/applications/"

# 图标按 hicolor 主题结构铺开
for size in 16 22 24 32 48 64 128 256 512; do
    src_icon="share/icons/hicolor/${size}x${size}/apps/snapctrlalt.png"
    [ -f "$src_icon" ] || continue
    install -d "$STAGE/usr/share/icons/hicolor/${size}x${size}/apps"
    install -m 0644 "$src_icon" "$STAGE/usr/share/icons/hicolor/${size}x${size}/apps/"
done
if [ -f share/icons/hicolor/scalable/apps/snapctrlalt.svg ]; then
    install -d "$STAGE/usr/share/icons/hicolor/scalable/apps"
    install -m 0644 share/icons/hicolor/scalable/apps/snapctrlalt.svg \
        "$STAGE/usr/share/icons/hicolor/scalable/apps/"
fi

install -d "$STAGE/usr/share/doc/$PKG"
install -m 0644 README.md "$STAGE/usr/share/doc/$PKG/README.md"
install -m 0644 CHANGELOG.md "$STAGE/usr/share/doc/$PKG/CHANGELOG.md"
install -m 0644 packaging/debian/copyright "$STAGE/usr/share/doc/$PKG/copyright"
gzip -9n -c packaging/debian/changelog > "$STAGE/usr/share/doc/$PKG/changelog.Debian.gz"
chmod 0644 "$STAGE/usr/share/doc/$PKG/changelog.Debian.gz"

# 时间戳归一，否则同一次源码每次构建出来的 deb 都不一样
find "$STAGE" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} + 2>/dev/null || true

# 权限归一（拷贝过来的文件带着构建者的 umask）
find "$STAGE" -type d -exec chmod 0755 {} +
find "$STAGE" -type f -path '*/usr/lib/python3/*' -exec chmod 0644 {} +
find "$STAGE" -type f \( -path '*/usr/bin/*' -o -path '*/DEBIAN/postinst' \
    -o -path '*/DEBIAN/prerm' -o -path '*/DEBIAN/postrm' \) -exec chmod 0755 {} +
find "$STAGE" -type f -path '*/usr/share/*' -exec chmod 0644 {} +

# ---------------------------------------------------------------- control ---
install -m 0755 packaging/debian/postinst "$STAGE/DEBIAN/postinst"
install -m 0755 packaging/debian/prerm    "$STAGE/DEBIAN/prerm"
install -m 0755 packaging/debian/postrm   "$STAGE/DEBIAN/postrm"

INSTALLED_SIZE="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"

sed -e 's/@VERSION@/'"$VERSION"'/g' \
    -e 's/@INSTALLED_SIZE@/'"$INSTALLED_SIZE"'/g' \
    packaging/debian/control > "$STAGE/DEBIAN/control.tmp"

# 二进制 control 里不能带构建期字段，也不能留 debhelper 的占位符：
# 只保留 Package 段落，并从源段落补上 Maintainer / Section 之类必需字段。
python3 - "$STAGE/DEBIAN/control.tmp" "$STAGE/DEBIAN/control" <<'PYEOF'
import sys

source, target = sys.argv[1], sys.argv[2]
drop_keys = ("Build-Depends", "Standards-Version", "Rules-Requires-Root")

paragraphs = [[("", [])]]
inserted_break = False
for raw in open(source, encoding="utf-8"):
    line = raw.rstrip("\n")
    if not line.strip():
        if paragraphs[-1] and any(k for k, _ in paragraphs[-1]):
            paragraphs.append([("", [])])
        continue
    if line[0] in " \t":                       # 上一字段的续行
        paragraphs[-1][-1][1].append(line.strip())
        continue
    key, _, value = line.partition(":")
    if key.strip() == "Package" and not inserted_break:
        if any(k for k, _ in paragraphs[-1]):
            paragraphs.append([("", [])])
        inserted_break = True
    paragraphs[-1].append((key.strip(), [value.strip()]))

binary = None
source_stanza = None
for fields in paragraphs:
    keys = [k for k, _ in fields if k]
    if not keys:
        continue
    if "Package" in keys:
        binary = fields
        continue
    if "Source" in keys and source_stanza is None:
        source_stanza = fields
if binary is None:
    raise SystemExit("no Package stanza found in %s" % source)

merged = list(binary)
present = {k for k, _ in binary if k}
if source_stanza:
    for key, value in source_stanza:
        if key and key not in present:
            merged.append((key, value))
            present.add(key)
binary = merged

order = ["Package", "Version", "Architecture", "Installed-Size", "Depends",
         "Pre-Depends", "Recommends", "Suggests", "Conflicts", "Breaks",
         "Replaces", "Provides", "Section", "Priority", "Maintainer",
         "Homepage", "Description"]
by_key = {k: v for k, v in binary if k}

out = []
for key in order:
    if key not in by_key:
        continue
    parts = list(by_key[key])
    if key in ("Depends", "Pre-Depends", "Recommends", "Suggests",
               "Conflicts", "Breaks", "Replaces", "Provides"):
        joined = " ".join(parts).replace("${misc:Depends}", "")
        items = [item.strip() for item in joined.split(",") if item.strip()]
        out.append("%s: %s" % (key, ", ".join(items)))
    elif key == "Description":
        out.append("Description: %s" % parts[0])
        out.extend(" " + part for part in parts[1:])
    else:
        out.append("%s: %s" % (key, " ".join(parts)))

for key, _ in binary:
    if key and key not in order and key not in drop_keys:
        out.append("%s: %s" % (key, " ".join(by_key[key])))

with open(target, "w", encoding="utf-8") as handle:
    handle.write("\n".join(out) + "\n")
PYEOF
rm -f "$STAGE/DEBIAN/control.tmp"
chmod 0644 "$STAGE/DEBIAN/control"

# ------------------------------------------------------------------ build ---
echo "==> packing"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT" >/dev/null
ln -sf "$(basename "$OUT")" "$DIST/${PKG}_${ARCH}.deb"

echo
echo "==> contents"
dpkg-deb --contents "$OUT" | awk '{print "   ", $1, $6}' | head -30
echo
echo "package: $OUT"
echo "size   : $(du -h "$OUT" | cut -f1)  (installed: ${INSTALLED_SIZE} KiB)"
echo "sha256 : $(sha256sum "$OUT" | cut -d' ' -f1)"
echo
echo "install with:  sudo apt install $OUT    # or: sudo dpkg -i $OUT"
