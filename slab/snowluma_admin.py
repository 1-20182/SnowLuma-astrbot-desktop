"""SnowLuma 侧：OneBot 配置注入、WebUI REST 调用与探活。

设计要点（均由实测证据支撑，见 ``research/snowluma.md``）
-------------------------------------------------------
* 配置目录是**相对于启动 cwd** 的 ``config/``（发行包根目录），不是 ``%APPDATA%``。
* ``config/onebot.json`` 是全局模板；``config/onebot_<uin>.json`` 若带 ``"mode":"snapshot"``
  **完全覆盖**模板，因此注入必须"写模板 + 覆盖每一个已存在的 per-UIN 文件"。
* ``statusCommand`` / ``historySync`` / ``notifications`` 三块是**必填**，per-UIN 文件缺了就校验失败。
* 适配器 ``name`` 在同一个账号内 **四类共用命名空间**，重名会被拒。
* 运行中的 SnowLuma 支持热重载：``POST /api/config/:uin`` 返回 ``applied``/``online``；
  真正"已连上"的权威判据是 ``GET /api/connections`` 里该 wsClient 的 ``status == "ok"``。
* WebUI 端口可能上漂（+1..+50），真实端口以 ``GET /api/system/settings`` 的 ``listeningPort`` 为准。

本模块不负责启动进程（由 supervisor / installer 负责），只负责"让配置正确 + 说清现状"。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import httpx

from . import paths
from .config import AppConfig

log = logging.getLogger("slab.snowluma")

#: 配置文件目录名（相对 SnowLuma 安装目录）
CONFIG_DIRNAME = "config"
#: 全局模板文件名
TEMPLATE_FILENAME = "onebot.json"
#: 账号级配置文件名模式
PER_UIN_PATTERN = re.compile(r"^onebot_(\d{5,10})\.json$")
#: 我们写入的 wsClient 适配器默认名（name 在同一账号内跨四类唯一）
DEFAULT_CLIENT_NAME = "astrbot"
#: 中继模式里我们写入的 wsServer 适配器默认名
RELAY_SERVER_NAME = "relay"
#: 默认中继端口（与 ``cfg.snowluma.relay_port`` 的默认值保持一致）
DEFAULT_RELAY_PORT = 3001
#: 默认 WebUI 端口
DEFAULT_WEBUI_PORT = 5099
#: 初始凭据日志行（仅 stdout）：``➜  Initial password: xxx`` / ``initial credentials: user=.. password=..``
_INITIAL_CREDENTIAL_PATTERNS = (
    re.compile(r"Initial username:\s*(\S+)"),
    re.compile(r"Initial password:\s*(\S+)"),
    re.compile(r"initial credentials:\s*user=(\S+)\s+password=(\S+)"),
)
_INITIAL_PASSWORD_PATTERN = re.compile(r"Initial password:\s*(\S+)")
#: 日志里可用于发现 QQ 号的行
_UIN_LOG_PATTERN = re.compile(r"UIN=(\d{5,10})")

NETWORK_KINDS = ("httpServers", "httpClients", "wsServers", "wsClients")
VALID_ROLES = ("api", "event", "universal")


class SnowLumaConfigError(RuntimeError):
    """写入的配置无法通过 SnowLuma 的校验（消息面向用户，可直接展示）。"""


class SnowLumaApiError(RuntimeError):
    """SnowLuma WebUI REST 调用失败。"""

    def __init__(self, message: str, *, status_code: int = 0, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


# --------------------------------------------------------------------------- #
# 路径与文件
# --------------------------------------------------------------------------- #
def home_dir(cfg: AppConfig, home: Optional[Path] = None) -> Path:
    """SnowLuma 安装目录（cwd），配置在其下的 ``config/``。"""

    return Path(home) if home else paths.snowluma_home(cfg.snowluma.install_dir)


def config_dir(cfg: AppConfig, home: Optional[Path] = None) -> Path:
    return home_dir(cfg, home) / CONFIG_DIRNAME


def read_json(path: Path, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读取 JSON 对象；不存在/损坏时返回 ``default``（不抛异常，调用方自己决定是否告警）。"""

    try:
        raw = Path(path).read_text("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return dict(default or {})
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("配置文件损坏，已按默认值处理: %s", path)
        return dict(default or {})
    return data if isinstance(data, dict) else dict(default or {})


def write_json(path: Path, data: Dict[str, Any]) -> Path:
    """原子写入 JSON（先写临时文件再 ``os.replace``），返回目标路径。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(payload, "utf-8")
    os.replace(tmp, target)
    return target


def _new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


# --------------------------------------------------------------------------- #
# 默认结构
# --------------------------------------------------------------------------- #
def default_onebot_config() -> Dict[str, Any]:
    """与 SnowLuma ``makeDefaultOneBotConfig()`` 对齐的工厂默认结构（仅作参考/测试）。"""

    return {
        "networks": {
            "httpServers": [
                {
                    "name": "http-default",
                    "accessToken": _new_token(32),
                    "messageFormat": "array",
                    "reportSelfMessage": False,
                    "host": "127.0.0.1",
                    "port": 3000,
                    "path": "/",
                    "enableWebSocket": False,
                }
            ],
            "httpClients": [],
            "wsServers": [
                {
                    "name": "ws-default",
                    "accessToken": _new_token(32),
                    "messageFormat": "array",
                    "reportSelfMessage": False,
                    "host": "127.0.0.1",
                    "port": DEFAULT_RELAY_PORT,
                    "path": "/",
                    "role": "Universal",
                }
            ],
            "wsClients": [],
        },
        "statusCommand": {"enabled": True, "swallow": False, "cooldownSeconds": 5, "trigger": "#sl"},
        "historySync": {"enabled": False},
        "notifications": {"channelIds": []},
    }


def minimal_onebot_config() -> Dict[str, Any]:
    """我们写盘时用的最小骨架：**不预置监听端口**。

    原因：SnowLuma 每个 QQ 账号会各自物化 ``3000/3001``，多开账号时会互相抢端口
    （实测：第二个账号上线后 ``network startup degraded``）。native 模式只需要一条
    反向 WS 客户端连接，不需要任何监听端口；中继模式由 :func:`ws_server_entry` 单独加。
    """

    return {
        "networks": {"httpServers": [], "httpClients": [], "wsServers": [], "wsClients": []},
        "statusCommand": {"enabled": True, "swallow": False, "cooldownSeconds": 5, "trigger": "#sl"},
        "historySync": {"enabled": False},
        "notifications": {"channelIds": []},
    }


def default_runtime_config() -> Dict[str, Any]:
    """与 SnowLuma ``config/runtime.json`` 默认值对齐。"""

    return {
        "webuiPort": DEFAULT_WEBUI_PORT,
        "webuiHost": "127.0.0.1",
        "hookAutoLoad": True,
        "webuiTls": {"enabled": False},
        "trustProxy": "",
        "logMaxTotalMb": 1024,
        "logRetainDays": 7,
        "logPerUin": False,
    }


def normalise_adapters(config: Dict[str, Any]) -> Dict[str, Any]:
    """给每个适配器补齐 SnowLuma 解析时会自动填的默认值（原地修改并返回）。

    SnowLuma 的 ``fromJson`` 会在校验前补 ``messageFormat``/``reportSelfMessage``/``role``
    等默认值，所以老配置文件里缺这些字段是**合法**的。我们写入前也补一遍，
    既避免自己的校验误判，也让写出的文件是规范形态（幂等）。
    """

    networks = config.get("networks")
    if not isinstance(networks, dict):
        return config
    for kind in NETWORK_KINDS:
        items = networks.get(kind)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item.setdefault("messageFormat", "array")
            item.setdefault("reportSelfMessage", False)
            if kind in ("wsServers", "wsClients"):
                role = item.get("role")
                item["role"] = str(role).strip().capitalize() if isinstance(role, str) and role.strip() else "Universal"
            if kind in ("httpServers", "wsServers"):
                item.setdefault("path", "/")
            if kind == "wsClients":
                item.setdefault("reconnectIntervalMs", 5000)
    return config


def ensure_skeleton(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """补齐 SnowLuma 必填的骨架字段，保留用户已有内容（不改动其它适配器）。

    只补"结构"，**不注入**任何默认监听适配器：用户没有的适配器就不会被凭空创建，
    避免多账号各自抢占 3000/3001。
    """

    base = dict(config or {})
    networks = base.get("networks")
    if not isinstance(networks, dict):
        networks = {}
    for kind in NETWORK_KINDS:
        items = networks.get(kind)
        networks[kind] = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    base["networks"] = networks

    template = minimal_onebot_config()
    for key, value in template.items():
        if key == "networks":
            continue
        current = base.get(key)
        if not isinstance(current, dict):
            base[key] = json.loads(json.dumps(value))
        else:
            merged = dict(value)
            merged.update({k: v for k, v in current.items() if v is not None})
            base[key] = merged
    # statusCommand.trigger 不能为空
    trigger = base["statusCommand"].get("trigger")
    if not isinstance(trigger, str) or not trigger.strip():
        base["statusCommand"]["trigger"] = "#sl"
    return normalise_adapters(base)


# --------------------------------------------------------------------------- #
# 校验（对齐 SnowLuma assertValidOneBotConfig 的关键规则）
# --------------------------------------------------------------------------- #
def validate_onebot_config(config: Dict[str, Any]) -> None:
    """按 SnowLuma 的规则做最小校验，失败抛 :class:`SnowLumaConfigError`（中文提示）。

    只校验"我们可能写错"的部分：name 唯一、url/role/messageFormat/reconnectIntervalMs、
    端口范围、server 绑定冲突、status 命令字段。
    """

    networks = config.get("networks")
    if not isinstance(networks, dict):
        raise SnowLumaConfigError("OneBot 配置缺少 networks 字段")
    seen: Dict[str, str] = {}
    for kind in NETWORK_KINDS:
        items = networks.get(kind)
        if items is None:
            continue
        if not isinstance(items, list):
            raise SnowLumaConfigError(f"networks.{kind} 必须是数组")
        for index, item in enumerate(items):
            at = f"networks.{kind}[{index}]"
            if not isinstance(item, dict):
                raise SnowLumaConfigError(f"{at} 必须是对象")
            name = str(item.get("name") or "")
            if not name.strip():
                raise SnowLumaConfigError(f"{at}.name 不能为空")
            if name != name.strip():
                raise SnowLumaConfigError(f"{at}.name 前后不能有空白字符")
            if name in seen:
                raise SnowLumaConfigError(
                    f"适配器名称 {name} 在 {seen[name]} 与 {kind} 中重复（SnowLuma 要求跨四类唯一）"
                )
            seen[name] = kind
            fmt = item.get("messageFormat", "array")
            if fmt not in ("array", "string"):
                raise SnowLumaConfigError(f"{at}.messageFormat 只能是 array 或 string")
            if not isinstance(item.get("reportSelfMessage"), bool):
                raise SnowLumaConfigError(f"{at}.reportSelfMessage 必须是布尔值")
            if kind in ("httpServers", "wsServers"):
                port = item.get("port")
                if not isinstance(port, int) or not (1 <= port <= 65535):
                    raise SnowLumaConfigError(f"{at}.port 必须是 1-65535 的整数")
                path = item.get("path", "/")
                if not isinstance(path, str) or (path and not path.startswith("/")):
                    raise SnowLumaConfigError(f"{at}.path 必须为空或以 / 开头")
                if kind == "wsServers" and "role" in item:
                    _validate_role(item["role"], f"{at}.role")
            else:
                url = item.get("url")
                enabled = item.get("enabled", True)
                if url is None and enabled is not False:
                    raise SnowLumaConfigError(f"{at}.url 在启用时必须填写")
                if url is not None and not isinstance(url, str):
                    raise SnowLumaConfigError(f"{at}.url 必须是字符串")
                if url and enabled is not False:
                    if not re.match(r"^(wss?)://", url) and kind == "wsClients":
                        raise SnowLumaConfigError(f"{at}.url 必须以 ws:// 或 wss:// 开头")
                    if not re.match(r"^(https?)://", url) and kind == "httpClients":
                        raise SnowLumaConfigError(f"{at}.url 必须以 http:// 或 https:// 开头")
                if kind == "wsClients" and "role" in item:
                    _validate_role(item["role"], f"{at}.role")
                if kind == "wsClients" and "reconnectIntervalMs" in item:
                    interval = item["reconnectIntervalMs"]
                    if not isinstance(interval, int) or interval < 1000:
                        raise SnowLumaConfigError(f"{at}.reconnectIntervalMs 必须是不小于 1000 的整数")

    status = config.get("statusCommand")
    if not isinstance(status, dict):
        raise SnowLumaConfigError("缺少 statusCommand 配置块")
    trigger = status.get("trigger")
    if not isinstance(trigger, str) or not trigger.strip() or len(trigger) > 32 or "\n" in trigger or "\r" in trigger:
        raise SnowLumaConfigError("statusCommand.trigger 必须是非空单行字符串且不超过 32 字符")
    for key in ("enabled", "swallow"):
        if not isinstance(status.get(key), bool):
            raise SnowLumaConfigError(f"statusCommand.{key} 必须是布尔值")
    cooldown = status.get("cooldownSeconds")
    if not isinstance(cooldown, int) or cooldown < 0:
        raise SnowLumaConfigError("statusCommand.cooldownSeconds 必须是非负整数")
    if not isinstance(config.get("historySync"), dict):
        raise SnowLumaConfigError("缺少 historySync 配置块")
    if not isinstance(config.get("notifications"), dict):
        raise SnowLumaConfigError("缺少 notifications 配置块")


def _validate_role(role: Any, at: str) -> None:
    if role is None:
        return
    if str(role).strip().lower() not in VALID_ROLES:
        raise SnowLumaConfigError(f"{at} 只能是 Api / Event / Universal")


# --------------------------------------------------------------------------- #
# 适配器条目
# --------------------------------------------------------------------------- #
def ws_client_entry(cfg: AppConfig, *, name: str = DEFAULT_CLIENT_NAME) -> Dict[str, Any]:
    """native 模式要写入的 wsClient 条目（SnowLuma → AstrBot 反向 WS）。

    实测握手：``Authorization: Bearer <accessToken>`` + ``X-Self-ID`` + ``X-Client-Role``，
    URL **不带** query token（AstrBot/aiocqhttp 只读 Authorization 头）。
    ``reportSelfMessage=False``：不把机器人自己发的消息再上报，避免与 AstrBot 形成回环。
    """

    host = str(cfg.link.ws_host or "127.0.0.1")
    port = int(cfg.link.ws_port)
    return {
        "name": name,
        "enabled": True,
        "url": f"ws://{host}:{port}/ws",
        "role": "Universal",
        "accessToken": str(cfg.link.token or ""),
        "messageFormat": "array",
        "reportSelfMessage": False,
        "reconnectIntervalMs": 5000,
    }


def ws_server_entry(cfg: AppConfig, *, name: str = RELAY_SERVER_NAME) -> Dict[str, Any]:
    """中继模式要写入的 wsServer 条目（供内置中继/其它 OneBot 应用连入）。"""

    return {
        "name": name,
        "enabled": True,
        "accessToken": str(cfg.link.token or ""),
        "messageFormat": "array",
        "reportSelfMessage": False,
        "host": "127.0.0.1",
        "port": int(cfg.snowluma.relay_port or DEFAULT_RELAY_PORT),
        "path": "/",
        "role": "Universal",
    }


def _url_without_query(url: str) -> str:
    return str(url or "").split("?", 1)[0].rstrip("/")


def _pick_client_name(config: Dict[str, Any], entry: Dict[str, Any]) -> str:
    """确定我们那条 wsClient 的 name。

    优先复用"URL 与期望一致"的既有条目名（用户改过名也不会产生第二条），
    否则使用 ``astrbot``；若该名已被其它三类占用，则顺延 ``astrbot-1``… 保证唯一。
    """

    networks = config.get("networks") or {}
    target = _url_without_query(str(entry.get("url", "")))
    for item in networks.get("wsClients") or []:
        if isinstance(item, dict) and _url_without_query(str(item.get("url", ""))) == target:
            name = str(item.get("name") or "").strip()
            if name:
                return name
    used = {
        str(item.get("name") or "")
        for kind in NETWORK_KINDS
        for item in (networks.get(kind) or [])
        if isinstance(item, dict)
    }
    preferred = str(entry.get("name") or DEFAULT_CLIENT_NAME)
    if preferred not in used:
        return preferred
    for index in range(1, 50):
        candidate = f"{preferred}-{index}"
        if candidate not in used:
            return candidate
    raise SnowLumaConfigError("无法为 astrbot 适配器分配唯一名称，请手动清理 SnowLuma 中同名连接")


def _is_our_client(item: Dict[str, Any], name: str, url: str) -> bool:
    """判断某条 wsClient 是否属于我们（同名或同 URL 都算，保证幂等/清理旧的）。"""

    if not isinstance(item, dict):
        return False
    if str(item.get("name") or "") == name:
        return True
    return _url_without_query(str(item.get("url", ""))) == _url_without_query(url)


def merge_ws_client(config: Dict[str, Any], entry: Dict[str, Any]) -> Dict[str, Any]:
    """把我们的 wsClient 合并进配置：替换同名/同 URL 条目，保留用户其它连接。"""

    merged = ensure_skeleton(config)
    url = str(entry.get("url", ""))
    name = _pick_client_name(merged, entry)
    client = dict(entry)
    client["name"] = name
    clients = [
        item
        for item in (merged["networks"].get("wsClients") or [])
        if not _is_our_client(item, name, url)
    ]
    clients.append(client)
    merged["networks"]["wsClients"] = clients
    return merged


def merge_ws_server(config: Dict[str, Any], entry: Dict[str, Any]) -> Dict[str, Any]:
    """把中继用的 wsServer 合并进配置：优先复用同端口条目，避免多监听冲突。"""

    merged = ensure_skeleton(config)
    port = int(entry.get("port", DEFAULT_RELAY_PORT))
    servers = [item for item in (merged["networks"].get("wsServers") or []) if isinstance(item, dict)]
    # 同端口优先复用（例如默认的 ws-default:3001）
    reuse = next((item for item in servers if item.get("port") == port), None)
    if reuse is not None:
        reuse.update(
            {
                "enabled": True,
                "accessToken": entry.get("accessToken", ""),
                "messageFormat": entry.get("messageFormat", "array"),
                "role": entry.get("role", "Universal"),
                "host": reuse.get("host") or entry.get("host", "127.0.0.1"),
                "path": reuse.get("path") or entry.get("path", "/"),
            }
        )
        merged["networks"]["wsServers"] = servers
        return merged

    name = str(entry.get("name") or RELAY_SERVER_NAME)
    used = {
        str(item.get("name") or "")
        for kind in NETWORK_KINDS
        for item in (merged["networks"].get(kind) or [])
        if isinstance(item, dict)
    }
    while name in used:
        name = f"{name}-{secrets.randbelow(90) + 10}"
    server = dict(entry)
    server["name"] = name
    servers.append(server)
    merged["networks"]["wsServers"] = servers
    return merged


# --------------------------------------------------------------------------- #
# 账号发现
# --------------------------------------------------------------------------- #
def accounts_from_files(home: Path) -> List[str]:
    """从 ``config/onebot_<uin>.json`` 提取账号。"""

    directory = home / CONFIG_DIRNAME
    found: List[str] = []
    try:
        for entry in sorted(directory.iterdir()):
            match = PER_UIN_PATTERN.match(entry.name)
            if match and entry.is_file():
                found.append(match.group(1))
    except OSError:
        return []
    return found


def accounts_from_data_dirs(home: Path) -> List[str]:
    """从 ``data/<uin>/`` 目录提取账号（SnowLuma 的账号数据目录）。"""

    found: List[str] = []
    try:
        for entry in sorted((home / "data").iterdir()):
            if entry.is_dir() and re.fullmatch(r"\d{5,10}", entry.name):
                found.append(entry.name)
    except OSError:
        return []
    return found


def accounts_from_logs(lines: Iterable[str]) -> List[str]:
    """从日志行提取账号（``[Hook] login detected`` / ``[Bridge] session started`` 等）。"""

    found: List[str] = []
    for line in lines:
        for match in _UIN_LOG_PATTERN.finditer(str(line)):
            found.append(match.group(1))
    return found


def _recent_log_lines(cfg: AppConfig, limit: int = 400) -> List[str]:
    """取 SnowLuma 的最近日志（优先事件总线，其次落盘文件）。"""

    lines: List[str] = []
    try:
        from .events import bus

        lines = [str(item.get("line", "")) for item in bus.logs("snowluma", limit)]
    except Exception:  # noqa: BLE001 - 事件总线不可用时退化为读文件
        lines = []
    if not lines:
        logfile = paths.LOG_DIR / "snowluma.log"
        try:
            raw = logfile.read_text("utf-8", "replace").splitlines()
            lines = raw[-limit:]
        except OSError:
            lines = []
    return lines


def list_accounts(
    cfg: AppConfig,
    *,
    home: Optional[Path] = None,
    log_lines: Optional[Sequence[str]] = None,
    include_configured: bool = True,
) -> List[str]:
    """汇总账号列表：per-UIN 配置文件 + ``data/<uin>/`` 目录 + 日志，去重后按数字排序。

    Args:
        cfg: 控制台配置（用于定位安装目录，以及 ``snowluma.qq_uin`` 兜底）。
        home: 显式指定安装目录（测试用）。
        log_lines: 显式提供日志行（测试用）；默认读事件总线/日志文件。
        include_configured: 是否把用户在控制台里手填的 ``snowluma.qq_uin`` 也算进去。
    """

    base = home_dir(cfg, home)
    lines = list(log_lines) if log_lines is not None else _recent_log_lines(cfg)
    found: List[str] = []
    found.extend(accounts_from_files(base))
    found.extend(accounts_from_data_dirs(base))
    found.extend(accounts_from_logs(lines))
    if include_configured:
        configured = str(cfg.snowluma.qq_uin or "").strip()
        if re.fullmatch(r"\d{5,10}", configured or ""):
            found.append(configured)
    unique = {uin for uin in found if re.fullmatch(r"\d{5,10}", uin)}
    return sorted(unique, key=int)


def account_sources(cfg: AppConfig, *, home: Optional[Path] = None,
                    log_lines: Optional[Sequence[str]] = None) -> Dict[str, List[str]]:
    """返回三个来源各自发现的账号（诊断用）。"""

    base = home_dir(cfg, home)
    lines = list(log_lines) if log_lines is not None else _recent_log_lines(cfg)
    return {
        "files": sorted(set(accounts_from_files(base)), key=int),
        "data_dirs": sorted(set(accounts_from_data_dirs(base)), key=int),
        "logs": sorted(set(accounts_from_logs(lines)), key=int),
    }


def relay_ws_url(cfg: AppConfig) -> str:
    """中继模式连 SnowLuma WS 服务端用的 URL（端口与 token 见配置）。"""

    port = int(cfg.snowluma.relay_port or DEFAULT_RELAY_PORT)
    return f"ws://127.0.0.1:{port}/"


def relay_token(cfg: AppConfig) -> str:
    """中继模式连接 SnowLuma WS 服务端用的 token（写入时与 wsServer 的 accessToken 一致）。"""

    return str(cfg.link.token or "")


# --------------------------------------------------------------------------- #
# WebUI REST
# --------------------------------------------------------------------------- #
#: WebUI token 缓存（SnowLuma token 有效期 24h；这里保守缓存 20 分钟，避免每次探活都登录）
_TOKEN_TTL_SECONDS = 20 * 60
_TOKEN_CACHE: Dict[str, Dict[str, Any]] = {}

#: 登录冷却：SnowLuma 对"登录失败 5 次/IP"会锁 15 分钟，冷却期内绝不能再试
_LOGIN_COOLDOWN: Dict[str, Any] = {"until": 0.0, "message": "", "retry_at": 0.0}
_DEFAULT_LOGIN_COOLDOWN = 900.0  # 429 时保守锁 15 分钟

#: SnowLuma 日志里的 wsClient 状态行（实测格式见 docs/evidence-snowluma-wsclient.log）
_WS_CLIENT_CONNECTED = re.compile(
    r"\[OneBot\.WS-Client\] \[(?P<name>[\w.-]+)\] connected (?P<url>ws://\S+)"
)
_WS_CLIENT_ERROR = re.compile(
    r"\[OneBot\.WS-Client\] \[(?P<name>[\w.-]+)\] error (?P<url>ws://\S+):\s*(?P<err>.+)"
)
_LOG_LINE_UIN = re.compile(r"\[(?P<uin>\d{5,10})\]\s+\[OneBot")


def login_cooldown() -> Dict[str, Any]:
    """当前登录冷却状态（``active`` 为 True 时禁止再发 login 请求）。"""

    remaining = max(0.0, float(_LOGIN_COOLDOWN["until"]) - time.time())
    return {
        "active": remaining > 0,
        "remaining": round(remaining, 1),
        "message": str(_LOGIN_COOLDOWN["message"] or ""),
    }


def clear_login_cooldown() -> None:
    """解除登录冷却（用户改了密码/重启后手动清除）。"""

    _LOGIN_COOLDOWN.update({"until": 0.0, "message": "", "retry_at": 0.0})


def _start_login_cooldown(seconds: float, message: str) -> None:
    _LOGIN_COOLDOWN.update(
        {
            "until": time.time() + max(1.0, float(seconds)),
            "message": str(message),
            "retry_at": time.time() + max(1.0, float(seconds)),
        }
    )


def _parse_retry_seconds(text: str) -> Optional[float]:
    """从"请 753 秒后重试" / "retry after 60" 里解析等待秒数。"""

    for pattern in (r"(\d+)\s*秒", r"retry[ -]?after[:\s]*(\d+)", r"(\d+)\s*seconds?"):
        match = re.search(pattern, str(text), re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def _cache_key(base_url: str, username: str = "admin", password: str = "") -> str:
    import hashlib

    digest = hashlib.sha256(str(password).encode("utf-8")).hexdigest()[:16]
    return f"{base_url}|{username}|{digest}"


class SnowLumaWebUI:
    """SnowLuma WebUI 的极简异步客户端（只实现控制台用到的端点）。

    鉴权：``POST /api/login {"password": ...}`` → ``{"token": ...}``，
    之后每个请求带 ``x-snowluma-token``。三道门禁（未同意协议 / 必须改密码）会在
    :class:`SnowLumaApiError` 里给出中文提示。

    token 会按 ``base_url + 密码指纹`` 缓存在进程内（默认 20 分钟），
    避免 ``/api/link/status`` 轮询时反复登录（SnowLuma 对登录失败有 IP 级锁）。
    """

    def __init__(
        self,
        base_url: str,
        *,
        password: str = "",
        token: str = "",
        timeout: float = 4.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.password = str(password or "")
        self.token = str(token or "")
        self._timeout = float(timeout)
        self._client = client
        self._owned = client is None

    # -------------------------------------------------------------- 生命周期
    async def __aenter__(self) -> "SnowLumaWebUI":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client is not None and self._owned:
            await self._client.aclose()
        self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, trust_env=False)
            self._owned = True
        return self._client

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["x-snowluma-token"] = self.token
        return headers

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """发一个请求并返回 JSON（失败抛 :class:`SnowLumaApiError`）。"""

        url = f"{self.base_url}{path}"
        try:
            response = await self._http().request(
                method, url, headers=self._headers(), **kwargs
            )
        except httpx.HTTPError as exc:
            raise SnowLumaApiError(f"无法连接 SnowLuma WebUI（{self.base_url}）：{exc}") from exc
        payload: Any
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text[:400]}
        if response.status_code == 401:
            raise SnowLumaApiError("SnowLuma WebUI 未授权（请检查 WebUI 密码）", status_code=401, payload=payload)
        if response.status_code == 403 and isinstance(payload, dict):
            if payload.get("consentRequired"):
                raise SnowLumaApiError(
                    "SnowLuma 要求先同意用户协议与隐私政策（可在启动时设置 "
                    "SNOWLUMA_ACCEPT_EULA=1 / SNOWLUMA_ACCEPT_PRIVACY=1）",
                    status_code=403,
                    payload=payload,
                )
            if payload.get("mustChangePassword"):
                raise SnowLumaApiError("SnowLuma 要求先修改 WebUI 密码", status_code=403, payload=payload)
            raise SnowLumaApiError(
                f"SnowLuma 拒绝请求：{payload.get('message') or '403'}", status_code=403, payload=payload
            )
        if response.status_code >= 400:
            message = payload.get("message") if isinstance(payload, dict) else None
            raise SnowLumaApiError(
                f"SnowLuma 请求失败（{response.status_code}）：{message or response.text[:200]}",
                status_code=response.status_code,
                payload=payload,
            )
        return payload

    # -------------------------------------------------------------- 鉴权
    async def login(self) -> str:
        """登录并缓存 token；失败抛 :class:`SnowLumaApiError`。

        失败（401/403/429）会写全局登录冷却：SnowLuma 对"登录失败 5 次/IP"锁 15 分钟，
        冷却期内**不再发送 login 请求**，直接抛错让上层走日志兜底。
        """

        if self.token:
            return self.token
        cooldown = login_cooldown()
        if cooldown["active"]:
            raise SnowLumaApiError(
                f"SnowLuma 登录已被限制，约 {int(cooldown['remaining'])} 秒后再试"
                f"（{cooldown['message'] or '登录失败次数过多'}）",
                status_code=429,
                payload=cooldown,
            )
        key = _cache_key(self.base_url, password=self.password)
        cached = _TOKEN_CACHE.get(key)
        if cached and cached.get("expires", 0) > time.time():
            self.token = str(cached["token"])
            return self.token
        if not self.password:
            raise SnowLumaApiError("未配置 SnowLuma WebUI 密码，无法登录（请在配置页填写）")
        try:
            payload = await self.request("POST", "/api/login", json={"password": self.password})
        except SnowLumaApiError as exc:
            seconds = _parse_retry_seconds(str(exc)) or (
                _DEFAULT_LOGIN_COOLDOWN if exc.status_code == 429 else 60.0
            )
            _start_login_cooldown(seconds, str(exc))
            raise
        token = ""
        if isinstance(payload, dict):
            token = str(payload.get("token") or "")
            if not token and isinstance(payload.get("data"), dict):
                token = str(payload["data"].get("token") or "")
        if not token:
            _start_login_cooldown(60.0, "登录未返回 token（密码可能不正确）")
            raise SnowLumaApiError("SnowLuma 登录未返回 token（密码可能不正确）", payload=payload)
        self.token = token
        _TOKEN_CACHE[key] = {"token": token, "expires": time.time() + _TOKEN_TTL_SECONDS}
        return token

    @staticmethod
    def invalidate_token_cache() -> None:
        """清空 token 缓存（改密码/切换实例后调用）。"""

        _TOKEN_CACHE.clear()
        clear_login_cooldown()

    async def ensure_consent(self) -> bool:
        """必要时记录协议同意；返回是否成功（失败不抛，交给上层提示）。"""

        try:
            agreements = await self.request("GET", "/api/agreements")
            version = ""
            if isinstance(agreements, dict):
                version = str(
                    agreements.get("version")
                    or (agreements.get("data") or {}).get("version")
                    or ""
                )
            if not version:
                return False
            await self.request("POST", "/api/agreements/record-consent", json={"version": version})
            return True
        except SnowLumaApiError:
            return False

    # -------------------------------------------------------------- 查询
    async def status(self) -> Dict[str, Any]:
        payload = await self.request("GET", "/api/status")
        return payload if isinstance(payload, dict) else {}

    async def system_settings(self) -> Dict[str, Any]:
        payload = await self.request("GET", "/api/system/settings")
        return payload if isinstance(payload, dict) else {}

    async def listening_port(self) -> Optional[int]:
        """真实监听端口（WebUI 端口可能上漂，以此为准）。"""

        settings = await self.system_settings()
        for key in ("listeningPort", "port"):
            value = settings.get(key)
            if isinstance(value, int) and value > 0:
                return value
        nested = settings.get("settings")
        if isinstance(nested, dict) and isinstance(nested.get("webuiPort"), int):
            return int(nested["webuiPort"])
        return None

    async def connections(self) -> List[Dict[str, Any]]:
        """每个账号的适配器健康度（``[{uin, nickname, adapters:[{name,kind,status,detail}]}]``）。"""

        payload = await self.request("GET", "/api/connections")
        items = payload.get("list") if isinstance(payload, dict) else payload
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def qq_list(self) -> List[Dict[str, Any]]:
        payload = await self.request("GET", "/api/qq-list")
        items = payload.get("list") if isinstance(payload, dict) else payload
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def processes(self) -> List[Dict[str, Any]]:
        payload = await self.request("GET", "/api/processes")
        items = payload.get("list") if isinstance(payload, dict) else payload
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def get_config(self, uin: str) -> Dict[str, Any]:
        payload = await self.request("GET", f"/api/config/{uin}")
        config = payload.get("config") if isinstance(payload, dict) else None
        if isinstance(config, dict):
            return config
        return payload if isinstance(payload, dict) else {}

    # -------------------------------------------------------------- 写入
    async def save_config(self, uin: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """保存并热重载（body 是**整份** OneBotConfig，不是 patch）。"""

        payload = await self.request("POST", f"/api/config/{uin}", json=config)
        return payload if isinstance(payload, dict) else {}

    async def invoke(self, uin: str, action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """直接调用任意 OneBot action（免 ws 服务端，探活神器）。"""

        payload = await self.request(
            "POST", "/api/debug/invoke", json={"uin": str(uin), "action": action, "params": params or {}}
        )
        return payload if isinstance(payload, dict) else {}

    # -------------------------------------------------------------- 便捷汇总
    async def ws_client_status(self, names: Sequence[str] = (DEFAULT_CLIENT_NAME,)) -> List[Dict[str, Any]]:
        """把 ``/api/connections`` 摊平成「我们的 wsClient 在每个账号上的状态」。"""

        return clients_from_connections(await self.connections(), names)


def clients_from_connections(
    connections: Sequence[Dict[str, Any]], names: Sequence[str] = ()
) -> List[Dict[str, Any]]:
    """从 ``/api/connections`` 的返回值里挑出 wsClient 适配器状态。

    Args:
        connections: ``[{"uin","nickname","adapters":[{"name","kind","status","detail"}]}]``。
        names: 只保留这些适配器名；为空表示全部 wsClient。

    Returns:
        ``[{"uin","nickname","name","status","detail","connected"}]``，
        其中 ``status == "ok"`` 表示"已连接"，``warn`` 表示重连中/连接中。
    """

    wanted = {str(name) for name in names if name}
    result: List[Dict[str, Any]] = []
    for account in connections or []:
        if not isinstance(account, dict):
            continue
        uin = str(account.get("uin") or "")
        adapters = account.get("adapters")
        for adapter in adapters if isinstance(adapters, list) else []:
            if not isinstance(adapter, dict):
                continue
            if str(adapter.get("kind") or "") != "wsClient":
                continue
            if wanted and str(adapter.get("name") or "") not in wanted:
                continue
            result.append(
                {
                    "uin": uin,
                    "nickname": str(account.get("nickname") or ""),
                    "name": str(adapter.get("name") or ""),
                    "status": str(adapter.get("status") or ""),
                    "detail": str(adapter.get("detail") or ""),
                    "connected": str(adapter.get("status") or "") == "ok",
                }
            )
    return result


# --------------------------------------------------------------------------- #
# 直接用 WebUI 调 OneBot 动作（native 模式下没有可用的 WS 服务端时用）
# --------------------------------------------------------------------------- #
async def invoke_action(
    cfg: AppConfig,
    action: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    uin: str = "",
    timeout: float = 8.0,
    api: Optional["SnowLumaWebUI"] = None,
) -> Dict[str, Any]:
    """通过 SnowLuma 的 ``POST /api/debug/invoke`` 直接执行一个 OneBot 动作。

    这是 native 模式下的"应急通道"：不依赖 SnowLuma 的 wsServers 是否监听，
    也不需要 QQ 侧任何额外配置。⚠️ 它走 WebUI 而不是真正的 OneBot 连接，
    只适合"发一条测试消息 / 查一下账号"这类低频操作。

    Returns:
        ``{"ok": bool, "uin": str, "action": str, "data": Any, "payload": dict}``

    Raises:
        SnowLumaApiError: 未配置密码、没有在线账号或调用失败。
    """

    creds = webui_credentials(cfg)
    if api is None and not creds["password"]:
        raise SnowLumaApiError("未配置 SnowLuma WebUI 密码，无法直接调用 OneBot 动作")
    client = api or SnowLumaWebUI(webui_url(cfg), password=creds["password"], timeout=timeout)
    owned = api is None
    try:
        await client.login()
        target_uin = str(uin or cfg.snowluma.qq_uin or "").strip()
        if not target_uin:
            connections = await client.connections()
            target_uin = str((connections[0] or {}).get("uin") or "") if connections else ""
        if not target_uin:
            raise SnowLumaApiError("SnowLuma 里没有在线的 QQ 账号，请先完成 QQ 登录")
        payload = await client.invoke(target_uin, action, params or {})
        data = payload.get("data") if isinstance(payload, dict) else None
        retcode = payload.get("retcode") if isinstance(payload, dict) else None
        if isinstance(retcode, int) and retcode not in (0, 1):
            message = str((payload or {}).get("message") or payload)
            raise SnowLumaApiError(f"{action} 执行失败(retcode={retcode})：{message}")
        return {
            "ok": True,
            "uin": target_uin,
            "action": action,
            "data": data,
            "payload": payload if isinstance(payload, dict) else {},
        }
    finally:
        if owned:
            await client.close()


# --------------------------------------------------------------------------- #
# 探活
# --------------------------------------------------------------------------- #
def _latest_log_file(home: Path) -> Optional[Path]:
    """``<home>/logs/*.log`` 里最近修改的那个（SnowLuma 按天/按账号落盘）。"""

    logs_dir = Path(home) / "logs"
    try:
        candidates = [path for path in logs_dir.glob("*.log") if path.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _read_log_tail(path: Path, limit: int = 3000) -> List[str]:
    try:
        return path.read_text("utf-8", "replace").splitlines()[-limit:]
    except OSError:
        return []


def log_client_states(
    cfg: AppConfig,
    *,
    home: Optional[Path] = None,
    log_lines: Optional[Sequence[str]] = None,
    max_age_seconds: float = 600.0,
    names: Sequence[str] = (),
) -> Dict[str, Any]:
    """从日志里复原 SnowLuma 的 wsClient 连接状态（WebUI 不可用时的兜底判据）。

    依据实测日志行（见 ``docs/evidence-snowluma-wsclient.log``）::

        18:19:39 INFO [3317812824] [OneBot.WS-Client] [astrbot] connected ws://127.0.0.1:6199/ws
        18:20:01 WARN [3317812824] [OneBot.WS-Client] [astrbot] error ws://...: ECONNREFUSED

    Args:
        log_lines: 显式日志行（测试用）；为空则依次取事件总线 / 最近日志文件。
        max_age_seconds: 仅当"日志足够新"时才采信文件内容，避免拿几小时前的旧日志误判。
        names: 只保留这些适配器名（空表示不过滤，再按 URL 兜底过滤）。

    Returns:
        ``{"available", "source_file", "age", "clients": [...], "stale": bool}``
    """

    base = home_dir(cfg, home)
    wanted_names = {str(name) for name in names if name}
    wanted_url = _url_without_query(ws_client_entry(cfg)["url"])

    source_file = ""
    age = 0.0
    lines: List[str] = []
    if log_lines is not None:
        lines = [str(line) for line in log_lines]
        source_file = "<injected>"
    else:
        bus_lines = _recent_log_lines(cfg)
        lines = bus_lines
        if not lines:
            latest = _latest_log_file(base)
            if latest is not None:
                age = max(0.0, time.time() - latest.stat().st_mtime)
                if age <= max_age_seconds:
                    lines = _read_log_tail(latest)
                    source_file = str(latest)
                else:
                    source_file = str(latest)

    states: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    def _accept(name: str, url: str) -> bool:
        if wanted_names:
            return name in wanted_names
        return _url_without_query(url) == wanted_url

    for line in lines:
        match = _WS_CLIENT_CONNECTED.search(line)
        if match:
            name, url = match.group("name"), match.group("url")
            if not _accept(name, url):
                continue
            uin_match = _LOG_LINE_UIN.search(line)
            key = f"{name}|{url}"
            if key not in states:
                order.append(key)
            states[key] = {
                "name": name,
                "url": _url_without_query(url),
                "uin": uin_match.group("uin") if uin_match else "",
                "status": "ok",
                "detail": "已连接",
                "connected": True,
                "last_line": line.strip()[:200],
            }
            continue
        match = _WS_CLIENT_ERROR.search(line)
        if match:
            name, url, err = match.group("name"), match.group("url"), match.group("err")
            if not _accept(name, url):
                continue
            uin_match = _LOG_LINE_UIN.search(line)
            key = f"{name}|{url}"
            if key not in states:
                order.append(key)
            states[key] = {
                "name": name,
                "url": _url_without_query(url),
                "uin": uin_match.group("uin") if uin_match else "",
                "status": "warn",
                "detail": err.strip()[:160],
                "connected": False,
                "last_line": line.strip()[:200],
            }

    clients = [states[key] for key in order]
    return {
        "available": bool(clients),
        "source_file": source_file,
        "age": round(age, 1),
        "stale": bool(source_file and source_file != "<injected>" and age > max_age_seconds),
        "clients": clients,
    }


def _process_state(name: str) -> Dict[str, Any]:
    """取自 supervisor 的服务状态（不可用时返回空壳）。"""

    try:
        from .supervisor import supervisor

        return supervisor.state(name).as_dict()
    except Exception:  # noqa: BLE001 - 单测/独立使用时不强依赖 supervisor
        return {}


async def _port_open_async(port: int, timeout: float = 0.4) -> bool:
    from .supervisor import port_open

    return await asyncio.to_thread(port_open, int(port), "127.0.0.1", timeout)


def webui_url(cfg: AppConfig) -> str:
    return f"http://127.0.0.1:{int(cfg.snowluma.webui_port)}"


def webui_credentials(cfg: AppConfig, *, log_lines: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """WebUI 用户名/密码来源：配置优先，其次启动日志里的 ``Initial password``。"""

    username = "admin"
    password = str(cfg.snowluma.webui_password or "")
    source = "config" if password else ""
    if not password:
        lines = list(log_lines) if log_lines is not None else _recent_log_lines(cfg)
        for line in reversed(lines):
            match = _INITIAL_PASSWORD_PATTERN.search(line) or _INITIAL_CREDENTIAL_PATTERNS[2].search(line)
            if match:
                password = match.group(match.lastindex or 1)
                source = "log"
                break
            user_match = _INITIAL_CREDENTIAL_PATTERNS[0].search(line)
            if user_match:
                username = user_match.group(1)
    return {"username": username, "password": password, "source": source}


def launch_env(cfg: AppConfig) -> Dict[str, str]:
    """建议注入给 SnowLuma 进程的环境变量（供 installer/supervisor 使用）。

    * ``SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD``：免去解析日志拿初始口令；
    * ``SNOWLUMA_ACCEPT_EULA`` / ``SNOWLUMA_ACCEPT_PRIVACY``：跳过协议门禁；
    * ``SNOWLUMA_HOOK_AUTOLOAD``：自动注入 QQ。
    """

    env = {
        "SNOWLUMA_ACCEPT_EULA": "1",
        "SNOWLUMA_ACCEPT_PRIVACY": "1",
        "SNOWLUMA_HOOK_AUTOLOAD": "1",
    }
    password = str(cfg.snowluma.webui_password or "")
    if password:
        env["SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD"] = password
    return env


async def probe(
    cfg: AppConfig,
    *,
    home: Optional[Path] = None,
    timeout: float = 1.5,
    log_lines: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """探测 SnowLuma 状态：部署/进程/端口/WebUI/账号/适配器连接。

    返回结构（供 ``/api/link/status`` 直接使用）::

        {
          "ok": True, "state": "connected|waiting_qq|reconnecting|config_missing|
                                 not_running|not_deployed", "hint": "人话建议",
          "deployed": bool, "home": "...", "config_dir": "...",
          "process": {...}, "webui": {port, listening_port, reachable, logged_in, error},
          "accounts": [...], "accounts_source": {...},
          "template": {"exists", "has_client"}, "per_uin": [...],
          "clients": [{uin,name,status,detail,connected}], "connected": bool,
          "suggestions": [...]
        }

    ``state`` 的语义刻意区分了「AstrBot 正常但还没登录 QQ」与「配置写错了」，
    因为这两种情况用户要做的事完全不同。
    """

    base = home_dir(cfg, home)
    cdir = base / CONFIG_DIRNAME
    deployed = base.is_dir()
    template_path = cdir / TEMPLATE_FILENAME
    template = read_json(template_path) if template_path.exists() else {}
    entry = ws_client_entry(cfg)
    wanted_url = _url_without_query(entry["url"])
    template_has_client = any(
        isinstance(item, dict) and _url_without_query(str(item.get("url", ""))) == wanted_url
        for item in ((template.get("networks") or {}).get("wsClients") or [])
    )

    per_uin: List[Dict[str, Any]] = []
    our_names = {DEFAULT_CLIENT_NAME}
    for uin in accounts_from_files(base):
        data = read_json(cdir / f"onebot_{uin}.json")
        clients = [item for item in ((data.get("networks") or {}).get("wsClients") or []) if isinstance(item, dict)]
        ours = [
            item for item in clients if _url_without_query(str(item.get("url", ""))) == wanted_url
        ]
        for item in ours:
            if item.get("name"):
                our_names.add(str(item["name"]))
        per_uin.append(
            {
                "uin": uin,
                "mode": str(data.get("mode") or ""),
                "has_client": bool(ours),
                "client_name": str((ours[0].get("name") if ours else "") or ""),
                "clients": len(clients),
            }
        )
    for item in ((template.get("networks") or {}).get("wsClients") or []):
        if isinstance(item, dict) and _url_without_query(str(item.get("url", ""))) == wanted_url:
            if item.get("name"):
                our_names.add(str(item["name"]))

    webui_port = int(cfg.snowluma.webui_port)
    reachable = await _port_open_async(webui_port, timeout=min(0.6, timeout))
    webui: Dict[str, Any] = {
        "url": webui_url(cfg),
        "port": webui_port,
        "reachable": reachable,
        "listening_port": None,
        "logged_in": False,
        "error": "",
        "cooldown": login_cooldown(),
    }
    clients: List[Dict[str, Any]] = []
    accounts = list_accounts(cfg, home=home, log_lines=log_lines)
    source = "none"

    if reachable:
        creds = webui_credentials(cfg, log_lines=log_lines)
        api = SnowLumaWebUI(webui_url(cfg), password=creds["password"], timeout=timeout)
        try:
            await api.login()
            webui["logged_in"] = True
            try:
                webui["listening_port"] = await api.listening_port()
            except SnowLumaApiError:
                pass
            raw_connections = await api.connections()
            clients = clients_from_connections(raw_connections, tuple(our_names))
            if not clients:
                # 适配器被用户改过名时，退化为"全部 wsClient"里按 URL 匹配
                clients = [
                    item for item in clients_from_connections(raw_connections)
                    if item.get("name") in our_names
                ]
            for account in raw_connections:
                uin = str(account.get("uin") or "")
                if uin and uin not in accounts:
                    accounts.append(uin)
            accounts = sorted({uin for uin in accounts if uin},
                              key=lambda x: (int(x) if x.isdigit() else 0, x))
            if clients:
                source = "webui"
        except SnowLumaApiError as exc:
            webui["error"] = str(exc)
        finally:
            await api.close()

    # WebUI 拿不到（限流/门禁/会话还没起来）时，用日志兜底 —— 这是"已连接"的可靠证据
    log_state: Dict[str, Any] = {"available": False, "source_file": "", "age": 0.0, "clients": []}
    if not clients:
        log_state = log_client_states(cfg, home=home, log_lines=log_lines, names=tuple(our_names))
        if log_state["clients"]:
            clients = log_state["clients"]
            source = "log"

    connected = [item for item in clients if item.get("connected")]
    warnings = [item for item in clients if not item.get("connected")]
    process = _process_state("snowluma")

    mismatch_warnings: List[str] = []
    online_uins = {str(item.get("uin") or "") for item in clients if item.get("uin")}
    configured_uin = str(cfg.snowluma.qq_uin or "").strip()
    if configured_uin and online_uins and configured_uin not in online_uins:
        mismatch_warnings.append(
            f"当前登录 QQ {sorted(online_uins)[0]} 与配置 {configured_uin} 不一致，"
            "管理员指令/账号相关配置可能失效（建议把配置里的 QQ 号改成实际登录的账号）"
        )

    suggestions: List[str] = []
    if not deployed:
        state = "not_deployed"
        hint = "尚未部署 SnowLuma，请先在「部署」页下载并解压发行包"
        suggestions.append("运行一键部署，或手动指定 SnowLuma 安装目录")
    elif process.get("status") in ("starting",) and not reachable:
        state = "starting"
        hint = "SnowLuma 正在启动，WebUI 端口还没就绪，稍等几秒"
    elif not reachable and process.get("status") not in ("running", "starting"):
        state = "not_running"
        hint = "SnowLuma 未运行，启动后它会自动连上 AstrBot"
        suggestions.append("点击「启动 SnowLuma」")
    elif connected:
        state = "connected"
        hint = f"已连接：{', '.join(item.get('uin') or item.get('name', '') for item in connected)}"
    elif not accounts and not clients:
        state = "waiting_qq"
        hint = "SnowLuma 正在运行，但还没有 QQ 账号登录；OneBot 适配器只在 QQ 会话建立后才监听/连接"
        suggestions.append("在 SnowLuma 面板扫码登录 QQ（或确认 QQ 已启动并注入 hook）")
    elif warnings:
        state = "reconnecting"
        hint = f"适配器未连上（{warnings[0].get('detail') or '重连中'}），请检查 AstrBot 是否已启动并监听联动端口"
        suggestions.append("确认 AstrBot 已启动（WebUI 端口可访问）且 aiocqhttp 反向 WS 正在监听")
        suggestions.append("确认 SnowLuma 与 AstrBot 的 token 一致（重新执行「写入配置」）")
    elif not clients:
        state = "config_missing"
        hint = "SnowLuma 在运行，但没有我们写入的反向 WS 连接（可能尚未写入配置，或 QQ 会话未建立）"
        suggestions.append("点击「写入配置」把 wsClient 注入 SnowLuma")
    else:  # pragma: no cover - 理论不可达
        state = "unknown"
        hint = "状态未知"

    if webui.get("error"):
        suggestions.insert(0, f"SnowLuma 面板登录/查询失败：{webui['error']}")
    if deployed and not template_has_client:
        suggestions.append("模板 config/onebot.json 里还没有 astrbot 连接，建议执行「写入配置」")
    suggestions.extend(mismatch_warnings)

    # 进程没跑时，日志里的历史"connected"只能作为历史证据（log 字段），
    # 不能对外宣称"当前已连接"，否则状态灯与实际不符。
    connected_now = bool(connected) and state not in ("not_running", "not_deployed")

    return {
        "ok": True,
        "state": state,
        "hint": hint,
        "source": source,
        "deployed": deployed,
        "home": str(base),
        "config_dir": str(cdir),
        "process": process,
        "webui": webui,
        "log": log_state,
        "accounts": accounts,
        "accounts_source": account_sources(cfg, home=home, log_lines=log_lines),
        "template": {"path": str(template_path), "exists": template_path.exists(),
                     "has_client": template_has_client},
        "per_uin": per_uin,
        "clients": clients,
        "connected": connected_now,
        "warnings": mismatch_warnings,
        "target_url": entry["url"],
        "suggestions": suggestions,
    }


# --------------------------------------------------------------------------- #
# 写入入口
# --------------------------------------------------------------------------- #
def _write_onebot_file(path: Path, config: Dict[str, Any], *, mode: Optional[str]) -> Dict[str, Any]:
    """校验并写单个 OneBot 配置文件。"""

    data = dict(config)
    if mode and "mode" not in data:
        data["mode"] = mode
    validate_onebot_config(data)
    write_json(path, data)
    return {"path": str(path), "mode": data.get("mode", "")}


async def apply_native(
    cfg: AppConfig,
    *,
    home: Optional[Path] = None,
    hot: bool = True,
    timeout: float = 4.0,
) -> Dict[str, Any]:
    """native 模式：让 SnowLuma 作为 OneBot 客户端连到 AstrBot 的反向 WS。

    策略（兼顾全新安装与老用户）::

        1. 写 config/onebot.json（全局模板，未持久化 per-UIN 时生效）；
        2. 对每个已存在的 config/onebot_<uin>.json 覆盖我们的 wsClient 条目，
           保留用户的其它适配器与 "mode":"snapshot"；
        3. 若 SnowLuma 正在运行且 WebUI 可登录，再走 REST 热重载（免重启）；
        4. 用 /api/connections 复核是否真的连上。

    Args:
        cfg: 控制台配置。
        home: 显式安装目录（测试用）。
        hot: 是否尝试 REST 热重载（进程没跑时自动跳过）。

    Returns:
        ``{"ok": bool, "steps": [...], "files": [...], "hot": {...}, "verify": {...}}``
    """

    base = home_dir(cfg, home)
    cdir = base / CONFIG_DIRNAME
    steps: List[Dict[str, Any]] = []
    files: List[str] = []

    if not base.is_dir():
        return {
            "ok": False,
            "error": f"SnowLuma 安装目录不存在：{base}（请先部署）",
            "steps": steps,
            "files": files,
        }

    entry_base = ws_client_entry(cfg)

    # 1) 模板
    template_path = cdir / TEMPLATE_FILENAME
    template = read_json(template_path) if template_path.exists() else minimal_onebot_config()
    template_merged = merge_ws_client(template, entry_base)
    info = _write_onebot_file(template_path, template_merged, mode=None)
    files.append(info["path"])
    steps.append({"step": "template", "ok": True, "detail": f"已写入 {info['path']}"})

    # 2) 每个已存在的 per-UIN 文件
    uins = accounts_from_files(base)
    per_uin_results: List[Dict[str, Any]] = []
    for uin in uins:
        path = cdir / f"onebot_{uin}.json"
        current = read_json(path)
        merged = merge_ws_client(current, entry_base)
        mode = str(current.get("mode") or "snapshot")
        info = _write_onebot_file(path, merged, mode=mode)
        files.append(info["path"])
        per_uin_results.append({"uin": uin, "path": info["path"], "mode": mode})
    steps.append(
        {
            "step": "per_uin",
            "ok": True,
            "detail": f"已覆盖 {len(per_uin_results)} 个账号配置" if per_uin_results else "没有已存在的账号配置（仅写模板）",
            "accounts": [item["uin"] for item in per_uin_results],
        }
    )

    # 3) 运行中热重载
    hot_result: Dict[str, Any] = {"attempted": False, "applied": [], "skipped": [], "error": "", "note": ""}
    verify: Dict[str, Any] = {"clients": [], "connected": False}
    creds = webui_credentials(cfg)
    if hot and creds["password"]:
        api = SnowLumaWebUI(webui_url(cfg), password=creds["password"], timeout=timeout)
        try:
            await api.login()
            hot_result["attempted"] = True
            # 在线账号可能还没有 per-UIN 文件（全新安装），以 /api/connections 为准
            live_uins: List[str] = []
            try:
                live_uins = [str(item.get("uin") or "") for item in await api.connections()]
            except SnowLumaApiError:
                live_uins = []
            targets = [u for u in dict.fromkeys(uins + live_uins + [str(cfg.snowluma.qq_uin or "").strip()]) if u]
            hot_result["targets"] = targets
            for uin in targets:
                try:
                    current = await api.get_config(uin)
                    merged = merge_ws_client(current, entry_base)
                    validate_onebot_config(merged)
                    payload = await api.save_config(uin, merged)
                    hot_result["applied"].append(
                        {
                            "uin": uin,
                            "saved": payload.get("saved"),
                            "applied": payload.get("applied"),
                            "online": payload.get("online"),
                            "reloaded": payload.get("reloaded"),
                            "message": payload.get("message", ""),
                        }
                    )
                except SnowLumaApiError as exc:
                    hot_result["skipped"].append({"uin": uin, "reason": str(exc)})
            try:
                verify = {
                    "clients": await api.ws_client_status(),
                    "connected": False,
                    "live_accounts": [u for u in live_uins if u],
                }
                verify["connected"] = any(item["connected"] for item in verify["clients"])
            except SnowLumaApiError:
                pass
        except SnowLumaApiError as exc:
            hot_result["error"] = str(exc)
        finally:
            await api.close()
    else:
        hot_result["note"] = (
            "未配置 SnowLuma WebUI 密码，已写入文件但不会热重载（重启 SnowLuma 后生效）"
            if hot
            else "已跳过热重载（仅写文件）"
        )
    steps.append(
        {
            "step": "hot_reload",
            "ok": bool(hot_result["applied"]) or not hot_result["attempted"],
            "detail": hot_result["error"]
            or hot_result["note"]
            or (f"已热重载 {len(hot_result['applied'])} 个账号" if hot_result["applied"] else "无需热重载"),
        }
    )

    return {
        "ok": True,
        "mode": "native",
        "steps": steps,
        "files": files,
        "accounts": uins,
        "per_uin": per_uin_results,
        "entry": entry_base,
        "hot": hot_result,
        "verify": verify,
        "restart_required": not hot_result["applied"],
        "note": "SnowLuma 每条 QQ 会话都会各自建立一条反向 WS 连接（多开 QQ 会看到多条连接）",
    }


async def apply_relay(
    cfg: AppConfig,
    *,
    home: Optional[Path] = None,
    hot: bool = False,
    timeout: float = 4.0,
) -> Dict[str, Any]:
    """中继模式：在 SnowLuma 侧写一个 wsServer，供内置中继（或其它 OneBot 应用）连入。"""

    base = home_dir(cfg, home)
    cdir = base / CONFIG_DIRNAME
    steps: List[Dict[str, Any]] = []
    files: List[str] = []
    if not base.is_dir():
        return {
            "ok": False,
            "error": f"SnowLuma 安装目录不存在：{base}（请先部署）",
            "steps": steps,
            "files": files,
        }

    entry = ws_server_entry(cfg)
    template_path = cdir / TEMPLATE_FILENAME
    template = read_json(template_path) if template_path.exists() else minimal_onebot_config()
    merged = merge_ws_server(template, entry)
    info = _write_onebot_file(template_path, merged, mode=None)
    files.append(info["path"])
    steps.append({"step": "template", "ok": True, "detail": f"已写入 wsServer 到 {info['path']}"})

    per_uin: List[Dict[str, Any]] = []
    for uin in accounts_from_files(base):
        path = cdir / f"onebot_{uin}.json"
        current = read_json(path)
        merged_uin = merge_ws_server(current, entry)
        mode = str(current.get("mode") or "snapshot")
        info = _write_onebot_file(path, merged_uin, mode=mode)
        files.append(info["path"])
        per_uin.append({"uin": uin, "path": info["path"], "mode": mode})
    steps.append(
        {
            "step": "per_uin",
            "ok": True,
            "detail": f"已覆盖 {len(per_uin)} 个账号配置" if per_uin else "没有已存在的账号配置（仅写模板）",
        }
    )

    hot_result: Dict[str, Any] = {"attempted": False, "applied": [], "error": ""}
    creds = webui_credentials(cfg)
    if hot and creds["password"] and accounts_from_files(base):
        api = SnowLumaWebUI(webui_url(cfg), password=creds["password"], timeout=timeout)
        try:
            await api.login()
            hot_result["attempted"] = True
            for uin in accounts_from_files(base):
                try:
                    current = await api.get_config(uin)
                    payload = await api.save_config(uin, merge_ws_server(current, entry))
                    hot_result["applied"].append({"uin": uin, "applied": payload.get("applied")})
                except SnowLumaApiError as exc:
                    hot_result["error"] = str(exc)
        except SnowLumaApiError as exc:
            hot_result["error"] = str(exc)
        finally:
            await api.close()

    return {
        "ok": True,
        "mode": "bridge",
        "steps": steps,
        "files": files,
        "per_uin": per_uin,
        "entry": entry,
        "relay_url": relay_ws_url(cfg),
        "relay_token": relay_token(cfg),
        "hot": hot_result,
        "restart_required": not hot_result["applied"],
    }


async def apply_webui(cfg: AppConfig, *, home: Optional[Path] = None) -> Dict[str, Any]:
    """确保 ``config/runtime.json`` 里的 WebUI 端口/监听地址正确。

    注意：``webuiPort``/``webuiHost`` 是"监听级"配置，**修改后需要重启 SnowLuma**才生效。
    """

    base = home_dir(cfg, home)
    cdir = base / CONFIG_DIRNAME
    if not base.is_dir():
        return {"ok": False, "error": f"SnowLuma 安装目录不存在：{base}（请先部署）"}
    path = cdir / "runtime.json"
    current = read_json(path, default_runtime_config())
    runtime = dict(default_runtime_config())
    runtime.update(current if current else {})
    changed: List[str] = []
    desired_port = int(cfg.snowluma.webui_port or DEFAULT_WEBUI_PORT)
    if runtime.get("webuiPort") != desired_port:
        runtime["webuiPort"] = desired_port
        changed.append("webuiPort")
    if runtime.get("webuiHost") not in ("127.0.0.1", "localhost"):
        runtime["webuiHost"] = "127.0.0.1"
        changed.append("webuiHost")
    if runtime.get("hookAutoLoad") is not True:
        runtime["hookAutoLoad"] = True
        changed.append("hookAutoLoad")
    tls = runtime.get("webuiTls")
    if not isinstance(tls, dict):
        runtime["webuiTls"] = {"enabled": False}
        changed.append("webuiTls")
    write_json(path, runtime)
    return {
        "ok": True,
        "path": str(path),
        "changed": changed,
        "restart_required": bool(changed),
        "detail": "已更新 runtime.json" + ("（需重启 SnowLuma 才生效）" if changed else "（无需改动）"),
    }


async def ensure_consent_files(cfg: AppConfig, *, home: Optional[Path] = None) -> Dict[str, Any]:
    """（可选）预置 ``config/consent.json`` 跳过协议门禁。

    版本号是内容哈希，硬编码会失效，因此只在拿到当前版本号后才写；
    更稳的做法是启动时注入 ``SNOWLUMA_ACCEPT_EULA`` / ``SNOWLUMA_ACCEPT_PRIVACY``。
    """

    base = home_dir(cfg, home)
    creds = webui_credentials(cfg)
    if not creds["password"]:
        return {"ok": False, "error": "未配置 WebUI 密码，无法自动同意协议"}
    api = SnowLumaWebUI(webui_url(cfg), password=creds["password"])
    try:
        await api.login()
        ok = await api.ensure_consent()
        return {"ok": ok, "detail": "已记录协议同意" if ok else "协议同意失败（可改用环境变量方式）"}
    except SnowLumaApiError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        await api.close()


def applied_summary(cfg: AppConfig, *, home: Optional[Path] = None) -> Dict[str, Any]:
    """同步快速摘要（不联网）：模板/per-UIN 里是否有我们的连接、账号清单。"""

    base = home_dir(cfg, home)
    cdir = base / CONFIG_DIRNAME
    entry = ws_client_entry(cfg)
    wanted = _url_without_query(entry["url"])
    template = read_json(cdir / TEMPLATE_FILENAME) if (cdir / TEMPLATE_FILENAME).exists() else {}
    template_ok = any(
        _url_without_query(str(item.get("url", ""))) == wanted
        for item in ((template.get("networks") or {}).get("wsClients") or [])
        if isinstance(item, dict)
    )
    return {
        "deployed": base.is_dir(),
        "home": str(base),
        "template": {"exists": (cdir / TEMPLATE_FILENAME).exists(), "has_client": template_ok},
        "accounts": accounts_from_files(base),
        "target_url": entry["url"],
        "captured_at": time.time(),
    }
