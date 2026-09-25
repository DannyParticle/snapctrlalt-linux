# 进度存档 — 坐标偏移修复 + deb 打包

> 断点续作说明。最后更新：本次会话「先保存当前工作进度」时。

## 一、当前状态

| 项 | 状态 |
|---|---|
| 目录重构为 oled-guard 同构（`src/` `bin/` `share/` `packaging/`） | ✅ 已完成 |
| 资源路径解析（`assets.py`，源码 / `~/.local` / deb 三种形态） | ✅ 已完成 |
| 新入口 `bin/snapctrlalt`（固定 `GDK_SCALE=1`） | ✅ 已完成 |
| 新启动脚本 `snapctrlalt.sh` | ✅ 已完成 |
| `.gitignore` / `.gitattributes` | ✅ 已完成 |
| `src/snapctrlalt/geometry.py`（标定 + 自证 + 纠正） | 🟡 已写，**尚未接入覆盖层** |
| 覆盖层改用新坐标系 | ⬜ 未开始 |
| `packaging/build-deb.sh` + `packaging/debian/*` | ⬜ 未开始 |
| `install.sh` 适配新结构 | ⬜ 未开始 |
| README / CHANGELOG 更新 | ⬜ 未开始 |
| 回归测试适配新结构 | ⬜ 未开始（测试文件仍在 `tests/`，路径引用已失效） |

**已知暂时损坏**：`tests/*.py`、`tools/perf.py`、`tools/diagnose.py` 里还有
`ROOT/..`、`sys.path.insert(ROOT)` 这类旧布局假设，重构后需要一并改。

## 二、偏移问题的根因（已确认）

本机实测（Linux Mint / Cinnamon / X11）：

| 量 | 不设 GDK_SCALE | 设 GDK_SCALE=1 |
|---|---|---|
| GTK 显示器逻辑几何 | 1440×900，scale=2 | 2880×1800，scale=1 |
| 普通窗口 scale_factor | 2 | 1 |
| `Gdk.pixbuf_get_from_window` 抓图 | **5760×3600** | 2880×1800 |
| `Xlib` 抓图 | 2880×1800 | 2880×1800 |
| X11 根窗口 | 2880×1800 | 2880×1800 |

结论：

1. **这台机器的真实像素就是 2880×1800**。原先的 5760×3600 不是真分辨率，
   而是 `Gdk.pixbuf_get_from_window` 按窗口 scale factor **虚拟放大**出来的。
2. 覆盖层窗口的分配尺寸同样受 GDK 缩放影响，于是「事件坐标 / 窗口分配 /
   抓图尺寸」三者由**不同的缩放口径**推导，任一处与假设不符就整体错位——
   这正是「圈的位置和截的位置不是一个位置」的来源。
3. 修复方向：把覆盖层固定在设备像素尺度（`GDK_SCALE=1`），让
   **事件坐标 = 窗口分配 = X11 根 = 抓图像素**，四个量同源，换算只剩一次。

## 三、下一步要做的修改（按顺序）

1. **接入 `geometry.py`**：覆盖层构造时用 `geometry.measure(img.size)` 得到标定，
   `canvas = root`、`zoom = img/root`（本机会是 1.0）；把 `_pos()` 换成
   `geo.ptr_to_root()`，`_to_img()` 换成 `geo.ptr_to_img()`，导出用
   `geo.rect_root_to_img()`；绘制不再做任何浮点缩放（`k` 恒为 1）。
2. **运行时自证**：第一次 `motion` 时把 GTK 事件坐标与 `geometry.root_pointer()`
   对账，不一致就写回修正并记日志；`--selftest` 增加标定用例，打印完整几何。
3. **抓图顺序**：`grab_full_screen` 改为 **Xlib 优先**（永远返回真实根像素），
   GDK 退为回退路径——避免再被 GDK 的缩放口径影响。
4. **逃生阀**：`SNAP_SCALE` / `SNAP_OFFSET_X` / `SNAP_OFFSET_Y` 环境变量
   （`geometry._apply_env_overrides` 已实现）+ 设置界面可调。
5. **打包**：`packaging/build-deb.sh` 照搬 oled-guard 约定（手工 stage +
   `dpkg-deb --root-owner-group`、`SOURCE_DATE_EPOCH` 固定时间戳、`control`
   占位符替换），`packaging/debian/{control,changelog,copyright,postinst,prerm,postrm}`；
   **装后默认不自启**，`postinst` 只刷新图标/desktop 缓存。
6. **收尾**：修 `tests/`、`tools/` 的路径假设，跑通自检 / 回归 / e2e，
   更新 README 与 CHANGELOG。

## 四、新目录结构

```
src/snapctrlalt/          Python 包（capture_linux / overlay / geometry / ...）
bin/snapctrlalt           命令行入口（固定 GDK_SCALE=1，deb 与源码通用）
share/applications/       snapctrlalt.desktop
share/autostart/          自启用 desktop（可选）
share/icons/hicolor/      各尺寸 PNG + scalable SVG
packaging/build-deb.sh    构建脚本（不依赖 debhelper）
packaging/debian/         control / changelog / copyright / postinst / prerm / postrm
tests/                    回归与端到端测试
tools/                    make_icons / diagnose / perf
snapctrlalt.sh            本地启动脚本
install.sh                用户级安装（~/.local）
```

## 五、复现与验证命令

```bash
./snapctrlalt.sh --selftest          # 基础自检（应打印完整几何标定）
./snapctrlalt.sh --once              # 截一次，手动核对框选与截取是否一致
python3 tests/test_overlay.py        # 界面回归（重构后需先修路径）
python3 tests/test_gui_e2e.py        # 端到端（真鼠标拖框 + 真剪贴板）
python3 tools/diagnose.py            # 环境诊断
```

对照实验（确认缩放口径）：

```bash
GDK_SCALE=1 python3 -c "..."   # 抓图 2880×1800
python3 -c "..."               # 抓图 5760×3600（虚拟放大）
```
