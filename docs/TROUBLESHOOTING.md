# 排错手册（TROUBLESHOOTING）

按「症状」查。每条都给了**怎么确认**和**怎么处理**，尽量不用命令行。

先记住三个通用工具：

1. **「① 总览 → 下一步建议」**：它会根据当前状态直接告诉你下一步做什么。
2. **「④ 运行 → 实时日志」**：绝大多数问题的真实原因就在这里（选服务 + 级别筛 error/warn）。
3. **命令行自检**：`cd 项目目录 && .venv\Scripts\python.exe -m slab doctor`
   会打印环境、配置问题、四个端口占用情况。
   Linux 上是 `.venv/bin/python3 -m slab doctor`。

> **Linux 版**先看这三条（Windows 上不存在）：第 16 条 `setcap cap_sys_ptrace`、
> 第 17 条 Yama `ptrace_scope`、第 18 条 `DISPLAY` / 图形会话。
> 完整 Linux 指南在 [LINUX.md](LINUX.md)。

---

## 1. 浏览器打不开 <http://127.0.0.1:8790>（拒绝连接 / 空白页）

**现象**：ERR_CONNECTION_REFUSED、页面一直转圈、或者 404。

**确认**

* 那个**命令行窗口 / 终端**还开着吗？里面有 `Uvicorn running on http://127.0.0.1:8790` 吗？
* 窗口里有没有红色 Python 报错（traceback）？

**处理**

1. 窗口已经关了 / 报错退出 → 重新双击 **`一键启动.bat`**（Linux：重跑 `./一键启动.sh`），看最后的 `[错误]` 提示。
2. 提示 `ModuleNotFoundError: No module named 'fastapi'`（或 uvicorn / httpx / websockets）→ 依赖没装全：
   ```bat
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
   还是不行就删掉 `.venv` 文件夹重跑 `一键启动.bat`。
3. 提示 `[Errno 10048] error while attempting to bind on address` → **8790 被占用**，见第 2 条。
4. 端口改过（不是 8790）→ 用窗口里打印的「控制台地址」访问。
5. 页面能开但一片空白 → 按 `F12` 看 Console：若是 `/assets/app.js` 404，说明 `slab/web/` 文件缺失，
   重新解压/拷贝前端文件；若是旧缓存，`Ctrl+F5` 强刷。

---

## 2. 端口被占用（8790 / 6185 / 5099 / 6199 / 3001）

**确认**：「① 总览 → 端口占用」表会显示 `被占用` 和 `PID 12345 python.exe`。

**手动核对**

```bat
netstat -ano -p TCP | findstr :6199
tasklist /FI "PID eq <上一步的PID>"
```

**处理**（从轻到重）

1. 是**上一个控制台**没退干净 → 任务管理器结束那个 `python.exe`，或：
   ```bat
   taskkill /PID <pid> /T /F
   ```
2. 是**我们自己的 SnowLuma/AstrBot 卡住** → 「④ 运行」页对应卡片点 **停止**（不行就杀 PID），再点 **启动**。
3. 是**别的软件**（常见：QQ 自己、网易云、某些开发工具占用 3001/5099）→ 换端口：

   **「③ 配置」改端口 → 保存设置 → 「⑤ 联调」写入联动配置 → 「④ 运行」重启全部**

   | 改哪个 | 建议值 |
   | --- | --- |
   | 控制台 8790 | 8791 / 18790 |
   | AstrBot 面板 6185 | 6285 / 16185 |
   | SnowLuma 面板 5099 | 5199 / 15099 |
   | OneBot 6199 | 6299 / 16199（两侧自动同步） |
   | 中继 3001（仅 bridge） | 3101 / 13001 |

4. 提示「端口重复」→ 四个端口（含 bridge 模式的 3001）必须**互不相同**。

---

## 3. QQ 没有登录：SnowLuma 面板里不显示二维码 / 二维码刷不出来

**现象**：SnowLuma 已启动，但 QQ 一直离线；面板里没有二维码或二维码加载失败。

**确认**

* 「④ 运行」日志里选 `snowluma`，找有没有 `login` / `qrcode` / `scan` 字样；
* SnowLuma 面板（`http://127.0.0.1:5099`）能不能打开。

**处理**

1. 面板打不开 → 先解决端口/进程问题（第 1、2 条）。
2. 面板能开但没有二维码 → 在面板里手动点「重新登录 / 刷新二维码」；
   或者拔掉网线重连一次（协议端首次联网要拉取服务器列表）。
3. 二维码显示但扫码失败 → 用**手机 QQ 扫**，不要用微信；确认手机和电脑在同一网络环境更稳。
4. **必须用协议端支持的账号**：新注册/风控账号大概率登录失败，换个小号试。
5. 登录成功后又掉线（需要设备验证）→ 按手机 QQ 提示做「新设备验证」，或暂停几分钟再试；
   频繁重登容易触发风控，**别连续刷**。
6. 确认账号填对了：「③ 配置 → QQ 号」要和控制台面板里登录的账号一致（管理员权限、白名单都按这个号算）。

---

## 4. AstrBot 面板打不开 / 报 500

**确认**：「④ 运行」页 AstrBot 是不是绿灯；日志里搜 `Error`、`Traceback`、`500`。

**处理**

1. 服务没起 → 点 **启动**；若秒退，看日志最后 20 行（常见：依赖没装全、`data/` 目录权限、Python 版本不对）。
2. 依赖没装全的典型报错 `ModuleNotFoundError` →
   ```bat
   cd /d 项目目录\runtime\astrbot
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
   没有 `.venv` 就重新在「② 部署」点「克隆并安装依赖」。
3. 面板端口被占 → 换 `astrbot.webui_port`。
4. 面板能登录但页面 500 → 多半是 `data/cmd_config.json` 结构被旧版本写坏了：
   把 `runtime\astrbot\data\cmd_config.json` 改名备份（例如 `.bak`）→「⑤ 联调」点 **写入联动配置** 重新生成 →「④ 运行」重启 AstrBot。
5. 登录密码忘了 → 「③ 配置」里的 *AstrBot 面板密码*（`***` 表示已保存）。想换成已知密码：
   输入新密码 → 保存 → 联调页写入联动配置 → 重启 AstrBot。

---

## 5. OneBot 未连接（6199 没有连接 / 状态灯灰色）

**确认**：「① 总览」OneBot 灯是灰/黄；「⑤ 联调」的 OneBot 状态块没有连接记录。

**处理**（按顺序）

1. **AstrBot 没起来**：6199 是 AstrBot 提供的「反向 WS 服务端」，AstrBot 不跑就没人监听。
   先确认 AstrBot 绿灯。
2. **SnowLuma 没登录 QQ**：没登录就不会去连 AstrBot（第 3 条）。
3. **token 不一致**：最常见的隐形坑。重新 **「⑤ 联调」→ 写入联动配置**（会把两侧 token 一起刷新），
   然后「④ 运行」→ **重启全部**。
4. **端口不一致**：手工改过某个项目的配置？同样用「写入联动配置」覆盖回来。
5. **模式选错**：`native` 模式下不需要中继；如果配置成 `bridge` 但中继没启动，
   OneBot 灯也不会绿 —— 检查「④ 运行」有没有 `bridge` 卡片并启动它。
6. **多开 QQ**：SnowLuma 会给**每个已登录账号**各建一条连接。
   只要有一条正常，灯就是绿的（显示 `1/2 条正常` 之类）；某个账号掉线不影响整体判断。

---

## 6. 消息发出去了，机器人不回复

**先分层确认**（这是最关键的一步）：

| 检查点 | 怎么看 | 结论 |
| --- | --- | --- |
| ① 大模型通不通 | 「③ 配置」的 API Key / Base URL / 模型名 | 401/403 = Key 错；404 = 模型名或 URL 错 |
| ② AstrBot 收到事件了吗 | 「④ 运行」日志选 astrbot，发消息看有没有新行 | 没反应 = 卡在 OneBot 层（第 5 条） |
| ③ 触发规则匹配吗 | 「③ 配置 → 触发规则」 | 群聊默认要 **@机器人** 或 **`/` 开头** |
| ④ 白名单/开关 | 「允许私聊」「允许群聊」是否勾选 | 关掉就完全不理 |
| ⑤ 机器人被禁言/风控 | QQ 里看机器人状态 | 禁言中发不出去 |

**处理**

1. **群里说话没反应**：默认规则是「@机器人 或以 `/` 开头」。要么 @它，要么发 `/你好`。
   想让它对所有消息都回复 → 触发规则选「所有消息都回复」（注意刷屏风险）。
2. **前缀不对**：`trigger_prefixes` 默认是 `/`。你发的是 `！你好`（全角）就不会触发；
   改成你习惯的前缀，或按默认发。
3. **私聊没反应**：确认「允许私聊」勾选，且你是在私聊机器人账号（不是群里）。
4. **API 报错**：
   - `401 / invalid api key` → Key 复制错了（前后空格、漏字符），重新填 → 保存 → 重启 AstrBot。
   - `404 / model not found` → 模型名不对。DeepSeek 用 `deepseek-chat`，OpenAI 用 `gpt-4o-mini` 之类。
   - `429` → 余额/频率限制，去大模型控制台看额度。
   - 超时（90 秒）→ 网络到大模型 API 不通，或用了需要代理的接口。可先「⑤ 联调」发测试消息验证 QQ 侧 OK。
5. **回复了两遍 / 自己回自己**：多开账号时两个账号都在群里会各自回复；
   勾上「忽略自己发的消息」，或只保留一个账号在线。
6. **改了配置没生效**：配置写入两个项目后需要 **重启服务**（「④ 运行」→ 重启全部）。

---

## 7. 中文乱码（日志里全是 `锟斤拷` / `????` / `å­¦`）

**原因**：Windows 控制台代码页与子进程输出编码不一致（典型是 `GBK` 与 `UTF-8` 混用）。

**处理**

1. 控制台**自己**的窗口乱码 → `一键启动.bat` 已内置 `chcp 65001`；如果是手动起的，先执行
   ```bat
   chcp 65001
   ```
2. 服务日志乱码 → 控制台启动子进程时已强制 `PYTHONIOENCODING=utf-8` / `PYTHONUNBUFFERED=1`。
   若仍然乱码，说明该服务自己重设了编码：在「④ 运行」日志面板看是否影响使用
   （前端 `data/logs/*.log` 是 UTF-8 落盘，通常正常）。
3. 用记事本打开 `data\config.json` 看到乱码 → 别用记事本！用 VS Code / Notepad++ 以 **UTF-8** 打开。
   保存时也必须是 UTF-8，否则配置里的中文（人设等）会变成乱码。
4. 终端字体缺中文字形 → Windows Terminal 设置里把字体改成 `Microsoft YaHei Mono` / `Cascadia Mono`。

---

## 8. 防火墙弹窗 / 局域网内另一台电脑打不开面板

**现象**：Windows 弹出「是否允许 Python 访问网络」；或同局域网手机打不开 `5099`。

**说明**：默认所有服务都只监听 `127.0.0.1`，**本机使用不需要放行任何端口**。

**处理**

1. 弹窗 → 只勾 **专用网络**，或者直接点「取消」（本机使用不影响）。
2. 想让手机/另一台电脑访问 SnowLuma 面板：
   - SnowLuma 面板监听地址改成 `0.0.0.0`（在 SnowLuma 自己的配置里）；
   - Windows 防火墙放行 TCP 5099：
     ```bat
     netsh advfirewall firewall add rule name="SnowLuma WebUI" dir=in action=allow protocol=TCP localport=5099
     ```
   - 控制台本身建议**只监听 127.0.0.1**（它没有登录鉴权，暴露到局域网等于把配置和 Key 送人）。
3. 公司/学校网络封端口 → 换到手机热点试一次，能通说明是网络策略。

---

## 9. 杀毒软件拦截 `node.exe` / `python.exe`（下载被删、启动秒退）

**现象**：部署到一半失败、`runtime\snowluma` 里 `node.exe` 消失、服务启动后立刻 `failed`（退出码非 0）。

**处理**

1. 把整个项目目录加入杀软白名单（Windows Defender：**设置 → 病毒和威胁防护 → 排除项 → 添加文件夹**）：
   - `项目目录\runtime\snowluma`
   - `项目目录\runtime\astrbot`
   - `项目目录\.venv`
2. 被隔离的文件要从隔离区**恢复**，然后重新部署（「② 部署」→ 重新点下载安装）。
3. 第三方「安全卫士」类软件同理，注意它们的「进程防护」会拦 `taskkill`，导致「停止」按钮无效。
4. 公司电脑受管控（AppLocker/组策略）→ 需要 IT 放行，控制台无法绕过。

> **Linux 上没有这一条**（没有杀软拦 `node`）。Linux 上「服务启动秒退」的等价原因多半是
> 注入前置条件不满足（第 16、17 条）或缺 `python3-venv` / Python 版本不够。

---

## 10. 下载失败 / 卡在 `git clone` / 进度条不动

**确认**：「② 部署」的滚动日志里最后一行是什么。

| 日志关键字 | 含义 | 处理 |
| --- | --- | --- |
| `SSL` / `certificate` / `timeout` | 网络到 GitHub 不通 | 挂代理后重试（`set HTTPS_PROXY=http://127.0.0.1:7890`），或用离线包（见 DEPLOY.md#3） |
| `404` / `Not Found` | 版本 tag 写错或已下架 | 版本下拉改回 `latest` / `main` |
| `Permission denied` / `拒绝访问` | 安装目录被占用或没权限 | 关闭资源管理器里打开的该目录、关掉杀软实时防护、别放在 `C:\Program Files` 下 |
| `无法将"git"项识别为 cmdlet` | 没装 Git | 装 Git for Windows，或用离线包 |
| `pip` 相关超时 | PyPI 慢 | `set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` 后重试 |
| 进度条长期停在同一步 | 大文件下载中（full 版含 node.exe，几十 MB） | 再等等；超过 10 分钟无变化就取消，改用离线包 |

**通用兜底**：任何「下不下来」的情况，都可以用 **离线部署**（[DEPLOY.md 第 3 节](DEPLOY.md#3-离线安装内网--网络很差)）：
手动解压到 `runtime\snowluma` / `runtime\astrbot`，控制台会认这两个目录，界面显示「已安装」。

---

## 11. 页面右上角一直显示「已断开，重连中…」

**说明**：这是 SSE 实时通道断了（不影响配置保存、服务启停）。

**处理**

1. 控制台进程还在吗？（第 1 条）重启控制台后页面会自动重连。
2. 浏览器扩展（广告拦截/隐私保护）可能拦 `text/event-stream` → 用无痕窗口试。
3. 公司代理/MITM 会缓冲 SSE → 这类环境建议只看「④ 运行」手动点「重新加载」。
4. 断线期间数据不会丢：重连后会补发最近 30 条事件；历史日志可用「重新加载」从服务端取。

---

## 12. 关掉控制台窗口后，SnowLuma / AstrBot 也停了

**这是设计行为**，不是 bug：控制台是它们的父进程，退出时会 `stop_all()` 清理整棵进程树，避免留下孤儿进程。

**想让它们常驻**？

* 让控制台窗口一直开着（最小化即可）；
* 或者绕开控制台，自己按 [DEPLOY.md 手动部署](DEPLOY.md#2-手动部署无界面--定制) 里的命令启动两个项目（但那样就没有界面日志和状态了）。

---

## 13. 卸载 / 重装失败（目录被占用、文件删不掉）

**处理**

1. 先「④ 运行」→ **停止全部**；再关掉控制台窗口。
2. 资源管理器里关掉所有位于 `runtime\` 的窗口；任务管理器结束残留的 `node.exe` / `python.exe`。
3. 仍然删不掉 → 重启电脑后再删（Windows 文件句柄未释放）。
4. 重装后「② 部署」仍显示已安装 → 点「① 总览 → 刷新状态」；
   还不行说明目录还在（半截安装），手动删掉 `runtime\<项目>` 再装。

---

## 14. 二维码扫了、也提示成功，但 QQ 立刻掉线 / 频繁重连

**原因**：协议端风控、设备锁、账号安全策略。

**处理**

1. 换**小号**（首选，最省事）。
2. 手机 QQ 里做「新设备验证 / 登录保护」；
3. 保持网络稳定，别频繁重启 SnowLuma（每次重启都可能触发风控）；
4. 24 小时内多次掉线 → 停一天再用，或换账号；
5. 账号已经出现「异地登录/存在风险」提示 → 立刻停止使用该账号，避免封号。

> 提醒：使用第三方 QQ 协议端存在封号风险，请始终使用小号，详见 README 的免责声明。

---

## 15. 一切都绿了，机器人还是不说话（终极检查表）

按顺序核对，**任一项不满足都会导致没回复**：

- [ ] SnowLuma 面板里 QQ 显示 **在线**（不是「登录中」）
- [ ] AstrBot 面板能正常登录，且「平台」里有 `aiocqhttp` 且是启用状态
- [ ] 「⑤ 联调」OneBot 状态块里 **至少一条连接** 正常
- [ ] 触发规则与你的发消息方式匹配（群里 @ 或 `/` 前缀）
- [ ] API Key 有效、模型名正确、账号有余额
- [ ] 机器人没被禁言、没被群管理限制
- [ ] 日志里没有 `401/404/429/Timeout`
- [ ] 改过配置后 **重启过服务**

还不行就把「④ 运行」里 `astrbot` 与 `snowluma` 的 error 级日志复制出来，对照第 6 条逐项排查。

---

## 16. Linux：注入报 `EPERM` / `Operation not permitted`（缺 `cap_sys_ptrace`）

> 本节只对 **Linux** 有效。Windows 上不会出这个错误（那边是 DLL 注入）。

**现象**：日志里出现 `EPERM`、`attach failed`、`ptrace(PTRACE_ATTACH)` 之类的报错；
「① 总览 → 环境自检」里 **「node 的 cap_sys_ptrace」** 一项是红或黄；QQ 明明登录了却接不上。

**确认**

```bash
NODE="$(readlink -f ~/.local/share/SnowLumaAstrBot/runtime/snowluma/node)"
getcap "$NODE"            # 期望：cap_sys_ptrace=ep；空 = 没有能力位
findmnt -no OPTIONS "$(dirname "$NODE")" | grep -o nosuid   # 有输出说明挂载带 nosuid，能力位不生效
```

**处理**

```bash
sudo setcap cap_sys_ptrace=ep "$NODE"
# 重启 SnowLuma（只重启 SnowLuma 即可，不必重启控制台）
```

- 一定要打**真实路径**（`readlink -f` 的结果），符号链接不算；
- **升级 / 重装 SnowLuma 会换掉 node 文件 → 能力位丢失**，升级后必须重跑一次；
- 文件所在分区是 `nosuid` 挂载时能力位无效 → 把存储根换到普通分区（`export SLAB_STORAGE_ROOT=/opt/…`），
  或改用第 17 条的 `ptrace_scope=1` + 同 uid；
- 控制台与 QQ **必须是同一个 uid**（都别用 sudo）；详见 [LINUX.md](LINUX.md) §4。

---

## 17. Linux：`ptrace_scope=3`（Yama 完全禁止 ptrace）

**现象**：`环境自检` 里 **「ptrace_scope」** 一项红，提示 `Yama ptrace_scope=3`；注入一定失败。

**确认**

```bash
cat /proc/sys/kernel/yama/ptrace_scope     # 3 = 完全禁止，连 root 都不行
```

**处理**

```bash
# 临时
sudo sysctl -w kernel.yama.ptrace_scope=1
# 永久（推荐）
echo 'kernel.yama.ptrace_scope=1' | sudo tee /etc/sysctl.d/99-snowluma.conf
sudo sysctl --system
```

0/1/2 都可用（1 是 Ubuntu 默认值，需要配合第 16 条的能力位）。值被云厂商镜像锁死、改不动时，
只能换机器或找管理员。

---

## 18. Linux：QQ 起不来 / 扫码登录不了（没有图形会话）

**现象**：`/opt/QQ/qq` 启动没反应或直接退出；日志里有 `cannot open display`；
「环境自检」的 **「图形会话（DISPLAY）」** 一项黄/红。

**确认**

```bash
echo "DISPLAY=[$DISPLAY] WAYLAND_DISPLAY=[$WAYLAND_DISPLAY]"   # 两个都空 = 没有图形会话
ls /tmp/.X11-unix/                                             # 有 X0/X99 说明本机有 X 服务
```

**处理**

1. 在**桌面会话**里（或通过远程桌面/物理显示器）操作：直接启动 QQ 扫码一次即可，SnowLuma 之后接管的是这个已登录进程；
2. 纯 SSH 无头服务器：按 [LINUX.md](LINUX.md) §7 起 `Xvfb + fluxbox + x11vnc + noVNC`，
   在 VNC 里开 QQ 扫码；带 `DISPLAY=:99` 的环境启动控制台；
3. Wayland 桌面：QQ 走 XWayland，通常正常；若扫码窗口空白，试试 `GDK_BACKEND=x11` 或直接在 X11 会话登录。

> 提示：SnowLuma **不会**自己弹二维码 —— 扫码始终发生在 QQ 客户端里。

---

_没覆盖到的情况？把「① 总览」的环境自检、「④ 运行」的 error 日志、`python -m slab doctor` 的输出整理一下，基本就能定位。_
