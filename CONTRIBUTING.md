# 参与开发

欢迎 issue 与 PR。这个项目是纯 Python，不需要编译，改完直接跑即可。

## 环境

```bash
sudo apt install python3-gi python3-gi-cairo python3-cairo python3-pil \
                 gir1.2-gtk-3.0 python3-xlib gir1.2-ayatanaappindicator3-0.1
```

## 跑起来

```bash
./snapctrlalt.sh              # 托盘常驻
./snapctrlalt.sh --once       # 截一次就退出
./snapctrlalt.sh --settings   # 设置
./snapctrlalt.sh --selftest   # 基础自检（含坐标标定）
python3 tools/diagnose.py     # 环境诊断，报 bug 时请附上它的输出
```

## 测试

改完请把这几条都跑一遍，PR 里说明结果：

```bash
python3 tests/test_overlay.py     # 界面回归（离屏，104 项）
python3 tests/test_coords.py      # 坐标标定（37 项，含「框选==截取」逐像素验证）
python3 tests/test_gui_e2e.py     # 端到端（真窗口+真鼠标+真剪贴板，29 项，约 25 秒）
./snapctrlalt.sh --perf           # 帧耗时基准
```

`tests/test_gui_e2e.py` 会接管鼠标约 25 秒并使用隔离的 `XDG_CONFIG_HOME`，
不会动你的配置。

## 打包

```bash
./packaging/build-deb.sh          # 产物在 dist/，构建前会自动跑测试
./packaging/build-deb.sh --version 1.2.7
```

构建是可复现的（固定 `SOURCE_DATE_EPOCH`），同一份源码两次构建出的 deb
字节一致 —— 所以发布校验和是可以被复现验证的。

## 几条约定

- **坐标是敏感区**：覆盖层运行在设备像素尺度（入口设 `GDK_SCALE=1`），
  「事件坐标 = 窗口 = X11 根 = 抓图像素」四者同源是硬约束。改动这块之前
  先读 `src/snapctrlalt/geometry.py` 的模块注释，并确保 `test_coords.py` 通过。
- **UI 与坐标分开**：`ui_scale` 只作用于绘制（工具栏/放大镜/手柄），
  绝不能参与坐标换算。
- **别让工具栏跑出屏幕**：`test_overlay.py` 里有 9 种选区形状的可达性检查，
  改工具栏布局时它会挡住回归。
- 面向用户的文字用中文，代码注释解释「为什么」而不是「做了什么」。
- 提交信息写清楚现象与原因；能附上复现步骤最好。

## 报 bug 请附

1. `python3 tools/diagnose.py` 的完整输出（含抓图/根窗口/指针映射的实测值）
2. 发行版与桌面环境（如 Linux Mint 22.3 / Cinnamon / X11）
3. 显示器配置（分辨率、缩放、是否多屏）
4. `./snapctrlalt.sh --selftest` 的结果
