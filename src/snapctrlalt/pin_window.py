"""贴图窗口（对标 QQ 截图的「贴到桌面」）。

把截图钉成一个无边框、可拖动、可缩放的置顶小窗，方便对照着看 / 截图时参考。
滚轮缩放，双击关闭，右键菜单可复制 / 保存 / 关闭。
"""

from __future__ import annotations

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from PIL import Image  # noqa: E402

from .overlay import pil_to_surface  # noqa: E402

_OPEN: list["PinWindow"] = []


class PinWindow:
    """一张钉在桌面上的截图。"""

    def __init__(self, img: Image.Image, app=None, pos: tuple[int, int] | None = None,
                 scale: float = 1.0, opacity: float = 1.0) -> None:
        self.app = app
        self.img = img
        self.surface = pil_to_surface(img.convert("RGB"))
        self.scale = scale
        self.opacity = opacity
        self._drag: tuple[float, float, float, float] | None = None

        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win.set_decorated(False)
        self.win.set_resizable(True)
        self.win.set_keep_above(True)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.win.set_app_paintable(True)
        self.win.set_opacity(opacity)
        self.win.set_title("贴图")
        if pos:
            self.win.move(*pos)

        self.area = Gtk.DrawingArea()
        w = max(40, int(img.width * self.scale))
        h = max(30, int(img.height * self.scale))
        self.area.set_size_request(w, h)
        self.area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.SCROLL_MASK
        )
        self.area.connect("draw", self._on_draw)
        self.area.connect("button-press-event", self._on_press)
        self.area.connect("button-release-event", self._on_release)
        self.area.connect("motion-notify-event", self._on_motion)
        self.area.connect("scroll-event", self._on_scroll)
        self.win.add(self.area)
        self.win.connect("key-press-event", self._on_key)
        self.win.connect("destroy", lambda *_: _OPEN.remove(self) if self in _OPEN else None)
        _OPEN.append(self)
        self.win.show_all()

    # ------------------------------------------------------------ 绘制

    def _on_draw(self, _w, cr) -> bool:
        aw = self.area.get_allocated_width()
        ah = self.area.get_allocated_height()
        cr.set_source_rgb(0.1, 0.1, 0.1)
        cr.paint()
        # 等比铺满
        sx = aw / self.img.width
        sy = ah / self.img.height
        cr.save()
        cr.scale(sx, sy)
        cr.set_source_surface(self.surface, 0, 0)
        pattern = cr.get_source()
        pattern.set_filter(cairo.FILTER_GOOD)
        cr.paint()
        cr.restore()
        # 边框
        cr.set_line_width(1)
        cr.set_source_rgba(1, 1, 1, 0.35)
        cr.rectangle(0.5, 0.5, aw - 1, ah - 1)
        cr.stroke()
        return False

    # ------------------------------------------------------------ 交互

    def _on_press(self, _w, event) -> bool:
        if event.button == 1:
            # 记录「根坐标 - 窗口坐标」的偏移，拖动时保持它不变
            self._drag = (event.x_root, event.y_root, event.x, event.y)
            return True
        if event.button == 3:
            self._menu(event)
            return True
        return False

    def _on_motion(self, _w, event) -> bool:
        if not self._drag:
            return False
        rx, ry, wx, wy = self._drag
        self.win.move(int(event.x_root - (rx - wx)), int(event.y_root - (ry - wy)))
        return True

    def _on_release(self, _w, _event) -> bool:
        self._drag = None
        return False

    def _on_scroll(self, _w, event) -> bool:
        up = event.direction == Gdk.ScrollDirection.UP
        if event.direction == Gdk.ScrollDirection.SMOOTH:
            _ok, _dx, dy = event.get_scroll_deltas()
            up = dy < 0
        factor = 1.1 if up else 1 / 1.1
        aw = self.area.get_allocated_width()
        ah = self.area.get_allocated_height()
        nw = max(40, min(6000, int(aw * factor)))
        nh = max(30, min(6000, int(ah * factor)))
        self.area.set_size_request(nw, nh)
        self.win.resize(nw, nh)
        return True

    def _on_key(self, _w, event) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.win.destroy()
            return True
        return False

    def _menu(self, event) -> None:
        menu = Gtk.Menu()
        items = (
            ("复制到剪贴板", lambda: self._copy()),
            ("保存为文件…", lambda: self._save()),
            ("不透明度 +", lambda: self._set_opacity(self.opacity + 0.15)),
            ("不透明度 −", lambda: self._set_opacity(self.opacity - 0.15)),
            ("原始大小", lambda: self._reset_size()),
            ("关闭贴图", lambda: self.win.destroy()),
        )
        for label, cb in items:
            it = Gtk.MenuItem.new_with_label(label)
            it.connect("activate", lambda _i, f=cb: f())
            menu.append(it)
        menu.show_all()
        menu.popup_at_pointer(event)

    def _set_opacity(self, val: float) -> None:
        self.opacity = max(0.2, min(1.0, val))
        self.win.set_opacity(self.opacity)

    def _reset_size(self) -> None:
        w, h = self.img.width, self.img.height
        self.area.set_size_request(w, h)
        self.win.resize(w, h)

    def _copy(self) -> None:
        try:
            from . import clipboard_linux

            clipboard_linux.copy_image(self.img)
            if self.app:
                self.app.notify("贴图已复制到剪贴板")
        except Exception as e:  # noqa: BLE001
            print(f"[贴图] 复制失败: {e}")

    def _save(self) -> None:
        if not self.app:
            return
        path = self.app.ask_save_path(self.win)
        if not path:
            return
        try:
            self.app.write_image(self.img, path)
            self.app.notify(f"已保存 {path}")
        except Exception as e:  # noqa: BLE001
            print(f"[贴图] 保存失败: {e}")

    @property
    def size(self) -> tuple[int, int]:
        return self.area.get_allocated_width(), self.area.get_allocated_height()


def close_all() -> None:
    for w in list(_OPEN):
        try:
            w.win.destroy()
        except Exception:  # noqa: BLE001
            pass
    _OPEN.clear()


def count() -> int:
    return len(_OPEN)


def selftest() -> int:
    print(f"[selftest] 贴图窗口：模块可用（当前 {count()} 个）")
    return 0
