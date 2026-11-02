# 更新日志

版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)：`主版本.次版本.修订号`。
唯一来源是仓库根目录的 [`VERSION`](VERSION)，`scripts/bump.ps1` 会同步本文件与 `desktop/package.json`。

## [1.8.0] - 2026-11-02

**Linux 版**：控制台从 Windows-only 扩到 Windows 10/11 x64 + Linux x64
（Ubuntu 22.04/24.04 主要支持，Debian 12 尽力，linux-arm64 未实测）。
Windows 行为保持完全不变——所有平台差异收在新的平台层里。

### 新增 · 平台层（`slab/plat.py`）
- 平台差异**唯一收口**：发行目标 `win-x64` / `linux-x64` / `linux-arm64`、可执行后缀、
  venv 解释器（`.venv\Scripts\python.exe` ↔ `.venv/bin/python3`）、QQ 安装目录（注册表 ↔ `/opt/QQ`）
- 端口 → PID：Windows 走 `GetExtendedTcpTable`（`netstat` 兜底），Linux 走
  `/proc/net/tcp{,6}` → socket inode → `/proc/<pid>/fd`（`ss` 兜底）
- 进程树收尾：Windows `taskkill /T /F`；POSIX 以 `start_new_session` 起进程组，
  先 `SIGTERM` 再 3 秒后 `SIGKILL`（保住 SnowLuma 的优雅关停）
- 存储根：Windows 沿用「非 C 盘盘符」策略；POSIX 走 XDG（`~/.local/share/SnowLumaAstrBot`），
  仍可用 `SLAB_STORAGE_ROOT` 覆盖
- Linux 注入自检：`ptrace_scope`、node 的 `cap_sys_ptrace`（`getcap`）、`DISPLAY` / `WAYLAND_DISPLAY`

### 新增 · 安装
- 安装器按平台选资产：Linux 用 `linux-x64` / `linux-arm64` 的 **tar.gz**，full 版用包内 `./node`，
  解压后自动补可执行位
- AstrBot 安装强制 **Python ≥ 3.12**：优先 uv（`uv python install 3.12` / `uv venv`），
  否则 `python3.12 -m venv`；缺依赖时直接给出可复制的 `apt` 命令
- 新增 **`一键启动.sh`**（与 `一键启动.bat` 等价：查 python3 / venv、建 `.venv`、装依赖、起服务）

### 新增 · QQ 接入（Linux）
- Linux **没有「深度分身（DLL 注入）」**：多开改成**独立 HOME**（QQ 单实例锁在
  `$HOME/.config/QQ/SingletonLock`），控制台入口叫「独立 HOME 多开」
- 支持官方 Linux QQ（默认 `/opt/QQ`），版本探测走 `dpkg-query`；未安装时页面直接给 apt / 官方 deb 指引
- 界面明确提示：注入需要 `sudo setcap cap_sys_ptrace=ep <node 真实路径>`、Yama `ptrace_scope` ≤ 2、
  且与 QQ 同一个 uid；`ptrace_scope=3` 会直接判死

### 新增 · 打包
- `packaging/build-linux.sh`：PyInstaller **onedir** → **两个产物**：`dist/*.tar.gz`（分发/首次安装）
  与 `dist/*.zip`（应用内「升级」用；`slab/upgrade.py::inspect_zip` 只认 `.zip`）。
  不用 onefile：每次启动解压到 `/tmp`，`noexec` 机器会直接失败
- `desktop/build-linux.sh`：Electron 桌面版同样产 `.tar.gz` + `.zip`，外加 `.desktop` 启动项；
  zip 里把 Electron 自带的 `version` 文件改名成 `electron-version`、包根放本仓库的 `VERSION`，
  否则升级检查会把包版本读成 Electron 的 `33.x`
- `packaging/console.spec` 平台条件化（Linux 上用 png 图标、不写 Windows 版本资源）；
  Electron 壳修掉 POSIX 侧「关窗后后端还在跑」（`detached: true` + 进程组 killpg）
- 新增 `.gitattributes`：`.sh` / `.desktop` 强制 LF，`.bat` / `.ps1` 强制 CRLF。
  不这么做，Windows 上 `core.autocrlf=true` 检出后 shell 脚本会带 `\r`，
  Linux 直接 `bad interpreter: /usr/bin/env bash^M`
- 仓库里的 `.sh` 是 Windows 侧新增的（索引里是 644），克隆后需 `chmod +x`；
  文档已写成 `bash 一键启动.sh` 兜底

### 修复
- **应用内升级（zip）在 Linux 上解压后丢可执行位 / 软链**：Python 的 `zipfile` 只写文件内容，
  不还原 POSIX 权限，覆盖完成后 `start.sh` / 内嵌可执行文件会变成 `0644`，用户一跑就
  `Permission denied`。升级助手现在按 zip 条目的 `external_attr` 还原权限位与软链接，
  并在覆盖后再补一次 `chmod +x`（Windows 走原 PowerShell 路径，行为不变）。
  已用**真实产物**验证：控制台包 64 个可执行条目标记全部恢复、桌面包 74 个 + 1 个软链全部还原，
  含深两层的 `resources/backend/start.sh`，0 失败
- **「能收不能发」复发**：aiocqhttp 的 `_wsr_api_clients` 是按 `X-Self-ID` 存的一张表，
  任意一条同账号连接断开时它都直接删这条登记（不校验归属）。控制台旧版本的 WS 握手探针
  用 `universal` 角色连了一次反向 WS，探针一关就把中继那条真实连接的登记删了 —— 事件照收、
  AstrBot 却认为「没有可用 bot」，所有回复 `ApiNotAvailable`（表现为「发一条，机器人不回」）。
  已实测复现：重启中继 → 发送正常；只要再请求一次 `/api/link/status`（触发探针）→ 立刻退化成
  `Failed to send message:`（空消息的 `ApiNotAvailable`）。修法：
  - 探针固定用 `event` 角色，只握手不登记（回归测试 `test_probe_onebot_ws_headers_and_mapping` 兜底）
  - `bridge.reregister()`：停中继 → 等 AstrBot 注销旧连接 → 起中继 → 等上游 WS 连上，
    专门用来「重新登记可发送的 bot」
  - 「修复发送链路」按模式修**真正接进 AstrBot 的那一侧**：bridge 模式重启中继（不再白停
    SnowLuma），native 模式才重启 SnowLuma；验证发送遇到空消息的 `ApiNotAvailable` 会再自愈
    一次后重试
  - 「双向自检」的自愈同样按模式走；顺手修掉自愈分支里 `bus` 未导入的 `NameError`
- **中继模式的状态/提示不再误导**：SnowLuma 侧没有 `wsClient` 本来就是正确状态，之前会报
  `config_missing` 并劝用户「写入配置」——照做就会写出第二条直连、把 bot 登记再冲一次。
  现在该状态单独标成 `relay`；总览页「OneBot 连接」按中继上游判定（不再是空列表 → 「未连接」）；
  `onebot.clients_connected` 在中继模式下按中继连接数计算
- **第二次启动只要点「一键启动全部」**（首次配置之后不该再有额外步骤）：
  - `supervisor.start_all()` 现在按模式等**接入方**真正连上才返回：bridge 模式等中继的
    上游/下游都 connected（以前只把任务拉起来就返回，界面还得手动刷新碰运气）
  - 刚启动时 SnowLuma 的面板还没就绪，`/api/link/status` 不再甩「面板登录/查询失败，
    检查 WebUI 密码」这种吓人的建议，改成「刚启动，约 10~30 秒就绪，稍后刷新」
  - AstrBot 就绪后的建议按模式区分：中继模式不再让用户去「写入 astrbot 反向 WS 连接」
  - 实测（真实环境）：停掉全部服务 → 点一次「一键启动全部」，34 秒内 AstrBot 就绪、
    SnowLuma 起来、中继双向连上，`/api/link/status` 直接 `ready=true`（clients=1，来源 bridge）

### 前端（`slab/web/`）
- 平台判定**只读后端字段**（`env.is_linux` / `env.tag` / `env.os` / `env.platform`），前端不猜系统
- 文案平台化：「C 盘 / 盘符」→ 挂载点 / 目录；「双击 exe」→ 运行启动脚本；`python.exe` → `python3`；
  `.venv\Scripts` → `.venv/bin`；「命令行黑框」在 Linux 不适用；占位符路径分隔符按平台
- 环境自检新增 Linux 专属项与**逐条修复命令**：python3 ≥ 3.12、python3-venv、git、uv（可选）、
  `ptrace_scope`、node 的 `cap_sys_ptrace`、图形会话（`DISPLAY` 缺失时提示扫码受限）
- QQ 页：「隔离启动 QQ」在 Linux 下改为「独立 HOME 多开」并跳过 DLL 分身分支；未安装时给 apt / deb 指引；
  帮助弹窗与故障折叠区的「Windows 用户 / 杀软拦截 node.exe」改为对应 Linux 说法
- 中英两套 i18n（`i18n.js` / `i18n-extra.js`）同步补齐新增词条；依旧零外链、零 CDN、无构建

### 文档
- 新增 **`docs/LINUX.md`**：支持矩阵、apt 依赖一行命令、Python/Node 门槛
  （`^24.22.1 || >=26.12.1`，发行版包必不满足 → 用 full 包自带 node）、setcap 与 `ptrace_scope`、
  官方 Linux QQ、扫码登录、独立 HOME 多开、无头服务器（Xvfb + fluxbox + x11vnc + noVNC）、
  `systemd --user` 保活、**应用内升级（zip / tar.gz 的区别与可执行位兜底）**、日志位置、
  常见故障表、Windows/Linux 措辞对照
- README 补 Linux 平台矩阵、Linux 快速开始分支、`build-linux.sh`、`slab/plat.py` 与 `一键启动.sh`
- DEPLOY / MANUAL / TROUBLESHOOTING / ARCHITECTURE 做必要补丁（平台分叉、Linux 路径与端口、
  故障树新增 setcap / ptrace / DISPLAY 三类）

### 测试
- 新增 `tests/test_web_assets_linux.py`：Linux 文案与 `docs/LINUX.md` 关键小节齐全、
  前端不再出现裸 `C:\` 之类硬编码（白名单例外）
- 新增 `tests/test_plat_linux.py` / `tests/test_installer_linux.py` / `tests/test_packaging_linux.py`：
  平台分支、资产选择、打包脚本静态校验（Windows 上也能跑）
- 修掉 `tests/test_web_assets.py::test_docs_exist_and_cover_key_topics` 与 1.7.2 重写后的 README 不一致

## [1.7.3] - 2026-11-01

### 修复
- **「QQ 里发一条消息，机器人连着回两条」**：中继（bridge）模式下 SnowLuma 里还留着 native 时代的直连
  `wsClient`（`astrbot` → AstrBot 反向 WS 6199），同一条 QQ 消息被「直连」和「内置中继」各投递一次，
  AstrBot 于是处理两遍、回两遍。现在切换/写入中继配置时会**自动清理**这条残留直连：
  - `apply_relay()` 写模板与每个账号配置时清掉指向 AstrBot 反向 WS 的 `wsClient`（用户其它连接保留）
  - 中继启动前自愈一次（`ensure_single_channel()`），老安装不点任何按钮也会自动修好
  - 「修复发送链路」「写入联动配置」返回结果里会写明清掉了几条，联调页状态也会提示该问题
  - 清掉那条直连后会让 AstrBot 重连一次中继、重新登记「可发送的 bot」（aiocqhttp 会按 self_id
    误删登记，不清这一步就会出现「能收不能发」）
  - 中继再加一道防线：同一条消息（同 `self_id` + `message_id`）5 秒内只转发一次（`events_duplicate` 可查）
- **顺手修掉一个会让「双向自检 / 修复发送链路」永远报发送失败的坑**：状态探测对 AstrBot 反向 WS 做握手时
  用的 `X-Client-Role: universal`，会让 aiocqhttp 把这条一次性探针登记成「可发送的 bot」；探针一断开，
  它按 `self_id` 直接删登记（不校验是不是自己），于是中继那条真实连接的登记被误删 → 走 AstrBot API 的
  发送全部 `ApiNotAvailable`。现在探针改用 `event` 角色，只握手不登记。

## [1.7.2] - 2026-11-01

### 变更
- 重写 README 作为仓库首页：功能一览、快速开始、**窗口操作（关闭是长按电源键）**、两种联动模式、FAQ、文档索引
- zip 内 `使用说明.txt` 同步更新（长按电源键、双向自检、手册指引）

### 优化
- 清理 800 MB 无用文件（`__pycache__`、`.pyc`、构建目录、开发用 runtime 与日志）
- 打包瘦身：不把 `docs/images` 塞进 exe，新增 `excludes`（tkinter/unittest/pydoc/setuptools 等）
  - 控制台：44.4 → 39.5 MB，zip 25.5 → 19.7 MB
  - 桌面版：135.2 → 129.4 MB
  - 热启动：5.7 s → **1.31 s**

## [1.7.1] - 2026-10-31

### 修复
- 「能收不能发」根治：默认改用**内置中继（bridge）**，中继作为 Universal 客户端接入 AstrBot，
  私聊 / 群聊 / 图片 / 文件 / @ 与插件动作全部透传
- 新增 `POST /api/link/repair`（修复发送链路）与 `POST /api/link/selftest`（双向四步自检）
- 联调页新增「双向自检」「修复发送链路」按钮

## [1.7.0] - 2026-10-31

### 新增
- AstrBot 面板密码重置：按 AstrBot 自己的 PBKDF2 格式直接写散列，解决"一键登录密码错误"
- 配置页「拉取可用模型」「从 AstrBot 读回」（列表形式的 API Key 拆包）

## [1.6.0] - 2026-10-31

### 新增
- QQ 进程选择与手动注入（默认不接管用户日常 QQ）、注入后回读账号校验
- 深度分身（注入 sandbox DLL：互斥体改名 + 已知文件夹重定向）
- 面板凭据页：密码显示 / 复制 / 一键登录

## [1.5.0] - 2026-10-31

### 新增
- 启动/退出擦屁股：启动清理父进程已消失的残留；退出停服务 + 清进程 + 释放端口
- 窗口：最小化修复（隐藏窗口 minimize 无效 → show+focus 后重试，退化 hide）、新增最大化按钮、
  关闭统一 `quitApp` + 9 秒兜底强制退出

## [1.4.0] - 2026-10-31

### 新增
- 从 zip 升级：备份 → 覆盖 → 重启，失败回滚；配置版本号 + 自动迁移（`slab/migrations.py`）
- 升级指南 `docs/UPGRADE.md`

## [1.3.0] - 2026-10-31

### 新增
- 无边框窗口 + 长按电源键关闭（收起 / 彻底退出）
- 启动性能：端口探测改 `GetExtendedTcpTable`（1.2 s → 10 ms）、自检结果落盘并后台刷新（state 1.3 s → 25 ms）

## [1.2.0] - 2026-10-31

### 新增
- 下载源测速（7 个源，自动挑最快）、自定义安装目录、卸载与清理（含彻底删除）
- 首次启动向导、端口自动避让、日志按天清理、中英双语、液态玻璃 UI

## [1.1.0] - 2026-10-31

### 新增
- 稳定性与体验：服务启动前端口预检、修复运行时长跳秒、静态资源版本号防缓存

## [1.0.0] - 2026-10-29

### 新增
- 首个版本：一键部署 SnowLuma + AstrBot、可视化配置、服务启停与日志、联调测试

