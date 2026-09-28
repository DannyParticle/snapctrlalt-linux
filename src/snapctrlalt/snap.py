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
from . import geometry  # noqa: E402
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
        self.editor = None
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

    def resolved_save_dir(self, persist: bool = False) -> Path:
        """解析保存目录，路径不可用时自动回退。

        配置里存的是绝对路径，把配置拷到另一台电脑就会指向不存在的目录
        （比如老机器的 /home/某人/图片）。这时不再去「创建」一个陌生的
        绝对路径，而是回退到本机的系统图片目录，并把配置修正回来。
        """
        raw = str(self.cfg.get("save_dir", "") or "").strip()
        if not raw:
            return app_settings.pictures_dir()
        p = Path(raw).expanduser()
        if p.is_dir():
            return p
        # 被删掉或换机器了：回退到系统图片目录
        fallback = app_settings.pictures_dir()
        self.notify(f"保存目录 {p} 不存在，已改为 {fallback}")
        if persist:
            self.cfg["save_dir"] = ""
            self._save_cfg()
        return fallback

    def save_image(self, img: Image.Image, silent: bool = False) -> str | None:
        """按配置把图存进保存目录（自动文件名）。"""
        fmt = str(self.cfg.get("save_format", "png")).lower()
        ext = "jpg" if fmt in ("jpg", "jpeg") else fmt
        d = self.resolved_save_dir(persist=True)
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
        start = Path(str(self.cfg.get("last_save_dir") or "")).expanduser()
        if not start.is_dir():
            start = self.resolved_save_dir()
        try:
            dlg.set_current_folder(str(start))
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

    def open_editor(self, img: Image.Image) -> None:
        """打开编辑器窗口（方案三）。完成/取消后回到托盘。"""
        from . import toolbar as tb_mod
        from .overlay import draw_icon

        def on_commit(result: Image.Image, action: str) -> None:
            self._shot_busy = False
            try:
                if action == "save":
                    path = self.ask_save_path()
                    if not path:
                        return
                    self.write_image(result, path)
                    self.notify(f"已保存 {path}")
                else:
                    from . import clipboard_linux

                    clipboard_linux.copy_image(
                        result, primary=bool(self.cfg.get("copy_to_primary")))
                    self.notify(f"已复制到剪贴板 {result.width}×{result.height}")
                    if str(self.cfg.get("after_capture")) == "copy_save":
                        saved = self.save_image(result, silent=True)
                        if saved:
                            self.notify(f"已保存 {saved}")
            except Exception as e:  # noqa: BLE001
                self.notify(f"处理失败：{e}")

        def on_cancel() -> None:
            self._shot_busy = False
            log("编辑器已取消")

        scale = getattr(self, "_last_ui_scale", 2.0)
        try:
            scale = float(self.cfg.get("ui_scale") or 2.0)
        except (TypeError, ValueError):
            pass
        from .overlay import _detect_ui_scale

        self.editor = tb_mod.EditorWindow(
            img, on_commit=on_commit, on_cancel=on_cancel,
            ui_scale=_detect_ui_scale(self.cfg), icon_painter=draw_icon)
        self.editor.show()
        self._shot_busy = True
        del scale

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
        geo = geometry.measure((img.width, img.height), gdk_scale=self._gdk_scale())
        log(f"抓图完成 {img.width}×{img.height}（{time.perf_counter() - then:.2f}s）")
        log("坐标标定 " + geo.describe().replace("\n", "\n         "))
        for problem in geo.check():
            log(f"坐标警告：{problem}")
        self._open_overlay(img, boxes, geo)

    @staticmethod
    def _gdk_scale() -> int:
        """GTK 自己报的缩放，仅用于日志与 `--selftest` 对照，不参与换算。"""
        try:
            w = Gtk.Window(type=Gtk.WindowType.POPUP)
            w.realize()
            gw = w.get_window()
            scale = int(gw.get_scale_factor()) if gw is not None else 0
            w.destroy()
            return scale
        except Exception:  # noqa: BLE001
            return 0

    def _effective_toolbar_mode(self) -> str:
        """本次要用的工具栏形态：一次性覆盖优先，其次配置。"""
        once = str(self.cfg.get("_toolbar_mode_once", "") or "").lower()
        if once in ("canvas", "editor"):
            return once
        return str(self.cfg.get("toolbar_mode", "canvas") or "canvas").lower()

    def _open_overlay(self, img: Image.Image, boxes, geo) -> None:
        if self.overlay is not None:
            try:
                self.overlay.cancel()
            except Exception:  # noqa: BLE001
                pass
            self.overlay = None
        try:
            from .overlay import _detect_ui_scale
            _raw = (self.cfg or {}).get("ui_scale", "<缺键>")
            try:
                from gi.repository import Gdk as _G
                from . import capture_linux as _cap
                _rw, _rh = _cap.root_geometry()
                _d = _G.Display.get_default()
                _mons = [( _d.get_monitor(i).get_geometry().width,
                           _d.get_monitor(i).get_scale_factor())
                         for i in range(_d.get_n_monitors())] if _d else None
                print(f"ui_scale 调试: 配置={_raw!r} 结果={_detect_ui_scale(self.cfg or {})} "
                      f"root={_rw}x{_rh} 显示器(逻辑宽,scale)={_mons} "
                      f"GDK_SCALE={os.environ.get('GDK_SCALE')}", flush=True)
            except Exception as _e:
                print(f"ui_scale 调试失败: {_e}", flush=True)
            self.overlay = ShotOverlay(
                app=self, screen_img=img, screen_box=boxes.box,
                on_close=self._on_overlay_close, status_cb=self._status,
                geo=geo, logical_size=self._logical_screen_size(),
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
        if reason == "editor":
            return                       # 交给编辑器窗口继续，别当作结束
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

    # ------------------------------------------------------------ 设置窗口

    def _hotkey_capture(self, label: str, current: str, on_apply) -> Gtk.Box:
        """一行热键设置：输入框 + 「录制」按钮（按下组合键即捕获）。

        手写热键容易写错（``ctrl+alt+` 之类）且没人会记得语法，所以给一个
        录制按钮：点一下，然后直接按想用的组合键。
        """
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        entry = Gtk.Entry()
        entry.set_text(current)
        entry.set_width_chars(18)
        btn = Gtk.Button(label="录制")
        btn.set_tooltip_text("点一下，然后按下想要的组合键（Esc 取消）")
        state = {"recording": False, "handler": 0}

        def stop() -> None:
            state["recording"] = False
            btn.set_label("录制")
            if state["handler"]:
                win = btn.get_toplevel()
                if isinstance(win, Gtk.Window) and state["handler"]:
                    win.disconnect(state["handler"])
                state["handler"] = 0

        def on_key(_w, event) -> bool:
            if not state["recording"]:
                return False
            kv = event.keyval
            if kv == Gdk.KEY_Escape:
                stop()
                return True
            name = Gdk.keyval_name(kv) or ""
            mods = []
            if event.state & Gdk.ModifierType.CONTROL_MASK:
                mods.append("ctrl")
            if event.state & Gdk.ModifierType.MOD1_MASK:
                mods.append("alt")
            if event.state & Gdk.ModifierType.SHIFT_MASK:
                mods.append("shift")
            if event.state & Gdk.ModifierType.SUPER_MASK:
                mods.append("super")
            key = name.lower()
            if key in ("control_l", "control_r", "alt_l", "alt_r", "shift_l",
                       "shift_r", "super_l", "super_r", "meta_l", "meta_r",
                       "iso_level3_shift", "caps_lock", "num_lock"):
                return True                      # 只按了修饰键，继续等主键
            if not mods:
                return True                      # 全局热键至少要一个修饰键
            entry.set_text("+".join(mods + [key]))
            stop()
            on_apply(entry.get_text())
            return True

        def start(_b) -> None:
            if state["recording"]:
                stop()
                return
            state["recording"] = True
            btn.set_label("请按键…")
            top = btn.get_toplevel()
            if isinstance(top, Gtk.Window):
                state["handler"] = top.connect("key-press-event", on_key)

        btn.connect("clicked", start)
        box.pack_start(entry, True, True, 0)
        box.pack_start(btn, False, False, 0)
        box.set_tooltip_text(f"{label}：也可以直接手写，例如 ctrl+alt+d、super+print")
        return box

    def show_settings(self) -> None:
        if self.settings_win is not None:
            self.settings_win.present()
            return
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_title(APP_TITLE + " · 设置")
        win.set_default_size(620, 660)
        win.set_position(Gtk.WindowPosition.CENTER)
        win.set_icon_name("camera-photo")
        win.connect("destroy", lambda *_: setattr(self, "settings_win", None))
        self.settings_win = win

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        win.add(outer)

        # ---- 标题 ----
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        header.set_margin_top(12)
        header.set_margin_bottom(8)
        header.set_margin_start(16)
        header.set_margin_end(16)
        outer.pack_start(header, False, False, 0)
        title = Gtk.Label()
        title.set_markup(f'<span size="large" weight="bold">{APP_TITLE}</span>')
        title.set_halign(Gtk.Align.START)
        header.pack_start(title, False, False, 0)
        sub = Gtk.Label(label=f"版本 {__version__} · 全局热键截图 → 标注 → 剪贴板")
        sub.set_halign(Gtk.Align.START)
        sub.get_style_context().add_class("dim-label")
        header.pack_start(sub, False, False, 0)

        # ---- 分页 ----
        # 设置项一多，单页会拉得很长（一屏放不下、找不到东西），所以按用途分栏。
        nb = Gtk.Notebook()
        nb.set_margin_start(12)
        nb.set_margin_end(12)
        # 限制高度：否则内容会把窗口撑到一千多像素高（一屏放不下）。
        # 每页内容超出时在该页内滚动。
        nb.set_size_request(580, 470)
        outer.pack_start(nb, True, True, 0)

        def make_page(tab_label: str) -> Gtk.Box:
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            inner.set_border_width(6)
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.add_with_viewport(inner)
            nb.append_page(scroller, Gtk.Label(label=tab_label))
            scroller.get_vadjustment().set_value(0)
            return inner

        def add_section(page: Gtk.Box, text: str) -> tuple[Gtk.Grid, list[int]]:
            """在页内加一个小节；返回 (网格, 行号计数器)。"""
            lab = Gtk.Label()
            lab.set_markup(f'<b>{GLib.markup_escape_text(text)}</b>')
            lab.set_halign(Gtk.Align.START)
            lab.set_margin_top(6)
            lab.set_margin_bottom(2)
            page.pack_start(lab, False, False, 0)
            g = Gtk.Grid(column_spacing=12, row_spacing=8)
            g.set_margin_bottom(4)
            page.pack_start(g, False, False, 0)
            return g, [0]

        def add_row(grid: Gtk.Grid, row: list, label: str, widget: Gtk.Widget,
                    hint: str | None = None) -> None:
            lab = Gtk.Label(label=label)
            lab.set_halign(Gtk.Align.START)
            lab.set_valign(Gtk.Align.CENTER)
            grid.attach(lab, 0, row[0], 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, row[0], 1, 1)
            row[0] += 1
            if hint:
                h = Gtk.Label(label=hint)
                h.set_halign(Gtk.Align.START)
                h.set_line_wrap(True)
                h.set_xalign(0)
                h.get_style_context().add_class("dim-label")
                grid.attach(h, 1, row[0], 1, 1)
                row[0] += 1

        def add_check(grid: Gtk.Grid, row: list, widget: Gtk.CheckButton) -> None:
            grid.attach(widget, 1, row[0], 1, 1)
            row[0] += 1

        # ================= 第 1 页：热键与截图 =================
        page1 = make_page("热键与截图")

        g1, r1 = add_section(page1, "全局热键")
        holder = Gtk.Box()          # 占位，稍后换成带「录制」按钮的行
        add_row(g1, r1, "截图热键", holder,
                "点「录制」后直接按组合键；留空则不注册全局热键（可改用 --once）")
        ent_quit = Gtk.Entry()
        ent_quit.set_text(str(self.cfg.get("quit_hotkey", "ctrl+alt+shift+q")))
        add_row(g1, r1, "退出热键", ent_quit)

        lbl_hotkey_status = Gtk.Label(label="")
        lbl_hotkey_status.set_halign(Gtk.Align.START)
        lbl_hotkey_status.set_xalign(0)
        lbl_hotkey_status.set_line_wrap(True)
        lbl_hotkey_status.get_style_context().add_class("dim-label")
        g1.attach(lbl_hotkey_status, 1, r1[0], 1, 1)
        r1[0] += 1

        g2, r2 = add_section(page1, "截完图之后")
        note = Gtk.Label(label="截图始终会复制到剪贴板；下面决定要不要顺便存一份文件。")
        note.set_halign(Gtk.Align.START)
        note.set_xalign(0)
        note.set_line_wrap(True)
        note.get_style_context().add_class("dim-label")
        g2.attach(note, 1, r2[0], 1, 1)
        r2[0] += 1

        combo = Gtk.ComboBoxText()
        for cid, label in (("copy", "只复制到剪贴板（推荐）"),
                           ("copy_save", "复制到剪贴板，同时另存一份"),
                           ("save", "只另存为文件，不复制")):
            combo.append(cid, label)
        cur = str(self.cfg.get("after_capture", "copy"))
        combo.set_active_id(cur if cur in ("copy", "copy_save", "save") else "copy")
        add_row(g2, r2, "同时", combo)

        box_dir = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        ent_dir = Gtk.Entry()
        ent_dir.set_text(str(self.cfg.get("save_dir", "")))
        ent_dir.set_placeholder_text(
            f"留空 = 系统图片目录（当前：{self.resolved_save_dir()}）")
        btn_dir = Gtk.Button(label="选择…")
        box_dir.pack_start(ent_dir, True, True, 0)
        box_dir.pack_start(btn_dir, False, False, 0)
        add_row(g2, r2, "保存目录", box_dir)

        combo_fmt = Gtk.ComboBoxText()
        for cid, label in (("png", "PNG（无损，推荐）"), ("jpg", "JPEG")):
            combo_fmt.append(cid, label)
        fmt = str(self.cfg.get("save_format", "png")).lower()
        combo_fmt.set_active_id("jpg" if fmt in ("jpg", "jpeg") else "png")
        add_row(g2, r2, "保存格式", combo_fmt)

        spin_delay = Gtk.SpinButton.new_with_range(1, 30, 1)
        spin_delay.set_value(float(self.cfg.get("delay", 3)))
        add_row(g2, r2, "延时截图", spin_delay, "托盘里「延时截图」用的秒数")

        g3, r3 = add_section(page1, "外部截图工具")
        chk_ext = Gtk.CheckButton(label="优先交给外部工具（Flameshot / Spectacle 等）")
        chk_ext.set_active(bool(self.cfg.get("prefer_external", True)))
        add_check(g3, r3, chk_ext)
        ent_ext = Gtk.Entry()
        detected = capture.find_external_tool()
        ent_ext.set_text(str(self.cfg.get("external_cmd", "")))
        ent_ext.set_placeholder_text(f"自动探测：{detected or '未安装'}")
        add_row(g3, r3, "自定义命令", ent_ext, "留空则自动探测；填了就优先用它")

        # ================= 第 2 页：界面与启动 =================
        page2 = make_page("界面与启动")

        g4, r4 = add_section(page2, "外观")
        combo_ui = Gtk.ComboBoxText()
        for cid, label in (("auto", "自动（HiDPI 屏自动放大）"), ("1", "1.0×"),
                           ("1.25", "1.25×"), ("1.5", "1.5×"), ("2", "2.0×")):
            combo_ui.append(cid, label)
        uis = str(self.cfg.get("ui_scale", "auto")).lower()
        combo_ui.set_active_id(uis if uis in ("auto", "1", "1.25", "1.5", "2") else "auto")
        add_row(g4, r4, "界面缩放", combo_ui, "只影响工具栏/放大镜的大小，不影响截图")

        combo_tb = Gtk.ComboBoxText()
        for cid, label in (("canvas", "方案① 画在覆盖层上（紧贴选区，推荐）"),
                           ("editor", "方案③ 选完区域后开编辑器窗口")):
            combo_tb.append(cid, label)
        cur_tb = str(self.cfg.get("toolbar_mode", "canvas") or "canvas").lower()
        combo_tb.set_active_id(cur_tb if cur_tb in ("canvas", "editor") else "canvas")
        add_row(g4, r4, "工具栏形态", combo_tb,
                "覆盖层里按 Ctrl+E 也可以在两者之间即时切换")

        # 一键切到另一个方案（只改本次运行时的形态，不动上面的默认值）
        box_switch = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_to_editor = Gtk.Button(label="临时切到方案③")
        btn_to_canvas = Gtk.Button(label="临时切到方案①")
        box_switch.pack_start(btn_to_editor, False, False, 0)
        box_switch.pack_start(btn_to_canvas, False, False, 0)
        add_row(g4, r4, "切换", box_switch,
                "只对接下来这次截图生效，默认值保持不变（改默认请用上面的下拉）")

        # 方案②的情况写清楚，并给出调试入口（调试勾选在下面）
        mask_note = Gtk.Label()
        mask_note.set_markup(
            "<small>方案②（独立窗口浮在遮罩上）<b>不可用</b>：实测窗口既不显示、"
            "也收不到点击 —— 窗口管理器不允许后台程序把窗口提到活动窗口之上。"
            "代码保留，需勾选下面的「调试」项才会启用，见 README。</small>")
        mask_note.set_halign(Gtk.Align.START)
        mask_note.set_xalign(0)
        mask_note.set_line_wrap(True)
        mask_note.get_style_context().add_class("dim-label")
        g4.attach(mask_note, 1, r4[0], 1, 1)
        r4[0] += 1

        g5, r5 = add_section(page2, "剪贴板")
        chk_primary = Gtk.CheckButton(label="同时写入 PRIMARY 选区（中键粘贴）")
        chk_primary.set_active(bool(self.cfg.get("copy_to_primary", False)))
        add_check(g5, r5, chk_primary)

        g6, r6 = add_section(page2, "启动与提醒")
        chk_auto = Gtk.CheckButton(label="开机自启")
        chk_auto.set_active(bool(self.cfg.get("autostart")))
        add_check(g6, r6, chk_auto)
        chk_tray = Gtk.CheckButton(label="显示托盘图标")
        chk_tray.set_active(bool(self.cfg.get("show_tray", True)))
        add_check(g6, r6, chk_tray)
        chk_notify = Gtk.CheckButton(label="显示桌面通知")
        chk_notify.set_active(bool(self.cfg.get("notify", True)))
        add_check(g6, r6, chk_notify)

        g7, r7 = add_section(page2, "调试")
        chk_mask_dbg = Gtk.CheckButton(
            label="启用方案二（独立窗口浮在遮罩上，已知不可用）")
        chk_mask_dbg.set_active(bool(self.cfg.get("toolbar_mask_debug", False)))
        chk_mask_dbg.set_tooltip_text(
            "该形态实测既不显示也收不到点击（窗口管理器不允许后台程序把窗口提到"
            "活动窗口之上）。打开只为复现该问题，需配合工具栏形态使用。")
        add_check(g7, r7, chk_mask_dbg)

        # ================= 第 3 页：关于 =================
        page3 = make_page("关于")
        about = Gtk.Label()
        about.set_markup(
            f"<b>{GLib.markup_escape_text(APP_TITLE)}</b>\n"
            f"版本 {__version__} · MIT 许可\n\n"
            "交互与功能设计来自 Windows 版 SnapCtrlAlt\n"
            "（作者 mimo、DeepSeek Harness 与 DannyParticle）。\n"
            "本仓库是其 Linux 移植：覆盖层用 GTK3 + Cairo 绘制，\n"
            "抓图走 X11（Xlib），纯 Wayland 会话回落到 XDG Portal。")
        about.set_halign(Gtk.Align.START)
        about.set_xalign(0)
        about.set_line_wrap(True)
        about.set_margin_top(10)
        about.set_margin_bottom(8)
        page3.pack_start(about, False, False, 0)

        paths = Gtk.Label()
        paths.set_markup(
            "<small>配置文件 <tt>{}</tt>\n自动保存目录 {}\n"
            "日志与诊断：<tt>snapctrlalt --selftest</tt> / "
            "<tt>python3 tools/diagnose.py</tt></small>".format(
                GLib.markup_escape_text(str(app_settings.config_path())),
                GLib.markup_escape_text(
                    str(self.cfg.get("save_dir") or app_settings.pictures_dir()))))
        paths.set_halign(Gtk.Align.START)
        paths.set_xalign(0)
        paths.set_line_wrap(True)
        paths.get_style_context().add_class("dim-label")
        page3.pack_start(paths, False, False, 0)

        # ================= 底部按钮（所有页共用）=================
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_margin_top(10)
        actions.set_margin_bottom(10)
        actions.set_margin_start(12)
        actions.set_margin_end(12)
        outer.pack_start(actions, False, False, 0)
        btn_shot = Gtk.Button(label="立即截图")
        btn_save = Gtk.Button(label="保存")
        btn_reset = Gtk.Button(label="恢复默认")
        btn_close = Gtk.Button(label="关闭")
        btn_save.get_style_context().add_class("suggested-action")
        for b in (btn_shot, btn_reset):
            actions.pack_start(b, False, False, 0)
        actions.pack_end(btn_close, False, False, 0)
        actions.pack_end(btn_save, False, False, 0)

        # ---- 回调 ----
        def refresh_hotkey_status(*_a) -> None:
            got = dict(self.hotkeys.status)
            if not got:
                lbl_hotkey_status.set_text("尚未注册（保存后生效）")
                return
            ok = got.get("shot")
            lbl_hotkey_status.set_text(
                f"当前状态：截图热键 {'已生效 ✓' if ok else '注册失败（可能被别的程序占用）'}"
                f"，退出热键 {'已生效 ✓' if got.get('quit') else '未生效'}")

        def browse(_b) -> None:
            d = Gtk.FileChooserDialog(title="选择保存目录", parent=win,
                                      action=Gtk.FileChooserAction.SELECT_FOLDER)
            d.add_buttons("取消", Gtk.ResponseType.CANCEL, "选择", Gtk.ResponseType.OK)
            if ent_dir.get_text():
                d.set_current_folder(ent_dir.get_text())
            if d.run() == Gtk.ResponseType.OK:
                ent_dir.set_text(d.get_filename() or "")
            d.destroy()

        def validate_hotkey(text: str) -> str | None:
            """返回错误说明；None 表示可用。"""
            text = (text or "").strip()
            if not text:
                return None
            try:
                from .hotkey_linux import parse_hotkey, keysym_of

                mods, key = parse_hotkey(text)
                keysym_of(key)
                if mods == 0:
                    return "至少要带一个修饰键（ctrl / alt / shift / super）"
            except ValueError as e:
                return str(e)
            return None

        def apply(_b=None) -> bool:
            hk = ent_hotkey.get_text().strip()
            qk = ent_quit.get_text().strip()
            for name, val in (("截图热键", hk), ("退出热键", qk)):
                err = validate_hotkey(val)
                if err:
                    self._show_error(f"{name} 无效：{err}")
                    return False
            if hk and qk and hk == qk:
                self._show_error("截图热键与退出热键不能相同")
                return False

            self.cfg["hotkey"] = hk
            self.cfg["quit_hotkey"] = qk
            self.cfg["prefer_external"] = chk_ext.get_active()
            self.cfg["external_cmd"] = ent_ext.get_text().strip()
            self.cfg["after_capture"] = combo.get_active_id() or "copy"
            self.cfg["save_dir"] = ent_dir.get_text().strip()
            self.cfg["save_format"] = combo_fmt.get_active_id() or "png"
            self.cfg["delay"] = int(spin_delay.get_value())
            self.cfg["ui_scale"] = combo_ui.get_active_id() or "auto"
            self.cfg["toolbar_mode"] = combo_tb.get_active_id() or "canvas"
            self.cfg["toolbar_mask_debug"] = chk_mask_dbg.get_active()
            self.cfg["show_tray"] = chk_tray.get_active()
            self.cfg["notify"] = chk_notify.get_active()
            self.cfg["copy_to_primary"] = chk_primary.get_active()
            self.notifier.enabled = chk_notify.get_active()

            want_auto = chk_auto.get_active()
            if want_auto != bool(self.cfg.get("autostart")):
                try:
                    app_settings.set_autostart(want_auto)
                    self.cfg["autostart"] = want_auto
                except RuntimeError as e:
                    self._show_error(str(e))
            self._save_cfg()
            self._apply_hotkeys()
            refresh_hotkey_status()
            self.refresh_tray()
            self.notify("设置已保存")
            return True

        def reset(_b) -> None:
            d = app_settings.DEFAULTS
            ent_hotkey.set_text(d["hotkey"])
            ent_quit.set_text(d["quit_hotkey"])
            chk_ext.set_active(d["prefer_external"])
            ent_ext.set_text("")
            combo.set_active_id(d["after_capture"])
            ent_dir.set_text("")
            combo_fmt.set_active_id(d["save_format"])
            spin_delay.set_value(d["delay"])
            combo_ui.set_active_id(d["ui_scale"])
            combo_tb.set_active_id(d["toolbar_mode"])
            chk_mask_dbg.set_active(d["toolbar_mask_debug"])
            chk_tray.set_active(d["show_tray"])
            chk_notify.set_active(d["notify"])
            chk_primary.set_active(d["copy_to_primary"])
            chk_auto.set_active(app_settings.autostart_enabled())

        def switch_once(mode: str) -> None:
            """临时切换形态：只影响下一次截图，不改保存的默认值。"""
            self.cfg["_toolbar_mode_once"] = mode
            combo_tb.set_active_id(mode)
            names = {"canvas": "方案①（覆盖层上）", "editor": "方案③（编辑器窗口）"}
            self.notify(f"下一次截图将使用{names.get(mode, mode)}"
                        "（默认值未改，按「保存」会写入下拉里的选择）")

        btn_to_editor.connect("clicked", lambda _b: switch_once("editor"))
        btn_to_canvas.connect("clicked", lambda _b: switch_once("canvas"))
        btn_dir.connect("clicked", browse)
        btn_shot.connect("clicked", lambda _b: self.trigger_shot())
        btn_save.connect("clicked", lambda _b: apply())
        btn_reset.connect("clicked", reset)
        btn_close.connect("clicked", lambda _b: win.destroy())
        win.connect("key-press-event", lambda _w, e: (win.destroy(), True)[1]
                    if e.keyval == Gdk.KEY_Escape else False)

        # 用带「录制」按钮的行替换占位（截图热键是最常改的一项）
        hk_box = self._hotkey_capture("截图热键", str(self.cfg.get("hotkey", "")),
                                      lambda t: None)
        g1.remove(holder)
        g1.attach(hk_box, 1, 0, 1, 1)
        ent_hotkey = hk_box.get_children()[0]

        win.show_all()
        win.present()
        refresh_hotkey_status()

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
        elif cmd == "dbgtb":
            o = self.overlay
            if o is not None and getattr(o, "_tb_pos", None):
                bx, by, bw, bh = o._tb_pos
                print(f"TBGEO 工具栏=({bx:.0f},{by:.0f},{bw:.0f}x{bh:.0f}) "
                      f"画布={o.cr_w}x{o.cr_h} ui={o.ui_scale}", flush=True)
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
            # 启动通知：让用户知道程序已经在托盘里跑起来了。
            # 桌面通知会自己淡出（由通知服务控制时长），这里给 4 秒。
            hotkey = str(self.cfg.get("hotkey", "") or "").strip() or "（未设置）"
            GLib.timeout_add(600, lambda: (
                self.notifier.notify(
                    f"已启动并常驻托盘 · 按 {hotkey} 截图",
                    title="SnapCtrlAlt 已启动", icon="camera-photo"),
                False)[1])
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
    rc |= _check("坐标标定", _geometry_selftest)
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


def _geometry_selftest() -> int:
    """标定 + 指针实测：把「事件坐标 → 根坐标」这条链验证一遍。"""
    from . import geometry as geo_mod

    info = capture.virtual_screen_bounds()
    img, real = capture.grab_full_screen(info)
    ge = geo_mod.measure((img.width, img.height), gdk_scale=App._gdk_scale())
    print("[自检] 坐标标定：" + ge.describe().replace("\n", "\n           "))
    problems = ge.check()
    for p in problems:
        print(f"[自检] 坐标警告：{p}")
    env_scale = os.environ.get("GDK_SCALE", "未设置")
    print(f"[自检] GDK_SCALE={env_scale}（覆盖层要求固定在 1，避免窗口被额外缩放）")
    if ge.win_w != ge.root_w:
        return 1
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
        geo = geometry.measure((img.width, img.height), gdk_scale=App._gdk_scale())
        scale = int(round(geo.zoom_x)) or 1
        print(f"实际抓图 {img.width}×{img.height}，根窗口 {geo.root_w}×{geo.root_h}"
              f"（缩放 x{scale}）")
        return perf.main([sys.argv[0], str(img.width), str(img.height), str(scale),
                          str(geo.root_w), str(geo.root_h)])
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
