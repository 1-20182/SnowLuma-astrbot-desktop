"""Lead 维护：QQ 客户端管理（只注入选中进程 / 隔离启动）+ 面板凭据。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from slab import installer, qq
from slab.config import AppConfig, get_config
from slab.server.app import create_app

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "slab" / "web" / "index.html"
APP_JS = ROOT / "slab" / "web" / "app.js"


# --------------------------------------------------------------------------- #
# 注入模式：默认"手动"，避免接管用户自己的 QQ
# --------------------------------------------------------------------------- #
def test_default_hook_mode_is_manual():
    cfg = AppConfig()
    cfg.normalise()
    assert cfg.snowluma.hook_mode == "manual"


def test_hook_env_follows_mode():
    cfg = AppConfig()
    cfg.normalise()
    assert installer.snowluma_env(cfg)["SNOWLUMA_HOOK_AUTOLOAD"] == "0"
    cfg.snowluma.hook_mode = "auto"
    assert installer.snowluma_env(cfg)["SNOWLUMA_HOOK_AUTOLOAD"] == "1"


def test_hook_mode_route():
    with TestClient(create_app()) as c:
        body = c.post("/api/qq/hook-mode", json={"mode": "auto"}).json()
        assert body["ok"] is True and body["mode"] == "auto"
        assert get_config().snowluma.hook_mode == "auto"
        c.post("/api/qq/hook-mode", json={"mode": "manual"})
        assert get_config().snowluma.hook_mode == "manual"
        bad = c.post("/api/qq/hook-mode", json={"mode": "nope"})
        assert bad.status_code == 400


# --------------------------------------------------------------------------- #
# QQ 检测 / 进程列表 / 隔离目录
# --------------------------------------------------------------------------- #
def test_qq_summary_shape():
    with TestClient(create_app()) as c:
        body = c.get("/api/qq/summary").json()
    assert body["ok"] is True
    assert isinstance(body["installs"], list)
    assert isinstance(body["processes"], list)
    assert body["isolated_profile"]
    for item in body["installs"]:
        assert "exe" in item and "dir" in item


def test_isolated_profile_under_storage_base():
    profile = qq.isolated_profile_dir()
    assert profile.name == "profile"
    assert "qq" in profile.parts


def test_qq_process_rows_have_expected_fields():
    for row in qq.list_processes():
        for key in ("pid", "exe", "started_at", "age_seconds", "isolated"):
            assert key in row


def test_inject_requires_pid():
    with TestClient(create_app()) as c:
        res = c.post("/api/qq/inject", json={})
    assert res.status_code == 400


# --------------------------------------------------------------------------- #
# 面板凭据：不用翻日志
# --------------------------------------------------------------------------- #
def test_panels_endpoint():
    with TestClient(create_app()) as c:
        body = c.get("/api/panels").json()
    assert body["ok"] is True
    snow = body["snowluma"]
    assert snow["url"].startswith("http://127.0.0.1:")
    assert snow["password"] == get_config().snowluma.webui_password
    astr = body["astrbot"]
    assert astr["username"]
    assert "url" in astr and "password" in astr


# --------------------------------------------------------------------------- #
# 前端接线
# --------------------------------------------------------------------------- #
def test_qq_and_panel_ui_wired():
    html = INDEX.read_text("utf-8")
    for needle in ('id="card-login-qq"', 'id="qq-refresh"', 'id="qq-launch"',
                   'id="qq-processes"', 'id="card-panels"', 'id="panel-list"'):
        assert needle in html, f"缺少 {needle}"
    js = APP_JS.read_text("utf-8")
    for needle in ("/api/qq/summary", "/api/qq/inject", "/api/qq/launch-isolated",
                   "/api/panels", "oneClickLogin", "openPanel"):
        assert needle in js, f"app.js 缺少 {needle}"


def test_desktop_preload_exposes_open_panel():
    preload = (ROOT / "desktop" / "preload.js").read_text("utf-8")
    assert "openPanel" in preload and "panel:open" in preload
    main = (ROOT / "desktop" / "main.js").read_text("utf-8")
    assert "ipcMain.handle('panel:open'" in main
    assert "nativeSet" in main and "requestSubmit" in main

# --------------------------------------------------------------------------- #
# 注入校验：只允许配置里的小号被接管
# --------------------------------------------------------------------------- #
def test_inject_rejects_wrong_account(monkeypatch):
    """注入的进程如果是主号，必须自动解除并报 409。"""

    from slab.server import routes_qq

    calls = {"load": 0, "unload": 0}

    class FakeUI:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, path, **kw):
            calls[path.rsplit("/", 1)[-1]] = calls.get(path.rsplit("/", 1)[-1], 0) + 1
            return {"success": True}

        async def processes(self):
            return [{"pid": 19048, "uin": "3317812824", "account": {"uin": "3317812824"}}]

    monkeypatch.setattr(routes_qq, "_webui", lambda cfg: FakeUI())
    from slab.config import store

    store.apply_patch({"snowluma": {"qq_uin": "3985934178"}})
    with TestClient(create_app()) as c:
        res = c.post("/api/qq/inject", json={"pid": 19048})
    assert res.status_code == 409
    body = res.json()
    text = str(body.get("detail") or body.get("error") or body)
    assert "3317812824" in text
    assert calls.get("load") == 1
    assert calls.get("unload") == 1


def test_runtime_json_gets_hook_switch(tmp_path):
    """SnowLuma 每次启动都要把 hookAutoLoad 落到 runtime.json（环境变量在部分版本不生效）。"""

    from slab import installer

    home = tmp_path / "snowluma"
    (home / "config").mkdir(parents=True)
    installer.write_runtime_config(home, 5099, hook_autoload=False)
    import json

    data = json.loads((home / "config" / "runtime.json").read_text("utf-8"))
    assert data["hookAutoLoad"] is False
    assert data["webuiPort"] == 5099
    installer.write_runtime_config(home, 5099, hook_autoload=True)
    data = json.loads((home / "config" / "runtime.json").read_text("utf-8"))
    assert data["hookAutoLoad"] is True
