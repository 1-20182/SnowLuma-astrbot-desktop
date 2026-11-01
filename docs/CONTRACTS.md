# 模块契约（Teammates 必读）

本文件是并行开发的唯一接口约定。**只改自己负责的文件**，需要跨界时先给 Lead 发消息。

## 目录与职责

```
slab/
  paths.py        [Lead]   路径常量
  config.py       [Lead]   AppConfig / ConfigStore / get_config()
  events.py       [Lead]   EventBus（bus.publish / bus.log / bus.logs / subscribe）
  supervisor.py   [Lead]   ServiceDef / ServiceState / supervisor（register/start/stop/restart/states）
  installer.py    [T1]     下载+解压 SnowLuma、克隆+建 venv 安装 AstrBot、版本查询、卸载
  astrbot_admin.py[T2]     生成 data/cmd_config.json、WebUI 登录、平台/提供商配置写入、健康检查
  snowluma_admin.py[T2]    生成 SnowLuma 配置文件（OneBot ws 客户端连接、账号）、WebUI 探测
  bridge.py       [T2]     内置中继服务（OneBot v11 客户端 ↔ AstrBot HTTP/WS）
  onebot/         [T2]     OneBot v11 事件模型、WS 客户端、动作调用
  server/app.py        [Lead] FastAPI 组装、静态资源、SSE
  server/routes_console.py [Lead] /api/state /api/config /api/services /api/logs /api/events
  server/routes_deploy.py  [T1]   /api/deploy/*
  server/routes_link.py    [T2]   /api/link/*
  web/          [T3] 纯静态前端（无构建步骤）
docs/           [T3] 架构、部署、排错
tests/          [各自写自己模块的测试] + [Lead 做端到端集成测试]
```

## 统一返回约定

REST 一律返回 JSON 对象。失败时 HTTP 400/500 且 body 为 `{"ok": false, "error": "人类可读的中文信息", "detail": "traceback 或原始输出（可选）"}`。
成功时至少包含 `{"ok": true, ...}`。

## 关键 API（前端按此实现）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | `{ok, version, uptime}` |
| GET | `/api/state` | `{ok, config(脱敏), services{}, env{}, ready{}}` |
| PUT | `/api/config` | body 为 config 局部片段（深合并），返回 `{ok, config}` |
| POST | `/api/config/reset` | 重置为默认 |
| POST | `/api/services/{name}/{action}` | name∈snowluma/astrbot/bridge，action∈start/stop/restart |
| POST | `/api/services/all/{action}` | 批量 start/stop |
| GET | `/api/logs?service=&limit=` | `{ok, logs:[{seq,ts,service,line,level}]}` |
| GET | `/api/events` | SSE，`data: {type,...}`，type∈log/service/deploy/message/link |
| GET | `/api/deploy/env` | T1：环境自检报告 |
| GET | `/api/deploy/versions` | T1：`{snowluma:[tag...], astrbot:'main'}` |
| POST | `/api/deploy/{target}` | T1：target∈snowluma/astrbot/both，同步执行，进度走 SSE `deploy` 事件 |
| GET | `/api/link/status` | T2：`{ok, snowluma:{...}, astrbot:{...}, onebot:{...}, bridge:{...}}` |
| POST | `/api/link/apply` | T2：把配置写入两个项目（含 token 同步），返回每步结果 |
| POST | `/api/link/test-send` | T2：body `{target_type:'group'|'private', target_id, text}` |
| GET | `/api/link/whoami` | T2：读取 QQ 登录信息（OneBot `get_login_info`） |

## SSE 事件格式

```
data: {"seq":12,"ts":1690000000.1,"type":"log","service":"astrbot","line":"...","level":"info"}
data: {"seq":13,"ts":...,"type":"deploy","target":"astrbot","step":"pip install","progress":0.45,"message":"..."}
data: {"seq":14,"ts":...,"type":"service","name":"snowluma","status":"running","pid":1234}
data: {"seq":15,"ts":...,"type":"message","direction":"in|out","platform":"qq","user":"...","text":"..."}
```

## ServiceDef 用法

```python
from .supervisor import ServiceDef, supervisor
from .config import get_config, AppConfig
from pathlib import Path

supervisor.register(ServiceDef(
    name="astrbot", display="AstrBot",
    argv=lambda cfg: [str(astrbot_python(cfg)), "main.py"],
    cwd=lambda cfg: astrbot_home(cfg.astrbot.install_dir),
    ready_port=lambda cfg: cfg.astrbot.webui_port,
    ready_regex=r"WebUI|Running on|http://",
    stop_timeout=15,
))
```

`kind="task"` 的服务用 `task_factory=<async callable>`（内置 bridge 用它，随控制台进程生命周期运行）。

## 配置字段（以 slab/config.py 为准）

* `config.link.mode`: `native`（SnowLuma 作为 OneBot WS 客户端直连 AstrBot 的 aiocqhttp 反向服务）/ `bridge`（内置中继）
* `config.link.ws_port` / `config.link.token`：两侧共享，`normalise()` 会自动同步到
  `astrbot.platform.ws_reverse_port/token` 与 `snowluma.onebot_url/onebot_token`
* `config.snowluma.onebot_mode`: `ws_client` / `ws_server`
* `config.snowluma.qq_uin`：机器人 QQ 号；若 `astrbot.admins_id` 为空会自动填入它（管理员）
* `config.snowluma.relay_port`（默认 3001）：仅 `link.mode="bridge"` 时使用，SnowLuma 的 OneBot WS 服务端端口
* `config.astrbot.admins_id`（list[str]）：写入 AstrBot `admins_id`，只增不减
* 端口：`app.port=8790`、`astrbot.webui_port=6185`、`snowluma.webui_port=5099`、`link.ws_port=6199`

## 测试要求

每个模块至少 1 个 pytest 文件，全部用 `pytest`（已装在 `.venv`）。
运行：`E:\AGENT\snowluma-astrbot-console\.venv\Scripts\python.exe -m pytest -q`
禁止在测试里真的下载大文件 / 真的启动 QQ：用 mock server + monkeypatch。
