# 更新日志

作者：参考 Windows 版 SnapCtrlAlt（作者 mimo、DeepSeek Harness 与 DannyParticle）。
许可：MIT。版本号与上游保持一致，便于对照功能。

## 1.2.0 — 坐标模型重构 + Debian 打包

**修：框选位置与截取位置不一致（本机 Linux Mint / Cinnamon / X11 实测定位）**

根因是两个「缩放」被混用且口径不同：

| 量 | 不设 GDK_SCALE | 设 GDK_SCALE=1 |
|---|---|---|
| GTK 显示器逻辑几何 | 1440×900（scale 2） | 2880×1800（scale 1） |
| `Gdk.pixbuf_get_from_window` 抓图 | 5760×3600 | 2880×1800 |
| Xlib 抓图 / X11 根窗口 | 2880×1800 | 2880×1800 |

- 这台机器的真实像素是 2880×1800；原先的 5760×3600 不是真分辨率，而是
  `Gdk.pixbuf_get_from_window` 按窗口 scale factor **虚拟放大**出来的。
- 覆盖层窗口的分配尺寸又受同一个 GDK 缩放影响，于是「事件坐标 / 窗口分配 /
  抓图尺寸」三者由不同缩放口径推导 —— 任一处与假设不符就整体错位。
- 现在覆盖层固定在设备像素尺度运行（入口脚本设 `GDK_SCALE=1`），让
  **事件坐标 = 覆盖层窗口 = X11 根窗口 = 抓图像素**，四者同源，只剩一次整数换算。
- 抓图顺序改为 **Xlib 优先**（永远返回根窗口真实像素），GDK 退为回退路径。
- 新增 `src/snapctrlalt/geometry.py`：运行时标定 + 指针自证。开屏后用 Xlib 读到的
  根指针对账 GTK 事件坐标，解出残留的平移与缩放并自动纠正，全过程写进日志；
  修正策略刻意保守，单个异常采样不会污染标定。
- 新增逃生阀 `SNAP_SCALE` / `SNAP_OFFSET_X` / `SNAP_OFFSET_Y`。
- 新增 `tests/test_coords.py`（24 项）：1×/1.5×/2×/3× 倍率换算、边界钳制、
  指针自证的平移/缩放/异常采样、以及真实抓图上的逐像素比对与全屏反查最佳匹配
  偏移（要求为 0）。

**加：UI 独立缩放**

坐标精确的代价是在 HiDPI 屏上按物理像素画 UI 会偏小。现在 UI（工具栏、放大镜、
手柄、提示）按显示器缩放系数单独放大（`ui_scale` 配置，默认 auto，HiDPI 取 2.0），
只影响绘制、不参与任何坐标换算。

**加：Debian 包**

- `packaging/build-deb.sh`：手工 stage + `dpkg-deb --root-owner-group`，
  **不需要 debhelper**；固定 `SOURCE_DATE_EPOCH`，同一份源码可复现出字节一致的 deb；
  构建前自动跑 overlay + coords 测试，测试不过就打不出包。
- `packaging/debian/{control,changelog,copyright,postinst,prerm,postrm}`。
- **装完默认不开机自启**：`postinst` 只刷新图标与 desktop 缓存，
  自启由托盘菜单里的开关决定；`postrm` 清理自启项但保留用户配置与截图。
- 运行期依赖：`python3-gi`、`python3-gi-cairo`、`python3-cairo`、`python3-pil`、
  `gir1.2-gtk-3.0`、`python3-xlib`；Recommends 托盘与通知的 typelib。

**改：目录结构改为与 oled-guard 同构**

`src/snapctrlalt/`（包）、`bin/snapctrlalt`（入口）、`share/{applications,icons}`、
`packaging/`、`tests/`、`tools/`；新增 `src/snapctrlalt/assets.py` 统一资源定位
（源码目录 / `~/.local` / `/usr/share` 三种形态都能找到图标），并修正
`project_root()` 在 `src/` 布局下少算一层的问题。

## 1.1.1 — Linux 首个可用版本

对标 Windows 版 1.1.1 的功能全集，交互、工具集、快捷键、工具栏布局逐一对应。

- 全局热键 `Ctrl+Alt+D` 唤起截图，`Ctrl+Alt+Shift+Q` 退出；注册失败会通知提示，
  并给出「改用 `--once` 绑桌面快捷键」的出路（不让它静默失效）。
- 框选后可拖 8 个手柄调整、在选区内平移、方向键微调；双击 / `Ctrl+A` 选整屏；
  框选时带 8× 放大镜、十字准星、坐标与 HEX 色值读数；选区上方实时尺寸标签。
- 标注工具：矩形、椭圆、箭头、画笔、荧光笔、文字、序号、马赛克、高斯模糊、取色，
  外加撤销 / 重做 / 清空 / 另存为 / 贴图 / 取消 / 完成。工具栏图标全部用 Cairo 自绘
  （不依赖图标字体，缺字体也不会变方框），悬停出中文提示。
- 系统托盘常驻（Ayatana AppIndicator，退回 Gtk.StatusIcon）：立即截图、延时截图、
  开机自启、外部工具优先、复制后自动存一份、设置、关于、退出。
- 贴图：截图钉成置顶小窗，滚轮缩放，右键菜单可复制 / 保存 / 调不透明度。
- 单实例：`$XDG_RUNTIME_DIR/snapctrlalt.sock` 做互斥与命令转发（shot / delay /
  settings / quit），重复启动不会抢热键。
- `--once` 模式适配桌面环境自带的快捷键；`--selftest` 不弹界面做基础自检。

**Linux 适配要点**

- 抓图三级回落：GDK `pixbuf_get_from_window` → Xlib `get_image` → XDG Desktop Portal
  （纯 Wayland 会话自动走 Portal）。任何一级失败都会说明原因，不静默返回黑图。
- HiDPI 三层坐标系（X11 根窗口 / GTK 窗口分配 / 抓图像素，本机 2880×1800、
  1440×900、5760×3600 三者互不相同）启动时按 X11 根几何标定，导出全分辨率原图。
  *修：早期版本用 GTK scale factor 反推，导致框选区域与输出尺寸对不上。*
- 多显示器按 Xinerama / RandR 取虚拟屏并集，覆盖层铺满整块虚拟屏。
- 键盘焦点：显式 `_NET_ACTIVE_WINDOW`（`present()`）+ X11 `set_input_focus` 兜底，
  并带重试与「弹对话框/输入文字时让出焦点」的保护。
  *修：只 `show_all()` 时窗口能收鼠标但拿不到键盘焦点，Enter/Esc 会跑到别的程序。*
- `finish()` / `save()` 幂等：一次 Enter 可能同时触发 Entry 的 `activate` 与窗口的
  `key-press-event`，重复执行会重复写剪贴板、重复存盘。
- 剪贴板写 `image/png` 并调用 `store()`，有剪贴板管理器时退出程序后仍可粘贴。
- 外部工具分流（对标「优先 QQ 截图」）：探测 Flameshot / Spectacle / gnome-screenshot /
  xfce4-screenshooter / ksnip / scrot / maim / ImageMagick import，命中即交给它，
  失败回落内置覆盖层。

**性能**（本机 2880×1800 @2x，抓图 5760×3600，40 帧实测）

| 路径 | 平均 | 约合 |
|------|------|------|
| 悬停 / 放大镜 | 0.7 ms | 1400+ fps |
| 拖框创建 | 1.1 ms | 870 fps |
| 拖动选区 | 2.0 ms | 500 fps |
| 标注态（8 个标注 + 工具栏） | 2.1 ms | 490 fps |

优化过程留痕：初版每帧把 5760² 整屏重采样一次，四条路径都在 10–14 ms；
改为「画布按事件坐标建 + 1:1 绘制」后降到 2 ms 上下，再缓存工具栏图标与
马赛克/模糊效果图，稳定在 60 fps 预算（16.7 ms）的 1/8 以内。

**测试**

- `tests/test_overlay.py`：54 项界面回归（离屏驱动真实覆盖层对象，覆盖框选、
  9 种标注、撤销/重做/清空、文字输入、裁切、手柄、平移、存盘、剪贴板、贴图、取消）。
- `tests/test_gui_e2e.py`：22 项端到端（真窗口 + XTEST 真鼠标拖框 + 独立进程读剪贴板
  + Esc 取消 + 托盘/热键/IPC 状态），自带屏幕截图取证。
- `tools/perf.py` / `snap.py --perf`：交互路径帧耗时基准。
- `tools/diagnose.py`：环境能力诊断。
