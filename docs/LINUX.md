# Linux 部署与使用（LINUX）

本文是 **Linux 版**专用的部署与排错说明。Windows 用户看 [README.md](../README.md) 与
[MANUAL.md](MANUAL.md) 就够了；两边行为不同的地方，本文会明确写出来。

> 一句话差异：Windows 版是「双击 exe」，Linux 版是「运行 `一键启动.sh`」；
> 而且 Linux 上 SnowLuma 接管 QQ 靠的是 **ptrace 注入**，比 Windows 多两道前置条件
> （`cap_sys_ptrace` 能力位 + Yama `ptrace_scope`）。这两件事没配好，其它都会「看起来正常但接不上 QQ」。

---

## 0. 支持矩阵

| 发行版 / 平台 | 状态 | 说明 |
| --- | --- | --- |
| **Ubuntu 24.04 LTS x64** | ✅ 主要支持 | 仓库自带 `python3` = 3.12，满足 AstrBot 的 Python ≥ 3.12 |
| **Ubuntu 22.04 LTS x64** | ✅ 主要支持 | 仓库只有 Python 3.10，**必须**用 uv 或 deadsnakes 补一个 3.12（见 §1.2） |
| Debian 12 (bookworm) x64 | ⚠️ 尽力支持 | 结构上与 Ubuntu 22.04 同代（Python 3.11）；依赖包名与 systemd 行为可能有小差异，未逐项验证 |
| 其它 systemd 发行版（Fedora / Arch 等） | ⚠️ 未验证 | 大概率可用，但本文的 `apt` 命令要自己翻译 |
| **linux-arm64**（树莓派 / ARM 云主机） | ⚠️ **未实测** | 控制台按 `plat.platform_tag()` 选 `linux-arm64` 资产，**选包 / 解压 / `chmod +x` / 建 venv 均已覆盖并有单测**；但 QQ 官方是否提供 arm64 deb 请到下载页确认，本项目**未在 arm64 实机验证**端到端链路（ptrace 注入 + QQ） |
| Windows 10 / 11 x64 | ✅ | 见 README，Linux 文档里的 setcap / ptrace 部分在 Windows 上**不适用** |
| macOS | ❌ 不支持 | `plat.require_supported()` 会直接报错 |

其它要求：

* 架构：x86_64（`uname -m` → `x86_64`）/ aarch64（未实测）
* 磁盘：≥ 3 GB 空闲
* 图形会话：**扫码登录需要一个图形会话**（X11 或 XWayland）。纯 SSH 无头服务器见 §7
* 权限：整个控制台**以一个普通用户**跑完（不要 sudo 跑控制台，也不要 sudo 跑 QQ，见 §4）

---

## 1. 安装

### 1.1 系统依赖（apt 一行）

Ubuntu 24.04：

```bash
sudo apt update && sudo apt install -y \
  git python3 python3-venv python3-pip libcap2-bin xdg-utils curl ca-certificates
```

Ubuntu 22.04 把 `python3` 换成 3.12 之后再装（见 §1.2），其余同上。

| 包 | 为什么需要 | 缺了会怎样 |
| --- | --- | --- |
| `git` | 克隆 AstrBot | 「部署」页报「未检测到 git」 |
| `python3` + `python3-venv` | 控制台自己与 AstrBot 的虚拟环境 | 建 venv 时报 `ensurepip is not available` |
| `libcap2-bin` | 提供 `getcap` / `setcap` | 控制台无法自检 node 是否有 `cap_sys_ptrace` |
| `xdg-utils` | 「打开目录」按钮（`xdg-open`） | 按钮点了没反应（不影响其它功能） |
| `curl` | 安装 uv / 下载发行包 | 下载失败 |

可选：`uv`（更快的 Python 包管理器，也能帮你拉 3.12）、`zenity` 或 `kdialog`（「选择…」按钮的原生目录选择框；
都没有时后端返回 501，只能手输路径）、`build-essential`（AstrBot 某些插件要编译 C 扩展）。

### 1.2 Python ≥ 3.12（AstrBot 的硬门槛）

AstrBot 要求 **Python ≥ 3.12**。Ubuntu 24.04 自带的就是 3.12，直接跳过本小节；
**Ubuntu 22.04 自带 3.10，必须补**：

```bash
# 方式 A（推荐）：用 uv 装一个 3.12，控制台部署 AstrBot 时会优先用它
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12

# 方式 B：deadsnakes PPA
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update && sudo apt install -y python3.12 python3.12-venv
```

检查：`python3 --version` 或 `uv python find 3.12`。

### 1.3 Node.js：**不要用发行版的包**

SnowLuma 对 Node 的版本要求是 **`^24.22.1 || >=26.12.1`**（见 `slab/installer.py:NODE_REQUIRED`）。
Ubuntu 24.04 仓库里的 `nodejs` 是 18.x、22.04 是 12.x —— **发行版包必然不满足**，装了也没用。

所以：

* **用 SnowLuma full 版**（默认选项）：发行包里自带 `./node`，控制台直接用它，**不需要系统装 Node**。
* 只有选了 **lite 版** 才需要自己准备 Node：要么 NodeSource 仓库，要么 `nvm` / `fnm`，
  要么直接用 full 版。装完后 `node --version` 必须满足上面的范围。

> full 版自带的 node 在 `<SnowLuma 安装目录>/node`，`setcap` 要打在**这个真实路径**上（见 §4）。

### 1.4 拿到控制台并启动

源码方式：

```bash
git clone <本仓库> snowluma-astrbot-console
cd snowluma-astrbot-console
chmod +x 一键启动.sh   # git 在 Windows 上记录不了可执行位，克隆下来先补一次
./一键启动.sh          # 建 .venv → 装 requirements → 起服务 → 打开 http://127.0.0.1:8790
```

> 不想 chmod 也行：直接 `bash 一键启动.sh`，效果一样。

`一键启动.sh` 做的事与 Windows 的 `一键启动.bat` 一一对应：检查 `python3` / `python3-venv`、
创建 `.venv`（优先 `uv venv`，否则 `python3 -m venv`）、装依赖、前台运行 `python3 -m slab serve`。

发布包方式（浏览器版 tar.gz，由 `packaging/build-linux.sh` 产出）：

```bash
tar -xzf SnowLuma-AstrBot-Console-v…-linux-x64.tar.gz
cd SnowLuma-AstrBot-Console-v…-linux-x64
./start.sh            # 包内自带的启动器（等价于上面的 一键启动.sh）
```

手动等价命令：

```bash
cd <项目目录>
.venv/bin/python3 -m slab serve --port 8790
```

> 与 Windows 的对应关系：`.venv\Scripts\python.exe` ↔ `.venv/bin/python3`，
> `一键启动.bat` ↔ `一键启动.sh`，`SnowLuma-Desktop.exe` ↔ 桌面版的可执行文件（Linux 上没有 `.exe` 后缀）。
> 前端会自动把这些说法按平台切换，不需要你记。

---

## 2. 平台层是什么（给二次开发者）

`slab/plat.py` 是 **Windows / Linux 差异的唯一收口处**，其它模块只 import 它，不许自己写
`if os.name == "nt"`。它负责：

| 能力 | Windows | Linux |
| --- | --- | --- |
| 发行目标 | `win-x64` | `linux-x64` / `linux-arm64` |
| 可执行文件后缀 | `.exe` | 无 |
| venv 解释器 | `.venv\Scripts\python.exe` | `.venv/bin/python3` |
| 子进程「不弹窗」 | `CREATE_NO_WINDOW` | 无此概念，改用独立进程组（`start_new_session`） |
| 结束进程树 | `taskkill /T /F` | 先对进程组 `SIGTERM`，3 秒后 `SIGKILL` |
| 端口 → PID | `GetExtendedTcpTable` / `netstat` | `/proc/net/tcp{,6}` → socket inode → `/proc/<pid>/fd`，`ss` 兜底 |
| 默认存储根 | 非 C 盘盘符 | `$XDG_DATA_HOME/SnowLumaAstrBot`（默认 `~/.local/share/SnowLumaAstrBot`） |
| QQ 安装位置 | 注册表 + `Program Files` | `/opt/QQ`（官方 deb） |
| QQ 多开 | 注入 sandbox DLL（「深度分身」） | **独立 HOME**（见 §6），没有 DLL 注入这回事 |

前端拿到的平台信息来自后端 `envcheck.env_report()`（`GET /api/state` 里的 `env`）：
`env.is_linux` / `env.is_windows` / `env.tag` / `env.os` / `env.platform`，以及
`env.checks[]`（环境自检逐项：`label` / `ok` / `value` / `fix`）。**前端不猜系统**。

---

## 3. 目录与日志在哪

默认存储根（XDG）：

```
~/.local/share/SnowLumaAstrBot/
├─ data/           配置（config.json）、日志、备份、安装包缓存
│  ├─ logs/        console.log / snowluma.log / astrbot.log / bridge.log
│  └─ backups/     卸载与升级前的备份
├─ runtime/        snowluma/ 与 astrbot/ 的实际安装（各自有 logs/）
├─ downloads/      安装包缓存
└─ qq/clones/      独立 HOME 多开用的分身目录
```

想换位置就设环境变量（优先级从高到低）：

```bash
export SLAB_STORAGE_ROOT=/opt/snowluma-astrbot   # 整体搬家（推荐）
export SLAB_DATA_DIR=…                            # 只换数据目录
export SLAB_RUNTIME=…                             # 只换两个项目的安装目录
export SLAB_DOWNLOAD_DIR=…
```

日志速查：

```bash
tail -f ~/.local/share/SnowLumaAstrBot/data/logs/console.log
tail -f ~/.local/share/SnowLumaAstrBot/data/logs/snowluma.log
ls      ~/.local/share/SnowLumaAstrBot/runtime/snowluma/logs/
journalctl --user -u snowluma-astrbot-console -f   # 用 §8 的 systemd 单元时
```

端口：控制台 **8790**、AstrBot 面板 **6185**、SnowLuma 面板 **5099**、AstrBot 反向 WS **6199**
（SnowLuma 自己的 OneBot 端口默认 3000/3001）。四个端口不能撞车，「总览 → 端口占用」会显示占用者
（Linux 上通过 `/proc` 拿到 `PID 1234 python3`）。

---

## 4. 注入前置条件：setcap + ptrace_scope + 同 uid

Linux 没有 Windows 那种 DLL 注入，SnowLuma 是 **用 ptrace 附着到已登录的 QQ 进程**上接管会话。
这要求三件事同时成立：

### 4.1 node 二进制的 `cap_sys_ptrace` 能力位

```bash
# 1) 找到控制台实际使用的 node（full 版自带）
NODE="$(readlink -f ~/.local/share/SnowLumaAstrBot/runtime/snowluma/node)"
# 2) 打上能力位
sudo setcap cap_sys_ptrace=ep "$NODE"
# 3) 确认
getcap "$NODE"        # 期望输出：/…/snowluma/node cap_sys_ptrace=ep
```

要点：

* 一定要打在 **真实路径**（`readlink -f` 的结果）上，符号链接不算。
* **升级 / 重装 SnowLuma 会换掉 node 文件，能力位随之丢失** → 升级后重跑一次 `setcap`。
  控制台「总览 → 环境自检」里的「node 的 cap_sys_ptrace」一项变红就是这个原因。
* 文件所在的文件系统不能是 `nosuid` 挂载（`findmnt -no OPTIONS <挂载点>`），否则能力位不生效。
  把存储根放到 `nosuid` 的分区上时，改用 §4.2 的 `ptrace_scope=1` + 同 uid 兜底。

### 4.2 Yama `ptrace_scope`

```bash
cat /proc/sys/kernel/yama/ptrace_scope
```

| 值 | 含义 | 能否注入 |
| --- | --- | --- |
| 0 | 传统模式：有 `CAP_SYS_PTRACE` 就能 attach | ✅ |
| 1 | 只能 attach 自己的子进程，或有 `CAP_SYS_PTRACE` | ✅（配合 §4.1 的能力位；QQ 不是 node 的子进程，所以这里**必须**有能力位） |
| 2 | 仅 root（`CAP_SYS_PTRACE`） | ✅（同上，能力位够就用） |
| **3** | **完全禁止 ptrace**（连 root 都不行） | ❌ 直接判死，必须改 |

Ubuntu 默认是 `1`，正常可用。若看到 `3`：

```bash
# 临时（重启失效）
sudo sysctl -w kernel.yama.ptrace_scope=1
# 永久
echo 'kernel.yama.ptrace_scope=1' | sudo tee /etc/sysctl.d/99-snowluma.conf
sudo sysctl --system
```

> `ptrace_scope=3` 常见于加固过的云主机镜像 / 企业基线。改不了（受管机器）就只能换机器或找管理员。

### 4.3 与 QQ **同一个 uid**

SnowLuma（node 进程）与 QQ 必须是**同一个 Linux 用户**。所以：

* **不要 `sudo` 跑控制台**，也不要用 `sudo` 启动 QQ；两边都在你自己的账号下跑。
* 也不要用「控制台用 A 用户、QQ 用 B 用户」这种组合（Windows 上那句「必须是同一个 Windows 用户」
  在 Linux 上等价为「必须是同一个 uid」）。
* 用 §8 的 `systemd --user` 单元时天然满足（都在你的 uid 下）。

三件事都满足后，「运行 → 接入 QQ → 刷新进程」才能列出 QQ 进程并注入；日志出现
`[Hook] login detected` 才算接上。

---

## 5. 安装官方 Linux QQ 并扫码登录

SnowLuma 接管的是**已经登录的 QQ 客户端**，它自己不弹二维码。所以第一步是把官方 Linux QQ 装好并登录。

> 要装的是 **QQNT**（新版，文件名形如 `QQ_<版本>_<日期>_<arch>_<序号>.deb`，装到 **`/opt/QQ`**）。
> `<arch>` 用 Debian 命名：**x64 是 `amd64`**（不是 `x86_64`、也不是 `linux-x64`），**arm64 是 `arm64`**。
> **版本 / 日期 / 序号段会随上游发版变化，具体文件名以官方下载页为准，别照抄下面的示例。**
> 老教程里的 `linuxqq_*.deb` 是已停更的 **QQ for Linux 2.0**，它装到 `/opt/tencent-qq`，
> **SnowLuma 注入不了**，别装错。

1. 从 **QQ 官方 Linux 下载页**（<https://im.qq.com/linuxqq/>）**按架构选择**下载 `.deb` 包：
   x64 机器选 `amd64`、arm64 机器选 `arm64`。
2. 安装（用 glob 装，不用记完整文件名）：

   ```bash
   sudo apt install -y ./QQ_*.deb      # x64 机器用 amd64 包、arm64 机器用 arm64 包；一般装到 /opt/QQ
   # 想显式区分架构（与上面等价）：
   #   sudo apt install -y ./QQ_*_amd64.deb     # x64
   #   sudo apt install -y ./QQ_*_arm64.deb     # arm64
   # 依赖缺失时：
   sudo apt -f install
   ```

3. 确认装到哪了（官方 QQNT deb 默认 **`/opt/QQ`**）：

   ```bash
   dpkg -c ./QQ_*.deb | grep -m1 opt/QQ   # 先看包装到哪
   ls /opt/QQ && /opt/QQ/qq --version
   ```

4. 在**图形会话**里启动 QQ 并扫码登录机器人小号：

   ```bash
   /opt/QQ/qq &
   ```

   * Wayland 桌面：QQ 走 XWayland，扫码窗口正常显示；桌面上没窗口时检查 `echo $DISPLAY`。
   * 登录后**别退出、别切号**，让 QQ 留在后台。
5. 回控制台「运行 → 接入 QQ」→「刷新进程」→ 对小号那个进程点「用这个」。

> **扫码受限的典型场景**：纯 SSH 登录（`DISPLAY` 为空）时 QQ 起不来，扫码也就无从谈起。
> 控制台的环境自检里「图形会话（DISPLAY）」一项会变黄/红并给出提示；要么在桌面里操作，
> 要么按 §7 搭一个 Xvfb + VNC 的虚拟桌面。

---

## 6. 独立 HOME 多开（Linux 的「分身」）

Windows 上的「深度分身」是注入 sandbox DLL 给互斥体改名；**Linux 上没有 DLL 注入这回事**，
等价做法是 **给每个 QQ 实例一个独立的 `HOME`**：

* QQ 的单实例锁在 `$HOME/.config/QQ/SingletonLock`；
* 换掉 `HOME`（以及 `XDG_CONFIG_HOME` / `XDG_CACHE_HOME` / `XDG_DATA_HOME`），第二个实例就起来了。

手工等价命令：

```bash
mkdir -p ~/qq-bot-home
HOME=~/qq-bot-home \
XDG_CONFIG_HOME=~/qq-bot-home/.config \
XDG_CACHE_HOME=~/qq-bot-home/.cache \
XDG_DATA_HOME=~/qq-bot-home/.local/share \
  /opt/QQ/qq --no-sandbox &
```

控制台里的入口：**「运行 → 接入 QQ」→「独立 HOME 多开」**（按钮在 Linux 上就是这个说法，
不再是「隔离启动 QQ」）。它会：

1. 在 `<存储根>/qq/clones/<名称>/home` 准备一个干净 HOME（默认存储根见 §3）；
2. 用这个 HOME + `--no-sandbox` 起 `/opt/QQ/qq`；
3. 列表里给这个实例打上「隔离实例」徽标。

之后在这个窗口里登录小号，再回控制台「刷新进程 → 用这个」注入。

> 注意：多开只是隔离了 QQ 自己的数据；**注入仍然要求 §4 的三件套**（能力位 / ptrace_scope / 同 uid）。
> 「未检测到已安装的 QQ」时，页面会直接给出 apt / 官方 deb 的安装指引（即 §5 的第 2 步）。

---

## 7. 无头服务器（Xvfb + VNC，仅指引）

> 本节只给操作步骤，**控制台不会自动帮你搭**。目的是：服务器没有物理显示器时，
> 也能给 QQ 一个图形会话完成扫码登录。

```bash
# 1) 装虚拟显示与 VNC
sudo apt install -y xvfb fluxbox x11vnc novnc websockify

# 2) 起一个虚拟 X 屏幕（记住 :99）
Xvfb :99 -screen 0 1280x900x24 &
export DISPLAY=:99

# 3) 起窗口管理器（不装也能跑，但窗口会没边框、位置乱）
fluxbox &

# 4) 把这块屏幕通过 VNC 暴露出来（只监听本机，安全）
x11vnc -display :99 -localhost -forever -shared -nopw -rfbport 5900 &

# 5) 浏览器版 VNC（可选，省得装 VNC 客户端）
websockify --web /usr/share/novnc 6080 localhost:5900 &
```

然后：

* 有 VNC 客户端：`ssh -L 5900:127.0.0.1:5900 <服务器>`，连 `127.0.0.1:5900`；
* 只有浏览器：同样做 SSH 隧道到 `6080`，打开 `http://127.0.0.1:6080/vnc.html`。

在 VNC 里启动 QQ（`DISPLAY=:99 /opt/QQ/qq &`）扫码登录；控制台本身仍用
`http://127.0.0.1:8790`（通过 SSH 隧道转发）。

**重要**：控制台（以及它拉起的 SnowLuma / node 注入进程）必须在**带 `DISPLAY=:99` 的环境**里启动，
否则 QQ 拿不到图形会话：

```bash
export DISPLAY=:99
./一键启动.sh
```

安全提醒：`x11vnc -nopw` 只适合「只监听 127.0.0.1 + SSH 隧道」。**永远不要把 5900 暴露到公网**；
需要远程直接用就加 `-rfbauth` 密码并配好防火墙。

---

## 8. 保活：systemd --user

浏览器版没有窗口可挂，推荐用 user 单元（不需要 root，跟你的 uid 一致——正好满足 §4.3）。

`~/.config/systemd/user/snowluma-astrbot-console.service`：

```ini
[Unit]
Description=SnowLuma x AstrBot console
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/snowluma-astrbot-console
ExecStart=%h/snowluma-astrbot-console/.venv/bin/python3 -m slab serve --port 8790
Restart=on-failure
RestartSec=5
# 注入需要与 QQ 同 uid；需要图形会话时补上这两行（无头机器按 §7 起 Xvfb）
#Environment=DISPLAY=:99
#Environment=XAUTHORITY=%h/.Xauthority
KillMode=control-group
TimeoutStopSec=20

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now snowluma-astrbot-console
# 未登录也要跑（服务器重启后自动起）
sudo loginctl enable-linger "$USER"
journalctl --user -u snowluma-astrbot-console -f
```

要点：

* `KillMode=control-group` + `TimeoutStopSec=20`：控制台自己会先停子服务，给足收尾时间；
  超时后 systemd 把整个 cgroup 收掉，不会留孤儿端口。
* 别用 `systemctl --system` + `User=` 混搭：那样 `DISPLAY` / `XDG_*` 与 QQ 的 uid 容易错位。
* 想退出时保留机器人不掉线：**不要在停机时 `stop` SnowLuma/AstrBot 之外还去 kill QQ**。

---

## 8.5 应用内升级（zip / tar.gz）

Linux 版每次发布都产出**两个包**，用途不同，别拿错：

| 产物 | 用途 | 怎么用 |
| --- | --- | --- |
| `SnowLuma-AstrBot-Console-v…-linux-x64.tar.gz` | **分发 / 首次安装** | 解压即用，包里就是 `start.sh` + `VERSION` + `README.md`（有文档时会带 `docs/LINUX.md`） |
| `SnowLuma-AstrBot-Console-v…-linux-x64.zip` | **应用内升级专用** | 「配置 → 维护 → 升级控制台」里选它 |

> 为什么不能拿 tar.gz 升级：`slab/upgrade.py` 的 `inspect_zip()` 只接受 `.zip`
> （`suffix != ".zip"` 直接拒绝），这是 Windows 时代就定下的约定。
> 桌面版同理：`SnowLuma-Desktop-v…-linux-x64.zip` 才是升级包。

**升级流程**（与 Windows 一致）：备份程序文件 → 程序自动退出 → 用新包覆盖 → 自动重启。
配置（`data/`）、SnowLuma / AstrBot 的安装、下载缓存都不会动，失败会自动回滚。

**版本判定**：升级助手读压缩包**根目录里最浅的那个 `VERSION`**（文件名大小写不敏感）。
桌面版 zip 里 Electron 自带一个叫 `version` 的文件（内容是 Electron 版本，形如 `33.4.11`），
打包脚本已把它改名成 `electron-version`，并在包根放上本项目的 `VERSION`，
否则升级会把包版本误判成 `33.x`。

**可执行位**：Python 的 `zipfile` 不还原 POSIX 权限，直接覆盖会让 `start.sh` 变成 `0644`。
升级助手现在按 zip 条目的 `external_attr` 还原权限位与软链，并在覆盖后再补一次 `chmod +x`，
所以**正常情况下你不需要手动 chmod**。

> 兜底：万一还是遇到 `Permission denied`（例如你手工用别的工具解压过），在包目录执行
> `chmod +x start.sh`（桌面版是 `chmod +x SnowLuma-Desktop`）即可，不用重装。

**手动整目录替换**：用 tar.gz —— 停掉控制台 → 解压到新目录 → 把 `data/`、`runtime/`
搬过去（或设 `SLAB_STORAGE_ROOT` 指回原处）→ `chmod +x start.sh && ./start.sh`。
跨文件系统或某些解压工具可能丢权限位，所以最后那句 `chmod +x` 建议照抄。

---

## 9. 常见故障

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 环境自检「node 的 cap_sys_ptrace」红 | 没 setcap / 升级后丢了 | §4.1 重打 `setcap cap_sys_ptrace=ep <真实 node 路径>`，然后重启 SnowLuma |
| 环境自检「ptrace_scope」红（值为 3） | Yama 完全禁止 ptrace | §4.2 改成 1（临时 + 永久两行） |
| 环境自检「图形会话（DISPLAY）」黄/红 | SSH 无头，没有 DISPLAY | 在桌面里跑，或按 §7 起 Xvfb；否则无法扫码登录 |
| 注入报 `EPERM` / 日志里 `Operation not permitted` | 能力位没生效（`nosuid` 挂载 / 打了错的路径 / 升级后丢了） | `getcap` 复查真实路径；`findmnt -no OPTIONS` 看是不是 `nosuid` |
| 注入「成功」但 QQ 没反应 | SnowLuma 与 QQ **不是同一个 uid**（一个 sudo 了一个没有） | 两边都别用 sudo，统一在你自己账号下跑 |
| 「未检测到已安装的 QQ」 | 没装官方 Linux QQ，或装在非常规路径 | §5 装 deb（默认 `/opt/QQ`）；非常规路径用「安装目录」手填 |
| 第二个 QQ 起不来 / 弹已有实例 | 没换 `HOME`（单实例锁） | 用 §6 的独立 HOME，或点「独立 HOME 多开」 |
| 建 venv 报 `ensurepip is not available` | 缺 `python3-venv` | `sudo apt install -y python3-venv` |
| AstrBot 安装报 Python 版本不符 | Ubuntu 22.04 只有 3.10 | §1.2 用 uv 装 3.12，然后重新部署 AstrBot |
| 「部署」页提示未检测到 git | 没装 git | `sudo apt install -y git` |
| lite 版启动即失败，日志里 node 版本不对 | 用了发行版的 nodejs（18.x / 12.x） | 改用 **full 版**（自带 node），或按 §1.3 装合规 Node |
| 升级后 `./start.sh` 报 `Permission denied` | 手工解压过、权限位丢了（升级助手正常会自己补） | `chmod +x start.sh`（桌面版 `chmod +x SnowLuma-Desktop`），见 §8.5 |
| 「升级控制台」选 tar.gz 提示不认 | 应用内升级只接受 `.zip`（`inspect_zip` 限定） | 同版本再下 `.zip` 升级包；tar.gz 用于首次安装/手动替换，见 §8.5 |
| 「打开目录」按钮报 501 / 没反应 | 缺 `xdg-open`（后端返回 501，提示「当前环境打不开文件管理器」） | `sudo apt install -y xdg-utils` |
| 控制台报 `/api/deploy/pick-dir` 501 | 没有图形目录选择框（缺 `zenity` / `kdialog`；后端会按 zenity → kdialog 顺序找） | 直接在输入框里手输完整路径（如 `/opt/SnowLuma`、`$XDG_DATA_HOME/SnowLumaAstrBot`）；想用弹框就 `sudo apt install -y zenity` |
| 端口被占用但看不到占用者 | 别的用户/容器的进程，`/proc` 读不到 fd | `sudo ss -ltnp` 手动确认，换端口 |
| 关掉终端后服务全停 | 控制台是前台进程（设计如此） | 用 §8 的 systemd --user 保活 |
| 升级控制台后 `setcap` 失效 | node 文件被替换 | 重跑 §4.1 |
| arm64 上各种奇怪失败 | arm64 未实测 | 换 x64 机器，或按 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) 逐段排查 |

---

## 10. 与 Windows 版的措辞对照

前端会按平台切换文案；看到下列词时两边指的是同一件事：

| Windows 说法 | Linux 对应 |
| --- | --- |
| C 盘 / 盘符 / 「想装到 D 盘」 | **挂载点 / 目录**（`$XDG_DATA_HOME` 或 `SLAB_STORAGE_ROOT`） |
| 「不会弹出命令行黑框」 | Linux 没有这个概念（本来就在终端里跑） |
| 「双击 `SnowLuma-Desktop.exe`」 | **运行 `一键启动.sh`** |
| `python.exe` / `.venv\Scripts\python.exe` | `python3` / `.venv/bin/python3` |
| 「深度分身（DLL 注入）」 | **独立 HOME 多开**（§6） |
| 「QQ 用管理员运行」 | **同一个 uid**（§4.3） |
| 「杀软拦截 `node.exe`」 | 无杀软拦截；对应的是 §4 的 `setcap` / `ptrace_scope` |
| 任务栏 / 资源管理器 | 桌面环境 / 文件管理器（`xdg-open`） |

---

## 11. 相关文档

* [README.md](../README.md) —— 项目首页、功能一览、快速开始
* [DEPLOY.md](DEPLOY.md) —— 部署细节（下载源、版本、目录、端口、离线安装）
* [UPGRADE.md](UPGRADE.md) —— 升级指南（版本规则、升级方式、回滚；Linux 的 zip/tar.gz 区别见本文 §8.5）
* [MANUAL.md](MANUAL.md) —— 操作手册（含「登录机器人 QQ」）
* [TROUBLESHOOTING.md](TROUBLESHOOTING.md) —— 逐条故障树
* [ARCHITECTURE.md](ARCHITECTURE.md) —— 架构与数据流（含平台层）
* [CHANGELOG.md](../CHANGELOG.md) —— 版本变更（Linux 版见 1.8.0）
