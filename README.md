# SnowLuma × AstrBot 联合控制台

> 在 **Windows / Linux** 上**点几下鼠标**，把 [SnowLuma](https://github.com/SnowLuma/SnowLuma)（QQ 协议端）和
> [AstrBot](https://github.com/AstrBotDevs/AstrBot)（大模型机器人框架）装好、连起来，
> 然后在 QQ 里和你自己的 AI 机器人聊天 —— **不用敲命令、不用改配置文件**。

```
版本 v1.8.0   ·   Windows 10/11 x64   ·   Linux x64（Ubuntu 22.04/24.04，Debian 12 尽力，arm64 未实测）
              ·   桌面版(Electron) / 浏览器版   ·   MIT
```

![总览](docs/images/overview.png)

---

## 目录

- [这是什么](#这是什么)
- [功能一览](#功能一览)
- [快速开始（3 步）](#快速开始3-步)
- [窗口怎么操作（关闭是长按）](#窗口怎么操作关闭是长按)
- [工作原理](#工作原理)
- [两种联动模式](#两种联动模式)
- [常见问题](#常见问题)
- [文档](#文档)
- [目录结构](#目录结构)
- [命令行与二次开发](#命令行与二次开发)
- [打包（exe / tar.gz）](#打包exe--targz)
- [许可与免责声明](#许可与免责声明)

---

## 这是什么

它把两个本来要命令行操作的开源项目，包成一个**能点着用的桌面程序**：

| 组件 | 角色 | 默认端口 |
| --- | --- | --- |
| **SnowLuma** | 接管你已登录的 QQ，转成 OneBot v11 接口 | 面板 5099 / OneBot 3000·3001 |
| **AstrBot** | 收消息 → 问大模型（DeepSeek / OpenAI 兼容）→ 回消息，可装插件 | 面板 6185 / 反向 WS 6199 |
| **本控制台** | 装、配、启停、连通、排错、升级 | 8790 |

四件事：**下载安装** → **可视化配置** → **一键启停 + 实时日志** → **联调自检（真发一条消息验证）**。

---

## 功能一览

| 能力 | 说明 |
| --- | --- |
| **跨平台** | Windows 10/11 x64 与 Linux x64（Ubuntu 22.04/24.04，Debian 12 尽力）同一套界面；平台差异全部收在 `slab/plat.py` |
| **四步向导** | 环境检查 → 一键装两个项目 → 填 QQ 号/Key → 启动；装完填完不再打扰 |
| **一键安装** | 自动下发行包 / 克隆源码 / 建独立 venv / 装依赖；Windows 全程静默无黑框，Linux 输出都在页面日志里 |
| **登录 QQ** | 「用这个」只注入你选中的 QQ 进程；Windows 用**深度分身**、Linux 用**独立 HOME 多开**（没有 DLL 注入这回事） |
| **大模型配置** | 拉取该 Key 可用的模型列表（下拉选）、真发一次请求测连通性、从 AstrBot 读回实际生效配置 |
| **一键启动全部** | 首次配置完就不用再折腾：之后每次打开控制台点这一个按钮，按 AstrBot → SnowLuma → 中继 起好，并等中继真正连上才返回 |
| **双向自检** | 四步逐条：连接 → 面板登录 → API Key → **真发一条消息到 QQ**；哪步红修哪步 |
| **一键修复发送链路** | 机器人「能收不能发」时点它：切中继模式 → 起服务 → 验证发送 |
| **面板免翻日志** | 直接显示两个面板的密码，可复制、一键登录；忘了能**重置密码** |
| **液态玻璃 UI** | 无边框窗口、深/浅双主题、中英双语 |
| **启动/退出干净** | 启动清理上次残留进程；退出停服务 + 清进程 + 释放端口（Linux 靠进程组，不会留孤儿占端口） |
| **默认不占系统盘** | Windows 自动挑非 C 盘；Linux 走 XDG（`~/.local/share/SnowLumaAstrBot`），可用 `SLAB_STORAGE_ROOT` 改 |
| **从 zip 升级** | 配置页「升级控制台」：备份 → 覆盖 → 重启，失败自动回滚；配置自动迁移 |
| **运维工具** | 端口避让、日志保留天数、清理残留进程、卸载/彻底删除、配置导出导入 |

---

## 快速开始（3 步）

**Windows**（桌面版 zip）

1. **解压** `SnowLuma-Desktop-v…-win-x64.zip`，双击 `SnowLuma-Desktop.exe`
2. 跟着**四步向导**：环境检查 → 装两个项目 → 填**机器人小号** QQ 号 + 大模型 API Key → 启动
3. 「运行 → 接入 QQ」点 **用这个** 注入机器人小号；然后私聊或群里 @ 它

**Linux**（浏览器版 tar.gz 或源码，x64）

1. 解压并进入目录：`tar -xzf SnowLuma-AstrBot-Console-v…-linux-x64.tar.gz && cd SnowLuma-AstrBot-Console-…`
2. **运行启动脚本** `./一键启动.sh`（首次会建 `.venv` 并装依赖；等价于 `python3 -m slab serve --port 8790`）
3. 打开 <http://127.0.0.1:8790>，跟着**四步向导**走；然后「运行 → 接入 QQ」点 **用这个** 注入小号

> Linux 上**先过一遍 [docs/LINUX.md](docs/LINUX.md)**：`apt` 依赖一行、Python ≥ 3.12、
> 官方 Linux QQ（`/opt/QQ`）、以及 `sudo setcap cap_sys_ptrace=ep <node 真实路径>` 与 Yama `ptrace_scope`
> —— 这两项没配好，界面全绿也接不上 QQ。

> 群里不回 / 插件不触发 → 「联调」页点 **双向自检**；多数是 native 模式的发送链路问题，点「修复发送链路」一键切中继。

---

## 窗口怎么操作（关闭是长按）

没有系统标题栏，**顶栏就是标题栏**（按住可拖动窗口）。右上角：

| 按钮 | 图标 | 操作 | 效果 |
| --- | --- | --- | --- |
| 最小化 | `—` | 单击 | 收进任务栏，服务继续跑 |
| 最大化/还原 | `▢` | 单击（或双击顶栏空白） | 全屏 / 还原 |
| 置顶 | `◎` | 单击 | 窗口常在最前 |
| **电源键** | `⏻` | **按住 1 秒**（外圈进度环画满） | 弹出「收起窗口 / 彻底退出」 |

- **收起窗口**：服务继续跑，机器人不掉线（日常推荐）
- **彻底退出**：停服务 + 清残留进程 + 释放端口
- `Ctrl+Q` = 同一菜单；`Alt+F4` 先停服务再关窗；卡住有 9 秒强制退出兜底

> 以上是**桌面版**的窗口行为（Windows 与 Linux 桌面版一致）。**浏览器版（Linux 服务器常见）没有窗口**：
> 控制台由 `./一键启动.sh` 或 `systemd --user` 拉起，退出用顶栏「退出」或 `systemctl --user stop`。

---

## 工作原理

```
QQ 客户端（小号登录）
      │  SnowLuma 注入进程，接管会话
      ▼
  SnowLuma  ──OneBot v11──►  AstrBot  ──HTTP──►  大模型（DeepSeek/OpenAI 兼容）
   面板 5099                  面板 6185
      │                          │
      └──────── 本控制台 8790：启停 / 配置 / 日志 / 自检 / 升级 ────────┘
```

> 图上那句「注入进程」在 Windows 上是 DLL 注入（深度分身），在 Linux 上是 **ptrace 附着**：
> 需要给 node 打 `cap_sys_ptrace` 能力位、并保证与 QQ 同一个 uid（详见 [docs/LINUX.md](docs/LINUX.md) §4）。

---

## 两种联动模式

| 模式 | 链路 | 适用 |
| --- | --- | --- |
| **native（直连）** | SnowLuma ⭢ AstrBot | 少一跳；但 AstrBot 的 aiocqhttp 重连后易丢失"可发送的 bot"，表现为**能收不能发** |
| **bridge（中继，推荐）** | SnowLuma ⇄ 内置中继 ⇄ AstrBot | 全双工客户端，**私聊 / 群聊 / 图片 / 文件 / @ 全部支持**，插件动作原样透传 |

---

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 能收到消息但不回 / 群聊不回 | 联调页「双向自检」→「修复发送链路」（切中继） |
| 插件不触发 | AstrBot 面板确认插件已启用；前缀默认 `/`；发送链路正常 |
| `401 API Key 无效` | 配置页「测试大模型连接」按提示换 Key / Base URL |
| `端口 6185 已被占用` | 配置页「清理残留进程」；重启会自动先清 |
| 一键登录密码错误 | 「运行 → 面板登录 → 重置密码」 |
| 首次启动慢 / 窗口"未响应" | 杀软扫描 + 首次解压，等 10 秒；日常用「收起窗口」 |
| 关窗后还有进程 | 升级到当前版本；配置页「清理残留进程」 |
| **Linux**：注入报 `EPERM` / QQ 接不上 | `sudo setcap cap_sys_ptrace=ep <node 真实路径>`，见 [docs/LINUX.md](docs/LINUX.md) §4 |
| **Linux**：环境自检里 `ptrace_scope` 红 | Yama 值被设成 3（完全禁止），改回 1 并写 `/etc/sysctl.d/` |
| **Linux**：扫码登录不了 / QQ 起不来 | 没有图形会话（`DISPLAY` 为空）；桌面里操作或按 LINUX.md §7 搭 Xvfb + VNC |

详见 [docs/MANUAL.md](docs/MANUAL.md) 与 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)。

---

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/MANUAL.md](docs/MANUAL.md) | **操作手册**：装、登录 QQ、配置、联动、插件命令、维护、故障速查 |
| [docs/LINUX.md](docs/LINUX.md) | **Linux 部署**：支持矩阵、apt 依赖、Python/Node 门槛、setcap 与 ptrace_scope、官方 Linux QQ、独立 HOME 多开、无头 VNC、systemd 保活 |
| [docs/UPGRADE.md](docs/UPGRADE.md) | **升级指南**：版本规则、三种升级方式、回滚 |
| [docs/DEPLOY.md](docs/DEPLOY.md) | 部署细节：下载源、版本、目录、端口 |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | 故障树与逐项排查 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 架构与数据流（含平台层） |
| [docs/CONTRACTS.md](docs/CONTRACTS.md) | 与两个项目的接口契约 |
| [CHANGELOG.md](CHANGELOG.md) | 更新日志（语义化版本） |

---

## 目录结构

```
snowluma-astrbot-console/
├─ 一键启动.bat          Windows 启动脚本（建 .venv / 装依赖 / 起服务 / 开浏览器）
├─ 一键启动.sh           Linux 启动脚本（等价，解释器 python3）
├─ slab/                 控制台后端（FastAPI）
│  ├─ plat.py            **平台层**：Windows / Linux 差异唯一收口（路径、进程组、端口表、QQ 目录）
│  ├─ installer.py       下载/校验/解压/克隆/建 venv/卸载
│  ├─ supervisor.py      服务启停（进程树、端口预检、退出清理）
│  ├─ envcheck.py        环境自检（并行探测 + 磁盘缓存 + 端口表 + 平台字段）
│  ├─ bridge.py          OneBot 中继（Universal 客户端，全双工）
│  ├─ sources.py         下载源目录与测速
│  ├─ orphans.py         残留进程识别与清理
│  ├─ migrations.py      配置版本迁移
│  ├─ upgrade.py         从 zip 升级（助手进程 + 回滚）
│  ├─ server/            HTTP 路由（console/deploy/link/qq/upgrade）
│  └─ web/               前端（原生 JS + CSS，无构建、无 CDN）
├─ desktop/              Electron 外壳（无边框窗口 + 长按电源键）
├─ packaging/            PyInstaller 打包脚本与 spec（build-exe.bat / build-linux.sh）
├─ tests/                pytest（365+ 项，含 Linux 平台分支）
├─ docs/                 文档与截图（docs/LINUX.md 是 Linux 入口）
├─ vendor/sandbox/       QQ 分身注入组件（仅 Windows）
└─ VERSION               版本号唯一来源
```

运行期目录（Windows 默认不在 C 盘；Linux 走 XDG）：

```
# Windows
<盘符>:\SnowLumaAstrBot\
# Linux
~/.local/share/SnowLumaAstrBot/
├─ data/        配置、日志、备份、升级缓存
├─ runtime/     snowluma / astrbot 的实际安装
└─ downloads/   安装包缓存
```

Linux 想换位置：`export SLAB_STORAGE_ROOT=/opt/snowluma-astrbot`（详见 [docs/LINUX.md](docs/LINUX.md) §3）。

---

## 命令行与二次开发

```bat
:: Windows：启动控制台（源码模式）
.venv\Scripts\python.exe -m slab serve --port 8790

:: Windows：跑测试
.venv\Scripts\python.exe -m pytest -o addopts="" -q

:: 版本号（同步 VERSION / package.json / CHANGELOG）
scripts\bump.ps1 patch|minor|major "说明"
```

```bash
# Linux：启动控制台（源码模式）
.venv/bin/python3 -m slab serve --port 8790

# Linux：跑测试
.venv/bin/python3 -m pytest -o addopts="" -q

# 版本号（bump.ps1 在 pwsh 7 下也能跑；有 bump.sh 时优先用它）
pwsh -File scripts/bump.ps1 patch "说明"
```

常用环境变量：`SLAB_DATA_DIR` / `SLAB_RUNTIME` / `SLAB_DOWNLOAD_DIR` / `SLAB_STORAGE_ROOT` /
`SLAB_LANG=en` / `SLAB_THEME=dark` / `SLAB_SNOWLUMA_HOOK_AUTOLOAD=1`。

---

## 打包（exe / tar.gz）

```bat
:: Windows
packaging\build-exe.bat        :: 控制台（浏览器版）→ dist\*.zip
desktop\build.bat             :: 桌面版（Electron）→ desktop\dist\*.zip
```

```bash
# Linux（x64）
packaging/build-linux.sh      # 控制台（浏览器版）→ dist/*.tar.gz + dist/*.zip
desktop/build-linux.sh        # 桌面版（Electron）→ desktop/dist/*.tar.gz + *.zip
```

四个脚本都带版本号生成产物；Windows 桌面版打包前有**冒烟测试**（启动 → 关窗 → 检查后端是否被回收）。
Linux 侧每个脚本产**两种包**：`.tar.gz` 用于分发/首次安装，`.zip` 用于应用内「升级」
（控制台只认 zip 升级包），详见 [docs/LINUX.md](docs/LINUX.md) §8.5。
Linux 侧用 **onedir**（不是 onefile）：onefile 每次启动都解压到 `/tmp`，`noexec` 的机器会直接失败。

---

## 许可与免责声明

- 本控制台代码：MIT
- 集成 [SnowLuma](https://github.com/SnowLuma/SnowLuma)、[AstrBot](https://github.com/AstrBotDevs/AstrBot)，遵循各自上游许可
- **协议端有封号风险**：请使用**小号**，不要用主号挂机器人；账号风险由使用者自负
- 本项目只做本地集成与可视化，不代理、不上传聊天内容；大模型请求直连你配置的服务商
