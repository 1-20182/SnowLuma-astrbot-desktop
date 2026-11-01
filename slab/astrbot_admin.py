"""AstrBot 侧：``data/cmd_config.json`` 的深合并写入、WebUI 口令提取与探活。

实证要点（见 ``.scratch/AstrBot`` 源码与 ``astrbot-run.log``）
--------------------------------------------------------------
* 配置文件路径：``<AstrBot 根目录>/data/cmd_config.json``（根目录默认是进程 cwd，
  可用 ``ASTRBOT_ROOT`` 覆盖）。
* ``platform`` / ``provider`` 都是**列表**；``provider`` 元素读 ``key``（列表）、
  ``api_base``、``model``（``astrbot/core/provider/sources/openai_source.py:373,397``）。
* ``dashboard.password`` 是 md5、另有 ``pbkdf2_password``；我们**不改用户口令**，
  只从启动日志里抓一次性打印的初始口令用于"一键打开面板"。
* AstrBot 启动时会用默认模板补齐缺失键并回写，所以"只写我们关心的键"是安全的；
  但**必须在 AstrBot 启动前写**，否则 WebUI 保存时会覆盖。
* 内置指令（``/help`` 等）要求发送者在 ``admins_id`` 里，否则会被拒绝。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from . import paths
from .config import AppConfig
from .onebot.client import probe_ws_handshake

log = logging.getLogger("slab.astrbot")

#: 我们写入的 platform 条目 id（AstrBot 日志会打印 ``aiocqhttp(<id>)``）
DEFAULT_PLATFORM_ID = "snowluma"
#: 我们写入的 provider 条目 id
DEFAULT_PROVIDER_ID = "snowluma-console"
#: 与 AstrBot 4.29.3 对齐的配置版本号
CONFIG_VERSION = 5
#: aiocqhttp 反向 WS 平台适配器类型
PLATFORM_TYPE = "aiocqhttp"
#: 默认模型适配器类型
DEFAULT_PROVIDER_TYPE = "openai_chat_completion"

#: 日志里抓初始口令/用户名的模式（``➜  Initial password: xxx``）
_PASSWORD_PATTERNS = (
    re.compile(r"Initial password:\s*(\S+)"),
    re.compile(r"initial credentials:\s*user=\S+\s+password=(\S+)"),
    re.compile(r"初始密码[：:]\s*(\S+)"),
)
_USERNAME_PATTERNS = (
    re.compile(r"Initial username:\s*(\S+)"),
    re.compile(r"initial credentials:\s*user=(\S+)\s+password="),
)
#: 就绪/连接相关日志
LOG_READY_WEBUI = re.compile(r"Starting WebUI at\s+(http\S+)")
LOG_PLATFORM_LOADED = re.compile(r"Loading IM platform adapter\s+aiocqhttp", re.IGNORECASE)
LOG_PLATFORM_CONNECTED = re.compile(r"aiocqhttp\(OneBot v11\) 适配器已连接|aiocqhttp.*已连接", re.IGNORECASE)
LOG_PORT_IN_USE = re.compile(r"端口\s*(\d+)\s*已被占用|address already in use", re.IGNORECASE)
LOG_ERROR = re.compile(r"\[(?:ERROR|CRITICAL)\]")

MASK = "***"


class AstrBotConfigError(RuntimeError):
    """写入的 AstrBot 配置不合法（消息面向用户）。"""


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def home_dir(cfg: AppConfig, home: Optional[Path] = None) -> Path:
    """AstrBot 根目录（进程 cwd）。"""

    return Path(home) if home else paths.astrbot_home(cfg.astrbot.install_dir)


def data_dir(cfg: AppConfig, home: Optional[Path] = None) -> Path:
    return home_dir(cfg, home) / "data"


def config_path(cfg: AppConfig, home: Optional[Path] = None) -> Path:
    """``data/cmd_config.json``。"""

    return data_dir(cfg, home) / "cmd_config.json"


def backup_dir(cfg: AppConfig, home: Optional[Path] = None) -> Path:
    return data_dir(cfg, home) / "backups"


def dashboard_url(cfg: AppConfig) -> str:
    return f"http://127.0.0.1:{int(cfg.astrbot.webui_port)}"


# --------------------------------------------------------------------------- #
# 文件读写
# --------------------------------------------------------------------------- #
def read_config(path: Path) -> Dict[str, Any]:
    """读取 cmd_config.json；不存在/损坏时返回空 dict（调用方用默认模板兜底）。"""

    try:
        raw = Path(path).read_text("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("AstrBot 配置损坏，将按最小结构重建：%s", path)
        return {}
    return data if isinstance(data, dict) else {}


def default_config() -> Dict[str, Any]:
    """最小可用配置（AstrBot 启动时会把缺的键补齐并回写）。"""

    return {
        "config_version": CONFIG_VERSION,
        "platform": [],
        "provider": [],
        "provider_sources": [],
        "dashboard": {
            "enable": True,
            "username": "astrbot",
            "host": "0.0.0.0",
            "port": 6185,
            "password": "",
            "pbkdf2_password": "",
            "password_change_required": True,
        },
        "admins_id": [],
        "platform_settings": {},
        "provider_settings": {},
        "persona": [],
    }


def backup(cfg: AppConfig, *, home: Optional[Path] = None, tag: str = "") -> Dict[str, Any]:
    """把现有 cmd_config.json 备份到 ``data/backups/``；文件不存在返回 ``ok: False``。"""

    source = config_path(cfg, home)
    if not source.is_file():
        return {"ok": False, "detail": f"没有可备份的配置文件：{source}"}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = f"-{tag}" if tag else ""
    target = backup_dir(cfg, home) / f"cmd_config-{stamp}{suffix}.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    except OSError as exc:
        return {"ok": False, "error": f"备份失败：{exc}"}
    return {"ok": True, "path": str(target), "size": target.stat().st_size}


def write_config(path: Path, data: Dict[str, Any]) -> Path:
    """原子写入配置（临时文件 + ``os.replace``）。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(tmp, target)
    return target


# --------------------------------------------------------------------------- #
# 条目构造
# --------------------------------------------------------------------------- #
def build_platform_entry(cfg: AppConfig, *, platform_id: str = DEFAULT_PLATFORM_ID) -> Dict[str, Any]:
    """aiocqhttp 反向 WS 平台条目（AstrBot 作为服务端，SnowLuma 连进来）。

    监听地址默认收窄：AstrBot 模板给的是 ``0.0.0.0``（暴露到局域网），而 SnowLuma
    与本控制台都在本机，因此把通配地址改写成 ``127.0.0.1``；用户显式填了别的地址则尊重原值。
    """

    platform_cfg = dict(cfg.astrbot.platform or {})
    host = str(platform_cfg.get("ws_reverse_host") or "127.0.0.1").strip()
    if host in ("0.0.0.0", "*", "::", "[::]"):
        host = "127.0.0.1"
    return {
        "id": platform_id,
        "type": PLATFORM_TYPE,
        "enable": True,
        "ws_reverse_host": host,
        "ws_reverse_port": int(cfg.link.ws_port),
        "ws_reverse_token": str(cfg.link.token or ""),
    }


def build_provider_entry(cfg: AppConfig, *, provider_id: str = DEFAULT_PROVIDER_ID) -> Dict[str, Any]:
    """OpenAI 兼容的模型提供商条目。

    Raises:
        AstrBotConfigError: API Key / 接口地址 / 模型名缺失时（消息可直接展示给用户）。
    """

    provider_cfg = dict(cfg.astrbot.provider or {})
    api_key = str(provider_cfg.get("api_key") or "").strip()
    base_url = str(provider_cfg.get("base_url") or "").strip()
    model = str(provider_cfg.get("model") or "").strip()
    adapter_type = str(provider_cfg.get("type") or DEFAULT_PROVIDER_TYPE).strip()
    if not api_key:
        raise AstrBotConfigError("请先在配置页填写大模型 API Key（config.astrbot.provider.api_key），否则 AstrBot 无法回复消息")
    if not base_url:
        raise AstrBotConfigError("请填写大模型接口地址（config.astrbot.provider.base_url），例如 https://api.deepseek.com/v1")
    if not model:
        raise AstrBotConfigError("请填写模型名（config.astrbot.provider.model），例如 deepseek-chat")
    return {
        "id": provider_id,
        "provider": str(provider_cfg.get("provider") or "openai"),
        "type": adapter_type,
        "provider_type": "chat_completion",
        "enable": True,
        "key": [api_key],
        "api_base": base_url.rstrip("/"),
        "model": model,
        "timeout": int(provider_cfg.get("timeout") or 120),
        "proxy": "",
        "custom_headers": {},
    }


# --------------------------------------------------------------------------- #
# 深合并
# --------------------------------------------------------------------------- #
def _as_list(value: Any) -> List[Any]:
    """AstrBot 的 list 型配置偶尔会被写成 dict（单条），这里统一成 list。"""

    if isinstance(value, list):
        return value
    if isinstance(value, dict) and value:
        return [value]
    return []


def merge_platform(
    config: Dict[str, Any], entry: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """把 aiocqhttp 平台条目合并进配置，返回 ``(新配置, 变更信息)``。

    规则：优先按 ``id`` 命中；其次按 ``type == aiocqhttp`` 就地更新（不新增重复条目）；
    都没有才 append。**绝不删除用户其它平台**。
    """

    merged = copy.deepcopy(config)
    platforms = _as_list(merged.get("platform"))
    target: Optional[Dict[str, Any]] = None
    matched_by = ""
    for item in platforms:
        if isinstance(item, dict) and str(item.get("id") or "") == entry["id"]:
            target = item
            matched_by = "id"
            break
    if target is None:
        for item in platforms:
            if isinstance(item, dict) and str(item.get("type") or "").lower() == PLATFORM_TYPE:
                target = item
                matched_by = "type"
                break
    before: Dict[str, Any] = {}
    if target is None:
        platforms.append(dict(entry))
        target = platforms[-1]
        merged["platform"] = platforms
        matched_by = "new"
    else:
        before = {key: target.get(key) for key in ("enable", "ws_reverse_host", "ws_reverse_port",
                                                   "ws_reverse_token")}
        target.update({key: value for key, value in entry.items() if key != "id"})
        # 保留既有 id，避免 AstrBot 里出现两条同类型平台
        target.setdefault("id", entry["id"])
        target["type"] = PLATFORM_TYPE
        merged["platform"] = platforms
    changed = matched_by == "new" or any(
        before.get(key) != target.get(key) for key in ("enable", "ws_reverse_host", "ws_reverse_port")
    )
    return merged, {
        "action": matched_by,
        "id": str(target.get("id") or entry["id"]),
        "port": int(target.get("ws_reverse_port") or 0),
        "token_changed": bool(before) and before.get("ws_reverse_token") != target.get("ws_reverse_token"),
        "changed": changed,
    }


def merge_provider(
    config: Dict[str, Any], entry: Dict[str, Any], *, set_default: bool = True
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """把模型提供商条目合并进配置（同样只增/就地更新，不删用户其它提供商）。"""

    merged = copy.deepcopy(config)
    providers = _as_list(merged.get("provider"))
    target: Optional[Dict[str, Any]] = None
    matched_by = ""
    for item in providers:
        if isinstance(item, dict) and str(item.get("id") or "") == entry["id"]:
            target = item
            matched_by = "id"
            break
    if target is None:
        for item in providers:
            if isinstance(item, dict) and str(item.get("type") or "") == entry["type"]:
                target = item
                matched_by = "type"
                break
    if target is None:
        # 放在列表首位：AstrBot 在没显式指定 provider_id 时取"第一个可用"的提供商
        providers.insert(0, dict(entry))
        target = providers[0]
        matched_by = "new"
    else:
        target.update(entry)
        target.setdefault("id", entry["id"])
        matched_by = "update"
    merged["provider"] = providers

    # 已被 AstrBot 迁移过的部署：provider 条目只剩 provider_source_id，
    # key/api_base 在 provider_sources 里。此时要同步更新 source，并把 provider 条目
    # 恢复成"新结构"（不把 key 重复写回 provider，避免 AstrBot 再次迁移时出现双份配置）。
    source_updated = False
    source_id = str(target.get("provider_source_id") or "")
    if source_id:
        sources = _as_list(merged.get("provider_sources"))
        source = next((item for item in sources
                       if isinstance(item, dict) and str(item.get("id") or "") == source_id), None)
        source_payload = {
            "id": source_id,
            "provider": entry.get("provider", "openai"),
            "type": entry["type"],
            "provider_type": entry.get("provider_type", "chat_completion"),
            "enable": True,
            "key": list(entry.get("key") or []),
            "api_base": entry.get("api_base", ""),
            "timeout": entry.get("timeout", 120),
            "proxy": entry.get("proxy", ""),
            "custom_headers": entry.get("custom_headers", {}),
        }
        if source is None:
            sources.append(source_payload)
        else:
            source.update(source_payload)
        merged["provider_sources"] = sources
        target.clear()
        target.update(
            {
                "id": str(entry["id"]),
                "enable": True,
                "model": entry.get("model", ""),
                "provider_source_id": source_id,
            }
        )
        source_updated = True

    default_set = False
    if set_default:
        default_set = _ensure_default_provider(merged, str(target.get("id") or entry["id"]))
    return merged, {
        "action": matched_by,
        "id": str(target.get("id") or entry["id"]),
        "model": str(target.get("model") or ""),
        "api_base": str((target.get("api_base") or entry.get("api_base") or "")),
        "provider_source_id": source_id,
        "provider_source_updated": source_updated,
        "default_set": default_set,
        "changed": True,
    }


def merge_reply_policy(config: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """写入产品约定的默认回复策略：**私聊全回、群里必须 @ 或指令前缀**。

    * ``platform_settings.friend_message_needs_wake_prefix = false``（私聊不需要前缀）；
    * 群聊由 AstrBot 默认行为保证（需要 @ 或 ``wake_prefix``），我们把 ``wake_prefix``
      缺失时补成默认 ``["/"]``，已存在则**不改**；
    * 不碰 ``unique_session`` / ``reply_with_mention``，保留用户原值。
    """

    merged = copy.deepcopy(config)
    settings = merged.get("platform_settings")
    if not isinstance(settings, dict):
        settings = {}
    before_friend = settings.get("friend_message_needs_wake_prefix")
    settings["friend_message_needs_wake_prefix"] = False
    merged["platform_settings"] = settings

    wake_before = merged.get("wake_prefix")
    wake_defaulted = False
    if not isinstance(wake_before, list) or not wake_before:
        merged["wake_prefix"] = ["/"]
        wake_defaulted = True

    return merged, {
        "friend_message_needs_wake_prefix": False,
        "friend_message_needs_wake_prefix_changed": before_friend not in (False, None, 0),
        "wake_prefix": list(merged.get("wake_prefix") or []),
        "wake_prefix_defaulted": wake_defaulted,
        "summary": "私聊自动回复 / 群聊需 @机器人 或指令前缀",
        "untouched": ["unique_session", "reply_with_mention", "reply_with_quote"],
    }


def _ensure_default_provider(config: Dict[str, Any], provider_id: str) -> bool:
    """把 ``agent_runner.config.model.provider_id`` 指向我们的 provider。

    只在**用户没有显式选择**（为空）时写入，避免用户以后手动加别的提供商时被我们反复覆盖。
    """

    agent_runner = config.get("agent_runner")
    if not isinstance(agent_runner, dict):
        return False
    if str(agent_runner.get("runner_type") or "local") != "local":
        return False
    runner_cfg = agent_runner.get("config")
    if not isinstance(runner_cfg, dict):
        return False
    model_cfg = runner_cfg.get("model")
    if not isinstance(model_cfg, dict):
        return False
    current = str(model_cfg.get("provider_id") or "")
    if current:
        return False
    model_cfg["provider_id"] = provider_id
    return True


def merge_dashboard(config: Dict[str, Any], cfg: AppConfig) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """更新 ``dashboard.port`` / ``username``（**绝不改密码**）。"""

    merged = copy.deepcopy(config)
    dashboard = merged.get("dashboard")
    if not isinstance(dashboard, dict):
        dashboard = dict(default_config()["dashboard"])
    before = {"port": dashboard.get("port"), "username": dashboard.get("username")}
    dashboard["port"] = int(cfg.astrbot.webui_port)
    if not str(dashboard.get("username") or "").strip():
        dashboard["username"] = str(cfg.astrbot.webui_username or "astrbot")
    dashboard.setdefault("host", "0.0.0.0")
    dashboard.setdefault("enable", True)
    merged["dashboard"] = dashboard
    return merged, {
        "port": dashboard["port"],
        "port_changed": before["port"] != dashboard["port"],
        "username": str(dashboard.get("username") or ""),
        "password_untouched": True,
    }


def merge_admins(config: Dict[str, Any], admins: Sequence[str]) -> Tuple[Dict[str, Any], List[str]]:
    """把管理员 QQ 合并进 ``admins_id``（**只增不减**，保留用户已有项）。"""

    wanted = [str(item).strip() for item in admins if str(item).strip()]
    if not wanted:
        return config, []
    merged = copy.deepcopy(config)
    current = merged.get("admins_id")
    existing = [str(item) for item in current] if isinstance(current, list) else []
    added = [item for item in wanted if item not in existing]
    merged["admins_id"] = existing + added
    return merged, added


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
async def apply(
    cfg: AppConfig,
    *,
    home: Optional[Path] = None,
    require_provider: bool = True,
    make_backup: bool = True,
) -> Dict[str, Any]:
    """把控制台配置写入 AstrBot 的 ``data/cmd_config.json``。

    行为
    ----
    * 读现有配置 → 深合并我们关心的键（``dashboard.port``、``platform`` 里的 aiocqhttp、
      ``provider`` 里的 OpenAI 兼容条目、``admins_id``）→ 备份 → 原子写；
    * **保留**用户其它平台/提供商/全部其它配置；
    * 文件不存在时用最小结构生成，并在结果里说明"AstrBot 启动后会补齐默认项"。

    Args:
        cfg: 控制台配置。
        home: 显式 AstrBot 根目录（测试用）。
        require_provider: 缺少 API Key 时是否报错（False 则跳过 provider 写入）。
        make_backup: 写入前是否备份。

    Returns:
        ``{"ok", "path", "backup", "changes": {...}, "warnings": [...], "restart_required"}``

    Raises:
        AstrBotConfigError: 目录不存在，或 ``require_provider=True`` 而缺少 API Key。
    """

    base = home_dir(cfg, home)
    if not base.is_dir():
        raise AstrBotConfigError(f"AstrBot 目录不存在：{base}（请先部署）")

    path = config_path(cfg, home)
    existing = read_config(path)
    created = not existing
    config = existing or default_config()
    warnings: List[str] = []

    if not isinstance(config.get("config_version"), int):
        config["config_version"] = CONFIG_VERSION
        if not created:
            warnings.append(f"配置缺少 config_version，已补为 {CONFIG_VERSION}（AstrBot 会自动迁移旧配置）")

    # 平台（连接）总是写
    platform_entry = build_platform_entry(cfg)
    config, platform_info = merge_platform(config, platform_entry)
    if platform_info["action"] == "type":
        warnings.append(
            "已就地更新既有的 aiocqhttp 平台条目（保留其 id），避免 AstrBot 出现两条同类型平台"
        )

    # 提供商（可选）
    provider_info: Dict[str, Any] = {"action": "skipped", "reason": ""}
    try:
        provider_entry = build_provider_entry(cfg)
    except AstrBotConfigError as exc:
        if require_provider:
            raise
        provider_info = {"action": "skipped", "reason": str(exc)}
        warnings.append(f"未写入模型提供商：{exc}")
    else:
        config, provider_info = merge_provider(config, provider_entry)

    # 面板端口（不动密码）
    config, dashboard_info = merge_dashboard(config, cfg)

    # 默认回复策略：私聊全回、群里必须 @ 或前缀
    config, reply_policy = merge_reply_policy(config)

    # 管理员 QQ
    admins = list(cfg.astrbot.admins_id or [])
    if not admins and str(cfg.snowluma.qq_uin or "").strip().isdigit():
        admins = [str(cfg.snowluma.qq_uin).strip()]
    config, admins_added = merge_admins(config, admins)
    if admins_added:
        warnings.append(f"已把 {', '.join(admins_added)} 加入 AstrBot 管理员（admins_id），否则 /help 等指令无效")

    backup_info = backup(cfg, home=home) if (make_backup and path.is_file()) else {"ok": False,
                                                                                  "detail": "首次生成，无需备份"}
    write_config(path, config)

    return {
        "ok": True,
        "path": str(path),
        "created": created,
        "backup": backup_info,
        "changes": {
            "platform": platform_info,
            "provider": provider_info,
            "dashboard": dashboard_info,
            "admins_added": admins_added,
            "reply_policy": reply_policy,
        },
        "warnings": warnings,
        "restart_required": True,
        "note": "AstrBot 只在启动时读取 cmd_config.json；WebUI 保存会覆盖，所以请在启动前写入",
    }


# --------------------------------------------------------------------------- #
# 口令提取
# --------------------------------------------------------------------------- #
def _recent_log_lines(service: str = "astrbot", limit: int = 600) -> List[str]:
    """取 AstrBot 最近日志（事件总线 → ``data/logs/astrbot.log`` → AstrBot 自己的日志文件）。"""

    lines: List[str] = []
    try:
        from .events import bus

        for name in (service, "astrbot", "console"):
            lines.extend(str(item.get("line", "")) for item in bus.logs(name, limit))
            if lines:
                break
    except Exception:  # noqa: BLE001 - 事件总线不可用时退化为读文件
        lines = []
    if lines:
        return lines
    for candidate in (
        paths.LOG_DIR / f"{service}.log",
        paths.DATA_DIR / "logs" / f"{service}.log",
    ):
        try:
            lines.extend(candidate.read_text("utf-8", "replace").splitlines()[-limit:])
        except OSError:
            continue
    return lines


def read_dashboard_password(
    cfg: AppConfig,
    *,
    home: Optional[Path] = None,
    log_lines: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """抓 AstrBot 首次启动时打印的 WebUI 初始口令。

    来源优先级：环境变量 ``ASTRBOT_DASHBOARD_INITIAL_PASSWORD`` → 子进程日志 → 日志文件。
    AstrBot 只在"首次生成/重置"时打印一次，所以抓不到是正常情况，此时返回 ``ok=False``
    并给出人话提示（让用户在 AstrBot 面板里自己改/看）。

    Returns:
        ``{"ok", "password", "username", "source", "change_required", "hint", "url"}``
    """

    lines = list(log_lines) if log_lines is not None else _recent_log_lines()
    username = str(cfg.astrbot.webui_username or "astrbot")
    password = ""
    source = ""

    env_password = str(os.environ.get("ASTRBOT_DASHBOARD_INITIAL_PASSWORD") or "").strip()
    if env_password:
        password, source = env_password, "env"

    if not password:
        for line in reversed(lines):
            match = next((pattern.search(line) for pattern in _PASSWORD_PATTERNS if pattern.search(line)), None)
            if match:
                password, source = match.group(1), "log"
                break
    for line in reversed(lines):
        match = next((pattern.search(line) for pattern in _USERNAME_PATTERNS if pattern.search(line)), None)
        if match:
            username = match.group(1)
            break

    config = read_config(config_path(cfg, home))
    dashboard = config.get("dashboard") if isinstance(config.get("dashboard"), dict) else {}
    change_required = bool(dashboard.get("password_change_required"))
    hint = ""
    if not password:
        hint = (
            "未在日志里找到初始口令（AstrBot 只打印一次）。请在 AstrBot 面板里登录后修改密码，"
            "或在启动参数里加 --reset-password 让它重新打印一次"
        )
    return {
        "ok": bool(password),
        "username": username,
        "password": password,
        "source": source,
        "change_required": change_required,
        "url": dashboard_url(cfg),
        "hint": hint,
    }


# --------------------------------------------------------------------------- #
# 控制面 API（/api/v1）
# --------------------------------------------------------------------------- #
def im_umo(cfg: AppConfig, target_type: str, target_id: str) -> str:
    """构造 AstrBot 主动发消息用的 UMO：``<platform_id>:<FriendMessage|GroupMessage>:<id>``。"""

    platform_id = DEFAULT_PLATFORM_ID
    try:
        summary = config_summary(cfg)
        platform_id = summary.get("platform_id") or DEFAULT_PLATFORM_ID
    except Exception:  # noqa: BLE001 - 没有配置文件时用默认 id
        platform_id = DEFAULT_PLATFORM_ID
    kind = "GroupMessage" if str(target_type).strip().lower() == "group" else "FriendMessage"
    return f"{platform_id}:{kind}:{str(target_id).strip()}"


#: JWT 缓存（AstrBot 的 JWT 有效期 7 天；这里保守缓存 6 小时，且登录有限流不能反复试）
_JWT_TTL_SECONDS = 6 * 3600
_JWT_CACHE: Dict[str, Dict[str, Any]] = {}


def _jwt_cache_key(base_url: str, username: str, password: str) -> str:
    import hashlib

    digest = hashlib.sha256(str(password).encode("utf-8")).hexdigest()[:16]
    return f"{base_url}|{username}|{digest}"


class AstrBotApi:
    """AstrBot 控制面 API 的极简异步客户端（只用于发消息/探活/读配置）。

    鉴权：``POST /api/v1/auth/login`` 拿 JWT（7 天），之后 ``Authorization: Bearer``。
    注意登录有限流（1 次/秒、突发 3），所以 JWT 会按 ``base_url + 账号 + 密码指纹``
    缓存在进程内（默认 6 小时），避免状态轮询反复登录触发限流。
    """

    def __init__(
        self,
        base_url: str,
        *,
        username: str = "astrbot",
        password: str = "",
        token: str = "",
        timeout: float = 6.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.username = str(username or "astrbot")
        self.password = str(password or "")
        self.token = str(token or "")
        self._timeout = float(timeout)
        self._client = client
        self._owned = client is None

    async def __aenter__(self) -> "AstrBotApi":
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

    def has_session(self) -> bool:
        return bool(self.token)

    async def login(self) -> str:
        """用面板账号密码换 JWT（带进程内缓存）；失败抛 :class:`AstrBotConfigError`。"""

        if self.token:
            return self.token
        key = _jwt_cache_key(self.base_url, self.username, self.password)
        cached = _JWT_CACHE.get(key)
        if cached and cached.get("expires", 0) > time.time():
            self.token = str(cached["token"])
            return self.token
        if not self.password:
            raise AstrBotConfigError("未配置 AstrBot 面板密码，无法调用控制面 API")
        try:
            response = await self._http().post(
                f"{self.base_url}/api/v1/auth/login",
                json={"username": self.username, "password": self.password},
            )
        except httpx.HTTPError as exc:
            raise AstrBotConfigError(f"无法连接 AstrBot 面板（{self.base_url}）：{exc}") from exc
        payload: Any
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        token = ""
        if isinstance(payload, dict):
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            token = str(data.get("token") or payload.get("token") or "")
        if response.status_code == 429:
            raise AstrBotConfigError("AstrBot 登录被限流，请稍等 1 秒再试")
        if not token:
            message = payload.get("message") if isinstance(payload, dict) else ""
            raise AstrBotConfigError(
                f"AstrBot 登录失败（HTTP {response.status_code}）：{message or '请检查面板用户名/密码'}"
            )
        self.token = token
        _JWT_CACHE[key] = {"token": token, "expires": time.time() + _JWT_TTL_SECONDS}
        return token

    @staticmethod
    def invalidate_token_cache() -> None:
        """清空 JWT 缓存（改密码/切换实例后调用）。"""

        _JWT_CACHE.clear()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = await self._http().request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise AstrBotConfigError(f"无法连接 AstrBot 面板（{self.base_url}）：{exc}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text[:400]}
        if response.status_code >= 400:
            message = payload.get("message") if isinstance(payload, dict) else ""
            raise AstrBotConfigError(
                f"AstrBot 接口 {path} 失败（HTTP {response.status_code}）：{message or response.text[:200]}"
            )
        return payload

    # -------------------------------------------------------------- 探活
    async def start_time(self) -> Dict[str, Any]:
        """启动时间（旧路由，**免鉴权**，适合轮询探活）。"""

        payload = await self.request("GET", "/api/stat/start-time")
        return payload if isinstance(payload, dict) else {}

    async def version(self) -> Dict[str, Any]:
        payload = await self.request("GET", "/api/v1/stats/version")
        return payload if isinstance(payload, dict) else {}

    async def bots_stats(self) -> Dict[str, Any]:
        await self.login()
        payload = await self.request("GET", "/api/v1/bots/stats")
        return payload if isinstance(payload, dict) else {}

    async def im_bots(self) -> List[Dict[str, Any]]:
        """可用于主动发消息的平台列表。"""

        await self.login()
        payload = await self.request("GET", "/api/v1/im/bots")
        data = payload.get("data") if isinstance(payload, dict) else payload
        items = data if isinstance(data, list) else (payload.get("bots") if isinstance(payload, dict) else None)
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def system_config(self) -> Dict[str, Any]:
        await self.login()
        payload = await self.request("GET", "/api/v1/system-config")
        config = payload.get("config") if isinstance(payload, dict) else None
        return config if isinstance(config, dict) else (payload if isinstance(payload, dict) else {})

    # -------------------------------------------------------------- 发消息
    async def send_message(self, umo: str, text: str) -> Dict[str, Any]:
        """经已连接的平台主动发一条纯文本消息（不需要 OneBot 客户端）。"""

        await self.login()
        payload = await self.request(
            "POST",
            "/api/v1/im/messages",
            json={"umo": umo, "message": [{"type": "plain", "text": str(text)}]},
        )
        return payload if isinstance(payload, dict) else {}


# --------------------------------------------------------------------------- #
# 探活
# --------------------------------------------------------------------------- #
def _process_state(name: str = "astrbot") -> Dict[str, Any]:
    try:
        from .supervisor import supervisor

        return supervisor.state(name).as_dict()
    except Exception:  # noqa: BLE001
        return {}


def _scan_logs(lines: Sequence[str]) -> Dict[str, Any]:
    info = {
        "webui_url": "",
        "platform_loaded": False,
        "platform_connected": False,
        "port_in_use": "",
        "last_error": "",
    }
    for line in lines:
        match = LOG_READY_WEBUI.search(line)
        if match:
            info["webui_url"] = match.group(1)
        if LOG_PLATFORM_LOADED.search(line):
            info["platform_loaded"] = True
        if LOG_PLATFORM_CONNECTED.search(line):
            info["platform_connected"] = True
        match = LOG_PORT_IN_USE.search(line)
        if match:
            info["port_in_use"] = line.strip()[:200]
        if LOG_ERROR.search(line):
            info["last_error"] = line.strip()[:300]
    return info


def _provider_has_key(entry: Dict[str, Any]) -> bool:
    """provider 条目是否配置了可用的 API Key（``key`` 可能是字符串或列表）。"""

    keys = entry.get("key")
    if isinstance(keys, str):
        return bool(keys.strip())
    if isinstance(keys, list):
        return any(str(item).strip() for item in keys)
    return False


def merge_provider_sources(
    providers: Sequence[Any], sources: Sequence[Any]
) -> List[Dict[str, Any]]:
    """把「新结构」（``provider`` + ``provider_source_id`` → ``provider_sources``）摊平。

    AstrBot 4.29.x 启动时会把我们写的老结构迁移成::

        provider:        [{id, enable, model, provider_source_id}]
        provider_sources:[{id, provider, type, provider_type, key, api_base, ...}]

    因此**回读/判定必须兼容两种结构**，否则会永远误报"没有模型提供商"。
    """

    source_map: Dict[str, Dict[str, Any]] = {}
    for item in sources or []:
        if isinstance(item, dict) and item.get("id"):
            source_map[str(item["id"])] = item
    merged: List[Dict[str, Any]] = []
    for item in providers or []:
        if not isinstance(item, dict):
            continue
        source = source_map.get(str(item.get("provider_source_id") or ""), {})
        entry = dict(source)
        entry.update(item)
        if source:
            entry["_merged_from_source"] = str(source.get("id") or "")
        merged.append(entry)
    return merged


def default_provider_id(config: Dict[str, Any]) -> str:
    """读取 ``agent_runner.config.model.provider_id``（空表示"取第一个可用的"）。"""

    agent_runner = config.get("agent_runner")
    if not isinstance(agent_runner, dict):
        return ""
    runner_cfg = agent_runner.get("config")
    if not isinstance(runner_cfg, dict):
        return ""
    model_cfg = runner_cfg.get("model")
    if not isinstance(model_cfg, dict):
        return ""
    return str(model_cfg.get("provider_id") or "")


def config_summary(cfg: AppConfig, *, home: Optional[Path] = None) -> Dict[str, Any]:
    """同步解析 cmd_config.json，给出平台/提供商/面板的关键结论（不联网）。

    提供商兼容**老结构**（key/api_base/model 内联在 ``provider`` 里）与
    **新结构**（``provider.provider_source_id`` 指向 ``provider_sources[].id``）。
    """

    path = config_path(cfg, home)
    config = read_config(path)
    platforms = _as_list(config.get("platform"))
    providers_raw = _as_list(config.get("provider"))
    sources_raw = _as_list(config.get("provider_sources"))
    providers = merge_provider_sources(providers_raw, sources_raw)
    platform_entry = next(
        (item for item in platforms if isinstance(item, dict)
         and str(item.get("type") or "").lower() == PLATFORM_TYPE),
        None,
    )
    provider_entry = next(
        (item for item in providers if str(item.get("id") or "") == DEFAULT_PROVIDER_ID),
        None,
    ) or next((item for item in providers if item), None)
    dashboard = config.get("dashboard") if isinstance(config.get("dashboard"), dict) else {}
    admins = config.get("admins_id") if isinstance(config.get("admins_id"), list) else []
    default_id = default_provider_id(config)

    platform_info = dict(platform_entry) if platform_entry else {}
    if "ws_reverse_token" in platform_info:
        platform_info["ws_reverse_token"] = MASK if platform_info["ws_reverse_token"] else ""
    provider_info = copy.deepcopy(provider_entry) if provider_entry else {}
    if isinstance(provider_info.get("key"), list):
        provider_info["key"] = [MASK for _ in provider_info["key"]]
    elif isinstance(provider_info.get("key"), str):
        provider_info["key"] = MASK if provider_info["key"] else ""

    enabled_providers = [
        item for item in providers if bool(item.get("enable", True)) and _provider_has_key(item)
    ]
    provider_ok = bool(provider_entry and _provider_has_key(provider_entry)) or bool(enabled_providers)

    return {
        "path": str(path),
        "exists": path.is_file(),
        "config_version": config.get("config_version"),
        "platform_count": len(platforms),
        "platform": platform_info,
        "platform_id": str((platform_entry or {}).get("id") or ""),
        "platform_port": int((platform_entry or {}).get("ws_reverse_port") or 0),
        "platform_token": str((platform_entry or {}).get("ws_reverse_token") or ""),
        "platform_enable": bool((platform_entry or {}).get("enable", False)),
        "provider_count": len(providers_raw),
        "provider_source_count": len(sources_raw),
        "provider": provider_info,
        "provider_id": str((provider_entry or {}).get("id") or ""),
        "provider_model": str((provider_entry or {}).get("model") or ""),
        "provider_api_base": str((provider_entry or {}).get("api_base") or ""),
        "provider_source_id": str((provider_entry or {}).get("provider_source_id") or ""),
        "provider_ok": provider_ok,
        "provider_enabled_count": len(enabled_providers),
        "default_provider_id": default_id,
        "effective_provider_id": default_id or (str((enabled_providers[0].get("id") or ""))
                                                if enabled_providers else ""),
        "dashboard_port": int(dashboard.get("port") or 0) if dashboard else 0,
        "dashboard_username": str(dashboard.get("username") or ""),
        "admins_id": [str(item) for item in admins],
        "change_required": bool(dashboard.get("password_change_required")) if dashboard else False,
    }


async def probe(
    cfg: AppConfig,
    *,
    home: Optional[Path] = None,
    timeout: float = 1.5,
    log_lines: Optional[Sequence[str]] = None,
    ws_probe: Optional[Any] = None,
) -> Dict[str, Any]:
    """探测 AstrBot：部署/进程/端口/配置一致性/日志就绪信号/反向 WS 握手。

    返回结构（供 ``/api/link/status`` 使用）::

        {"ok", "state", "hint", "deployed", "home", "config": {...},
         "process": {...}, "port": {...}, "onebot_ws": {...}, "log": {...},
         "token_match", "port_match", "suggestions"}

    Args:
        ws_probe: 可注入的握手探针（默认 :func:`probe_onebot_ws`），单测用它避免真实联网。
    """

    base = home_dir(cfg, home)
    deployed = base.is_dir()
    summary = config_summary(cfg, home=home)
    lines = list(log_lines) if log_lines is not None else _recent_log_lines()
    log_info = _scan_logs(lines)
    process = _process_state()

    from .supervisor import port_open

    port = int(cfg.astrbot.webui_port)
    reachable = await asyncio.to_thread(port_open, port, "127.0.0.1", min(0.6, timeout))
    link_port = int(cfg.link.ws_port)
    link_reachable = await asyncio.to_thread(port_open, link_port, "127.0.0.1", min(0.6, timeout))
    # aiocqhttp 反向 WS 的"真·可用"判据：带 token 握一次手
    probe_fn = ws_probe or probe_onebot_ws
    onebot_ws = await probe_fn(cfg, timeout=max(1.0, timeout))

    token_match = bool(summary["platform_token"]) and summary["platform_token"] == str(cfg.link.token or "")
    port_match = summary["platform_port"] == link_port
    provider_ok = bool(summary.get("provider_ok"))
    dashboard_ok = summary["dashboard_port"] == port

    suggestions: List[str] = []
    if not deployed:
        state, hint = "not_deployed", "尚未部署 AstrBot，请先在「部署」页克隆仓库并安装依赖"
        suggestions.append("运行一键部署（git clone + venv + pip install）")
    elif not summary["exists"]:
        state, hint = "config_missing", "AstrBot 还没有配置文件，执行「写入配置」会自动生成"
        suggestions.append("点击「写入配置」")
    elif not summary["platform_id"]:
        state, hint = "platform_missing", "AstrBot 配置里没有 aiocqhttp(OneBot v11) 平台，SnowLuma 无处可连"
        suggestions.append("点击「写入配置」生成 aiocqhttp 平台配置")
    elif not (token_match and port_match):
        state, hint = "config_mismatch", "AstrBot 的反向 WS 端口或 token 与控制台配置不一致，连接会被拒绝"
        suggestions.append("点击「写入配置」重新同步 token 与端口（写入后需重启 AstrBot）")
    elif process.get("status") in ("stopped", "failed") and not reachable:
        state, hint = "not_running", "AstrBot 未运行，启动后才会监听反向 WS 端口"
        suggestions.append("点击「启动 AstrBot」")
    elif not reachable:
        state, hint = "starting", "AstrBot 正在启动，WebUI 端口还没就绪"
    elif onebot_ws["status_code"] in (401, 403):
        state, hint = "token_rejected", f"反向 WS 端口在线，但握手被拒（{onebot_ws['detail']}）"
        suggestions.append("AstrBot 里的 ws_reverse_token 与控制台不一致，重新「写入配置」并重启 AstrBot")
    elif not onebot_ws["ok"] and not onebot_ws["status_code"]:
        state, hint = "onebot_unavailable", "AstrBot 面板在线，但反向 WS 端口没在监听（平台适配器可能加载失败）"
        suggestions.append("查看 AstrBot 日志里 aiocqhttp 平台是否加载成功（端口是否被占用）")
    elif not provider_ok:
        state, hint = "missing_provider", "AstrBot 已就绪，但没有可用的模型提供商，消息不会被回复"
        suggestions.append("在配置页填写大模型 API Key 后重新「写入配置」")
    else:
        state = "ready"
        hint = "AstrBot 已就绪，OneBot 反向 WS 已在线" + (
            "，等待 SnowLuma 连接" if not log_info["platform_connected"] else ""
        )
        suggestions.append("确认 SnowLuma 已登录 QQ 并已写入 astrbot 反向 WS 连接")

    if log_info["port_in_use"]:
        suggestions.append(f"端口被占用：{log_info['port_in_use']}")
    if not dashboard_ok and summary["exists"]:
        suggestions.append("AstrBot 面板端口与控制台设置不一致，执行「写入配置」后重启")

    return {
        "ok": True,
        "state": state,
        "hint": hint,
        "deployed": deployed,
        "home": str(base),
        "config": summary,
        "process": process,
        "port": {
            "webui": port,
            "webui_open": reachable,
            "onebot": link_port,
            "onebot_open": link_reachable,
        },
        "onebot_ws": onebot_ws,
        "log": log_info,
        "token_match": token_match,
        "port_match": port_match,
        "provider_ok": provider_ok,
        "dashboard_ok": dashboard_ok,
        "suggestions": suggestions,
    }


async def probe_onebot_ws(cfg: AppConfig, *, timeout: float = 3.0) -> Dict[str, Any]:
    """带 token 对 AstrBot 的反向 WS 做一次握手探针。

    这是「OneBot 服务端是否真的可用」最可靠的判据（不依赖 SnowLuma、不依赖 QQ）：
    ``101`` 可用、``401/403`` token 不对、连不上表示适配器没起来。
    """

    url = f"ws://127.0.0.1:{int(cfg.link.ws_port)}/ws"
    headers = {
        "User-Agent": "OneBot/11",
        "X-Self-ID": str(cfg.snowluma.qq_uin or "10000"),
        "X-Client-Role": "universal",
    }
    if cfg.link.token:
        headers["Authorization"] = f"Bearer {cfg.link.token}"
    result = await probe_ws_handshake(url, headers, timeout=timeout)
    if result["status_code"] == 101:
        result["detail"] = "AstrBot 反向 WS 在线且 token 正确"
    return result

def probe_provider(base_url: str, model: str, api_key: str, *, timeout: float = 20.0) -> Dict[str, Any]:
    """用真实请求验一次大模型配置：直接告诉用户是 key 错、地址错还是网络问题。

    走 ``POST {base}/chat/completions``（OpenAI 兼容），最多 1 个 token。
    """

    import httpx

    base = str(base_url or "").rstrip("/")
    if not base:
        return {"ok": False, "kind": "no-base", "message": "接口地址为空"}
    url = base + "/chat/completions"
    payload = {
        "model": model or "deepseek-chat",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
    except httpx.ConnectError as exc:
        return {"ok": False, "kind": "network", "status": 0,
                "message": f"连不上 {base}：{exc}。检查接口地址/代理，或先确认本机能否访问该域名。"}
    except httpx.TimeoutException:
        return {"ok": False, "kind": "timeout", "status": 0,
                "message": f"请求 {base} 超时（>{int(timeout)}s）：网络慢或被墙，换镜像/代理再试。"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "kind": "error", "status": 0, "message": f"请求失败：{exc}"}

    if resp.status_code in (401, 403):
        return {"ok": False, "kind": "auth", "status": resp.status_code,
                "message": "API Key 无效或没有权限（401/403）：去服务商后台重新复制一次 Key，注意别带空格。"}
    if resp.status_code == 404:
        return {"ok": False, "kind": "not-found", "status": 404,
                "message": f"接口路径不存在（404）：确认 Base URL 是 {base}/v1 这样的形式，模型名 {model} 是否存在。"}
    if resp.status_code == 402 or resp.status_code == 429:
        return {"ok": False, "kind": "quota", "status": resp.status_code,
                "message": "额度不足或请求太频繁（402/429）：去后台充值/稍后再试。"}
    if resp.status_code >= 400:
        return {"ok": False, "kind": "http", "status": resp.status_code,
                "message": f"接口返回 {resp.status_code}：{resp.text[:200]}"}
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {"ok": True, "kind": "ok", "status": resp.status_code,
                "message": "接口可达（返回不是 JSON，但鉴权已通过）"}
    usage = data.get("usage") or {}
    return {
        "ok": True,
        "kind": "ok",
        "status": resp.status_code,
        "message": f"连接正常：{model} 已响应（completion_tokens={usage.get('completion_tokens', '?')}）",
    }

#: 接口不给 /models 时的兜底建议（按 Base URL 猜服务商）
_FALLBACK_MODELS = {
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "openai": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "o3-mini"],
    "moonshot": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
    "dashscope": ["qwen-plus", "qwen-max", "qwen-turbo"],
    "siliconflow": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-7B-Instruct"],
    "zhipu": ["glm-4-plus", "glm-4-air"],
}


def fallback_models(base_url: str) -> List[str]:
    low = str(base_url or "").lower()
    for key, models in _FALLBACK_MODELS.items():
        if key in low:
            return list(models)
    return []


def list_provider_models(base_url: str, api_key: str, *, timeout: float = 20.0) -> Dict[str, Any]:
    """拉可用模型列表（OpenAI 兼容 ``GET {base}/models``）。

    拿到就用接口的；接口不支持（404/无 JSON）就退回内置建议，并说明来源。
    """

    import httpx

    base = str(base_url or "").rstrip("/")
    # 先看 Key：没 Key 时"填 Key"比"填地址"更可执行
    if not str(api_key or "").strip():
        return {"ok": False, "kind": "no-key", "models": [], "source": "",
                "message": "还没填 API Key，无法读取模型列表"}
    if not base:
        return {"ok": False, "kind": "no-base", "models": [], "source": "",
                "message": "接口地址为空，先填 Base URL"}

    url = base + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers)
    except httpx.ConnectError as exc:
        return {"ok": False, "kind": "network", "models": fallback_models(base), "source": "fallback",
                "message": f"连不上 {base}：{exc}"}
    except httpx.TimeoutException:
        return {"ok": False, "kind": "timeout", "models": fallback_models(base), "source": "fallback",
                "message": f"请求 {base} 超时（>{int(timeout)}s）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "kind": "error", "models": [], "source": "",
                "message": f"请求失败：{exc}"}

    if resp.status_code in (401, 403):
        return {"ok": False, "kind": "auth", "models": [], "source": "",
                "message": "API Key 无效（401/403），先修 Key 再列模型"}
    if resp.status_code >= 400:
        fallback = fallback_models(base)
        return {
            "ok": bool(fallback),
            "kind": "no-endpoint",
            "models": fallback,
            "source": "fallback" if fallback else "",
            "message": (f"这个接口不支持列出模型（HTTP {resp.status_code}），"
                        + ("下面按服务商给了常用建议，可直接选。" if fallback else "请手动填写模型名。")),
        }

    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        fallback = fallback_models(base)
        return {"ok": bool(fallback), "kind": "no-json", "models": fallback,
                "source": "fallback" if fallback else "",
                "message": "接口返回的不是模型列表，改用内置建议" if fallback else "接口返回不是 JSON"}

    items = data.get("data") if isinstance(data, dict) else data
    models: List[str] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                mid = str(item.get("id") or item.get("name") or "").strip()
            else:
                mid = str(item or "").strip()
            if mid and mid not in models:
                models.append(mid)
    models.sort()
    if not models:
        fallback = fallback_models(base)
        return {"ok": bool(fallback), "kind": "empty", "models": fallback,
                "source": "fallback" if fallback else "",
                "message": "接口没返回任何模型，改用内置建议" if fallback else "接口没返回任何模型"}
    return {"ok": True, "kind": "ok", "models": models, "source": "api",
            "message": f"读取到 {len(models)} 个可用模型"}

def _key_text(value: Any) -> str:
    """AstrBot 的 key 可能是字符串，也可能是 ["sk-…"] 列表；统一成字符串。"""

    if isinstance(value, (list, tuple)):
        for item in value:
            text = str(item or "").strip()
            if text:
                return text
        return ""
    return str(value or "").strip()


def inspect_provider(cfg: AppConfig, home: Optional[Path] = None) -> Dict[str, Any]:
    """读出 AstrBot **当前生效**的默认 provider（provider + provider_sources 已摊平）。

    返回 ``{"provider": {id, model, api_base, key, provider_type}, "path": ...}``；
    没有配置就返回空 provider。
    """

    path = config_path(cfg, home)
    config = read_config(path) or {}
    providers = merge_provider_sources(_as_list(config.get("provider")),
                                       _as_list(config.get("provider_sources")))
    pid = default_provider_id(config)
    entry: Dict[str, Any] = {}
    for item in providers:
        if isinstance(item, dict) and item.get("id") == pid:
            entry = item
            break
    if not entry and providers:
        entry = providers[0] if isinstance(providers[0], dict) else {}
    return {
        "path": str(path),
        "provider": {
            "id": str(entry.get("id") or ""),
            "model": str(entry.get("model") or ""),
            "api_base": str(entry.get("api_base") or entry.get("base_url") or ""),
            # AstrBot 的 key 可能是列表形式（["sk-…"]），必须拆出来，否则会带着方括号去请求 → 401
            "key": _key_text(entry.get("key")),
            "provider_type": str(entry.get("type") or entry.get("provider_type") or ""),
            "provider": str(entry.get("provider") or ""),
        },
    }

#: 与 AstrBot 4.x 完全一致的散列参数（见 astrbot/core/utils/auth_password.py）
_PBKDF2_ITERATIONS = 600_000
_PBKDF2_SALT_BYTES = 16


def hash_dashboard_password(raw: str) -> str:
    """生成 ``pbkdf2_sha256$600000$salt$digest``（AstrBot 的存储格式）。"""

    import hashlib
    import secrets

    salt = secrets.token_hex(_PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"),
                                 bytes.fromhex(salt), _PBKDF2_ITERATIONS).hex()
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest}"


def set_dashboard_password(cfg: AppConfig, raw_password: str, *, home: Optional[Path] = None) -> Dict[str, Any]:
    """把 AstrBot 面板密码改成指定值（直接写散列，不需要重启它的 CLI 重置流程）。

    要求 AstrBot 已停止：它退出时会把内存里的 config 写回，先改文件会被覆盖。
    """

    import hashlib

    password = str(raw_password or "").strip()
    if len(password) < 8:
        raise AstrBotConfigError("AstrBot 面板密码至少 8 位（它自己的复杂度策略）")

    path = config_path(cfg, home)
    config = read_config(path)
    if not config:
        raise AstrBotConfigError(f"读不到 AstrBot 配置：{path}")
    dashboard = config.setdefault("dashboard", {})
    if not isinstance(dashboard, dict):
        dashboard = config["dashboard"] = {}
    dashboard["pbkdf2_password"] = hash_dashboard_password(password)
    dashboard["password"] = hashlib.md5(password.encode("utf-8")).hexdigest()
    dashboard["password_storage_upgraded"] = True
    dashboard["password_change_required"] = False
    username = str(config.get("dashboard", {}).get("username") or cfg.astrbot.webui_username or "astrbot")
    write_config(path, config)
    return {"ok": True, "username": username, "password": password, "path": str(path)}

# --------------------------------------------------------------------------- #
# AstrBot 双向自检：用它的 OpenAPI（/api/v1/im/messages）真的发一条消息
# --------------------------------------------------------------------------- #
def _dashboard_token(base_url: str, username: str, password: str, *, timeout: float = 15.0) -> str:
    """用面板账号密码换 JWT（AstrBot WebUI 的 /api/auth/login）。"""

    import httpx

    url = f"http://{base_url}/api/auth/login" if not base_url.startswith("http") else f"{base_url}/api/auth/login"
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json={"username": username, "password": password})
    if resp.status_code >= 400:
        raise AstrBotConfigError(f"登录 AstrBot 面板失败（HTTP {resp.status_code}）：检查面板用户名/密码")
    data = resp.json()
    token = ((data.get("data") or {}).get("token")) or ""
    if not token:
        raise AstrBotConfigError("登录成功但没拿到 token")
    return token


def ensure_api_key(base_url: str, jwt: str, *, name: str = "slab-console", scopes=None,
                   timeout: float = 15.0) -> str:
    """拿到一把带 im 权限的 API Key（有就复用，没有就建）。"""

    import httpx

    scopes = list(scopes or ["im"])
    root = base_url if base_url.startswith("http") else f"http://{base_url}"
    headers = {"Authorization": f"Bearer {jwt}"}
    with httpx.Client(timeout=timeout) as client:
        listing = client.get(f"{root}/api/v1/api-keys", headers=headers)
        if listing.status_code >= 400:
            raise AstrBotConfigError(f"读取 API Key 列表失败（HTTP {listing.status_code}）")
        for item in (listing.json().get("data") or []):
            if item.get("name") == name and not item.get("revoked_at"):
                key = item.get("api_key") or ""
                if key:
                    return key
        created = client.post(f"{root}/api/v1/api-keys", headers=headers,
                              json={"name": name, "scopes": scopes})
        if created.status_code >= 400:
            raise AstrBotConfigError(f"创建 API Key 失败（HTTP {created.status_code}）")
        key = ((created.json().get("data") or {}).get("api_key")) or ""
        if not key:
            raise AstrBotConfigError("创建 API Key 成功但没返回明文 key")
        return key


def send_im_message(base_url: str, api_key: str, umo: str, text: str, *, timeout: float = 25.0) -> Dict[str, Any]:
    """通过 AstrBot 的 OpenAPI 发一条消息（这一步会真的走到 SnowLuma → QQ）。"""

    import httpx

    root = base_url if base_url.startswith("http") else f"http://{base_url}"
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(f"{root}/api/v1/im/messages", headers=headers,
                           json={"umo": umo, "message": text})
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"raw": resp.text[:200]}
    if resp.status_code >= 400 or (isinstance(body, dict) and body.get("status") == "error"):
        msg = body.get("message") if isinstance(body, dict) else str(body)
        return {"ok": False, "status": resp.status_code, "message": str(msg or "发送失败")}
    return {"ok": True, "status": resp.status_code, "message": "已通过 AstrBot 发出"}
