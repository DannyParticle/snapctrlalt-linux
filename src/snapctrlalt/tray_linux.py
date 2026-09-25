"""系统托盘（Linux），对标 Windows 版 tray.py。

优先用 Ayatana AppIndicator（Cinnamon / GNOME / KDE / Xfce 都认），
没有的话退回 GTK 的 ``StatusIcon``（XEmbed 老式托盘）。
菜单项用 ``(标题, 命令 id, 是否勾选)`` 三件套描述，
与 Windows 版 ``tray.py`` 的 ``set_menu`` 接口保持一致。
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

_INDICATOR = None
try:  # 首选 Ayatana（Mint / Ubuntu 默认自带）
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as _INDICATOR  # type: ignore
except Exception:  # noqa: BLE001
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as _INDICATOR  # type: ignore
    except Exception:  # noqa: BLE001
        _INDICATOR = None

APPINDICATOR_ID = "snapctrlalt"


class TrayIcon:
    """托盘图标。

    ``on_command(cid)`` 收到的是菜单项 id（字符串）；``on_left()`` 是左键单击。
    """

    def __init__(
        self,
        tooltip: str = "截图工具",
        menu_items: list[tuple[str, str, bool]] | None = None,
        on_command=None,
        on_left=None,
        icon_path: str | None = None,
        icon_name: str = "camera-photo-symbolic",
    ) -> None:
        self.tooltip = tooltip
        self.menu_items = list(menu_items or [])
        self.on_command = on_command or (lambda cid: None)
        self.on_left = on_left or (lambda: None)
        self.icon_path = icon_path
        self.icon_name = icon_name
        self.backend = "none"
        self._indicator = None
        self._status_icon = None
        self._menu: Gtk.Menu | None = None

    # ------------------------------------------------------------ 启动

    def start(self) -> bool:
        # 有图标文件就用文件（AppIndicator 需要路径），否则用主题图标名
        icon = self.icon_path if self.icon_path else self.icon_name
        if _INDICATOR is not None:
            try:
                ind = _INDICATOR.Indicator.new(
                    APPINDICATOR_ID, icon, _INDICATOR.IndicatorCategory.APPLICATION_STATUS
                )
                if self.icon_path:
                    ind.set_icon_full(self.icon_path, self.tooltip)
                ind.set_status(_INDICATOR.IndicatorStatus.ACTIVE)
                ind.set_title(self.tooltip)
                self._indicator = ind
                self.backend = "appindicator"
                self.set_menu(self.menu_items)
                return True
            except Exception as e:  # noqa: BLE001
                print(f"[托盘] AppIndicator 不可用（{e}），改用 StatusIcon")

        try:
            icon_obj = Gtk.StatusIcon()
            if self.icon_path:
                icon_obj.set_from_file(self.icon_path)
            else:
                icon_obj.set_from_icon_name(self.icon_name)
            icon_obj.set_tooltip_text(self.tooltip)
            icon_obj.set_visible(True)
            icon_obj.connect("activate", lambda *_: self.on_left())
            icon_obj.connect("popup-menu", self._on_status_popup)
            self._status_icon = icon_obj
            self.backend = "statusicon"
            self.set_menu(self.menu_items)
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[托盘] 托盘不可用：{e}")
            self.backend = "none"
            return False

    # ------------------------------------------------------------ 菜单

    def set_menu(self, items: list[tuple[str, str, bool]]) -> None:
        self.menu_items = list(items)
        if self.backend == "none":
            return
        menu = Gtk.Menu()
        for label, cid, checked in self.menu_items:
            if label == "-":
                menu.append(Gtk.SeparatorMenuItem())
                continue
            if checked:
                it = Gtk.CheckMenuItem.new_with_label(label)
                it.set_active(True)
            else:
                it = Gtk.MenuItem.new_with_label(label)
            it.connect("activate", self._on_item, cid)
            menu.append(it)
        menu.show_all()

        if self._indicator is not None:
            self._indicator.set_menu(menu)
        elif self._status_icon is not None:
            self._status_icon.connect("popup-menu", self._on_status_popup)
        self._menu = menu

    def set_tooltip(self, tip: str) -> None:
        self.tooltip = tip
        if self._indicator is not None:
            self._indicator.set_title(tip)
        if self._status_icon is not None:
            self._status_icon.set_tooltip_text(tip)

    def _on_item(self, _widget, cid: str) -> None:
        try:
            self.on_command(cid)
        except Exception as e:  # noqa: BLE001
            print(f"[托盘] 命令 {cid} 失败: {e}")

    def _on_status_popup(self, icon, button, activate_time) -> None:
        if self._menu is None:
            return
        self._menu.popup(None, None, Gtk.StatusIcon.position_menu,
                         icon, button, activate_time)

    def stop(self) -> None:
        if self._indicator is not None:
            try:
                self._indicator.set_status(_INDICATOR.IndicatorStatus.PASSIVE)
            except Exception:  # noqa: BLE001
                pass
            self._indicator = None
        if self._status_icon is not None:
            try:
                self._status_icon.set_visible(False)
            except Exception:  # noqa: BLE001
                pass
            self._status_icon = None


def selftest() -> int:
    print(f"[selftest] 托盘后端：{'AyatanaAppIndicator' if _INDICATOR else '仅 Gtk.StatusIcon'}")
    print(f"[selftest] StatusIcon 可用：{hasattr(Gtk, 'StatusIcon')}")
    return 0
