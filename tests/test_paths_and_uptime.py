"""Lead 维护：运行时长平滑刷新 + 自定义安装路径 / 配置备份。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from slab.server import routes_deploy
from slab.server.app import create_app

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "slab" / "web" / "index.html"
APP_JS = ROOT / "slab" / "web" / "app.js"
THEME_CSS = ROOT / "slab" / "web" / "theme.css"


# --------------------------------------------------------------------------- #
# 运行时长：后端给 started_at，前端自己每秒算（否则会跳秒）
# --------------------------------------------------------------------------- #
def test_state_exposes_started_at():
    with TestClient(create_app()) as c:
        body = c.get("/api/state").json()
    assert "started_at" in body
    assert isinstance(body["started_at"], (int, float))
    # uptime 与 started_at 应该自洽（允许 5 秒误差）
    import time

    assert abs((time.time() - body["started_at"]) - body["uptime"]) < 5


def test_health_exposes_started_at():
    with TestClient(create_app()) as c:
        body = c.get("/api/health").json()
    assert "started_at" in body


def test_frontend_ticks_uptime_every_second():
    js = APP_JS.read_text("utf-8")
    assert "function tickUptimes" in js
    assert "setInterval(" in js and "1000" in js
    assert "S.startedAt" in js
    # 服务卡片的运行时长也按 started_at 现算，而不是渲染时固化
    assert "st.started_at" in js


# --------------------------------------------------------------------------- #
# 自定义安装路径
# --------------------------------------------------------------------------- #
def test_config_accepts_custom_install_dirs():
    with TestClient(create_app()) as c:
        r = c.put("/api/config", json={
            "snowluma": {"install_dir": r"D:\bots\snowluma"},
            "astrbot": {"install_dir": r"D:\bots\astrbot"},
        })
        cfg = r.json()["config"]
        assert cfg["snowluma"]["install_dir"] == r"D:\bots\snowluma"
        assert cfg["astrbot"]["install_dir"] == r"D:\bots\astrbot"
        # 还原，别污染其它用例
        c.put("/api/config", json={"snowluma": {"install_dir": ""}, "astrbot": {"install_dir": ""}})


def test_pick_dir_route_returns_chosen_path(monkeypatch):
    monkeypatch.setattr(
        routes_deploy, "_pick_dir_native",
        lambda title, initial: {"ok": True, "path": r"D:\bots\snowluma"},
    )
    with TestClient(create_app()) as c:
        body = c.post("/api/deploy/pick-dir", json={"title": "选择目录"}).json()
    assert body["ok"] is True
    assert body["path"].endswith("snowluma")


def test_pick_dir_cancel(monkeypatch):
    monkeypatch.setattr(routes_deploy, "_pick_dir_native", lambda title, initial: {"ok": False, "cancelled": True})
    with TestClient(create_app()) as c:
        body = c.post("/api/deploy/pick-dir", json={}).json()
    assert body["ok"] is False and body["cancelled"] is True


def test_pick_dir_native_dispatches_per_platform(monkeypatch):
    """目录选择框的实现按平台来：Windows 用 PowerShell，Linux 用 zenity/kdialog。"""

    from slab import plat

    calls: list[str] = []
    monkeypatch.setattr(routes_deploy, "_pick_dir_windows",
                        lambda title, initial: calls.append("windows") or {"ok": True, "path": "win"})
    monkeypatch.setattr(routes_deploy, "_pick_dir_posix",
                        lambda title, initial: calls.append("posix") or {"ok": True, "path": "posix"})
    body = routes_deploy._pick_dir_native("选择目录", "")
    assert body["ok"] is True
    assert calls == ["windows" if plat.IS_WINDOWS else "posix"]


def test_config_page_has_path_controls():
    html = INDEX.read_text("utf-8")
    for needle in ('id="cfg-snowluma-dir"', 'id="cfg-astrbot-dir"', 'id="cfg-data-dir"',
                   'data-pick-dir="snowluma"', 'data-pick-dir="astrbot"',
                   'data-open-dir="snowluma"', 'data-cfg="snowluma.install_dir"'):
        assert needle in html, f"配置页缺少 {needle}"
    js = APP_JS.read_text("utf-8")
    assert "function pickInstallDir" in js
    assert "function openDir" in js
    assert "/api/deploy/pick-dir" in js
    css = THEME_CSS.read_text("utf-8")
    assert ".path-row" in css


# --------------------------------------------------------------------------- #
# 配置导出 / 导入
# --------------------------------------------------------------------------- #
def test_config_export_is_downloadable_json():
    with TestClient(create_app()) as c:
        res = c.get("/api/config/export")
    assert res.status_code == 200
    assert "attachment" in res.headers.get("content-disposition", "")
    data = json.loads(res.text)
    assert "link" in data and "token" in data["link"]
    assert data["snowluma"]["edition"] in ("full", "lite")


def test_config_import_replaces_values():
    with TestClient(create_app()) as c:
        exported = json.loads(c.get("/api/config/export").text)
        exported["snowluma"]["edition"] = "lite"
        exported["link"]["trigger_mode"] = "all"
        r = c.post("/api/config/import", json=exported)
        assert r.status_code == 200
        cfg = r.json()["config"]
        assert cfg["snowluma"]["edition"] == "lite"
        assert cfg["link"]["trigger_mode"] == "all"
        # 还原
        c.put("/api/config", json={"snowluma": {"edition": "full"}, "link": {"trigger_mode": "at_or_prefix"}})


def test_config_import_rejects_duplicate_ports():
    with TestClient(create_app()) as c:
        exported = json.loads(c.get("/api/config/export").text)
        exported["app"]["port"] = exported["astrbot"]["webui_port"]
        r = c.post("/api/config/import", json=exported)
    assert r.status_code == 400
    assert "导入失败" in r.json()["error"]
