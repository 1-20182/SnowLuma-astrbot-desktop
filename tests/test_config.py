import json

import pytest

from slab.config import AppConfig, ConfigStore, get_config, new_token


def test_defaults_normalised():
    cfg = AppConfig()
    cfg.normalise()
    assert cfg.link.token
    assert cfg.snowluma.onebot_url == f"ws://{cfg.link.ws_host}:{cfg.link.ws_port}/ws"
    assert cfg.astrbot.platform["ws_reverse_port"] == cfg.link.ws_port
    assert cfg.astrbot.platform["ws_reverse_token"] == cfg.link.token
    assert cfg.snowluma.onebot_token == cfg.link.token
    assert cfg.astrbot.webui_password and cfg.snowluma.webui_password


def test_patch_deep_merge_and_mask(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    cfg = store.load()
    token = cfg.link.token

    patched = store.apply_patch({"astrbot": {"provider": {"api_key": "sk-test", "model": "deepseek-chat"}}})
    assert patched.astrbot.provider["api_key"] == "sk-test"
    # 未提及的字段保持不变
    assert patched.astrbot.provider["base_url"] == cfg.astrbot.provider["base_url"]
    assert patched.link.token == token

    masked = patched.masked()
    assert masked["astrbot"]["provider"]["api_key"] == "***"
    assert masked["link"]["token"] == "***"
    assert masked["_secrets_configured"]["astrbot_api_key"] is True

    # 写回 *** 不应覆盖真实密钥
    store.apply_patch({"astrbot": {"provider": {"api_key": "***"}}})
    assert store.load().astrbot.provider["api_key"] == "sk-test"


def test_port_change_keeps_link_in_sync(tmp_path):
    store = ConfigStore(tmp_path / "c.json")
    cfg = store.apply_patch({"link": {"ws_port": 7001}})
    assert cfg.astrbot.platform["ws_reverse_port"] == 7001
    assert cfg.snowluma.onebot_url.endswith(":7001/ws")


def test_validate_detects_duplicate_ports(tmp_path):
    store = ConfigStore(tmp_path / "c.json")
    cfg = store.apply_patch({"app": {"port": 6185}})
    issues = cfg.validate()
    assert any("端口重复" in i for i in issues)


def test_config_file_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.load()
    data = json.loads(path.read_text("utf-8"))
    assert data["link"]["token"]
    cfg2 = ConfigStore(path).load()
    assert cfg2.link.token == data["link"]["token"]


def test_token_uniqueness():
    assert new_token() != new_token()
