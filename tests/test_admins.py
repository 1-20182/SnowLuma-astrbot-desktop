"""SnowLuma / AstrBot 两侧配置注入的测试（T2）。

覆盖点：
* SnowLuma：模板 + per-UIN 注入、保留用户已有连接、幂等、name 冲突顺延、校验报错、账号发现、runtime.json；
* AstrBot：cmd_config.json 深合并不丢字段、provider 字段形状（key 列表 / api_base / model）、
  admins_id 只增不减、不改密码、备份、初始口令解析、探活状态机。

全部用 tmp_path 造假目录，不触碰真实安装。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slab import astrbot_admin, snowluma_admin
from slab.config import AppConfig


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def cfg() -> AppConfig:
    config = AppConfig()
    config.normalise()
    config.link.token = "test-token-123"
    config.link.ws_host = "127.0.0.1"
    config.link.ws_port = 6199
    config.snowluma.relay_port = 3001
    return config


def _sl_home(tmp_path: Path) -> Path:
    home = tmp_path / "snowluma"
    (home / "config").mkdir(parents=True)
    return home


def _ab_home(tmp_path: Path) -> Path:
    home = tmp_path / "astrbot"
    (home / "data").mkdir(parents=True)
    return home


def _read(path: Path) -> dict:
    return json.loads(path.read_text("utf-8"))


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def _per_uin_config() -> dict:
    """一份"用户已有配置"的 per-UIN 快照（应被完整保留，除我们的 wsClient 之外）。"""

    return {
        "mode": "snapshot",
        "networks": {
            "httpServers": [
                {
                    "name": "user-http",
                    "accessToken": "keep-me",
                    "messageFormat": "array",
                    "reportSelfMessage": False,
                    "host": "127.0.0.1",
                    "port": 3100,
                    "path": "/",
                    "enableWebSocket": False,
                }
            ],
            "httpClients": [],
            "wsServers": [
                {
                    "name": "user-ws",
                    "enabled": True,
                    "accessToken": "user-ws-token",
                    "messageFormat": "array",
                    "reportSelfMessage": False,
                    "host": "127.0.0.1",
                    "port": 3101,
                    "path": "/",
                    "role": "Universal",
                }
            ],
            "wsClients": [
                {
                    "name": "external-bot",
                    "enabled": True,
                    "url": "ws://10.0.0.9:9999/ws",
                    "role": "Universal",
                    "accessToken": "other",
                    "messageFormat": "array",
                    "reportSelfMessage": False,
                    "reconnectIntervalMs": 5000,
                }
            ],
        },
        "statusCommand": {"enabled": True, "swallow": True, "cooldownSeconds": 9, "trigger": "#x"},
        "historySync": {"enabled": True},
        "notifications": {"channelIds": ["ops"]},
    }


# --------------------------------------------------------------------------- #
# SnowLuma 注入
# --------------------------------------------------------------------------- #
async def test_apply_native_writes_template_and_per_uin(cfg: AppConfig, tmp_path: Path) -> None:
    home = _sl_home(tmp_path)
    _write(home / "config" / "onebot_3985934176.json", _per_uin_config())

    result = await snowluma_admin.apply_native(cfg, home=home, hot=False)

    assert result["ok"] is True
    assert result["mode"] == "native"
    assert result["accounts"] == ["3985934176"]

    template = _read(home / "config" / "onebot.json")
    clients = template["networks"]["wsClients"]
    assert len(clients) == 1
    entry = clients[0]
    assert entry["name"] == "astrbot"
    assert entry["url"] == "ws://127.0.0.1:6199/ws"  # URL 不带 query token（AstrBot 只读 header）
    assert entry["accessToken"] == "test-token-123"
    assert entry["role"] == "Universal"
    assert entry["messageFormat"] == "array"
    assert entry["reconnectIntervalMs"] >= 1000

    per_uin = _read(home / "config" / "onebot_3985934176.json")
    # 用户的其它适配器 / 状态块 / snapshot 语义都要保留
    assert per_uin["mode"] == "snapshot"
    assert [item["name"] for item in per_uin["networks"]["httpServers"]] == ["user-http"]
    assert [item["name"] for item in per_uin["networks"]["wsServers"]] == ["user-ws"]
    assert per_uin["notifications"] == {"channelIds": ["ops"]}
    assert per_uin["statusCommand"]["trigger"] == "#x"
    names = [item["name"] for item in per_uin["networks"]["wsClients"]]
    assert names == ["external-bot", "astrbot"]


async def test_apply_native_is_idempotent(cfg: AppConfig, tmp_path: Path) -> None:
    home = _sl_home(tmp_path)
    _write(home / "config" / "onebot_3985934176.json", _per_uin_config())

    first = await snowluma_admin.apply_native(cfg, home=home, hot=False)
    snapshot = {
        "template": (home / "config" / "onebot.json").read_text("utf-8"),
        "per_uin": (home / "config" / "onebot_3985934176.json").read_text("utf-8"),
    }
    second = await snowluma_admin.apply_native(cfg, home=home, hot=False)
    assert first["ok"] and second["ok"]
    assert (home / "config" / "onebot.json").read_text("utf-8") == snapshot["template"]
    assert (home / "config" / "onebot_3985934176.json").read_text("utf-8") == snapshot["per_uin"]
    per_uin = _read(home / "config" / "onebot_3985934176.json")
    assert [item["name"] for item in per_uin["networks"]["wsClients"]].count("astrbot") == 1


async def test_apply_native_reuses_existing_client_name(cfg: AppConfig, tmp_path: Path) -> None:
    """URL 相同但用户改过名字：复用该名字，不产生第二条连接。"""

    home = _sl_home(tmp_path)
    data = _per_uin_config()
    data["networks"]["wsClients"].append(
        {
            "name": "我的AstrBot",
            "enabled": True,
            "url": "ws://127.0.0.1:6199/ws",
            "role": "Universal",
            "accessToken": "old-token",
            "messageFormat": "array",
            "reportSelfMessage": False,
            "reconnectIntervalMs": 3000,
        }
    )
    _write(home / "config" / "onebot_3985934176.json", data)

    await snowluma_admin.apply_native(cfg, home=home, hot=False)
    clients = _read(home / "config" / "onebot_3985934176.json")["networks"]["wsClients"]
    ours = [item for item in clients if item["url"] == "ws://127.0.0.1:6199/ws"]
    assert len(ours) == 1
    assert ours[0]["name"] == "我的AstrBot"
    assert ours[0]["accessToken"] == "test-token-123"  # token 已同步


async def test_apply_native_does_not_delete_user_ws_clients(cfg: AppConfig, tmp_path: Path) -> None:
    home = _sl_home(tmp_path)
    _write(home / "config" / "onebot_3985934176.json", _per_uin_config())
    await snowluma_admin.apply_native(cfg, home=home, hot=False)
    clients = _read(home / "config" / "onebot_3985934176.json")["networks"]["wsClients"]
    assert any(item["name"] == "external-bot" for item in clients)
    assert any(item["name"] == "astrbot" for item in clients)


def test_merge_ws_client_avoids_name_collision(cfg: AppConfig) -> None:
    """name 在同一账号内跨四类唯一：已被 httpServers 占用时顺延为 astrbot-1。"""

    config = snowluma_admin.ensure_skeleton({})
    config["networks"]["httpServers"].append(
        {
            "name": "astrbot",
            "accessToken": "x",
            "messageFormat": "array",
            "reportSelfMessage": False,
            "host": "127.0.0.1",
            "port": 3999,
            "path": "/",
            "enableWebSocket": False,
        }
    )
    merged = snowluma_admin.merge_ws_client(config, snowluma_admin.ws_client_entry(cfg))
    names = [item["name"] for item in merged["networks"]["wsClients"]]
    assert names == ["astrbot-1"]


async def test_apply_native_fails_when_not_deployed(cfg: AppConfig, tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    result = await snowluma_admin.apply_native(cfg, home=missing, hot=False)
    assert result["ok"] is False
    assert "安装目录不存在" in result["error"]


def test_validate_onebot_config_rejects_bad_values() -> None:
    config = snowluma_admin.ensure_skeleton({})
    config["networks"]["wsClients"].append(
        {
            "name": "a",
            "enabled": True,
            "url": "ws://127.0.0.1:1/",
            "role": "Wrong",
            "accessToken": "x",
            "messageFormat": "array",
            "reportSelfMessage": False,
            "reconnectIntervalMs": 1000,
        }
    )
    with pytest.raises(snowluma_admin.SnowLumaConfigError):
        snowluma_admin.validate_onebot_config(config)

    bad_port = snowluma_admin.ensure_skeleton({})
    bad_port["networks"]["wsServers"].append(
        {"name": "s", "accessToken": "", "messageFormat": "array", "reportSelfMessage": False,
         "host": "127.0.0.1", "port": 70000, "path": "/", "role": "Universal"}
    )
    with pytest.raises(snowluma_admin.SnowLumaConfigError):
        snowluma_admin.validate_onebot_config(bad_port)

    dup = snowluma_admin.ensure_skeleton({})
    entry = snowluma_admin.ws_client_entry(AppConfig())
    dup["networks"]["wsClients"] = [dict(entry), dict(entry)]
    with pytest.raises(snowluma_admin.SnowLumaConfigError):
        snowluma_admin.validate_onebot_config(dup)


async def test_apply_relay_reuses_existing_ws_server(cfg: AppConfig, tmp_path: Path) -> None:
    home = _sl_home(tmp_path)
    config = _per_uin_config()
    config["networks"]["wsServers"].append(
        {
            "name": "ws-default",
            "enabled": True,
            "accessToken": "old-ws-token",
            "messageFormat": "array",
            "reportSelfMessage": False,
            "host": "127.0.0.1",
            "port": 3001,
            "path": "/",
            "role": "Universal",
        }
    )
    _write(home / "config" / "onebot_3985934176.json", config)

    result = await snowluma_admin.apply_relay(cfg, home=home, hot=False)
    assert result["ok"] is True
    servers = _read(home / "config" / "onebot_3985934176.json")["networks"]["wsServers"]
    by_port = [item for item in servers if item["port"] == 3001]
    assert len(by_port) == 1  # 不新增第二个同端口监听
    assert by_port[0]["accessToken"] == "test-token-123"  # token 与中继一致
    assert result["relay_url"] == "ws://127.0.0.1:3001/"


async def test_apply_relay_adds_server_when_missing(cfg: AppConfig, tmp_path: Path) -> None:
    home = _sl_home(tmp_path)
    result = await snowluma_admin.apply_relay(cfg, home=home, hot=False)
    assert result["ok"] is True
    template = _read(home / "config" / "onebot.json")
    servers = template["networks"]["wsServers"]
    assert len(servers) == 1
    assert servers[0]["port"] == 3001
    assert servers[0]["name"] == snowluma_admin.RELAY_SERVER_NAME


async def test_apply_webui_writes_runtime_json(cfg: AppConfig, tmp_path: Path) -> None:
    home = _sl_home(tmp_path)
    _write(home / "config" / "runtime.json", {"webuiPort": 5199, "logRetainDays": 3, "logPerUin": True})
    cfg.snowluma.webui_port = 5299

    result = await snowluma_admin.apply_webui(cfg, home=home)
    assert result["ok"] is True
    assert "webuiPort" in result["changed"]
    runtime = _read(home / "config" / "runtime.json")
    assert runtime["webuiPort"] == 5299
    assert runtime["webuiHost"] == "127.0.0.1"
    assert runtime["logRetainDays"] == 3  # 用户其它字段保留
    assert runtime["logPerUin"] is True
    assert runtime["webuiTls"] == {"enabled": False}


def test_list_accounts_merges_three_sources(cfg: AppConfig, tmp_path: Path) -> None:
    home = _sl_home(tmp_path)
    _write(home / "config" / "onebot_111111111.json", _per_uin_config())
    (home / "data" / "222222222").mkdir(parents=True)
    logs = [
        "[Hook] login detected: PID=40948 UIN=333333333",
        "[OneBot] session started: UIN=111111111",
        "no uin here",
    ]
    assert snowluma_admin.list_accounts(cfg, home=home, log_lines=logs) == [
        "111111111",
        "222222222",
        "333333333",
    ]
    sources = snowluma_admin.account_sources(cfg, home=home, log_lines=logs)
    assert sources["files"] == ["111111111"]
    assert sources["data_dirs"] == ["222222222"]
    assert sources["logs"] == ["111111111", "333333333"]


def test_relay_url_and_token(cfg: AppConfig) -> None:
    assert snowluma_admin.relay_ws_url(cfg) == "ws://127.0.0.1:3001/"
    assert snowluma_admin.relay_token(cfg) == "test-token-123"


async def test_snowluma_probe_not_deployed(cfg: AppConfig, tmp_path: Path) -> None:
    result = await snowluma_admin.probe(cfg, home=tmp_path / "missing", timeout=0.3)
    assert result["state"] == "not_deployed"
    assert result["connected"] is False
    assert result["suggestions"]


async def test_snowluma_probe_waiting_qq(cfg: AppConfig, tmp_path: Path) -> None:
    home = _sl_home(tmp_path)
    (home / "config" / "onebot.json").write_text("{}", "utf-8")
    cfg.snowluma.webui_port = 65021  # 未监听 → 走"文件已就绪但没账号"分支
    result = await snowluma_admin.probe(cfg, home=home, timeout=0.3, log_lines=[])
    assert result["state"] in ("not_running", "waiting_qq")


# --------------------------------------------------------------------------- #
# AstrBot 注入
# --------------------------------------------------------------------------- #
def test_build_provider_entry_shape(cfg: AppConfig) -> None:
    cfg.astrbot.provider = {
        "type": "openai_chat_completion",
        "base_url": "https://api.deepseek.com/v1/",
        "api_key": "sk-abc",
        "model": "deepseek-chat",
    }
    entry = astrbot_admin.build_provider_entry(cfg)
    assert entry["key"] == ["sk-abc"]           # 列表，不是字符串
    assert entry["api_base"] == "https://api.deepseek.com/v1"
    assert entry["model"] == "deepseek-chat"    # 顶层 model（openai_source 直接读它）
    assert entry["provider_type"] == "chat_completion"
    assert entry["enable"] is True


def test_build_provider_entry_requires_api_key(cfg: AppConfig) -> None:
    cfg.astrbot.provider["api_key"] = ""
    with pytest.raises(astrbot_admin.AstrBotConfigError) as excinfo:
        astrbot_admin.build_provider_entry(cfg)
    assert "API Key" in str(excinfo.value)


def test_build_platform_entry_narrows_wildcard_host(cfg: AppConfig) -> None:
    cfg.astrbot.platform = {"type": "aiocqhttp", "enable": True, "ws_reverse_host": "0.0.0.0",
                            "ws_reverse_port": 1, "ws_reverse_token": ""}
    entry = astrbot_admin.build_platform_entry(cfg)
    assert entry["ws_reverse_host"] == "127.0.0.1"
    assert entry["ws_reverse_port"] == cfg.link.ws_port
    assert entry["ws_reverse_token"] == "test-token-123"

    cfg.astrbot.platform["ws_reverse_host"] = "192.168.1.5"
    assert astrbot_admin.build_platform_entry(cfg)["ws_reverse_host"] == "192.168.1.5"


async def test_astrbot_apply_deep_merges_and_preserves(cfg: AppConfig, tmp_path: Path) -> None:
    home = _ab_home(tmp_path)
    original = {
        "config_version": 5,
        "platform": [{"id": "tg", "type": "telegram", "enable": True, "telegram_token": "x"}],
        "provider": [
            {"id": "mine", "type": "anthropic_chat_completion", "provider_type": "chat_completion",
             "enable": True, "key": ["sk-mine"], "api_base": "https://api.anthropic.com", "model": "claude"}
        ],
        "dashboard": {"enable": True, "username": "astrbot", "port": 7000,
                      "password": "md5-hash", "pbkdf2_password": "pbkdf2$hash",
                      "password_change_required": True},
        "admins_id": ["old-admin"],
        "agent_runner": {"runner_type": "local", "config": {"model": {"provider_id": ""}}},
        "persona": [{"name": "a"}],
        "timezone": "Asia/Shanghai",
    }
    _write(home / "data" / "cmd_config.json", original)
    cfg.astrbot.provider["api_key"] = "sk-new"
    cfg.astrbot.admins_id = ["123456789"]

    result = await astrbot_admin.apply(cfg, home=home)

    assert result["ok"] is True
    assert result["created"] is False
    assert result["backup"]["ok"] is True
    data = _read(home / "data" / "cmd_config.json")

    # 用户既有平台/提供商/其它配置全部保留
    assert [item["id"] for item in data["platform"]] == ["tg", "snowluma"]
    provider_ids = [item["id"] for item in data["provider"]]
    assert "mine" in provider_ids and astrbot_admin.DEFAULT_PROVIDER_ID in provider_ids
    assert data["persona"] == [{"name": "a"}]
    assert data["timezone"] == "Asia/Shanghai"

    # 我们写入的字段正确
    platform = next(item for item in data["platform"] if item["type"] == "aiocqhttp")
    assert platform["ws_reverse_port"] == cfg.link.ws_port
    assert platform["ws_reverse_token"] == cfg.link.token
    our_provider = next(item for item in data["provider"]
                        if item["id"] == astrbot_admin.DEFAULT_PROVIDER_ID)
    assert our_provider["key"] == ["sk-new"]
    assert our_provider["model"] == cfg.astrbot.provider["model"]
    # 新 provider 放第一位，且默认 provider 指向它
    assert data["provider"][0]["id"] == astrbot_admin.DEFAULT_PROVIDER_ID
    assert data["agent_runner"]["config"]["model"]["provider_id"] == astrbot_admin.DEFAULT_PROVIDER_ID

    # 面板：只改端口，不动密码
    assert data["dashboard"]["port"] == cfg.astrbot.webui_port
    assert data["dashboard"]["password"] == "md5-hash"
    assert data["dashboard"]["pbkdf2_password"] == "pbkdf2$hash"

    # admins_id 只增不减
    assert data["admins_id"] == ["old-admin", "123456789"]


async def test_astrbot_apply_is_idempotent(cfg: AppConfig, tmp_path: Path) -> None:
    home = _ab_home(tmp_path)
    cfg.astrbot.provider["api_key"] = "sk-new"
    cfg.astrbot.admins_id = ["123456789"]

    await astrbot_admin.apply(cfg, home=home)
    first = _read(home / "data" / "cmd_config.json")
    await astrbot_admin.apply(cfg, home=home)
    second = _read(home / "data" / "cmd_config.json")

    assert len(second["platform"]) == 1
    assert len(second["provider"]) == 1
    assert second["admins_id"] == ["123456789"]
    assert second["platform"][0]["ws_reverse_token"] == first["platform"][0]["ws_reverse_token"]


async def test_astrbot_apply_reuses_existing_aiocqhttp_platform(cfg: AppConfig, tmp_path: Path) -> None:
    home = _ab_home(tmp_path)
    _write(
        home / "data" / "cmd_config.json",
        {
            "config_version": 5,
            "platform": [
                {"id": "default", "type": "aiocqhttp", "enable": True,
                 "ws_reverse_host": "0.0.0.0", "ws_reverse_port": 6200, "ws_reverse_token": "old"}
            ],
            "provider": [],
            "dashboard": {"port": 6185, "password": "keep"},
        },
    )
    cfg.astrbot.provider["api_key"] = "sk-new"
    result = await astrbot_admin.apply(cfg, home=home)
    data = _read(home / "data" / "cmd_config.json")
    assert len(data["platform"]) == 1
    assert data["platform"][0]["id"] == "default"  # 就地更新，保留 id
    assert data["platform"][0]["ws_reverse_port"] == cfg.link.ws_port
    assert data["platform"][0]["ws_reverse_token"] == cfg.link.token
    assert any("就地更新" in warning for warning in result["warnings"])


async def test_astrbot_apply_require_provider_flag(cfg: AppConfig, tmp_path: Path) -> None:
    home = _ab_home(tmp_path)
    cfg.astrbot.provider["api_key"] = ""
    with pytest.raises(astrbot_admin.AstrBotConfigError):
        await astrbot_admin.apply(cfg, home=home)

    result = await astrbot_admin.apply(cfg, home=home, require_provider=False)
    assert result["ok"] is True
    assert result["changes"]["provider"]["action"] == "skipped"
    data = _read(home / "data" / "cmd_config.json")
    assert data["platform"][0]["type"] == "aiocqhttp"  # 平台仍写入了
    assert any("未写入模型提供商" in warning for warning in result["warnings"])


async def test_astrbot_apply_missing_dir(cfg: AppConfig, tmp_path: Path) -> None:
    with pytest.raises(astrbot_admin.AstrBotConfigError):
        await astrbot_admin.apply(cfg, home=tmp_path / "missing")


def test_read_dashboard_password_from_logs(cfg: AppConfig, tmp_path: Path) -> None:
    home = _ab_home(tmp_path)
    logs = [
        "[17:51:25] [Core] [INFO] [dashboard.server:662]: Starting WebUI at http://0.0.0.0:6185",
        "   ➜  Initial username: astrbot",
        "   ➜  Initial password: AstrBotTest123",
        "   ➜  Change it after logging in",
    ]
    result = astrbot_admin.read_dashboard_password(cfg, home=home, log_lines=logs)
    assert result["ok"] is True
    assert result["password"] == "AstrBotTest123"
    assert result["username"] == "astrbot"
    assert result["source"] == "log"


def test_read_dashboard_password_from_env(cfg: AppConfig, tmp_path: Path, monkeypatch) -> None:
    home = _ab_home(tmp_path)
    monkeypatch.setenv("ASTRBOT_DASHBOARD_INITIAL_PASSWORD", "EnvPw123")
    result = astrbot_admin.read_dashboard_password(cfg, home=home, log_lines=["nothing here"])
    assert result["password"] == "EnvPw123"
    assert result["source"] == "env"


def test_read_dashboard_password_missing(cfg: AppConfig, tmp_path: Path) -> None:
    home = _ab_home(tmp_path)
    result = astrbot_admin.read_dashboard_password(cfg, home=home, log_lines=["boot"])
    assert result["ok"] is False
    assert result["hint"]


async def test_im_umo(cfg: AppConfig, tmp_path: Path) -> None:
    home = _ab_home(tmp_path)
    cfg.astrbot.install_dir = str(home)
    cfg.astrbot.provider["api_key"] = "sk-new"
    await astrbot_admin.apply(cfg, home=home)
    private = astrbot_admin.im_umo(cfg, "private", "20002")
    group = astrbot_admin.im_umo(cfg, "group", "123")
    assert private == f"{astrbot_admin.DEFAULT_PLATFORM_ID}:FriendMessage:20002"
    assert group == f"{astrbot_admin.DEFAULT_PLATFORM_ID}:GroupMessage:123"


def test_config_summary_masks_secrets(cfg: AppConfig, tmp_path: Path) -> None:
    home = _ab_home(tmp_path)
    _write(
        home / "data" / "cmd_config.json",
        {
            "platform": [{"id": "snowluma", "type": "aiocqhttp", "enable": True,
                          "ws_reverse_host": "127.0.0.1", "ws_reverse_port": 6199,
                          "ws_reverse_token": "super-secret"}],
            "provider": [{"id": "snowluma-console", "type": "openai_chat_completion",
                          "provider_type": "chat_completion", "enable": True,
                          "key": ["sk-secret"], "api_base": "https://x/v1", "model": "m"}],
            "dashboard": {"port": 6185, "password": "md5"},
            "admins_id": ["1"],
        },
    )
    summary = astrbot_admin.config_summary(cfg, home=home)
    assert summary["platform_token"] == "super-secret"  # 内部用于比对，不直接给前端
    assert summary["platform"]["ws_reverse_token"] == astrbot_admin.MASK
    assert summary["provider"]["key"] == [astrbot_admin.MASK]
    assert summary["provider_model"] == "m"
    assert summary["dashboard_port"] == 6185


async def test_probe_onebot_ws_headers_and_mapping(cfg: AppConfig, monkeypatch) -> None:
    """AstrBot 反向 WS 探针：握手头必须与 SnowLuma 一致，且能翻译 403。"""

    captured: dict = {}

    async def fake_handshake(url, headers, timeout=3.0):  # noqa: ANN001, ARG001
        captured["url"] = url
        captured["headers"] = dict(headers)
        return {"ok": False, "status_code": 403, "url": url, "detail": "token 不匹配", "error": ""}

    monkeypatch.setattr(astrbot_admin, "probe_ws_handshake", fake_handshake)
    cfg.snowluma.qq_uin = "3985934176"
    result = await astrbot_admin.probe_onebot_ws(cfg, timeout=1.0)

    assert result["status_code"] == 403
    assert captured["url"] == f"ws://127.0.0.1:{cfg.link.ws_port}/ws"
    headers = captured["headers"]
    assert headers["Authorization"] == f"Bearer {cfg.link.token}"
    assert headers["X-Client-Role"].lower() == "universal"
    assert headers["X-Self-ID"] == "3985934176"
    assert headers["User-Agent"] == "OneBot/11"


async def test_invoke_action_via_webui(cfg: AppConfig) -> None:
    """native 模式的应急通道：用 WebUI 的 /api/debug/invoke 直接执行 OneBot 动作。"""

    class FakeApi:
        def __init__(self) -> None:
            self.calls: list = []
            self.token = ""

        async def login(self) -> str:
            self.token = "tok"
            return self.token

        async def connections(self) -> list:
            self.calls.append(("connections",))
            return [{"uin": "3985934176", "nickname": "机器人", "adapters": []}]

        async def invoke(self, uin, action, params):  # noqa: ANN001
            self.calls.append((uin, action, params))
            return {"status": "ok", "retcode": 0, "data": {"message_id": 7}}

        async def close(self) -> None:
            return None

    api = FakeApi()
    result = await snowluma_admin.invoke_action(
        cfg, "send_private_msg", {"user_id": "20002", "message": "hi"}, api=api
    )
    assert result["ok"] is True
    assert result["uin"] == "3985934176"
    assert result["data"] == {"message_id": 7}
    assert ("3985934176", "send_private_msg", {"user_id": "20002", "message": "hi"}) in api.calls

    class FailingApi(FakeApi):
        async def invoke(self, uin, action, params):  # noqa: ANN001
            return {"status": "failed", "retcode": 1400, "message": "参数错误"}

    with pytest.raises(snowluma_admin.SnowLumaApiError):
        await snowluma_admin.invoke_action(cfg, "send_private_msg", {}, api=FailingApi())


async def test_invoke_action_without_online_account(cfg: AppConfig) -> None:
    class NoAccountApi:
        token = ""

        async def login(self) -> str:
            return "tok"

        async def connections(self) -> list:
            return []

        async def invoke(self, uin, action, params):  # noqa: ANN001, ARG002
            raise AssertionError("不应该被调用")

        async def close(self) -> None:
            return None

    with pytest.raises(snowluma_admin.SnowLumaApiError) as excinfo:
        await snowluma_admin.invoke_action(cfg, "get_login_info", api=NoAccountApi())
    assert "QQ" in str(excinfo.value)


async def test_snowluma_probe_log_fallback(cfg: AppConfig, tmp_path: Path, monkeypatch) -> None:
    """WebUI 拿不到连接状态时（限流/门禁/会话未起），用日志兜底判定"已连接"。"""

    home = _sl_home(tmp_path)
    (home / "logs").mkdir()
    logfile = home / "logs" / "snowluma-2026-10-29.log"
    logfile.write_text(
        "18:19:39 INFO [3317812824] [OneBot.WS-Client] [astrbot] connected ws://127.0.0.1:6199/ws\n",
        "utf-8",
    )
    monkeypatch.setattr(snowluma_admin, "_recent_log_lines", lambda *a, **k: [])
    monkeypatch.setattr(snowluma_admin, "_process_state", lambda name: {"status": "running"})

    async def reachable(port, timeout=0.4):  # noqa: ANN001, ARG001
        return True

    async def failing_login(self):  # noqa: ANN001
        raise snowluma_admin.SnowLumaApiError(
            "SnowLuma 请求失败（429）：登录尝试过多，请 753 秒后重试", status_code=429
        )

    monkeypatch.setattr(snowluma_admin, "_port_open_async", reachable)
    monkeypatch.setattr(snowluma_admin.SnowLumaWebUI, "login", failing_login)
    cfg.snowluma.webui_password = "pw"

    result = await snowluma_admin.probe(cfg, home=home, timeout=0.2)

    assert result["source"] == "log"
    assert result["connected"] is True
    assert result["state"] == "connected"
    assert result["clients"][0]["uin"] == "3317812824"
    assert result["log"]["available"] is True
    assert "429" in result["webui"]["error"]


async def test_snowluma_probe_log_error_state(cfg: AppConfig, tmp_path: Path) -> None:
    home = _sl_home(tmp_path)
    logs = [
        "18:19:39 INFO [3317812824] [OneBot.WS-Client] [astrbot] connected ws://127.0.0.1:6199/ws",
        "18:19:44 WARN [3317812824] [OneBot.WS-Client] [astrbot] error ws://127.0.0.1:6199/ws: "
        "ECONNREFUSED connect ECONNREFUSED 127.0.0.1:6199",
    ]
    state = snowluma_admin.log_client_states(cfg, home=home, log_lines=logs)
    assert state["available"] is True
    assert state["clients"][0]["connected"] is False
    assert state["clients"][0]["status"] == "warn"
    assert "ECONNREFUSED" in state["clients"][0]["detail"]


async def test_snowluma_probe_warns_on_uin_mismatch(cfg: AppConfig, tmp_path: Path, monkeypatch) -> None:
    home = _sl_home(tmp_path)
    cfg.snowluma.qq_uin = "3985934176"
    monkeypatch.setattr(snowluma_admin, "_process_state", lambda name: {"status": "running"})

    async def reachable(port, timeout=0.4):  # noqa: ANN001, ARG001
        return True

    async def fake_connections(self):  # noqa: ANN001
        return [{"uin": "3317812824", "nickname": "实际在线", "adapters": [
            {"name": "astrbot", "kind": "wsClient", "status": "ok", "detail": "已连接"}
        ]}]

    async def noop(self):  # noqa: ANN001
        return None

    monkeypatch.setattr(snowluma_admin, "_port_open_async", reachable)
    monkeypatch.setattr(snowluma_admin.SnowLumaWebUI, "login", noop)
    monkeypatch.setattr(snowluma_admin.SnowLumaWebUI, "connections", fake_connections)
    cfg.snowluma.webui_password = "pw"

    result = await snowluma_admin.probe(cfg, home=home, timeout=0.2)
    assert result["source"] == "webui"
    assert result["connected"] is True
    assert any("3317812824" in warning and "3985934176" in warning
               for warning in result["warnings"])


async def test_login_cooldown_blocks_repeat_login(cfg: AppConfig, monkeypatch) -> None:
    snowluma_admin.clear_login_cooldown()
    calls = {"n": 0}

    async def fake_request(self, method, path, **kwargs):  # noqa: ANN001, ARG002
        calls["n"] += 1
        if path == "/api/login":
            raise snowluma_admin.SnowLumaApiError(
                "SnowLuma 请求失败（429）：登录尝试过多，请 753 秒后重试", status_code=429
            )
        return {}

    monkeypatch.setattr(snowluma_admin.SnowLumaWebUI, "request", fake_request)
    api = snowluma_admin.SnowLumaWebUI("http://127.0.0.1:1", password="pw")
    with pytest.raises(snowluma_admin.SnowLumaApiError):
        await api.login()
    assert calls["n"] == 1
    cooldown = snowluma_admin.login_cooldown()
    assert cooldown["active"] is True
    assert cooldown["remaining"] > 600  # 解析出服务端给的 753 秒

    # 冷却期内即使新建客户端也绝不发登录请求
    api2 = snowluma_admin.SnowLumaWebUI("http://127.0.0.1:1", password="pw")
    with pytest.raises(snowluma_admin.SnowLumaApiError) as excinfo:
        await api2.login()
    assert calls["n"] == 1
    assert "限制" in str(excinfo.value)
    snowluma_admin.clear_login_cooldown()


async def test_token_cache_avoids_second_login(cfg: AppConfig, monkeypatch) -> None:
    snowluma_admin.SnowLumaWebUI.invalidate_token_cache()
    calls = {"n": 0}

    async def fake_request(self, method, path, **kwargs):  # noqa: ANN001, ARG002
        calls["n"] += 1
        return {"token": "cached-token"}

    monkeypatch.setattr(snowluma_admin.SnowLumaWebUI, "request", fake_request)
    base = "http://127.0.0.1:65099"
    first = snowluma_admin.SnowLumaWebUI(base, password="pw")
    second = snowluma_admin.SnowLumaWebUI(base, password="pw")
    assert await first.login() == "cached-token"
    assert await second.login() == "cached-token"
    assert calls["n"] == 1
    snowluma_admin.SnowLumaWebUI.invalidate_token_cache()


def test_config_summary_understands_new_provider_structure(cfg: AppConfig, tmp_path: Path) -> None:
    """AstrBot 启动后会把 provider 迁移成 provider + provider_source_id → provider_sources。"""

    home = _ab_home(tmp_path)
    _write(
        home / "data" / "cmd_config.json",
        {
            "config_version": 5,
            "platform": [{"id": "snowluma", "type": "aiocqhttp", "enable": True,
                          "ws_reverse_host": "127.0.0.1", "ws_reverse_port": 6199,
                          "ws_reverse_token": "tok"}],
            "provider": [{"id": "snowluma-console", "enable": True, "model": "fake-model",
                          "provider_source_id": "snowluma-console_source"}],
            "provider_sources": [
                {"id": "snowluma-console_source", "provider": "openai",
                 "type": "openai_chat_completion", "provider_type": "chat_completion",
                 "key": ["sk-real"], "api_base": "http://127.0.0.1:9/v1", "timeout": 120}
            ],
            "agent_runner": {"runner_type": "local",
                             "config": {"model": {"provider_id": "", "fallback_provider_ids": []}}},
            "dashboard": {"port": 6185},
        },
    )
    summary = astrbot_admin.config_summary(cfg, home=home)
    assert summary["provider_ok"] is True
    assert summary["provider_source_id"] == "snowluma-console_source"
    assert summary["provider_source_count"] == 1
    assert summary["provider_model"] == "fake-model"
    assert summary["provider_api_base"] == "http://127.0.0.1:9/v1"
    assert summary["provider"]["key"] == [astrbot_admin.MASK]
    assert summary["default_provider_id"] == ""
    assert summary["effective_provider_id"] == "snowluma-console"


async def test_astrbot_probe_provider_ok_with_new_structure(cfg: AppConfig, tmp_path: Path,
                                                          monkeypatch) -> None:
    home = _ab_home(tmp_path)
    _write(
        home / "data" / "cmd_config.json",
        {
            "platform": [{"id": "snowluma", "type": "aiocqhttp", "enable": True,
                          "ws_reverse_host": "127.0.0.1", "ws_reverse_port": cfg.link.ws_port,
                          "ws_reverse_token": cfg.link.token}],
            "provider": [{"id": "snowluma-console", "enable": True, "model": "m",
                          "provider_source_id": "src"}],
            "provider_sources": [{"id": "src", "provider": "openai",
                                  "type": "openai_chat_completion",
                                  "provider_type": "chat_completion",
                                  "key": ["sk"], "api_base": "http://x/v1"}],
            "dashboard": {"port": cfg.astrbot.webui_port},
        },
    )

    async def ws_ok(_cfg, timeout: float = 3.0) -> dict:  # noqa: ARG001
        return {"ok": True, "status_code": 101, "url": "x", "detail": "ok", "error": ""}

    async def reachable(port, timeout=0.4):  # noqa: ANN001, ARG001
        return True

    monkeypatch.setattr("slab.supervisor.port_open", lambda *a, **k: True)
    result = await astrbot_admin.probe(cfg, home=home, timeout=0.2, log_lines=[], ws_probe=ws_ok)
    assert result["provider_ok"] is True
    assert result["state"] == "ready"


async def test_apply_writes_reply_policy(cfg: AppConfig, tmp_path: Path) -> None:
    home = _ab_home(tmp_path)
    _write(
        home / "data" / "cmd_config.json",
        {
            "config_version": 5,
            "platform_settings": {
                "unique_session": True,
                "reply_with_mention": True,
                "friend_message_needs_wake_prefix": True,
            },
            "wake_prefix": ["!"],
            "dashboard": {"port": 6185},
        },
    )
    cfg.astrbot.provider["api_key"] = "sk"
    result = await astrbot_admin.apply(cfg, home=home)
    data = _read(home / "data" / "cmd_config.json")

    assert data["platform_settings"]["friend_message_needs_wake_prefix"] is False
    assert data["platform_settings"]["unique_session"] is True   # 用户原值保留
    assert data["platform_settings"]["reply_with_mention"] is True  # 不动
    assert data["wake_prefix"] == ["!"]  # 已存在则不改
    policy = result["changes"]["reply_policy"]
    assert policy["friend_message_needs_wake_prefix"] is False
    assert "私聊" in policy["summary"]
    assert data["platform_settings"]["friend_message_needs_wake_prefix"] is False


async def test_apply_defaults_wake_prefix_when_missing(cfg: AppConfig, tmp_path: Path) -> None:
    home = _ab_home(tmp_path)
    cfg.astrbot.provider["api_key"] = "sk"
    result = await astrbot_admin.apply(cfg, home=home)
    data = _read(home / "data" / "cmd_config.json")
    assert data["wake_prefix"] == ["/"]
    assert result["changes"]["reply_policy"]["wake_prefix_defaulted"] is True


async def test_apply_updates_migrated_provider_source(cfg: AppConfig, tmp_path: Path) -> None:
    """已被 AstrBot 迁移过的配置：更新 provider_sources 而不是把 key 写回 provider。"""

    home = _ab_home(tmp_path)
    _write(
        home / "data" / "cmd_config.json",
        {
            "config_version": 5,
            "platform": [{"id": "snowluma", "type": "aiocqhttp", "enable": True,
                          "ws_reverse_host": "127.0.0.1", "ws_reverse_port": 6199,
                          "ws_reverse_token": "old-token"}],
            "provider": [{"id": "snowluma-console", "enable": True, "model": "old-model",
                          "provider_source_id": "snowluma-console_source"}],
            "provider_sources": [{"id": "snowluma-console_source", "provider": "openai",
                                  "type": "openai_chat_completion",
                                  "provider_type": "chat_completion",
                                  "key": ["sk-old"], "api_base": "http://old/v1"}],
            "dashboard": {"port": 6185},
        },
    )
    cfg.astrbot.provider = {
        "type": "openai_chat_completion",
        "base_url": "http://new/v1",
        "api_key": "sk-new",
        "model": "new-model",
    }

    result = await astrbot_admin.apply(cfg, home=home)
    data = _read(home / "data" / "cmd_config.json")

    assert len(data["provider"]) == 1
    entry = data["provider"][0]
    assert entry["provider_source_id"] == "snowluma-console_source"
    assert "key" not in entry            # 不把 key 重复写回 provider
    assert entry["model"] == "new-model"
    source = next(item for item in data["provider_sources"]
                  if item["id"] == "snowluma-console_source")
    assert source["key"] == ["sk-new"]
    assert source["api_base"] == "http://new/v1"
    assert result["changes"]["provider"]["provider_source_updated"] is True

    summary = astrbot_admin.config_summary(cfg, home=home)
    assert summary["provider_ok"] is True
    assert summary["provider_model"] == "new-model"


async def test_astrbot_probe_states(cfg: AppConfig, tmp_path: Path, monkeypatch) -> None:
    cfg.astrbot.webui_port = 65031
    cfg.snowluma.webui_port = 65032
    cfg.link.ws_port = 65033

    async def fake_ws_probe(_cfg: AppConfig, timeout: float = 3.0) -> dict:  # noqa: ARG001
        return {"ok": False, "status_code": 0, "url": "ws://127.0.0.1:65033/ws",
                "detail": "连不上", "error": "ConnectionRefused"}

    home = tmp_path / "missing"
    result = await astrbot_admin.probe(cfg, home=home, timeout=0.2, log_lines=[],
                                       ws_probe=fake_ws_probe)
    assert result["state"] == "not_deployed"

    home = _ab_home(tmp_path)
    result = await astrbot_admin.probe(cfg, home=home, timeout=0.2, log_lines=[],
                                       ws_probe=fake_ws_probe)
    assert result["state"] == "config_missing"

    cfg.astrbot.provider["api_key"] = "sk-new"
    await astrbot_admin.apply(cfg, home=home)
    result = await astrbot_admin.probe(cfg, home=home, timeout=0.2, log_lines=[],
                                       ws_probe=fake_ws_probe)
    assert result["token_match"] is True
    assert result["port_match"] is True
    assert result["state"] in ("not_running", "onebot_unavailable", "starting")

    # 端口可达 + 握手 101 + provider 就绪 → ready
    import slab.supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "port_open", lambda *args, **kwargs: True)

    async def ok_ws_probe(_cfg: AppConfig, timeout: float = 3.0) -> dict:  # noqa: ARG001
        return {"ok": True, "status_code": 101, "url": "ws://127.0.0.1:65033/ws",
                "detail": "ok", "error": ""}

    result = await astrbot_admin.probe(cfg, home=home, timeout=0.2,
                                       log_lines=["Running on http://0.0.0.0:65031"],
                                       ws_probe=ok_ws_probe)
    assert result["state"] == "ready"
    assert result["onebot_ws"]["status_code"] == 101
