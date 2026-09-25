"""桌面通知（对标 Windows 版托盘气泡提示）。

优先用 GLib 的 ``org.freedesktop.Notifications``（桌面原生），没有就退回
``notify-send``；再不行就只打印到终端，绝不静默丢消息。
"""

from __future__ import annotations

import shutil
import subprocess

try:
    import gi

    gi.require_version("Gio", "2.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import Gio, GLib

    _HAS_GIO = True
except Exception:  # noqa: BLE001
    Gio = None  # type: ignore[assignment]
    GLib = None  # type: ignore[assignment]
    _HAS_GIO = False


class Notifier:
    def __init__(self, app_name: str = "SnapCtrlAlt", enabled: bool = True) -> None:
        self.app_name = app_name
        self.enabled = enabled
        self._proxy = None
        self._backend = "none"
        self._init_backend()

    def _init_backend(self) -> None:
        if _HAS_GIO:
            try:
                self._proxy = Gio.DBusProxy.new_for_bus_sync(
                    Gio.BusType.SESSION,
                    Gio.DBusProxyFlags.NONE,
                    None,
                    "org.freedesktop.Notifications",
                    "/org/freedesktop/Notifications",
                    "org.freedesktop.Notifications",
                    None,
                )
                self._backend = "dbus"
                return
            except Exception:  # noqa: BLE001
                self._proxy = None
        if shutil.which("notify-send"):
            self._backend = "notify-send"
        else:
            self._backend = "print"

    def notify(self, body: str, title: str | None = None, icon: str = "camera-photo") -> bool:
        if not self.enabled:
            return False
        title = title or self.app_name
        if self._backend == "dbus" and self._proxy is not None:
            try:
                self._proxy.call_sync(
                    "Notify",
                    GLib.Variant("(susssasa{sv}i)",
                                 (self.app_name, 0, icon, title, body, [], {}, 3000)),
                    2000, None, None,
                )
                return True
            except Exception:  # noqa: BLE001
                self._backend = "notify-send" if shutil.which("notify-send") else "print"
        if self._backend == "notify-send":
            try:
                subprocess.Popen(
                    ["notify-send", "-i", icon, "-t", "3000", title, body],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return True
            except OSError:
                pass
        print(f"[通知] {title}: {body}")
        return False

    @property
    def backend(self) -> str:
        return self._backend


def selftest() -> int:
    n = Notifier()
    print(f"[selftest] 通知后端：{n.backend}")
    return 0
