# 更新日志

作者：参考 Windows 版 SnapCtrlAlt（作者 mimo、DeepSeek Harness 与 DannyParticle）。
许可：MIT。版本号与上游保持一致，便于对照功能。

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
