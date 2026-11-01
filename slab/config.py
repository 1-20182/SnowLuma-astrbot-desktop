"""控制台配置：读写 data/config.json，并对 Web 层做脱敏输出。

设计原则
--------
* 单一 JSON 文件，便于用户备份/迁移；
* 写入使用「临时文件 + 原子替换」，避免半截文件；
* ``apply_patch`` 支持前端按字段局部更新（嵌套字典深合并）；
* ``masked()`` 把密码/token/api_key 替换成 ``***`` 后再返回给浏览器。
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List

from . import paths

SECRET_HINTS = ("password", "token", "api_key", "apikey", "secret", "access_token")
CONFIG_VERSION = 1


def new_token(n: int = 24) -> str:
    return secrets.token_urlsafe(n)


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #
@dataclass
class AppSettings:
    host: str = "127.0.0.1"
    port: int = 8790
    open_browser: bool = True
    theme: str = "auto"
    # 维护：日志/安装包保留天数（0 = 不清理）
    log_retain_days: int = 7
    download_retain_days: int = 7
    # 下载源：auto（测速挑最快）/ official / ghfast / ghproxy_net / gh_proxy / moeyy / llkk / ghproxy_cc
    download_mirror: str = "auto"


@dataclass
class SnowLumaSettings:
    edition: str = "full"  # full(内置 node.exe，开箱即用) | lite(需系统 Node ≥ 24.22.1)
    version: str = "latest"  # latest 或具体 tag（如 v2.2.1）
    install_dir: str = ""
    webui_port: int = 5099
    webui_password: str = ""
    qq_uin: str = ""
    onebot_mode: str = "ws_client"  # ws_client(反向连接 AstrBot) | ws_server(等 AstrBot 连入)
    onebot_url: str = "ws://127.0.0.1:6199/ws"
    onebot_token: str = ""
    relay_port: int = 3001  # 中继模式下 SnowLuma 的 OneBot WS 服务端端口
    # manual：不自动注入，由控制台只注入你选中的 QQ 进程（默认，避免接管自己正在用的 QQ）
    # auto：SnowLuma 自动注入所有检测到的 QQ
    hook_mode: str = "manual"
    autostart: bool = True


@dataclass
class AstrBotSettings:
    version: str = "main"
    install_dir: str = ""
    webui_port: int = 6185
    webui_username: str = "astrbot"
    webui_password: str = ""
    python: str = ""  # 留空=自动探测（uv/venv）
    autostart: bool = True
    # 管理员 QQ 号（写入 AstrBot 的 admins_id，否则 /help 等内置指令无效）
    admins_id: List[str] = field(default_factory=list)
    # 平台适配器：aiocqhttp（OneBot v11 反向 WS 服务端）
    platform: Dict[str, Any] = field(
        default_factory=lambda: {
            "type": "aiocqhttp",
            "enable": True,
            "ws_reverse_host": "0.0.0.0",
            "ws_reverse_port": 6199,
            "ws_reverse_token": "",
        }
    )
    # 对话模型提供商
    provider: Dict[str, Any] = field(
        default_factory=lambda: {
            "type": "openai_chat_completion",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "",
            "model": "deepseek-chat",
        }
    )
    # 对话行为
    behavior: Dict[str, Any] = field(
        default_factory=lambda: {
            "persona": "你是一个友好、简洁的中文聊天助手。",
            "max_context_tokens": 4000,
            "reply_timeout": 90,
        }
    )


@dataclass
class LinkSettings:
    mode: str = "native"  # native(SnowLuma→AstrBot 直连) | bridge(内置桥接进程)
    ws_host: str = "127.0.0.1"
    ws_port: int = 6199
    token: str = ""
    allow_private: bool = True
    allow_group: bool = True
    trigger_mode: str = "at_or_prefix"  # all | at_or_prefix
    trigger_prefixes: List[str] = field(default_factory=lambda: ["/"])
    ignore_self: bool = True
    forward_non_text: bool = True


@dataclass
class AppConfig:
    version: int = CONFIG_VERSION
    app: AppSettings = field(default_factory=AppSettings)
    snowluma: SnowLumaSettings = field(default_factory=SnowLumaSettings)
    astrbot: AstrBotSettings = field(default_factory=AstrBotSettings)
    link: LinkSettings = field(default_factory=LinkSettings)

    # ---------------------------------------------------------------- 序列化
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "AppConfig":
        cfg = cls()
        cfg.version = int(raw.get("version", CONFIG_VERSION) or CONFIG_VERSION)
        for name in ("app", "snowluma", "astrbot", "link"):
            section = raw.get(name) or {}
            if not isinstance(section, dict):
                continue
            target = getattr(cfg, name)
            for f in fields(target):
                if f.name in section and section[f.name] is not None:
                    cur = getattr(target, f.name)
                    val = section[f.name]
                    if isinstance(cur, dict) and isinstance(val, dict):
                        merged = copy.deepcopy(cur)
                        merged.update(val)
                        setattr(target, f.name, merged)
                    else:
                        setattr(target, f.name, val)
        cfg.normalise()
        return cfg

    # ---------------------------------------------------------------- 业务
    def normalise(self) -> None:
        """补齐联动字段（端口一致、token 非空），并兜底纠正非法枚举值。"""

        if not self.link.token:
            self.link.token = new_token()
        if self.snowluma.onebot_mode not in ("ws_client", "ws_server"):
            self.snowluma.onebot_mode = "ws_client"
        if self.snowluma.hook_mode not in ("manual", "auto"):
            self.snowluma.hook_mode = "manual"
        if self.link.mode not in ("native", "bridge"):
            self.link.mode = "native"
        if self.snowluma.edition not in ("lite", "full"):
            self.snowluma.edition = "lite"
        if self.link.trigger_mode not in ("all", "at_or_prefix"):
            self.link.trigger_mode = "at_or_prefix"
        # 反向连接：SnowLuma 作为 WS 客户端连到 AstrBot 监听的端口
        self.astrbot.platform.setdefault("type", "aiocqhttp")
        self.astrbot.platform["ws_reverse_port"] = int(self.link.ws_port)
        self.astrbot.platform["ws_reverse_token"] = self.link.token
        self.astrbot.platform.setdefault("ws_reverse_host", "0.0.0.0")
        if self.snowluma.onebot_mode == "ws_client":
            self.snowluma.onebot_url = f"ws://{self.link.ws_host}:{self.link.ws_port}/ws"
        self.snowluma.onebot_token = self.link.token
        if not self.astrbot.webui_password:
            self.astrbot.webui_password = new_token(12)
        if not self.snowluma.webui_password:
            self.snowluma.webui_password = new_token(12)
        # 填了 QQ 号就默认把它当作 AstrBot 管理员（否则 /help 等内置指令不可用）
        self.astrbot.admins_id = [str(x).strip() for x in (self.astrbot.admins_id or []) if str(x).strip()]
        if not self.astrbot.admins_id and str(self.snowluma.qq_uin).strip().isdigit():
            self.astrbot.admins_id = [str(self.snowluma.qq_uin).strip()]

    def validate(self) -> List[str]:
        issues: List[str] = []
        for label, port in (
            ("控制台端口", self.app.port),
            ("AstrBot WebUI 端口", self.astrbot.webui_port),
            ("SnowLuma WebUI 端口", self.snowluma.webui_port),
            ("OneBot 联动端口", self.link.ws_port),
        ):
            if not (1 <= int(port) <= 65535):
                issues.append(f"{label}必须在 1-65535 之间（当前 {port}）")
        ports = [self.app.port, self.astrbot.webui_port, self.snowluma.webui_port, self.link.ws_port]
        if self.link.mode == "bridge":
            ports.append(self.snowluma.relay_port)
        if len(set(ports)) != len(ports):
            dup = sorted({p for p in ports if ports.count(p) > 1})
            issues.append(f"端口重复：{dup}，四个端口必须互不相同")
        if self.astrbot.platform.get("type") != "aiocqhttp":
            issues.append("AstrBot 平台类型目前仅支持 aiocqhttp（OneBot v11）")
        if not str(self.astrbot.provider.get("api_key", "")).strip():
            issues.append("尚未填写大模型 API Key，AstrBot 将无法回复消息")
        return issues

    # ---------------------------------------------------------------- 脱敏
    def masked(self) -> Dict[str, Any]:
        data = self.to_dict()
        _mask_inplace(data)
        data["_secrets_configured"] = {
            "link_token": bool(self.link.token),
            "astrbot_api_key": bool(self.astrbot.provider.get("api_key")),
            "astrbot_webui_password": bool(self.astrbot.webui_password),
            "snowluma_webui_password": bool(self.snowluma.webui_password),
        }
        return data

    def clone(self) -> "AppConfig":
        return AppConfig.from_dict(self.to_dict())


def _mask_inplace(node: Any) -> None:
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, (dict, list)):
                _mask_inplace(v)
            elif isinstance(v, str) and v:
                key = k.lower()
                if key.endswith("_required") or key.startswith("_"):
                    continue
                if any(h in key for h in SECRET_HINTS):
                    node[k] = "***"
    elif isinstance(node, list):
        for item in node:
            _mask_inplace(item)


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #
class ConfigStore:
    """线程安全的配置读写。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else paths.CONFIG_PATH
        self._lock = threading.RLock()
        self._cfg: AppConfig | None = None

    def load(self) -> AppConfig:
        with self._lock:
            if self._cfg is not None:
                return self._cfg
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text("utf-8"))
                except Exception:
                    raw = {}
            else:
                raw = {}
            # 老版本配置先做迁移（加字段/改默认值），迁移前自动留 .bak
            try:
                from . import migrations

                if isinstance(raw, dict):
                    migrated, _from, steps = migrations.migrate(raw)
                    if steps:
                        try:
                            self.path.with_name(
                                f"config.v{_from}.bak.{time.strftime('%Y%m%d-%H%M%S')}.json"
                            ).write_text(self.path.read_text("utf-8"), "utf-8")
                        except OSError:
                            pass
                        raw = migrated
                        bus_first = True
                    else:
                        raw = migrated
                        bus_first = False
            except Exception:
                pass
            cfg = AppConfig.from_dict(raw)
            cfg.normalise()
            self._cfg = cfg
            if not self.path.exists():
                self.save(cfg)
            return cfg

    def save(self, cfg: AppConfig | None = None) -> AppConfig:
        with self._lock:
            cfg = cfg or self._cfg or self.load()
            cfg.normalise()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            from .migrations import CURRENT_CONFIG_VERSION

            payload = cfg.to_dict()
            payload["config_version"] = CURRENT_CONFIG_VERSION
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
            os.replace(tmp, self.path)
            self._cfg = cfg
            return cfg

    def apply_patch(self, patch: Dict[str, Any]) -> AppConfig:
        """前端局部更新：深合并，跳过脱敏占位符 ``***``。"""

        with self._lock:
            base = self.load().to_dict()
            merged = _deep_merge(base, _strip_masks(patch))
            cfg = AppConfig.from_dict(merged)
            cfg.normalise()
            return self.save(cfg)

    def reset(self) -> AppConfig:
        with self._lock:
            self._cfg = AppConfig()
            self._cfg.normalise()
            return self.save(self._cfg)

    def replace(self, raw: Dict[str, Any]) -> AppConfig:
        """整份替换配置（用于「导入配置」）。缺失的字段回落到默认值。"""

        if not isinstance(raw, dict):
            raise ValueError("配置文件格式不正确：顶层必须是一个 JSON 对象")
        with self._lock:
            cfg = AppConfig.from_dict(raw)
            cfg.normalise()
            issues = cfg.validate()
            hard = [i for i in issues if "端口" in i or "必须在" in i]
            if hard:
                raise ValueError("；".join(hard))
            return self.save(cfg)


def _strip_masks(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_masks(v) for k, v in node.items() if v != "***"}
    if isinstance(node, list):
        return [_strip_masks(v) for v in node]
    return node


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if k.startswith("_") or k == "version":
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


store = ConfigStore()


def get_config() -> AppConfig:
    return store.load()
