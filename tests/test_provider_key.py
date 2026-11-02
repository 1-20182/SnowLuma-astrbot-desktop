"""Lead 维护：大模型 Key 连通性检查 + 机器人号自动同步。

背景（真实线上问题）：用户填了「接口地址 + 模型名」但没填 API Key，
AstrBot 在收到消息时才报 401「API Key 无效」，日志里一大片堆栈，界面上却看不出来。
这里把问题提前暴露：配置页可以一键测试，账号可以一键同步。
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from slab import astrbot_admin
from slab.config import store
from slab.server import routes_link
from slab.server.app import create_app


def test_provider_test_reports_missing_key():
    store.apply_patch({"astrbot": {"provider": {"api_key": ""}}})
    with TestClient(create_app()) as c:
        body = c.post("/api/link/provider/test").json()
    assert body["ok"] is False
    assert body["kind"] == "no-key"
    assert "API Key" in body["message"]


def test_provider_test_reports_bad_base():
    store.apply_patch({"astrbot": {"provider": {"api_key": "sk-test", "base_url": ""}}})
    with TestClient(create_app()) as c:
        body = c.post("/api/link/provider/test").json()
    assert body["ok"] is False
    assert body["kind"] == "no-base"


def test_probe_provider_handles_unreachable():
    result = astrbot_admin.probe_provider("http://127.0.0.1:1/v1", "deepseek-chat", "sk-x", timeout=2)
    assert result["ok"] is False
    assert result["kind"] in ("network", "timeout", "error")


def test_sync_account_writes_qq_uin(monkeypatch):
    async def fake_status(force=False):  # noqa: ANN001
        return {
            "onebot": {
                "self_id": "3985934176",
                "clients": [{"uin": "3985934176"}, {"uin": "3317812824"}],
            }
        }

    monkeypatch.setattr(routes_link, "status", fake_status)
    store.apply_patch({"astrbot": {"admins_id": "3317812824"}})
    with TestClient(create_app()) as c:
        body = c.post("/api/link/sync-account").json()
    assert body["ok"] is True
    # 多账号时优先选不是管理员主号的那个
    assert body["qq_uin"] == "3985934176"
    assert store.load().snowluma.qq_uin == "3985934176"


def test_sync_account_without_login():
    async def fake_status(force=False):  # noqa: ANN001
        return {"onebot": {"self_id": "", "clients": []}}

    import pytest

    from slab.server import routes_link as mod

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mod, "status", fake_status)
    try:
        with TestClient(create_app()) as c:
            res = c.post("/api/link/sync-account")
        assert res.status_code == 409
    finally:
        monkeypatch.undo()


def test_config_page_has_tools():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "slab" / "web" / "index.html").read_text("utf-8")
    assert 'id="cfg-sync-account"' in html
    assert 'id="cfg-test-provider"' in html
    js = (Path(__file__).resolve().parents[1] / "slab" / "web" / "app.js").read_text("utf-8")
    assert "/api/link/sync-account" in js
    assert "/api/link/provider/test" in js

# --------------------------------------------------------------------------- #
# 模型列表：让用户从下拉里挑，而不是手敲模型名
# --------------------------------------------------------------------------- #
def test_models_endpoint_without_key():
    store.apply_patch({"astrbot": {"provider": {"api_key": ""}}})
    with TestClient(create_app()) as c:
        body = c.post("/api/link/provider/models").json()
    assert body["ok"] is False
    assert body["kind"] == "no-key"
    assert body["models"] == []


def test_models_fallback_by_service():
    from slab import astrbot_admin

    assert "deepseek-chat" in astrbot_admin.fallback_models("https://api.deepseek.com/v1")
    assert "gpt-4o-mini" in astrbot_admin.fallback_models("https://api.openai.com/v1")
    assert astrbot_admin.fallback_models("https://example.com/v1") == []


def test_models_endpoint_calls_api(monkeypatch):
    from slab import astrbot_admin

    def fake_list(base, key, timeout=20.0):  # noqa: ANN001
        return {"ok": True, "kind": "ok", "source": "api",
                "models": ["deepseek-chat", "deepseek-reasoner"], "message": "读取到 2 个可用模型"}

    monkeypatch.setattr(astrbot_admin, "list_provider_models", fake_list)
    with TestClient(create_app()) as c:
        body = c.post("/api/link/provider/models", json={"base_url": "https://api.deepseek.com/v1",
                                                         "api_key": "sk-x"}).json()
    assert body["ok"] is True
    assert len(body["models"]) == 2
    assert body["base_url"].startswith("https://api.deepseek.com")


def test_models_ui_has_datalist_and_button():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "slab" / "web" / "index.html").read_text("utf-8")
    assert 'id="model-options"' in html
    assert 'id="cfg-fetch-models"' in html
    assert 'list="model-options"' in html
    js = (root / "slab" / "web" / "app.js").read_text("utf-8")
    assert "/api/link/provider/models" in js
    assert "fetchProviderModels" in js

# --------------------------------------------------------------------------- #
# 从 AstrBot 读回真实 provider（列表形式的 key 是个坑）
# --------------------------------------------------------------------------- #
def test_key_text_unwraps_list():
    from slab import astrbot_admin

    assert astrbot_admin._key_text(["sk-abc"]) == "sk-abc"
    assert astrbot_admin._key_text(["", "sk-b"]) == "sk-b"
    assert astrbot_admin._key_text("sk-plain") == "sk-plain"
    assert astrbot_admin._key_text(None) == ""
    assert astrbot_admin._key_text([]) == ""


def test_inspect_provider_reads_list_key(tmp_path):
    from slab import astrbot_admin
    from slab.config import AppConfig

    cfg = AppConfig()
    home = tmp_path / "astrbot"
    (home / "data").mkdir(parents=True)
    (home / "data" / "cmd_config.json").write_text(
        json.dumps({
            "provider": [{"id": "p1", "model": "cn:deepseek-v4.1-flash",
                          "provider_source_id": "s1"}],
            "provider_sources": [{"id": "s1", "api_base": "http://127.0.0.1:7864/v1",
                                  "key": ["wbk_secret"], "type": "openai_chat_completion"}],
        }),
        "utf-8",
    )
    info = astrbot_admin.inspect_provider(cfg, home=home)
    assert info["provider"]["key"] == "wbk_secret"      # 不能带着方括号
    assert info["provider"]["api_base"] == "http://127.0.0.1:7864/v1"
    assert info["provider"]["model"] == "cn:deepseek-v4.1-flash"


def test_import_provider_route_writes_config(tmp_path, monkeypatch):
    from slab import astrbot_admin
    from slab.server import routes_link

    monkeypatch.setattr(
        astrbot_admin, "inspect_provider",
        lambda cfg, home=None: {"provider": {"id": "p", "model": "cn:auto",
                                             "api_base": "http://127.0.0.1:7864/v1",
                                             "key": "wbk_x"}},
    )
    monkeypatch.setattr(routes_link.astrbot_admin, "inspect_provider",
                        lambda cfg, home=None: {"provider": {"id": "p", "model": "cn:auto",
                                                             "api_base": "http://127.0.0.1:7864/v1",
                                                             "key": "wbk_x"}})
    with TestClient(create_app()) as c:
        body = c.post("/api/link/provider/import").json()
    assert body["ok"] is True
    assert body["model"] == "cn:auto"
    assert body["has_key"] is True
    assert store.load().astrbot.provider["api_key"] == "wbk_x"


def test_import_provider_ui_wired():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "slab" / "web" / "index.html").read_text("utf-8")
    assert 'id="cfg-import-provider"' in html
    js = (root / "slab" / "web" / "app.js").read_text("utf-8")
    assert "/api/link/provider/import" in js
    assert "importProviderFromAstrBot" in js

# --------------------------------------------------------------------------- #
# AstrBot 面板密码重置（"一键登录密码不对"）
# --------------------------------------------------------------------------- #
def test_astrbot_hash_format():
    from slab import astrbot_admin

    hashed = astrbot_admin.hash_dashboard_password("secret123")
    parts = hashed.split("$")
    assert parts[0] == "pbkdf2_sha256"
    assert parts[1] == "600000"
    assert len(bytes.fromhex(parts[2])) == 16
    assert len(bytes.fromhex(parts[3])) == 32
    # 每次盐不同
    assert hashed != astrbot_admin.hash_dashboard_password("secret123")


def test_set_dashboard_password_writes_hashes(tmp_path):
    import hashlib
    import json

    from slab import astrbot_admin
    from slab.config import AppConfig

    cfg = AppConfig()
    home = tmp_path / "astrbot"
    (home / "data").mkdir(parents=True)
    path = home / "data" / "cmd_config.json"
    path.write_text(json.dumps({"dashboard": {"username": "astrbot"}}), "utf-8")

    result = astrbot_admin.set_dashboard_password(cfg, "NewPass123", home=home)
    assert result["ok"] is True
    saved = json.loads(path.read_text("utf-8"))
    dash = saved["dashboard"]
    assert dash["pbkdf2_password"].startswith("pbkdf2_sha256$600000$")
    assert dash["password"] == hashlib.md5(b"NewPass123").hexdigest()
    assert dash["password_change_required"] is False
    assert dash["password_storage_upgraded"] is True


def test_set_dashboard_password_rejects_short(tmp_path):
    import pytest

    from slab import astrbot_admin
    from slab.config import AppConfig

    cfg = AppConfig()
    home = tmp_path / "astrbot"
    (home / "data").mkdir(parents=True)
    (home / "data" / "cmd_config.json").write_text("{}", "utf-8")
    with pytest.raises(astrbot_admin.AstrBotConfigError):
        astrbot_admin.set_dashboard_password(cfg, "short", home=home)


def test_reset_password_route(monkeypatch):
    import hashlib

    from slab import astrbot_admin
    from slab.config import store
    from slab.server import routes_console

    async def fake_stop(name): return {}
    async def fake_start(name): return {}

    monkeypatch.setattr(routes_console.supervisor, "is_active", lambda name: False)
    monkeypatch.setattr(routes_console.supervisor, "stop", fake_stop)
    monkeypatch.setattr(routes_console.supervisor, "start", fake_start)

    def fake_set(cfg, raw, home=None):
        return {"ok": True, "username": "astrbot", "password": raw, "path": "x"}

    monkeypatch.setattr(astrbot_admin, "set_dashboard_password", fake_set)

    with TestClient(create_app()) as c:
        body = c.post("/api/panels/astrbot-password", json={"password": "GivenPass123"}).json()
    assert body["ok"] is True
    assert body["password"] == "GivenPass123"
    assert store.load().astrbot.webui_password == "GivenPass123"


def test_panels_ui_has_reset_button():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    js = (root / "slab" / "web" / "app.js").read_text("utf-8")
    assert "resetAstrBotPassword" in js
    assert "/api/panels/astrbot-password" in js

# --------------------------------------------------------------------------- #
# 双向自检 / 修复发送链路（AstrBot 的 bot 登记被旧连接误删）
# --------------------------------------------------------------------------- #
def test_selftest_and_repair_routes_exist():
    from slab.server import routes_link

    paths = [r.path for r in routes_link.router.routes]
    assert "/api/link/selftest" in paths
    assert "/api/link/repair" in paths


def test_repair_restarts_snowluma_and_reports(monkeypatch):
    """native 模式：接进 AstrBot 的是 SnowLuma 自己那条连接 → 重启 SnowLuma。"""

    from slab.config import AppConfig
    from slab.server import routes_link

    cfg = AppConfig()
    cfg.normalise()
    cfg.link.mode = "native"
    monkeypatch.setattr(routes_link, "get_config", lambda: cfg)

    calls = []

    class _State:
        ready = True
        status = "running"

    class FakeSup:
        async def stop(self, name): calls.append(("stop", name))
        async def start(self, name): calls.append(("start", name))
        def is_active(self, name): return True
        def state(self, name): return _State()

    async def fake_status(force=False):  # noqa: ANN001
        return {"onebot": {"clients_connected": 1, "self_id": "3985934176"}}

    import slab.supervisor as sup_mod

    monkeypatch.setattr(sup_mod, "supervisor", FakeSup())
    monkeypatch.setattr(routes_link, "status", fake_status)

    with TestClient(create_app()) as c:
        body = c.post("/api/link/repair?send_test=false").json()
    assert body["ok"] is True
    assert [c[0] for c in calls] == ["stop", "start"]
    names = [s["name"] for s in body["steps"]]
    assert any("SnowLuma" in n for n in names)


def test_repair_restarts_bridge_after_cleaning_direct_client(monkeypatch):
    """bridge 模式：接进 AstrBot 的是中继 → 清残留直连并重启中继重新登记 bot。"""

    from slab.config import AppConfig
    from slab.server import routes_link

    cfg = AppConfig()
    cfg.normalise()
    cfg.link.mode = "bridge"
    monkeypatch.setattr(routes_link, "get_config", lambda: cfg)

    calls = []

    class FakeSup:
        async def stop(self, name): calls.append(("stop", name))
        async def start(self, name, cfg=None): calls.append(("start", name))
        async def restart(self, name, cfg=None): calls.append(("restart", name))

    async def fake_reregister(_cfg=None, **_kwargs):
        calls.append(("reregister", "bridge"))
        return {"ok": True, "detail": "中继已重新接入 AstrBot", "self_id": "3985934176"}

    async def fake_heal(cfg, *, home=None, hot=True, timeout=4.0):  # noqa: ANN001
        return {"ok": True, "changed": True, "files": ["x"], "removed": [{"name": "astrbot"}],
                "detail": "已清理 1 条 native 直连适配器"}

    import slab.supervisor as sup_mod

    monkeypatch.setattr(sup_mod, "supervisor", FakeSup())
    monkeypatch.setattr(routes_link.bridge, "reregister", fake_reregister)
    monkeypatch.setattr(routes_link.snowluma_admin, "ensure_single_channel", fake_heal)

    with TestClient(create_app()) as c:
        body = c.post("/api/link/repair?send_test=false").json()
    assert ("reregister", "bridge") in calls
    assert not [n for kind, n in calls if kind in ("stop", "start") and n == "snowluma"]
    names = [s["name"] for s in body["steps"]]
    assert "重启中继（重新登记可发送的 bot）" in names
    assert "清理残留直连适配器" in names


def test_selftest_ui_buttons():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "slab" / "web" / "index.html").read_text("utf-8")
    assert 'id="link-selftest"' in html and 'id="link-repair"' in html
    js = (root / "slab" / "web" / "app.js").read_text("utf-8")
    assert "/api/link/selftest" in js and "/api/link/repair" in js
    assert "runLinkSelftest" in js and "repairSendLink" in js
