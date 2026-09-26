# 进度存档 — 坐标偏移修复 + Debian 打包

> 项目位置：`本仓库/`（2026-09-27 从 `工作区/` 移入独立文件夹）

> 断点续作说明。最后更新：1.2.0 完成时。

## 一、状态：已完成 ✅

| 项 | 状态 |
|---|---|
| 目录重构为 oled-guard 同构（`src/` `bin/` `share/` `packaging/`） | ✅ |
| 资源路径解析（`assets.py`，源码 / `~/.local` / deb 三种形态） | ✅ |
| 坐标偏移修复（设备像素模型 + 运行时标定 + 指针自证） | ✅ |
| UI 独立缩放（坐标精确的同时界面不缩水） | ✅ |
| `packaging/build-deb.sh` + `packaging/debian/*` | ✅ 构建可复现 |
| `install.sh` 适配新结构 | ✅ |
| README / CHANGELOG 更新 | ✅ |
| 回归 / 坐标专项 / 端到端测试 | ✅ 54 + 36 + 28 全通过 |
| 设置窗口（曾因 Gtk API 误用崩溃） | ✅ 已修，真实打开验证通过 |

产物：`dist/snapctrlalt_1.2.3_all.deb`（88K，installed 432 KiB，sha256 见构建输出）

1.2.1 追加：多显示器负原点的钳制修正、启动器软链健壮性、debhelper 缺失时的
明确提示、坐标测试扩到 36 项（负原点 + 分数缩放全屏扫描）。

## 二、偏移问题的根因与修法

本机实测（Linux Mint / Cinnamon / X11）：

| 量 | 不设 GDK_SCALE | 设 GDK_SCALE=1 |
|---|---|---|
| GTK 显示器逻辑几何 | 1440×900（scale 2） | 2880×1800（scale 1） |
| `Gdk.pixbuf_get_from_window` 抓图 | 5760×3600 | 2880×1800 |
| Xlib 抓图 / X11 根窗口 | 2880×1800 | 2880×1800 |

真实像素是 2880×1800；5760×3600 是 GDK 按窗口 scale factor 虚拟放大的结果。
覆盖层窗口的分配尺寸又受同一个缩放影响，于是「事件坐标 / 窗口分配 / 抓图尺寸」
三者由**不同缩放口径**推导，任一处与假设不符就整体错位。

修法：
1. 入口（`bin/snapctrlalt` / `snapctrlalt.sh`）固定 `GDK_SCALE=1`，让
   事件坐标 = 覆盖层窗口 = X11 根 = 抓图像素，四者同源。
2. `geometry.py` 运行时标定 + 指针自证（Xlib 根指针对账 GTK 事件坐标），
   解出残留平移/缩放并自动纠正；策略保守，异常采样不污染标定。
3. 抓图 **Xlib 优先**（永远返回根窗口真实像素）。
4. 逃生阀：`SNAP_SCALE` / `SNAP_OFFSET_X` / `SNAP_OFFSET_Y`。
5. UI 单独缩放（`ui_scale`，HiDPI 自动 2.0），不参与坐标换算。

验证：`tests/test_coords.py` 在真实抓图上逐像素比对 + 全屏反查最佳匹配偏移 = 0。

## 三、目录结构

```
src/snapctrlalt/          Python 包（capture_linux / overlay / geometry / assets / ...）
bin/snapctrlalt           命令行入口（固定 GDK_SCALE=1，deb 与源码通用）
share/applications/       snapctrlalt.desktop
share/icons/hicolor/      各尺寸 PNG + scalable SVG
packaging/build-deb.sh    构建 deb（不依赖 debhelper，可复现）
packaging/debian/         control / changelog / copyright / postinst / prerm / postrm
tests/                    test_overlay(54) / test_coords(36) / test_gui_e2e(28)
tools/                    make_icons / diagnose / perf
snapctrlalt.sh            源码目录启动脚本
install.sh                用户级安装（~/.local）
```

## 四、复现与验证命令

```bash
./packaging/build-deb.sh             # 构建 deb（内部先跑 overlay + coords 测试）
sudo apt install ./dist/*.deb        # 安装（默认不开自启）
./snapctrlalt.sh --selftest          # 基础自检（含坐标标定）
./snapctrlalt.sh --once              # 截一次手动核对
python3 tests/test_overlay.py        # 界面回归 54 项
python3 tests/test_coords.py         # 坐标专项 36 项（含逐像素验证）
python3 tests/test_gui_e2e.py        # 端到端 28 项（真鼠标框选+标注，约 25 秒）
python3 tools/diagnose.py            # 环境诊断
./snapctrlalt.sh --perf              # 帧耗时基准
```

## 五、其他备注

- deb 装完**默认不自启**，托盘菜单里勾选「开机自启」即可。
- 覆盖层现在按物理像素运行：在 2880 宽的屏上界面按 `ui_scale=2.0` 放大，
  视觉尺寸与 1080p 屏一致；如需调整，改 `~/.config/snapctrlalt/config.json`
  里的 `ui_scale`（`auto` 或 0.5~3.0 的数值）。
- 若某台机器上仍观察到偏移，先跑 `python3 tools/diagnose.py`，它会打印
  抓图 / X11 根 / 覆盖层窗口三者的实测尺寸与指针映射，一眼能看出是哪一项不对。
