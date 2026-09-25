"""全局热键（Linux：X11 XGrabKey），对标 Windows 版 hotkey.py。

Windows 用 ``RegisterHotKey`` + 线程消息泵；这里用 ``XGrabKey`` + 独立线程读
X 事件。回调统一用 ``GLib.idle_add`` 派发回 GTK 主线程 —— 与 Windows 版
「回调在线程里执行、由调用方切回 UI 线程」的约定一致，但更安全。

热键被占用时 ``XGrabKey`` 不会抛异常，而是往连接上送一个 ``BadAccess`` 错误，
所以这里挂了错误处理器把注册结果抓出来：注册失败要让调用方知道（对照
Windows 版「热键被占必须提示，不能静默」的要求）。
"""

from __future__ import annotations

import re
import threading
from typing import Callable

from Xlib import X, XK, error, display as xdisplay
from Xlib.ext import xfixes  # noqa: F401  (导入以确认扩展可用)

try:
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    _HAS_GLIB = True
except Exception:  # noqa: BLE001  pragma: no cover
    GLib = None  # type: ignore[assignment]
    _HAS_GLIB = False

# 修饰键
SHIFT = 0x01
LOCK = 0x02          # CapsLock
CONTROL = 0x04
MOD1 = 0x08          # Alt
MOD2 = 0x10          # NumLock（绝大多数布局）
MOD3 = 0x20
MOD4 = 0x40          # Super / Win
MOD5 = 0x80

# 锁键掩码的通配：注册时对每种组合都抓一次，按下大小写/数字锁也能触发
_LOCK_VARIANTS = (0, LOCK, MOD2, LOCK | MOD2)

_MOD_NAMES = {
    "ctrl": CONTROL, "control": CONTROL,
    "alt": MOD1, "mod1": MOD1, "meta": MOD1,
    "shift": SHIFT,
    "super": MOD4, "win": MOD4, "mod4": MOD4, "cmd": MOD4,
    "hyper": MOD3, "mod3": MOD3,
    "altgr": MOD5, "mod5": MOD5,
}

_ALIAS_KEYSYMS = {
    "print": "Print", "printscreen": "Print", "prtsc": "Print",
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape",
    "space": "space", "tab": "Tab", "del": "Delete", "delete": "Delete",
    "ins": "Insert", "insert": "Insert", "pgup": "Prior", "pgdn": "Next",
    "home": "Home", "end": "End", "backspace": "BackSpace",
    "plus": "plus", "minus": "minus", "equal": "equal", "comma": "comma",
    "period": "period", "slash": "slash", "grave": "grave",
}


def parse_hotkey(spec: str) -> tuple[int, str]:
    """``"ctrl+alt+d"`` -> ``(CONTROL|MOD1, "d")``；解析失败抛 ValueError。"""
    parts = [p.strip().lower() for p in re.split(r"[+\-\s]+", str(spec or "")) if p.strip()]
    if not parts:
        raise ValueError("热键为空")
    mods = 0
    key = ""
    for p in parts:
        if p in _MOD_NAMES:
            mods |= _MOD_NAMES[p]
        else:
            key = p
    if not key:
        raise ValueError(f"热键缺少主键：{spec!r}")
    key = _ALIAS_KEYSYMS.get(key, key)
    return mods, key


def keysym_of(name: str) -> int:
    """键名 -> keysym。"""
    ks = XK.string_to_keysym(name)
    if ks == 0 and len(name) == 1:
        ks = XK.string_to_keysym(name.lower())
    if ks == 0:
        raise ValueError(f"无法识别的按键：{name}")
    return ks


def _keycode_of(display, keysym: int) -> int:
    code = display.keysym_to_keycode(keysym)
    if not code:
        raise ValueError(f"当前键盘布局里没有这个键（keysym 0x{keysym:x}）")
    return code


class HotkeyManager:
    """在独立线程里抓键并读事件；``on_shot`` / ``on_quit`` 会切回主线程执行。"""

    def __init__(self) -> None:
        self._display = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._grabbed: list[tuple[int, int]] = []
        self.status: dict[str, bool] = {}
        self.messages: list[str] = []

    # ------------------------------------------------------------ 注册

    def _grab_variants(self, display, keycode: int, mods: int) -> bool:
        """对锁键的每种组合都抓一次；返回是否至少成功一次。"""
        bad: list[int] = []

        def _handler(err, _req):  # noqa: ANN001
            if isinstance(err, error.BadAccess):
                bad.append(1)
            return 0

        old = display.display.set_error_handler(_handler)
        ok = False
        try:
            for extra in _LOCK_VARIANTS:
                try:
                    display.screen().root.grab_key(
                        keycode, mods | extra, False,
                        X.GrabModeAsync, X.GrabModeAsync,
                    )
                except Exception:  # noqa: BLE001
                    continue
            display.sync()
            ok = not bad
            if ok:
                self._grabbed.append((keycode, mods))
        finally:
            display.display.set_error_handler(old)
        return ok

    def _ungrab_all(self) -> None:
        if self._display is None:
            return
        for keycode, mods in self._grabbed:
            for extra in _LOCK_VARIANTS:
                try:
                    self._display.screen().root.ungrab_key(keycode, mods | extra)
                except Exception:  # noqa: BLE001
                    pass
        self._grabbed.clear()
        try:
            self._display.sync()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 生命周期

    def start(
        self,
        on_shot: Callable[[], None],
        on_quit: Callable[[], None],
        hotkey: str = "ctrl+alt+d",
        quit_hotkey: str = "ctrl+alt+shift+q",
        timeout: float = 2.0,
    ) -> dict[str, bool]:
        """注册热键并在后台线程读事件。返回 ``{"shot": bool, "quit": bool}``。"""
        if self._thread and self._thread.is_alive():
            return dict(self.status)
        self._stop.clear()
        self._ready.clear()
        self.messages = []

        try:
            mods, key = parse_hotkey(hotkey)
            shot_ks = keysym_of(key)
            qmods, qkey = parse_hotkey(quit_hotkey)
            quit_ks = keysym_of(qkey)
        except ValueError as e:
            self.status = {"shot": False, "quit": False}
            self.messages.append(f"热键解析失败：{e}")
            self._ready.set()
            return dict(self.status)

        def _loop() -> None:
            try:
                display = xdisplay.Display()
            except Exception as e:  # noqa: BLE001
                self.status = {"shot": False, "quit": False}
                self.messages.append(f"连不上 X11 显示，全局热键不可用：{e}")
                self._ready.set()
                return
            self._display = display
            try:
                shot_ok = False
                quit_ok = False
                try:
                    shot_ok = self._grab_variants(display, _keycode_of(display, shot_ks), mods)
                except ValueError as e:
                    self.messages.append(f"截图热键不可用：{e}")
                try:
                    quit_ok = self._grab_variants(display, _keycode_of(display, quit_ks), qmods)
                except ValueError as e:
                    self.messages.append(f"退出热键不可用：{e}")
                self.status = {"shot": shot_ok, "quit": quit_ok}
                if not shot_ok:
                    self.messages.append(f"热键 {hotkey} 注册失败，可能已被其他程序占用")
                if not quit_ok:
                    self.messages.append(f"热键 {quit_hotkey} 注册失败，可能已被其他程序占用")
                self._ready.set()

                root = display.screen().root
                root.change_attributes(event_mask=X.KeyPressMask)
                while not self._stop.is_set():
                    # next_event 会阻塞；用 pending 轮询以便及时响应停止
                    while display.pending_events():
                        ev = display.next_event()
                        if ev.type != X.KeyPress:
                            continue
                        if not shot_ok and not quit_ok:
                            continue
                        state = ev.state & ~(LOCK | MOD2)
                        if shot_ok and ev.detail == _keycode_of(display, shot_ks) and state == mods:
                            _dispatch(on_shot)
                        elif quit_ok and ev.detail == _keycode_of(display, quit_ks) and state == qmods:
                            _dispatch(on_quit)
                    self._stop.wait(0.05)
            finally:
                self._ungrab_all()
                try:
                    display.close()
                except Exception:  # noqa: BLE001
                    pass
                self._display = None

        self._thread = threading.Thread(target=_loop, name="hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return dict(self.status)

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=1.0)

    def restart(self, on_shot, on_quit, hotkey: str, quit_hotkey: str) -> dict[str, bool]:
        self.stop()
        self._thread = None
        return self.start(on_shot, on_quit, hotkey, quit_hotkey)

    @property
    def active(self) -> bool:
        return bool(self.status.get("shot") or self.status.get("quit"))


def _dispatch(fn: Callable[[], None]) -> None:
    """切回 GTK 主线程执行回调；没有主循环时直接调用。"""
    if _HAS_GLIB:
        GLib.idle_add(_safe_call, fn, priority=GLib.PRIORITY_HIGH)
    else:  # pragma: no cover
        _safe_call(fn)


def _safe_call(fn: Callable[[], None]) -> bool:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        print(f"[热键] 回调失败: {e}")
    return False  # 不再重复


def selftest() -> int:
    """自检：解析默认热键 + 试注册（不启动事件线程）。"""
    rc = 0
    for spec in ("ctrl+alt+d", "ctrl+alt+shift+q", "super+print"):
        try:
            mods, key = parse_hotkey(spec)
            ks = keysym_of(key)
            print(f"[selftest] 热键解析 {spec!r} -> mods=0x{mods:x} keysym=0x{ks:x} 通过")
        except ValueError as e:
            print(f"[selftest] 热键解析 {spec!r} 失败：{e}")
            rc = 1
    try:
        display = xdisplay.Display()
        mods, key = parse_hotkey("ctrl+alt+d")
        code = _keycode_of(display, keysym_of(key))
        print(f"[selftest] 键盘映射 ctrl+alt+d -> keycode {code} 通过")
        display.close()
    except Exception as e:  # noqa: BLE001
        print(f"[selftest] 键盘映射不可用：{e}")
    return rc
