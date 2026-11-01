# 部署指南（DEPLOY）

本文覆盖：**自动部署**（界面点点点）、**手动部署**（命令行 / 完全离线）、**端口冲突**、**升级与回滚**、**卸载**。

> 所有的「安装目录」都在项目下的 `runtime/` 里：
> `runtime/snowluma/`、`runtime/astrbot/`。
> 想换位置？启动前设置环境变量 `SLAB_RUNTIME`（例如放到 D 盘）。

---

## 0. 前置条件

| 项 | 要求 | 怎么检查 |
| --- | --- | --- |
| 操作系统 | Windows 10 / 11（x64）。SnowLuma 的 QQ 接入仅支持 Windows | `winver` |
| Python | **3.12+**（控制台自身用） | `python --version` |
| 磁盘 | 至少 **3 GB** 空闲（两个项目 + 依赖 + 缓存） | 「① 总览」环境自检会显示 |
| 网络 | 能访问 GitHub / PyPI / 大模型 API | 浏览器打开 github.com |
| Git（可选） | 自动部署 AstrBot 需要；没有可用离线包 | `git --version` |
| Node.js（可选） | 只有 SnowLuma **lite** 版需要（≥ 24.22.1）；**full** 版自带 | `node --version` |

「① 总览 → 环境自检」会把上面这些逐条列出来，缺什么一眼能看到。

---

## 1. 自动部署（推荐，小白路线）

### 1.1 启动控制台

双击项目根目录的 **`一键启动.bat`**。它会依次：

1. 检查 `.venv`，没有就创建（优先 `uv venv --python 3.12 .venv`，没有 uv 则 `python -m venv .venv`）；
2. 安装 `requirements.txt`（已装则跳过）；
3. 5 秒后自动打开 <http://127.0.0.1:8790>；
4. 前台运行 `python -m slab serve`。

> 窗口关掉 = 控制台停止，同时会停掉它启动的 SnowLuma / AstrBot。这是有意设计的（不留孤儿进程）。

手动等价命令：

```bat
cd /d <项目目录>
.venv\Scripts\python.exe -m slab serve
```

### 1.2 安装两个项目

打开「**② 部署**」页：

1. **SnowLuma**：选版本（`latest` 即可）→ 安装类型选 **full**（自带 Node，推荐）→ 点 **下载安装**。
2. **AstrBot**：分支保持 `main` → 点 **克隆并安装依赖**（会 `git clone` + 建独立虚拟环境 + 装依赖）。
3. 也可以直接点右上角 **一键安装两个项目**，两者按顺序安装。

安装过程中：

* 进度条 + 滚动日志实时刷新（由 SSE `deploy` 事件驱动）；
* **不要关闭页面，也不要关掉控制台窗口**；
* AstrBot 依赖较多，3–10 分钟属正常。

装完后「② 部署」页会显示 **已安装** 徽标与安装路径，可点「复制安装目录」粘到资源管理器。

### 1.3 配置 → 写入联动 → 启动

```
③ 配置：QQ 号 + 大模型 API Key + 模型名 → 保存设置
⑤ 联调：写入联动配置
④ 运行：一键启动全部（首次需在 SnowLuma 面板里扫码登录 QQ）
⑤ 联调：发一条测试消息 / 在 QQ 里 @机器人
```

---

## 2. 手动部署（无界面 / 定制）

控制台不在时也能装，适合想自己控制版本、或界面出问题时的兜底。

### 2.1 SnowLuma（手工）

1. 从 SnowLuma 官方仓库的 Releases 下载 **Windows full 版**压缩包（自带 `node.exe`）。
2. 解压到 `runtime/snowluma/`（确保目录里有启动用的 `node`/`exe` 与 `config/`）。
3. 首次启动后，浏览器打开 `http://127.0.0.1:5099` 扫码登录 QQ。
4. OneBot 适配器配置在 `runtime/snowluma/config/onebot*.json`，关键字段：

```jsonc
{
  "ws": {
    "host": "127.0.0.1",     // AstrBot 所在主机
    "port": 6199,            // link.ws_port
    "token": "和 AstrBot 一致" // link.token
  },
  "qq": "你的QQ号"            // 具体键名随版本略有差异
}
```

### 2.2 AstrBot（手工）

```bat
cd /d <项目目录>\runtime
git clone https://github.com/AstrBotDevs/AstrBot.git astrbot
cd astrbot
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py
```

启动后打开 `http://127.0.0.1:6185`（默认账号 `astrbot`，密码在控制台「③ 配置」里可以看到/改）。
平台与模型配置写在 `runtime/astrbot/data/cmd_config.json`：

```jsonc
{
  "platform": [{
    "type": "aiocqhttp",
    "enable": true,
    "ws_reverse_host": "0.0.0.0",   // 监听所有网卡；纯本机可写 127.0.0.1
    "ws_reverse_port": 6199,        // = link.ws_port
    "ws_reverse_token": "和 SnowLuma 一致"  // = link.token
  }],
  "provider": [{
    "type": "openai_chat_completion",
    "base_url": "https://api.deepseek.com/v1",
    "key": ["sk-你的key"],           // 注意：AstrBot 用数组包一层
    "model": "deepseek-chat"
  }],
  "admins_id": ["你的QQ号"]
}
```

> 手工改过这些文件后，**再从控制台点一次「写入联动配置」会覆盖你的手改内容**。
> 想长期手工维护，就别在控制台里点那个按钮。

### 2.3 不需要界面的命令行安装

```bat
.venv\Scripts\python.exe -m slab install both
.venv\Scripts\python.exe -m slab doctor        :: 环境/配置/端口自检
```

---

## 3. 离线安装（内网 / 网络很差）

思路：**先把能下载的东西在联网机器上准备好，再整体拷过去**。

### 3.1 准备（联网机器）

1. 控制台依赖：
   ```bat
   .venv\Scripts\python.exe -m pip download -r requirements.txt -d offline\wheels
   ```
2. SnowLuma：下载官方 **full 版** 压缩包，放到 `offline\snowluma.zip`。
3. AstrBot：下载 zip 源码包（或 `git clone` 后打包），放到 `offline\astrbot.zip`。
   其依赖同理，在联网机器上 `pip download -r runtime\astrbot\requirements.txt -d offline\astrbot-wheels`。

### 3.2 目标机器（离线）

```bat
:: 1) 用本地 wheel 装控制台依赖
python -m venv .venv
.venv\Scripts\python.exe -m pip install --no-index --find-links offline\wheels -r requirements.txt

:: 2) 解压两个项目到 runtime/
mkdir runtime
tar -xf offline\snowluma.zip -C runtime\snowluma
tar -xf offline\astrbot.zip  -C runtime\astrbot

:: 3) AstrBot 依赖离线装
runtime\astrbot\.venv\Scripts\python.exe -m pip install --no-index --find-links offline\astrbot-wheels -r runtime\astrbot\requirements.txt

:: 4) 启动控制台
.venv\Scripts\python.exe -m slab serve
```

> 离线环境下「② 部署」页的下载/克隆会失败（那是正常的，它需要外网）。
> 只要 `runtime/snowluma` 与 `runtime/astrbot` 目录存在，「总览」就会显示 **已安装**，
> 之后可以正常配置、启动、联调。

---

## 3.5 自定义安装位置 / 数据目录（多盘、共享机）

默认两个项目都装在**程序目录下的 `runtime\`**；想放 D 盘或指定目录：

1. 「③ 配置」→「安装位置与备份」→ 点 **选择…**（桌面版用系统原生对话框；浏览器版由后端弹 PowerShell 目录框），
   或直接把路径粘进输入框，例如 `D:\bots\snowluma`、`D:\bots\astrbot`；
2. 点「保存设置」；
3. 回「② 部署」重新安装（或手动把已有目录搬过去后，在配置里指向新路径）。

注意：

- 目录必须是**本机绝对路径**，不能指向 `C:\Windows` 等系统目录；父目录不存在会自动创建；
- 改完安装目录后，旧的安装不会自动搬走，可以手动移动整个目录（`config/` 与 `data/` 一起搬）；
- **数据目录**（配置、日志、下载缓存）固定在程序旁边：exe 版是 `<程序目录>\data`，桌面版是
  `%APPDATA%\SnowLuma AstrBot Console\data`。想整体换盘就把程序文件夹一起搬走；
- 「导出配置」会把安装目录、端口、QQ 号、API Key 一起导出（含密钥，别乱发）；
  「导入配置」是**整份覆盖**，换机器时最省事，导入后建议重启一次控制台。

---
## 4. 端口冲突处理

**症状**：启动报「端口被占用」/ 面板打不开 / 「总览」端口表显示红色。

**先定位占用者**： «① 总览 → 端口占用» 表里已经写了 `PID 12345 xxx.exe`，或者：

```bat
netstat -ano -p TCP | findstr :6199
tasklist /FI "PID eq <上一步的PID>"
```

**处置顺序**：

1. **是上一个控制台没退干净**：任务管理器结束该 `python.exe`，或 `taskkill /PID <pid> /T /F`。
2. **是我们自己的服务残留**（SnowLuma/AstrBot 卡死）：在「④ 运行」页点对应卡片 **停止** → **启动**；
   停不掉就直接杀 PID，然后点「重启全部」。
3. **是别的软件占了**：在「③ 配置」换端口 → 保存 → 「⑤ 联调」写入联动配置 → 「④ 运行」重启全部。

**换端口的建议**：

| 端口 | 建议换成 | 注意 |
| --- | --- | --- |
| 8790（控制台） | 8791 / 18790 | 换后访问地址要跟着改；`一键启动.bat` 里的 URL 是 8790，需要自己改或直接用新地址访问 |
| 6185（AstrBot） | 6285 / 16185 | 改完在「运行」页重新打开面板链接 |
| 5099（SnowLuma） | 5199 / 15099 | 同上 |
| 6199（OneBot） | 6299 / 16199 | **两侧会自动同步**，不用手动改两个项目 |
| 3001（中继） | 3101 / 13001 | 仅 bridge 模式；四个端口不能重复 |

> 校验规则：8790 / 6185 / 5099 / 6199（+ bridge 模式的 3001）必须 **互不相同**，
> 否则「总览」的下一步建议会直接报「端口重复」。

---

## 4.5 端口自动避让 / 日志清理

**端口冲突**：控制台启动时如果配置端口（默认 8790）被占用，会自动往后找空闲端口并在控制台日志里提示
（想固定端口就用 `--port 8790` 显式指定，或设 `SLAB_STRICT_PORT=1` 关掉避让）。
AstrBot / SnowLuma / OneBot 三个端口由配置决定，可在「③ 配置 → 维护」点 **检查并自动换端口** 一次性理顺。

**日志与安装包**：默认保留 7 天，控制台每 6 小时自动清理一次（`data/logs/*.log`、`data/downloads/*.zip`）；
单个日志超过 20 MB 会先截断再保留尾部。改天数：「配置 → 维护」里的
`app.log_retain_days` / `app.download_retain_days`（0 = 不自动清理）。想立刻清一次就点「立即清理」。

---
## 5. 升级

### 5.1 升级控制台本身

```bat
:: 1) 备份配置和两个项目（最重要：data/config.json）
xcopy /E /I /Y data backup\data
xcopy /E /I /Y runtime backup\runtime

:: 2) 更新代码后重装依赖
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 5.2 升级 SnowLuma / AstrBot

* 界面路线：「② 部署」→ 对应卡片选新版本 → 重新点「下载安装」/「克隆并安装依赖」。
  实现上会重新拉取并覆盖安装目录，`runtime/<项目>/data` 里的业务数据会被保留（部署模块会避开 data 目录）。
* 稳妥路线（推荐给重要环境）：

```bat
:: 先备份整个安装目录
xcopy /E /I /Y runtime\astrbot backup\astrbot-20251029
:: 再升级，然后「写入联动配置」+「重启全部」
```

升级后 **务必重新点一次「写入联动配置」**，因为新版本可能改了配置文件结构。

---

## 6. 回滚

| 想回滚什么 | 怎么做 |
| --- | --- |
| 控制台配置 | 把 `backup\data\config.json` 复制回 `data\config.json`，然后重启控制台 |
| 某个项目版本 | 用备份目录整体覆盖 `runtime\<项目>`，或「② 部署」里选旧版本 tag 重装 |
| 全部 | 关掉控制台 → 用备份的 `data` + `runtime` 覆盖 → 重新 `一键启动.bat` |

> 回滚后建议在「④ 运行」页「重启全部」，并在「⑤ 联调」发一条测试消息确认链路。

---

## 7. 卸载

* **界面**：「② 部署」→ 对应卡片的 **卸载**（删除安装目录，`data/` 里的配置保留）。
* **手动**：关闭控制台 → 删除 `runtime\snowluma`、`runtime\astrbot`。
* **彻底清理**：再删除 `data\`（含配置、日志、下载缓存）与 `.venv\`（约 200 MB）。

卸载不会删除你在 QQ 侧的任何东西，也不会影响大模型账号。

---

## 8. 部署检查清单（装机后逐条对照）

- [ ]「一键启动.bat」跑完，浏览器能打开 <http://127.0.0.1:8790>
- [ ]「① 总览」环境自检：Python ✓、Git ✓（或已用离线包）、磁盘 ≥ 3 GB
- [ ]「② 部署」两个项目都显示 **已安装**
- [ ]「③ 配置」QQ 号、API Key、模型名已填，`保存设置` 无红色报错
- [ ]「⑤ 联调」点过 **写入联动配置**，四块状态至少 SnowLuma / AstrBot 有数据
- [ ]「④ 运行」两个服务绿灯；SnowLuma 面板里 QQ 显示已登录
- [ ]「① 总览」OneBot 连接灯为绿（多开 QQ 时至少一条正常即可）
- [ ] QQ 里 @机器人 或发 `/你好` 能收到回复
