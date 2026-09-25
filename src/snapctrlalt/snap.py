"""SnapCtrlAlt Linux —— 入口、托盘常驻、热键分发（对标 Windows 版 snap.py）。

    python3 snap.py            常驻托盘（有托盘时），按 Ctrl+Alt+D 截图
    python3 snap.py --once     只截一次，截完退出（适合绑桌面环境的自定义快捷键）
    python3 snap.py --selftest 不弹界面做基础自检，退出码 0 为通过
    python3 snap.py --settings 直接打开设置窗口

单实例：用 ``$XDG_RUNTIME_DIR/snapctrlalt.sock`` 做互斥 + 命令转发，
重复启动只会把命令（shot / settings / quit）发给已在跑的实例，不会抢热键。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

if __package__ in (None, ""):  # 允许直接 python3 snap.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    __package__ = "snapctrlalt"

import gi  # noqa: E402

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from PIL import Image  # noqa: E402

from . import __version__  # noqa: E402
from . import capture_linux as capture  # noqa: E402
from . import clipboard_linux, notify_linux, overlay as overlay_mod, pin_window  # noqa: E402
from . import settings as app_settings  # noqa: E402
from .hotkey_linux import HotkeyManager  # noqa: E402
from .overlay import ShotOverlay  # noqa: E402
from .tray_linux import TrayIcon  # noqa: E402

APP_TITLE = "SnapCtrlAlt 截图工具"

# 托盘命令 id
CMD_SHOT = "shot"
CMD_DELAY = "delay"
CMD_AUTOSTART = "autostart"
CMD_PREFER = "prefer"
CMD_SAVE = "save"
CMD_SETTINGS = "settings"
CMD_ABOUT = "about"
CMD_QUIT = "quit"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- 单实例


def socket_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/{os.getuid()}"
    return Path(base) / "snapctrlalt.sock"


def send_command(cmd: str) -> bool:
    """把命令发给已在跑的实例；没人监听返回 False。"""
    p = socket_path()
    if not p.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.5)
            s.connect(str(p))
            s.sendall((cmd + "\n").encode("utf-8"))
            try:
                s.recv(64)
            except OSError:
                pass
        return True
    except OSError:
        return False


class CommandServer:
    """常驻实例的本地命令通道。"""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.path = socket_path()
        self.sock: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self._stop = False

    def start(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                # 已有实例：探测一下是不是活的
                if send_command("ping"):
                    return False
                self.path.unlink()
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(str(self.path))
            self.sock.listen(4)
            self.sock.settimeout(0.5)
        except OSError as e:
            log(f"命令通道不可用：{e}")
            return False
        self.thread = threading.Thread(target=self._loop, name="ipc", daemon=True)
        self.thread.start()
        return True

    def _loop(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                data = conn.recv(256).decode("utf-8", "replace").strip()
                if data and data != "ping":
                    GLib.idle_add(self.handler, data)
                conn.sendall(b"ok\n")
            except OSError:
                pass
            finally:
                conn.close()

    def stop(self) -> None:
        self._stop = True
        try:
            if self.sock:
                self.sock.close()
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------- 应用


class App:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.overlay: ShotOverlay | None = None
        self.tray: TrayIcon | None = None
        self.hotkeys = HotkeyManager()
        self.notifier = notify_linux.Notifier(enabled=bool(cfg.get("notify", True)))
        self.server = CommandServer(self._on_ipc)
        self.settings_win: Gtk.Window | None = None
        self.status_item: Gtk.MenuItem | None = None
        self._countdown_win: Gtk.Window | None = None
        self._timers: list[int] = []
        self._shot_busy = False
        self._capture_error: str | None = None
        self.last_save: str | None = None
        self.pins: list = []
        self._once = False

    # ------------------------------------------------------------ 回调

    def notify(self, msg: str) -> None:
        if not msg:
            return
        log(msg)
        # 覆盖层开着的时候不再弹系统通知：会盖住正在标注的画面，
        # 而且同一条消息会既进日志又弹一次，看起来像执行了两遍
        if self.overlay is None and self.cfg.get("notify", True):
            self.notifier.notify(msg)
        if self.status_item is not None:
            try:
                self.status_item.set_label(msg)
            except Exception:  # noqa: BLE001
                pass

    def save_image(self, img: Image.Image, silent: bool = False) -> str | None:
        """按配置把图存进保存目录（自动文件名）。"""
        fmt = str(self.cfg.get("save_format", "png")).lower()
        ext = "jpg" if fmt in ("jpg", "jpeg") else fmt
        directory = self.cfg.get("save_dir") or str(app_settings.pictures_dir())
        try:
            d = Path(directory).expanduser()
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            d = app_settings.pictures_dir()
        name = time.strftime("Screenshot_%Y-%m-%d_%H-%M-%S") + f".{ext}"
        path = d / name
        try:
            self.write_image(img, path)
        except Exception as e:  # noqa: BLE001
            if not silent:
                self.notify(f"保存失败：{e}")
            return None
        self.last_save = str(path)
        self.cfg["last_save_dir"] = str(d)
        try:
            app_settings.save_settings(self.cfg)
        except RuntimeError:
            pass
        return str(path)

    def write_image(self, img: Image.Image, path: str | Path) -> None:
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix in (".jpg", ".jpeg"):
            img.convert("RGB").save(path, "JPEG",
                                    quality=int(self.cfg.get("jpeg_quality", 95)))
        elif not suffix:
            path = path.with_suffix(".png")
            img.save(path, "PNG")
        else:
            img.save(path)

    def ask_save_path(self, parent=None) -> str | None:
        dlg = Gtk.FileChooserDialog(
            title="保存截图", parent=parent,
            action=Gtk.FileChooserAction.SAVE,
        )
        dlg.add_buttons("取消", Gtk.ResponseType.CANCEL, "保存", Gtk.ResponseType.OK)
        dlg.set_do_overwrite_confirmation(True)
        start = self.cfg.get("last_save_dir") or self.cfg.get("save_dir") \
            or str(app_settings.pictures_dir())
        try:
            dlg.set_current_folder(str(Path(start).expanduser()))
        except Exception:  # noqa: BLE001
            pass
        dlg.set_current_name(time.strftime("Screenshot_%Y-%m-%d_%H-%M-%S.png"))
        filt = Gtk.FileFilter()
        filt.set_name("PNG 图片")
        filt.add_pattern("*.png")
        dlg.add_filter(filt)
        filt2 = Gtk.FileFilter()
        filt2.set_name("JPEG 图片")
        filt2.add_pattern("*.jpg")
        filt2.add_pattern("*.jpeg")
        dlg.add_filter(filt2)
        filt3 = Gtk.FileFilter()
        filt3.set_name("所有文件")
        filt3.add_pattern("*")
        dlg.add_filter(filt3)
        resp = dlg.run()
        path = dlg.get_filename() if resp == Gtk.ResponseType.OK else None
        dlg.destroy()
        if path:
            self.cfg["last_save_dir"] = str(Path(path).parent)
            try:
                app_settings.save_settings(self.cfg)
            except RuntimeError:
                pass
        return path

    def pin_image(self, img: Image.Image) -> None:
        pos = None
        try:
            disp = Gdk.Display.get_default()
            seat = disp.get_default_seat()
            _win, x, y = seat.get_pointer().get_position()
            pos = (int(x) + 24, int(y) + 24)
        except Exception:  # noqa: BLE001
            pos = None
        self.pins.append(pin_window.PinWindow(img, app=self, pos=pos))

    # ------------------------------------------------------------ 截图主流程

    def trigger_shot(self, delay: float = 0.0) -> None:
        if self._shot_busy:
            log("上一次截图还没结束，忽略本次触发")
            return
        self._shot_busy = True
        try:
            self._release_modifiers()
            if delay > 0:
                self._countdown(delay, self._start_shot)
            else:
                GLib.timeout_add(120, self._start_shot_once)
        except Exception:
            self._shot_busy = False
            raise

    def _start_shot_once(self) -> bool:
        self._start_shot()
        return False

    @staticmethod
    def _release_modifiers() -> None:
        """等热键修饰键松开：否则 Ctrl+Alt 会被判成「按住修饰键的点击」。"""
        try:
            from Xlib import X, display as xdisplay

            d = xdisplay.Display()
            root = d.screen().root
            deadline = time.time() + 1.2
            masks = (X.ControlMask, X.Mod1Mask, X.Mod4Mask, X.ShiftMask)
            while time.time() < deadline:
                q = root.query_pointer()
                if not (q.mask & (masks[0] | masks[1] | masks[2])):
                    break
                time.sleep(0.02)
            d.close()
        except Exception:  # noqa: BLE001
            pass

    def _countdown(self, seconds: float, then) -> None:
        win = Gtk.Window(type=Gtk.WindowType.POPUP)
        win.set_decorated(False)
        win.set_keep_above(True)
        win.set_skip_taskbar_hint(True)
        win.set_app_paintable(True)
        label = Gtk.Label()
        label.set_markup(
            f'<span font="48" weight="bold" foreground="#ffffff">{int(seconds)}</span>')
        win.add(label)
        win.get_style_context().add_class("countdown")
        win.show_all()
        self._countdown_win = win
        remaining = [max(1, int(round(seconds)))]

        def tick() -> bool:
            remaining[0] -= 1
            if remaining[0] <= 0:
                win.destroy()
                self._countdown_win = None
                self._start_shot()
                return False
            label.set_markup(
                f'<span font="48" weight="bold" foreground="#ffffff">{remaining[0]}</span>')
            return True

        GLib.timeout_add(1000, tick)

    def _start_shot(self) -> None:
        then = time.perf_counter()
        try:
            boxes = capture.virtual_screen_bounds()
        except Exception as e:  # noqa: BLE001
            self._shot_busy = False
            self.notify(f"读不到屏幕尺寸：{e}")
            return
        # 外部工具优先（对标 Windows 版的「QQ 优先」）
        if self.cfg.get("prefer_external", True) and not self.cfg.get("force_local"):
            cmd = capture.external_command()
            if cmd and capture.run_external(cmd):
                log(f"已交给外部截图工具：{' '.join(cmd)}")
                self._shot_busy = False
                return
        try:
            img, boxes = capture.grab_full_screen(boxes)
        except Exception as e:  # noqa: BLE001
            self._shot_busy = False
            msg = f"抓图失败：{e}"
            log(msg)
            self.notify(msg)
            self._show_error(msg)
            return
        scale = self._measure_coord_scale(img, boxes)
        log(f"抓图完成 {img.width}×{img.height}（{time.perf_counter() - then:.2f}s，"
            f"坐标缩放 {scale}）")
        self._open_overlay(img, boxes, scale)

    def _measure_coord_scale(self, img: Image.Image, boxes) -> int:
        """求「GTK 事件坐标 → 抓图像素」的倍数。

        HiDPI 下这三个尺寸可以两两不同（实测本机：X11 根窗口 2880×1800、
        GTK 事件坐标范围 2880、抓到的图 5760×3600），靠 GTK 的 scale factor
        反推会算错，所以先用 X11 的真实根窗口几何把它标定出来，GTK 没起来时
        再退回顾数比例。
        """
        env = os.environ.get("SNAP_SCALE", "").strip()
        if env.isdigit() and int(env) > 0:
            return int(env)
        try:
            gw, gh = capture.root_geometry()
            ratio = img.width / float(gw)
            cand = int(round(ratio))
            if cand >= 1 and abs(ratio - cand) < 0.15:
                return cand
            print(f"[抓图] 坐标缩放不是整数（{ratio:.2f}），取整为 {max(1, cand)}")
            return max(1, cand)
        except Exception as e:  # noqa: BLE001
            print(f"[抓图] 读不到 X11 根窗口尺寸（{e}），退回显示器几何推算")
        return self._guess_scale(img.width)

    def _guess_scale(self, img_w: int) -> int:
        """物理像素 / GTK 逻辑像素：覆盖层靠它把逻辑坐标换算成图像像素。"""
        env = os.environ.get("SNAP_SCALE", "").strip()
        if env.isdigit() and int(env) > 0:
            return int(env)
        try:
            disp = Gdk.Display.get_default()
            if disp is None:
                return 1
            mon = disp.get_primary_monitor() or disp.get_monitor(0)
            if mon is None:
                return 1
            geo = mon.get_geometry()
            if geo.width <= 0:
                return 1
            ratio = img_w / float(geo.width)
            for cand in (1, 2, 3, 4):
                if abs(ratio - cand) < 0.25:
                    return cand
            return 1
        except Exception:  # noqa: BLE001
            return 1

    def _open_overlay(self, img: Image.Image, boxes, scale: int) -> None:
        if self.overlay is not None:
            try:
                self.overlay.cancel()
            except Exception:  # noqa: BLE001
                pass
            self.overlay = None
        try:
            self.overlay = ShotOverlay(
                app=self, screen_img=img, screen_box=boxes.box,
                on_close=self._on_overlay_close, status_cb=self._status,
                scale=scale, logical_size=self._logical_screen_size(),
            )
        except Exception as e:  # noqa: BLE001
            self._shot_busy = False
            log("覆盖层创建失败：\n" + traceback.format_exc())
            self.notify(f"覆盖层创建失败：{e}")
            self._show_error(f"覆盖层创建失败：{e}")

    @staticmethod
    def _logical_screen_size() -> tuple[int, int] | None:
        """GTK 眼里的屏幕逻辑尺寸（用来摆全屏覆盖层窗口）。"""
        try:
            disp = Gdk.Display.get_default()
            if disp is None:
                return None
            n = disp.get_n_monitors()
            if n <= 0:
                return None
            x1 = y1 = -(1 << 30)
            for i in range(n):
                g = disp.get_monitor(i).get_geometry()
                x1 = max(x1, g.x + g.width)
                y1 = max(y1, g.y + g.height)
            if x1 <= 1 or y1 <= 1:
                return None
            return (x1, y1)
        except Exception:  # noqa: BLE001
            return None

    def _on_overlay_close(self, reason: str) -> None:
        self.overlay = None
        self._shot_busy = False
        if reason == "finish":
            # 剪贴板是选区所有者模型：立刻驱动主循环把数据交付出去
            GLib.timeout_add(60, self._pump_clipboard)
        if self._once:
            self._finish_once()

    @staticmethod
    def _pump_clipboard() -> bool:
        clipboard_linux.pump(120)
        return False

    def _status(self, text: str) -> None:
        # 覆盖层在的时候不要弹系统通知（会盖住截图界面），只在托盘菜单里露一行
        if not text:
            return
        if self.overlay is not None:
            log(text)
            return
        self.notify(text)

    # ------------------------------------------------------------ 托盘

    def tray_menu(self) -> list[tuple[str, str, bool]]:
        items = [
            ("立即截图", CMD_SHOT, False),
            (f"延时 {int(self.cfg.get('delay', 3))} 秒截图", CMD_DELAY, False),
            ("-", "", False),
            ("开机自启", CMD_AUTOSTART, bool(self.cfg.get("autostart"))),
            ("优先调用外部截图工具", CMD_PREFER, bool(self.cfg.get("prefer_external"))),
            ("复制后自动存一份", CMD_SAVE,
             str(self.cfg.get("after_capture")) == "copy_save"),
            ("-", "", False),
            ("设置…", CMD_SETTINGS, False),
            ("关于", CMD_ABOUT, False),
            ("退出", CMD_QUIT, False),
        ]
        return items

    def start_tray(self) -> bool:
        if not self.cfg.get("show_tray", True):
            return False
        from . import assets as _assets

        icon = _assets.tray_icon_path()
        self.tray = TrayIcon(
            tooltip=f"SnapCtrlAlt · {self.cfg.get('hotkey', 'ctrl+alt+d')} 截图",
            menu_items=self.tray_menu(),
            on_command=lambda cid: self._on_tray(cid),
            on_left=lambda: self.trigger_shot(),
            icon_path=icon,
        )
        ok = self.tray.start()
        if ok:
            log(f"托盘已就绪（{self.tray.backend}）")
            self.tray.set_menu(self.tray_menu() + [("-", "", False), ("就绪", "noop", False)])
        else:
            log("托盘不可用，改为快捷键/命令行触发")
        return ok

    def refresh_tray(self) -> None:
        if self.tray:
            self.tray.set_menu(self.tray_menu())

    def _on_tray(self, cid: str) -> None:
        if cid in (CMD_SHOT, "noop"):
            if cid == CMD_SHOT:
                self.trigger_shot()
        elif cid == CMD_DELAY:
            self.trigger_shot(float(self.cfg.get("delay", 3)))
        elif cid == CMD_AUTOSTART:
            self._toggle_autostart()
        elif cid == CMD_PREFER:
            self.cfg["prefer_external"] = not self.cfg.get("prefer_external", True)
            self._save_cfg()
            self.refresh_tray()
            self.notify(f"外部截图工具优先已{'开启' if self.cfg['prefer_external'] else '关闭'}")
        elif cid == CMD_SAVE:
            cur = str(self.cfg.get("after_capture", "copy"))
            self.cfg["after_capture"] = "copy" if cur == "copy_save" else "copy_save"
            self._save_cfg()
            self.refresh_tray()
            self.notify("完成后自动存一份已"
                        f"{'开启' if self.cfg['after_capture'] == 'copy_save' else '关闭'}")
        elif cid == CMD_SETTINGS:
            self.show_settings()
        elif cid == CMD_ABOUT:
            self.show_about()
        elif cid == CMD_QUIT:
            self.quit()

    def _toggle_autostart(self) -> None:
        want = not bool(self.cfg.get("autostart"))
        try:
            app_settings.set_autostart(want)
            self.cfg["autostart"] = want
            self._save_cfg()
            self.notify(f"开机自启已{'开启' if want else '关闭'}")
        except RuntimeError as e:
            self.notify(str(e))
        self.refresh_tray()

    def _save_cfg(self) -> None:
        try:
            app_settings.save_settings(self.cfg)
        except RuntimeError as e:
            self.notify(str(e))

    # ------------------------------------------------------------ 设置窗口

    def show_settings(self) -> None:
        if self.settings_win is not None:
            self.settings_win.present()
            return
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_title(APP_TITLE + " · 设置")
        win.set_default_size(460, -1)
        win.set_position(Gtk.WindowPosition.CENTER)
        win.set_icon_name("camera-photo")
        win.connect("destroy", lambda *_: setattr(self, "settings_win", None))
        self.settings_win = win

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        win.add(outer)
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        header.set_border_width(16)
        outer.pack_start(header, False, False, 0)
        title = Gtk.Label()
        title.set_markup(f'<span size="large" weight="bold">{APP_TITLE}</span>')
        title.set_halign(Gtk.Align.START)
        header.pack_start(title, False, False, 0)
        sub = Gtk.Label(label=f"版本 {__version__} · 全局热键截图 → 标注 → 剪贴板")
        sub.set_halign(Gtk.Align.START)
        sub.get_style_context().add_class("dim-label")
        header.pack_start(sub, False, False, 0)

        grid = Gtk.Grid(column_spacing=10, row_spacing=10, border_width=16)
        outer.pack_start(grid, False, False, 0)
        row = [0]

        def add_row(label: str, widget: Gtk.Widget, hint: str | None = None) -> None:
            lab = Gtk.Label(label=label)
            lab.set_halign(Gtk.Align.START)
            grid.attach(lab, 0, row[0], 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, row[0], 1, 1)
            row[0] += 1
            if hint:
                h = Gtk.Label(label=hint)
                h.set_halign(Gtk.Align.START)
                h.set_line_wrap(True)
                h.get_style_context().add_class("dim-label")
                grid.attach(h, 1, row[0], 1, 1)
                row[0] += 1

        # 热键
        ent_hotkey = Gtk.Entry()
        ent_hotkey.set_text(str(self.cfg.get("hotkey", "ctrl+alt+d")))
        add_row("截图热键", ent_hotkey, "形如 ctrl+alt+d、super+print；留空则不注册全局热键")

        ent_quit = Gtk.Entry()
        ent_quit.set_text(str(self.cfg.get("quit_hotkey", "ctrl+alt+shift+q")))
        add_row("退出热键", ent_quit)

        # 自启
        chk_auto = Gtk.CheckButton(label="开机自启（写入 ~/.config/autostart）")
        chk_auto.set_active(bool(self.cfg.get("autostart")))
        grid.attach(chk_auto, 1, row[0], 1, 1)
        row[0] += 1

        chk_tray = Gtk.CheckButton(label="显示托盘图标")
        chk_tray.set_active(bool(self.cfg.get("show_tray", True)))
        grid.attach(chk_tray, 1, row[0], 1, 1)
        row[0] += 1

        chk_notify = Gtk.CheckButton(label="显示桌面通知")
        chk_notify.set_active(bool(self.cfg.get("notify", True)))
        grid.attach(chk_notify, 1, row[0], 1, 1)
        row[0] += 1

        # 外部工具
        chk_ext = Gtk.CheckButton(label="优先调用外部截图工具（Flameshot / Spectacle 等）")
        chk_ext.set_active(bool(self.cfg.get("prefer_external", True)))
        grid.attach(chk_ext, 1, row[0], 1, 1)
        row[0] += 1

        box_ext = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        ent_ext = Gtk.Entry()
        ent_ext.set_text(str(self.cfg.get("external_cmd", "")))
        ent_ext.set_placeholder_text(
            f"自动探测：{capture.find_external_tool() or '未安装'}")
        box_ext.pack_start(ent_ext, True, True, 0)
        add_row("自定义命令", box_ext, "空则自动探测可用的外部截图工具")

        # 完成动作
        combo = Gtk.ComboBoxText()
        for cid, label in (("copy", "复制到剪贴板"),
                           ("copy_save", "复制并另存一份"),
                           ("save", "只保存到文件")):
            combo.append(cid, label)
        cur = str(self.cfg.get("after_capture", "copy"))
        combo.set_active_id(cur if cur in ("copy", "copy_save", "save") else "copy")
        add_row("完成后", combo)

        box_dir = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        ent_dir = Gtk.Entry()
        ent_dir.set_text(str(self.cfg.get("save_dir", "")))
        ent_dir.set_placeholder_text(str(app_settings.pictures_dir()))
        btn_dir = Gtk.Button(label="…")
        box_dir.pack_start(ent_dir, True, True, 0)
        box_dir.pack_start(btn_dir, False, False, 0)
        add_row("保存目录", box_dir)

        spin_delay = Gtk.SpinButton.new_with_range(1, 30, 1)
        spin_delay.set_value(float(self.cfg.get("delay", 3)))
        add_row("延时截图", spin_delay, "托盘「延时截图」用的秒数")

        # 底部按钮
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_border_width(12)
        outer.pack_start(actions, False, False, 0)
        btn_shot = Gtk.Button(label="立即截图")
        btn_reset = Gtk.Button(label="恢复默认")
        btn_close = Gtk.Button(label="关闭")
        actions.pack_start(btn_shot, False, False, 0)
        actions.pack_end(btn_close, False, False, 0)
        actions.pack_end(btn_reset, False, False, 0)

        info = Gtk.Label()
        info.set_markup(
            "<small>配置文件：<tt>{}</tt>\n"
            "自动保存目录：{}</small>".format(
                app_settings.config_path(),
                self.cfg.get("save_dir") or app_settings.pictures_dir()))
        info.set_halign(Gtk.Align.START)
        info.set_line_wrap(True)
        info.set_border_width(16)
        info.get_style_context().add_class("dim-label")
        outer.pack_start(info, False, False, 0)

        def browse(_b) -> None:
            d = Gtk.FileChooserDialog(title="选择保存目录", parent=win,
                                      action=Gtk.FileChooserAction.SELECT_FOLDER)
            d.add_buttons("取消", Gtk.ResponseType.CANCEL, "选择", Gtk.ResponseType.OK)
            if ent_dir.get_text():
                d.set_current_folder(ent_dir.get_text())
            if d.run() == Gtk.ResponseType.OK:
                ent_dir.set_text(d.get_filename() or "")
            d.destroy()

        def apply(_b=None) -> None:
            self.cfg["hotkey"] = ent_hotkey.get_text().strip()
            self.cfg["quit_hotkey"] = ent_quit.get_text().strip()
            self.cfg["prefer_external"] = chk_ext.get_active()
            self.cfg["external_cmd"] = ent_ext.get_text().strip()
            self.cfg["after_capture"] = combo.get_active_id() or "copy"
            self.cfg["save_dir"] = ent_dir.get_text().strip()
            self.cfg["delay"] = int(spin_delay.get_value())
            self.cfg["show_tray"] = chk_tray.get_active()
            self.cfg["notify"] = chk_notify.get_active()
            self.notifier.enabled = chk_notify.get_active()
            want_auto = chk_auto.get_active()
            if want_auto != bool(self.cfg.get("autostart")):
                try:
                    app_settings.set_autostart(want_auto)
                    self.cfg["autostart"] = want_auto
                except RuntimeError as e:
                    self.notify(str(e))
            self._save_cfg()
            self._apply_hotkeys()
            self.refresh_tray()
            self.notify("设置已保存")

        def reset(_b) -> None:
            defaults = app_settings.DEFAULTS
            ent_hotkey.set_text(defaults["hotkey"])
            ent_quit.set_text(defaults["quit_hotkey"])
            chk_ext.set_active(defaults["prefer_external"])
            ent_ext.set_text("")
            combo.set_active_id(defaults["after_capture"])
            ent_dir.set_text("")
            spin_delay.set_value(defaults["delay"])
            chk_tray.set_active(defaults["show_tray"])
            chk_notify.set_active(defaults["notify"])
            chk_auto.set_active(app_settings.autostart_enabled())

        btn_dir.connect("clicked", browse)
        btn_shot.connect("clicked", lambda _b: self.trigger_shot())
        btn_close.connect("clicked", lambda _b: (apply(), win.destroy()))
        btn_reset.connect("clicked", reset)
        win.connect("key-press-event",
                    lambda _w, e: (apply(), win.destroy(), True)[2]
                    if e.keyval == Gdk.KEY_Escape else False)
        win.show_all()

    def show_about(self) -> None:
        dlg = Gtk.MessageDialog(
            transient_for=None, flags=0, message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.CLOSE, text=f"SnapCtrlAlt Linux {__version__}",
        )
        dlg.format_secondary_markup(
            "按 <b>{}</b> 唤起截图，框选标注后进剪贴板。\n"
            "参考 Windows 项目 SnapCtrlAlt 制作（MIT）。\n"
            "界面：GTK3 + Cairo；抓图：X11 (Xlib) / XDG Portal。".format(
                GLib.markup_escape_text(str(self.cfg.get("hotkey", "ctrl+alt+d")))))
        dlg.run()
        dlg.destroy()

    def _show_error(self, msg: str) -> None:
        if self.tray is not None:
            return
        dlg = Gtk.MessageDialog(transient_for=None, flags=0,
                                message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.CLOSE, text="截图失败")
        dlg.format_secondary_text(msg)
        dlg.run()
        dlg.destroy()

    # ------------------------------------------------------------ 热键

    def _apply_hotkeys(self) -> dict:
        hotkey = str(self.cfg.get("hotkey", "") or "").strip()
        quit_key = str(self.cfg.get("quit_hotkey", "") or "").strip()
        if not hotkey:
            self.hotkeys.stop()
            log("未配置截图热键")
            return {}
        self.hotkeys.stop()
        status = self.hotkeys.start(
            on_shot=lambda: self.trigger_shot(),
            on_quit=self.quit,
            hotkey=hotkey, quit_hotkey=quit_key or "ctrl+alt+shift+q",
        )
        for m in self.hotkeys.messages:
            log(m)
        if not status.get("shot"):
            msg = (f"热键 {hotkey} 注册失败，可能已被其他程序占用。"
                   "可在设置里换一个组合，或用 `snap.py --once` 绑到桌面快捷键上。")
            log(msg)
            self.notify(msg)
        else:
            log(f"全局热键已就绪：{hotkey}")
        return status

    # ------------------------------------------------------------ IPC / 退出

    def _on_ipc(self, cmd: str) -> bool:
        log(f"收到命令：{cmd}")
        if cmd == "shot":
            self.trigger_shot()
        elif cmd == "delay":
            self.trigger_shot(float(self.cfg.get("delay", 3)))
        elif cmd == "settings":
            self.show_settings()
        elif cmd == "quit":
            self.quit()
        return False

    def quit(self) -> None:
        log("正在退出…")
        self.hotkeys.stop()
        if self.tray:
            self.tray.stop()
        if self.server:
            self.server.stop()
        if self.overlay is not None:
            try:
                self.overlay.cancel()
            except Exception:  # noqa: BLE001
                pass
        pin_window.close_all()
        Gtk.main_quit()

    def run(self, resident: bool) -> int:
        self.server.start()
        self._apply_hotkeys()
        if resident:
            self.start_tray()
            Gtk.main()
        return 0

    def run_once(self) -> int:
        """--once：截一次就退出（适合绑桌面环境自带的快捷键）。"""
        self._once = True
        self.server.stop()
        self._apply_hotkeys()
        self.trigger_shot()
        Gtk.main()
        return 0

    def _finish_once(self) -> None:
        """--once 模式：覆盖层关掉后把剪贴板数据交付出去再退出。"""
        clipboard_linux.pump(300)
        GLib.timeout_add(150, self._quit_once)

    def _quit_once(self) -> bool:
        Gtk.main_quit()
        return False


# ---------------------------------------------------------------- 自检


def selftest() -> int:
    print(f"SnapCtrlAlt Linux {__version__} 自检")
    print(f"  会话：XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE', '?')} "
          f"DISPLAY={os.environ.get('DISPLAY', '-')} "
          f"WAYLAND={os.environ.get('WAYLAND_DISPLAY', '-')}")
    print(f"  配置：{app_settings.config_path()}")
    rc = 0
    rc |= _check("配置读写", _check_settings)
    rc |= _check("屏幕与抓图", capture.selftest)
    rc |= _check("剪贴板", clipboard_linux.selftest)
    rc |= _check("全局热键", _hotkey_selftest)
    rc |= _check("托盘", _tray_selftest)
    rc |= _check("通知", notify_linux.selftest)
    rc |= _check("贴图", pin_window.selftest)
    rc |= _check("渲染（离屏）", _render_selftest)
    print("自检" + ("通过" if rc == 0 else "存在问题"))
    return rc


def _check(name: str, fn) -> int:
    try:
        rc = fn()
    except Exception:  # noqa: BLE001
        print(f"[自检] {name}：异常\n{traceback.format_exc()}")
        return 1
    return int(rc or 0)


def _check_settings() -> int:
    cfg = app_settings.load_settings()
    path = app_settings.config_path()
    print(f"[自检] 配置：通过（{path}，{len(cfg)} 项）")
    return 0


def _hotkey_selftest() -> int:
    from . import hotkey_linux

    return hotkey_linux.selftest()


def _tray_selftest() -> int:
    from . import tray_linux

    return tray_linux.selftest()


def _render_selftest() -> int:
    """离屏渲染一张假截图并叠加各种标注，验证 Cairo 绘制路径不炸。"""
    img = Image.new("RGB", (800, 600), (30, 60, 90))
    for i in range(0, 800, 40):
        for j in range(0, 600, 40):
            if (i // 40 + j // 40) % 2 == 0:
                for x in range(i, min(800, i + 40)):
                    for y in range(j, min(600, j + 40)):
                        img.putpixel((x, y), (200, 200, 210))
    surf = overlay_mod.pil_to_surface(img)
    out = overlay_mod.surface_to_pil(surf)
    same = out.size == img.size and out.convert("RGB").getpixel((5, 5)) == img.getpixel((5, 5))
    print(f"[自检] 渲染：{'通过' if same else '失败'}（PIL→Cairo→PIL 往返 {out.size[0]}×{out.size[1]}）")
    return 0 if same else 1


def _perf() -> int:
    """--perf：用真实抓图尺寸跑一遍帧耗时基准。"""
    sys.argv = [sys.argv[0]]
    sys.path.insert(0, str(app_settings.project_root() / "tools"))
    try:
        import perf  # type: ignore
    except ImportError:
        print("找不到 tools/perf.py")
        return 1
    try:
        info = capture.virtual_screen_bounds()
        img, real = capture.grab_full_screen(info)
        scale = App(cfg={})._guess_scale(img.width)
        logical_w = max(1, img.width // max(1, scale))
        logical_h = max(1, img.height // max(1, scale))
        print(f"实际抓图 {img.width}×{img.height}，屏幕逻辑尺寸约 {logical_w}×{logical_h}")
        return perf.main([sys.argv[0], str(img.width), str(img.height), str(scale),
                          str(logical_w), str(logical_h)])
    except Exception as e:  # noqa: BLE001
        print(f"无法取得真实屏幕尺寸（{e}），改用 1920×1080 @1x 估算")
        return perf.main([sys.argv[0], "1920", "1080", "1"])


# ---------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="snap.py", description=APP_TITLE)
    parser.add_argument("--once", action="store_true", help="只截一次后退出")
    parser.add_argument("--tray", action="store_true", help="常驻托盘（默认）")
    parser.add_argument("--settings", action="store_true", help="打开设置窗口")
    parser.add_argument("--selftest", action="store_true", help="基础自检，不弹界面")
    parser.add_argument("--local", action="store_true", help="本次强制用内置覆盖层截图")
    parser.add_argument("--delay", type=float, default=0.0, help="延时秒数")
    parser.add_argument("--perf", action="store_true",
                        help="测量覆盖层各交互路径的帧耗时后退出")
    parser.add_argument("--version", action="version", version=f"SnapCtrlAlt {__version__}")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.perf:
        return _perf()

    cfg = app_settings.ensure_config()
    if args.local:
        cfg["force_local"] = True

    # 让状态栏 / 标题里的中文正常显示
    GLib.set_prgname("snapctrlalt")
    GLib.set_application_name("SnapCtrlAlt")

    if not args.once:
        # 已有实例在跑：把命令转过去
        cmd = "settings" if args.settings else ("delay" if args.delay else "shot")
        if send_command(cmd):
            log("已把命令交给正在运行的实例")
            return 0

    if capture.is_wayland() and not capture.has_x11():
        log("检测到纯 Wayland 会话：抓图会走 XDG Portal（可能弹一次授权框），"
            "覆盖层标注仍可用")

    app = App(cfg)
    if args.once:
        return app.run_once() if not args.settings else _settings_only(app)
    if args.settings:
        app.server.start()
        app._apply_hotkeys()
        app.start_tray()
        app.show_settings()
        Gtk.main()
        return 0
    return app.run(resident=True)


def _settings_only(app: App) -> int:
    app._apply_hotkeys()
    app.show_settings()
    Gtk.main()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
