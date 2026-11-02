# 桌面版外壳（Electron）

把已经做好的控制台（FastAPI + 静态前端）包进一个原生窗口：**关掉窗口 = 退出程序**，
不会像浏览器那样关掉标签页后还留着后台进程。

```
desktop/
├─ main.js          # 主进程：拉起 Python 后端 → 开窗 → 关窗时停服务 + 杀整棵进程树
├─ preload.js       # 只暴露 window.slabDesktop.quit()，页面里的「退出」按钮用它
├─ splash.html      # 启动画面（等后端 /api/health 就绪）
├─ assets/icon.ico  # 窗口图标（Windows）
├─ assets/icon.png  # 窗口图标（Linux 的 GTK/KDE 只认 png）
├─ package.json     # electron 依赖 + npm start / npm run smoke
├─ build.ps1        # 组装 + 冒烟 + 打 zip（不依赖 electron-builder，可离线）
├─ build.bat        # 双击版
└─ build-linux.sh   # Linux 版：组装 + 打 tar.gz + .desktop（由 packaging/build-linux.sh 的后端驱动）
```

## 目录关系

```
desktop/dist/SnowLuma-Desktop/
├─ SnowLuma-Desktop.exe          # 双击入口（Electron 运行时改名而来；Linux 上无扩展名）
├─ *.dll / locales/ ...          # Electron 运行时
├─ 使用说明.txt
└─ resources/
   ├─ app/                       # 外壳代码（main.js / preload.js / splash.html）
   └─ backend/                   # 内置的 Python 控制台（packaging 打出来的绿色版）
      └─ SnowLuma-AstrBot-Console.exe ...
```

Linux 版多三个文件：`SnowLuma-Desktop.sh`（启动器，自动处理 Chromium SUID 沙箱）、
`SnowLuma-Desktop.desktop`（桌面项）、`install-desktop-entry.sh`（把桌面项装进
`~/.local/share/applications`，按实际解压路径改写 Exec/Icon）。

## 生命周期

| 时刻 | 行为 |
| --- | --- |
| 启动 | 选一个空闲端口（8790 起）→ 用隐藏窗口拉起 `backend/SnowLuma-AstrBot-Console[.exe] serve` → 轮询 `/api/health`（45s 超时）→ 打开窗口 |
| 数据位置 | Windows：`%APPDATA%\SnowLuma AstrBot Console\`（`data\` 配置日志、`runtime\` 装的两个项目），菜单「控制台 → 打开数据目录」；Linux：XDG（`~/.local/share/SnowLumaAstrBot`），root 分区快满时自动换更大的挂载点 |
| 关窗 / Ctrl+Q | 先 `POST /api/system/shutdown` 让后端停掉 SnowLuma / AstrBot → 等 4 秒 → Windows `taskkill /T /F`；Linux 给**整个进程组**发 `SIGTERM`，等 2.2 秒再 `SIGKILL` |
| 页面「退出」 | 走 IPC `app:quit` → 同上 |
| 后端意外退出 | 弹窗告知并退出外壳，避免留下空窗口 |

> **Linux 关键点**：后端是 `spawn(..., { detached: true })` 起的，内核会把它 `setsid`
> 成新进程组的组长，它拉起的 SnowLuma（node）和 AstrBot（python）都留在这一组里。
> 所以关闭窗口必须用 `process.kill(-pid, ...)` 按组收——只 `kill(pid)` 会出现
> 典型的「窗口关了，后端还在跑」。外壳自身崩溃时还有 `process.on('exit')` 里的
> `SIGKILL` 兜底。

外链（AstrBot / SnowLuma 面板）用系统浏览器打开，不再嵌套窗口。

## 构建

Windows：

```bat
:: 先有后端（packaging 打出来的绿色版）
packaging\build-exe.bat
:: 再打桌面版
desktop\build.bat
```

Linux（顺序一样，脚本换成 .sh）：

```bash
bash packaging/build-linux.sh     # 后端 onedir → dist/SnowLuma-AstrBot-Console/ + tar.gz（含冒烟）
bash desktop/build-linux.sh       # 组装 Electron 桌面版 → desktop/dist/*.tar.gz + .desktop
```

两个 .sh 都是 **LF** 结尾（不要用 Windows 记事本改成 CRLF，否则 `#!/usr/bin/env bash\r` 会报
`bad interpreter`）。Electron 运行时只 `npm install` 一次（`cd desktop && npm install`），
之后打包完全离线。

> 小坑：在 Windows 上写/提交的 .sh 不会带可执行位，Linux 上第一次用如果提示
> `Permission denied`，先 `chmod +x 一键启动.sh packaging/build-linux.sh desktop/build-linux.sh`，
> 或者直接用 `bash <脚本>` 运行。

`build.ps1` 会：`npm install`（首次约 180 MB，走 npmmirror）→ 跑一次冒烟测试（开窗 8 秒自动关窗，
检查后端是否被回收）→ 组装 `dist/SnowLuma-Desktop/` → 压成 zip。

## 验收 / 调试开关

| 环境变量 | 作用 |
| --- | --- |
| `SLAB_BACKEND_EXE` | 指定后端 exe（默认用 resources/backend 里的） |
| `SLAB_SMOKE_MS=60000` | `--smoke-test` 时延长关窗时间，便于在关窗前启动服务做端到端验证 |
| `SLAB_SHOT=out.png` | 窗口加载完成后截一张图再退出（用于文档/回归） |
| `SLAB_SHOT_DELAY=6000` | 截图前等多久（默认 2500ms；配合向导首屏用） |
| `SLAB_LANG=en` | 强制英文界面（等价于点顶栏「EN」） |
| `SLAB_DATA_DIR` / `SLAB_RUNTIME` | 覆盖数据与安装目录 |

自动化验证「关窗真的能带走子进程」：

```powershell
$env:SLAB_RUNTIME="..\runtime"; $env:SLAB_SMOKE_MS="60000"
.\desktop\dist\SnowLuma-Desktop\SnowLuma-Desktop.exe --smoke-test
# 期间调 /api/services/snowluma/start 拉起 SnowLuma，60 秒后窗口自动关闭
# 期望：backend 0 个、node.exe 0 个、electron 0 个、8790 不再监听
```
