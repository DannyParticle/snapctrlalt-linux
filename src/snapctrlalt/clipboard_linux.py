"""把位图写进剪贴板（对标 Windows 版 clipboard_win.py）。

Windows 版用 Win32 剪贴板 API 写 CF_DIB / PNG；Linux 走 GTK 剪贴板。
GTK 的 Python 绑定只暴露 ``set_image`` / ``set_text`` / ``store``（``set_with_owner``
在 GTK 3.24 的 introspection 里不可用），所以这里用 ``Gtk.Clipboard.set_image``
配合 ``set_can_store``：后者让剪贴板管理器（Klipper / CopyQ / GSD 等）在
本进程退出后依然能提供内容 —— 也就是「复制完就能关掉程序」。

数据安全：``GdkPixbuf.Pixbuf.new_from_data`` 会借用 Python bytes 的缓冲区，
对象被回收就成悬垂指针，所以统一用 ``new_from_bytes``（GLib.Bytes 自己持有数据）。
"""

from __future__ import annotations

import io
import time

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GdkPixbuf, Gtk  # noqa: E402

from PIL import Image  # noqa: E402

# 值得放进剪贴板持久化列表的 flavor：GTK 系 / Qt 系 / 浏览器都能取到
IMAGE_TARGETS = ("image/png", "image/bmp", "image/x-bmp", "image/x-MS-bmp")

_png_cache: tuple[int, bytes] | None = None
# 持有最近写入的图像，防止剪贴板在 GTK 内部还引用它时被回收
_ALIVE: list = []


def png_bytes(img: Image.Image) -> bytes:
    """PNG 编码（同一张图只编码一次）。"""
    global _png_cache
    key = id(img)
    if _png_cache and _png_cache[0] == key:
        return _png_cache[1]
    buf = io.BytesIO()
    img.save(buf, "PNG")
    data = buf.getvalue()
    _png_cache = (key, data)
    return data


def bmp_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "BMP")
    return buf.getvalue()


def pixbuf_from_pil(img: Image.Image) -> GdkPixbuf.Pixbuf:
    """PIL -> GdkPixbuf，经 PNG 字节走 GLib.Bytes，杜绝悬垂指针。"""
    import gi as _gi

    _gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    data = png_bytes(img)
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(data)
    loader.close()
    pb = loader.get_pixbuf()
    if pb is None:  # 极端情况：退化到内存拷贝路径
        rgba = img.convert("RGBA")
        buf = rgba.tobytes()
        pb = GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(buf), GdkPixbuf.Colorspace.RGB, True, 8,
            rgba.width, rgba.height, rgba.width * 4,
        )
    return pb


def pil_from_pixbuf(pb: GdkPixbuf.Pixbuf) -> Image.Image:
    w, h = pb.get_width(), pb.get_height()
    channels = pb.get_n_channels()
    mode = "RGB" if channels == 3 else "RGBA"
    img = Image.frombuffer(mode, (w, h), pb.get_pixels(), "raw", mode,
                           pb.get_rowstride(), 1)
    return img.copy()


def copy_image(img: Image.Image, primary: bool = False) -> None:
    """把 PIL 图像写进 CLIPBOARD；``primary`` 为真时同时写 PRIMARY（中键粘贴）。"""
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    warnings: list[str] = []

    for sel, name in ((Gdk.SELECTION_CLIPBOARD, "CLIPBOARD"),) + (
            ((Gdk.SELECTION_PRIMARY, "PRIMARY"),) if primary else ()):
        cb = Gtk.Clipboard.get(sel)
        try:
            pb = pixbuf_from_pil(img)
            cb.set_image(pb)
            _ALIVE.append(pb)
            del _ALIVE[:-3]
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{name}: {e}")
            continue
        # 让剪贴板管理器留一份：本进程退出后还能粘贴
        try:
            cb.set_can_store([Gdk.Atom.intern(t, False) for t in IMAGE_TARGETS])
            cb.store()
        except Exception:  # noqa: BLE001
            pass
        # 驱动主循环，把 SelectionRequest 处理掉再返回
        pump(60)

    if warnings and len(warnings) == (2 if primary else 1):
        raise RuntimeError("写入剪贴板失败：" + "；".join(warnings))


def copy_text(text: str, primary: bool = False) -> None:
    sels = [Gdk.SELECTION_CLIPBOARD] + ([Gdk.SELECTION_PRIMARY] if primary else [])
    for sel in sels:
        cb = Gtk.Clipboard.get(sel)
        cb.set_text(text, -1)
        try:
            cb.set_can_store([Gdk.Atom.intern("UTF8_STRING", False)])
            cb.store()
        except Exception:  # noqa: BLE001
            pass


def pump(ms: int = 150) -> None:
    """驱动主循环若干毫秒，让挂起的 SelectionRequest 有机会被处理。"""
    end = time.time() + ms / 1000.0
    while time.time() < end:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.004)


def read_clipboard_image() -> Image.Image | None:
    pb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_image()
    return pil_from_pixbuf(pb) if pb is not None else None


def clipboard_has_image() -> bool:
    ok, targets = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_targets()
    if not ok or not targets:
        return False
    return bool({t.name() for t in targets} & set(IMAGE_TARGETS))


def selftest() -> int:
    """自检：写一张纯色图进剪贴板，再读回来比对。"""
    src = Image.new("RGB", (64, 40), (12, 130, 246))
    try:
        copy_image(src)
    except Exception as e:  # noqa: BLE001
        print(f"[自检] 剪贴板：写入失败（{e}）")
        return 1
    pump(200)
    back = read_clipboard_image()
    if back is None:
        print("[自检] 剪贴板：写入成功，但读回为空（无剪贴板管理器时属正常）")
        return 0
    ok = back.size == src.size and back.convert("RGB").getpixel((8, 8)) == (12, 130, 246)
    print(f"[自检] 剪贴板：{'通过' if ok else '失败'}（读回 {back.size[0]}×{back.size[1]}）")
    return 0 if ok else 1
