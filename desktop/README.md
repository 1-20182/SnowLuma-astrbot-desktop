# 桌面版外壳（Electron）

把已经做好的控制台（FastAPI + 静态前端）包进一个原生窗口：**关掉窗口 = 退出程序**，
不会像浏览器那样关掉标签页后还留着后台进程。

```
desktop/
├─ main.js          # 主进程：拉起 Python 后端 → 开窗 → 关窗时停服务 + 杀整棵进程树
├─ preload.js       # 只暴露 window.slabDesktop.quit()，页面里的「退出」按钮用它
├─ splash.html      # 启动画面（等后端 /api/health 就绪）
├─ assets/icon.ico  # 窗口图标
├─ package.json     # electron 依赖 + npm start / npm run smoke
├─ build.ps1        # 组装 + 冒烟 + 打 zip（不依赖 electron-builder，可离线）
└─ build.bat        # 双击版
```

## 目录关系

```
desktop/dist/SnowLuma-Desktop/
├─ SnowLuma-Desktop.exe          # 双击入口（Electron 运行时改名而来）
├─ *.dll / locales/ ...          # Electron 运行时
├─ 使用说明.txt
└─ resources/
   ├─ app/                       # 外壳代码（main.js / preload.js / splash.html）
   └─ backend/                   # 内置的 Python 控制台（packaging 打出来的绿色版）
      └─ SnowLuma-AstrBot-Console.exe ...
```

## 生命周期

| 时刻 | 行为 |
| --- | --- |
| 启动 | 选一个空闲端口（8790 起）→ 用隐藏窗口拉起 `backend/SnowLuma-AstrBot-Console.exe serve` → 轮询 `/api/health`（45s 超时）→ 打开窗口 |
| 数据位置 | `%APPDATA%\SnowLuma AstrBot Console\`（`data\` 配置日志、`runtime\` 装的两个项目），菜单「控制台 → 打开数据目录」 |
| 关窗 / Ctrl+Q | 先 `POST /api/system/shutdown` 让后端停掉 SnowLuma / AstrBot → 等 6 秒 → 还活着就 `taskkill /T /F` 兜底 |
| 页面「退出」 | 走 IPC `app:quit` → 同上 |
| 后端意外退出 | 弹窗告知并退出外壳，避免留下空窗口 |

外链（AstrBot / SnowLuma 面板）用系统浏览器打开，不再嵌套窗口。

## 构建

```bat
:: 先有后端（packaging 打出来的绿色版）
packaging\build-exe.bat
:: 再打桌面版
desktop\build.bat
```

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
