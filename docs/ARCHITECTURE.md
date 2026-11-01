# 架构说明（ARCHITECTURE）

本文解释「消息怎么走」「端口谁占谁」「控制台改了配置之后写到哪里」。
目标读者：想自己排错、二次开发，或者只想搞清楚原理的人。

---

## 1. 三个角色，各管一段

| 角色 | 是什么 | 负责 | **不**负责 |
| --- | --- | --- | --- |
| **SnowLuma** | 第三方 QQ 协议端（Node 程序） | 登录 QQ 账号；收发 QQ 消息；把 QQ 消息翻译成 OneBot v11 事件；执行 OneBot 动作（发消息、撤回等） | 不生成回复内容 |
| **AstrBot** | 大模型机器人框架（Python 程序） | 接收 OneBot 事件；按规则决定是否回复；调用大模型；把回复通过 OneBot 动作发回 | 不碰 QQ 协议本身 |
| **本控制台** | 本地网页应用（Python + 静态前端） | 下载安装两个项目；生成/同步两边的配置；启停子进程并采集日志；把状态与消息流水展示给人看 | **不参与消息转发**（`native` 模式）；不改这两个项目的源码 |

> 关键点：控制台是「运维外壳」，不是「消息中间件」。`bridge`（内置中继）模式下它才会临时充当一段转发通道。

---

## 2. 数据流（ASCII 总图）

### native（原生直连，默认）

```
 ┌──────────┐   ① QQ 消息      ┌───────────────┐  ② OneBot v11 事件   ┌───────────────┐
 │  你的 QQ │ ───────────────► │   SnowLuma    │ ───────────────────► │    AstrBot    │
 │  客户端  │                  │  QQ 协议端    │  ws://127.0.0.1:6199 │  机器人框架   │
 │          │ ◄─────────────── │  (Node)       │ ◄─────────────────── │  (Python)     │
 └──────────┘   ⑦ 机器人消息    └───────────────┘  ⑥ 动作调用(发消息) └──────┬────────┘
                                                                             │ ③ 请求
                                                                             ▼
                                                                   ┌───────────────┐
                                                                   │   大模型 API  │
                                                                   │ (DeepSeek/…） │
                                                                   └───────────────┘
                                                                             │ ④ 回复
                                                                             ▼
                                                                   ⑤ 生成回复文本
```

① 你在 QQ 里发消息 → ② SnowLuma 转成 OneBot 事件 → ③ AstrBot 调大模型 → ④ 拿到回复 →
⑤ 组装成发送指令 → ⑥ 通过 OneBot 动作 `send_msg` 调 SnowLuma → ⑦ QQ 里出现机器人回复。

### bridge（内置中继）

```
 ┌──────────┐        ┌───────────────┐    ⑤ 转发/记录    ┌───────────────┐
 │  你的 QQ │ ◄────► │   SnowLuma    │ ◄──────────────► │  内置中继      │
 └──────────┘        │  QQ 协议端    │  :3001 WS 服务端  │ (控制台进程内) │
                     │  :3001 监听   │                  │  OneBot 客户端 │
                     └───────────────┘                  └───────┬───────┘
                                                                │ ⑥ OneBot v11
                                                                ▼
                                                        ┌───────────────┐
                                                        │    AstrBot    │ ──► 大模型 API
                                                        └───────────────┘
```

差别只有一处：消息不直连，而是穿过控制台进程里的中继，因此每一句 **收发都能在「联调」页的消息流水里看到**。

---

## 3. 端口表

所有端口都可以在控制台「③ 配置」页改；改完必须在「⑤ 联调」点 **写入联动配置** 才会同步到两个项目。

| 端口（默认） | 谁在监听 | 用途 | 改了会影响什么 |
| --- | --- | --- | --- |
| **8790** | 控制台（uvicorn） | 图形界面 + REST + SSE | 换端口后要用新地址访问（`一键启动.bat` 里写的是 8790） |
| **6185** | AstrBot | WebUI（机器人面板）+ 内置 HTTP 服务 | 「打开 AstrBot 面板」链接；同时是 AstrBot 的 aiocqhttp 反向 WS 服务端口所在的进程 |
| **5099** | SnowLuma | SnowLuma WebUI（扫码登录、协议端设置） | 「打开 SnowLuma 面板」链接 |
| **6199** | AstrBot（aiocqhttp 反向 WS 服务端） | **OneBot v11 通道**：SnowLuma 作为 WS 客户端连进来 | 联动核心端口，改了两侧必须一致（控制台会自动同步） |
| **3001** | SnowLuma | **仅 bridge 模式**：SnowLuma 自己的 OneBot WS 服务端端口 | 中继连它；需与其它端口不同 |

约束（后端 `AppConfig.validate()` 强制）：

* 四个基本端口（8790 / 6185 / 5099 / 6199）**必须互不相同**；
* `bridge` 模式下 `snowluma.relay_port`（3001）也参与去重检查；
* 端口必须是 1–65535 的整数。

---

## 4. 进程模型与生命周期

```
控制台进程 (python -m slab serve)
 ├─ uvicorn / FastAPI（前台，Ctrl+C 退出）
 ├─ EventBus（进程内事件总线 + 日志环形缓冲 800 行）
 └─ supervisor（子进程管理）
     ├─ astrbot  : python main.py             [kind=process, 就绪=端口 6185 可连]
     ├─ snowluma : node ...（或自带 exe）      [kind=process, 就绪=端口 5099 可连]
     └─ bridge   : asyncio 任务（无独立进程）  [kind=task,   随控制台生命周期]
```

* **状态机**：`stopped → starting → running → stopping → stopped`，异常退出记 `failed`。
* **就绪判定**：端口可连接（可选叠加日志正则），最多等 180 秒。
* **停止**：Windows 下用 `taskkill /PID <pid> /T /F` 杀掉整棵进程树（QQ 客户端会带子进程）。
* **日志**：子进程 stdout/stderr 逐行 → 推入 EventBus → 落盘 `data/logs/<service>.log`。
* **退出**：控制台关闭时会 `stop_all()`，把自己拉起来的服务一起停掉（不会留下孤儿进程）。

---

## 5. 配置注入点（重点）

控制台只有一份配置：`data/config.json`（可用 `SLAB_DATA_DIR` 改位置）。
**它不直接等于**两个项目的配置，而是被「翻译」成两边能读的格式：

| 控制台字段 | 写到哪里 | 备注 |
| --- | --- | --- |
| `snowluma.qq_uin` | SnowLuma `config/onebot*.json`（OneBot 适配器配置里的账号字段） | SnowLuma 的不同版本文件名可能略有差异，实现按 `config/onebot*.json` 匹配 |
| `snowluma.onebot_url` / `onebot_token` | SnowLuma `config/onebot*.json`（WS 客户端地址与鉴权 token） | 由 `link.ws_host/ws_port/token` 自动推导 |
| `snowluma.relay_port` | SnowLuma 中继监听端口（bridge 模式） | 仅 bridge 模式写入 |
| `snowluma.webui_port` | SnowLuma 启动参数 / 配置（WebUI 端口） | 用于「打开 SnowLuma 面板」 |
| `astrbot.provider.base_url` / `api_key` / `model` | AstrBot `data/cmd_config.json` 的 provider 段 | AstrBot 用 `key: [api_key]` 形式存放，控制台负责转换 |
| `astrbot.platform.ws_reverse_host/port/token` | AstrBot `data/cmd_config.json` 的 aiocqhttp 平台段 | 端口与 token 由 `link.ws_*` 自动同步 |
| `astrbot.admins_id` | AstrBot `data/cmd_config.json` 的管理员列表 | 留空时后端自动填入 `snowluma.qq_uin` |
| `link.mode/trigger_mode/trigger_prefixes/allow_*` | AstrBot 会话/触发设置 + 控制台中继逻辑 | 决定「什么时候回复」 |

### 自动同步规则（`AppConfig.normalise()`）

不需要手工对齐，后端在每次保存时都会做：

1. `link.token` 为空 → 自动生成一个随机 token；
2. `astrbot.platform.ws_reverse_port := link.ws_port`；
3. `astrbot.platform.ws_reverse_token := link.token`；
4. `snowluma.onebot_token := link.token`；
5. `snowluma.onebot_mode == ws_client` 时，`snowluma.onebot_url := ws://{link.ws_host}:{link.ws_port}/ws`；
6. 面板密码为空 → 自动生成随机密码；
7. `astrbot.admins_id` 为空且 `snowluma.qq_uin` 是数字 → 自动填成该 QQ 号。

> 所以「改完配置 → 联调页写入联动配置 → 运行页重启全部」这三步，本质是把 `data/config.json`
> 的当前内容重新翻译并落盘到两个项目。**手工改过两个项目里的配置文件也会被覆盖**，请以控制台为准。

### 脱敏

`GET /api/state` 与 `PUT /api/config` 返回的配置里，`token / api_key / ws_reverse_token / password类字段` 一律显示为 `***`。
前端表单对这些字段 **留空 = 不修改**（后端 `_strip_masks()` 会丢弃 `***`）。

---

## 6. native 模式时序（一次完整问答）

```
用户        QQ         SnowLuma          AstrBot          大模型        控制台
 │           │              │                │               │            │
 │ 发 /你好  │              │                │               │            │
 ├──────────►│              │                │               │            │
 │           │  OneBot 事件 │                │               │            │
 │           ├─────────────►│  ws:6199       │               │            │
 │           │              ├───────────────►│  POST /v1/chat│            │
 │           │              │                ├──────────────►│            │
 │           │              │                │◄──────────────┤            │
 │           │              │◄───────────────┤ send_msg 动作 │            │
 │           │◄─────────────┤  发消息 API    │               │            │
 │◄──────────┤  机器人回复   │                │               │            │
 │           │              │                │               │  日志/状态  │
 │           │              │                │               ├───────────►│ SSE → 页面
```

页面看到的「收到 / 回复」来自中继模式的事件，或（native 模式下）服务日志中的收发记录。

## 7. bridge 模式时序

```
用户        QQ         SnowLuma         内置中继         AstrBot        大模型
 │           │              │                │               │            │
 │ 发 /你好  ├─────────────►│                │               │            │
 │           │              │ OneBot 事件    │               │            │
 │           │              ├───────────────►│  记录 in      │            │
 │           │              │                ├──────────────►│            │
 │           │              │                │               ├───────────►│
 │           │              │                │  记录 out     │◄───────────┤
 │           │              │◄───────────────┤ send_msg      │            │
 │◄──────────┤              │                │               │            │
```

中继的额外价值：**消息流水可见**、可以在此做限流/改写/多机器人分发。

---

## 8. 控制台接口与前端

```
浏览器 (slab/web, 纯静态)
   │  ① GET /api/state        每 15s 轮询（页面隐藏时暂停）
   │  ② GET /api/events       SSE：hello / log / service / deploy / message / link（每 15s 有 ": ping" 心跳）
   │  ③ REST 动作             /api/config, /api/services/*, /api/deploy/*, /api/link/*
   ▼
FastAPI (slab/server)
   ├─ routes_console.py  → state / config / services / logs / events
   ├─ routes_deploy.py   → 安装、版本、卸载
   └─ routes_link.py     → 联动状态、写入配置、测试发送、账号信息
   ▼
config.py (data/config.json) + supervisor.py (子进程) + events.py (事件总线)
```

前端原则：**没有构建步骤**，`index.html` 直接 `<script src="/assets/app.js">`；
不引用任何 CDN；所有资源相对路径，`/assets/*` 由 FastAPI 的 StaticFiles 提供。

SSE 事件类型：

| type | 何时发 | 前端用途 |
| --- | --- | --- |
| `hello` | 刚连上时 | 补全最近 30 条事件 + 服务/配置快照 |
| `log` | 子进程输出一行 | 「运行」页实时日志 |
| `service` | 服务状态变化 | 状态灯、卡片按钮可用性 |
| `deploy` | 安装进度 | 「部署」页进度条 + 滚动日志 |
| `message` | OneBot 收发消息 | 「联调」页消息流水 |
| `link` | 联动配置变化 | 刷新联动状态块 |

### 终端页（term.js）与进程监视

「终端」页是独立脚本 `slab/web/term.js`，自己开一条 `EventSource`（不依赖 `app.js` 内部状态）：

* 把 `log` / `deploy` / `service` / `message` 四类事件统一渲染成终端行（清理 ANSI 转义与重复时间戳），
  用 `requestAnimationFrame` 合批追加，最多保留 3000 行；
* `GET /api/procs` 每 1.5s 拉一次控制台进程树（psutil `children(recursive=True)`），
  展示 服务 / 进程 / PID / CPU / 内存 / 运行时长 / 完整命令行；只在本页可见时才轮询；
* 顶栏「终端」按钮打开右下角**浮动窗**（`Alt+T` 开关、`Esc` 关闭），可拖拽缩放；
  首次触发部署（`deploy` 事件 `step=start`）会自动弹出，用户关掉后本次会话不再自动弹。

---

## 9. 排错分层（按链路从近到远）

```
① 控制台打不开      → 8790 端口 / python 进程 / 依赖
② 服务起不来        → 是否已安装？端口被占？看「运行」页该服务日志
③ SnowLuma 没登录   → 打开 5099 面板扫码；看 SnowLuma 日志有没有 login 字样
④ OneBot 没连上     → 6199 是否监听；两侧 token 是否一致（重新「写入联动配置」）
⑤ 消息不回复        → AstrBot 日志：是否收到事件？API Key / 模型名 / 触发规则
⑥ 回复太慢/超时     → 大模型接口连通性、`behavior.reply_timeout`
```

具体处方见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。
