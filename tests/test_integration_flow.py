"""Lead 维护的跨模块集成测试：把路由、配置、状态拼在一起验，不重复单模块用例。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from slab import bridge
from slab.server.app import create_app


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:
        yield c


def test_apply_returns_200_when_only_link_not_ready(monkeypatch):
    """配置写入成功、只是链路没就绪（服务没启动）时，不应把整体判成 400 错误。"""

    async def fake_apply(cfg, restart=True, require_provider=True):  # noqa: ANN001
        return {
            "ok": False,
            "mode": "native",
            "steps": [
                {"step": "apply_astrbot", "ok": True, "detail": "已写入"},
                {"step": "apply_snowluma_native", "ok": True, "detail": "已写入"},
                {"step": "verify", "ok": False,
                 "detail": "配置已写入，但链路尚未就绪（见 suggestions）"},
            ],
            "verification": {"ready": False, "suggestions": ["点击「启动 SnowLuma」"]},
        }

    monkeypatch.setattr(bridge, "apply_link", fake_apply)
    with TestClient(create_app()) as c:
        r = c.post("/api/link/apply", json={"restart": False})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["ready"] is False
    assert body["suggestions"] == ["点击「启动 SnowLuma」"]


def test_apply_still_fails_on_real_step_error(monkeypatch):
    """真正的步骤失败（例如没填 API Key）必须还是 400。"""

    async def fake_apply(cfg, restart=True, require_provider=True):  # noqa: ANN001
        return {
            "ok": False,
            "steps": [
                {"step": "apply_astrbot", "ok": False, "detail": "尚未填写大模型 API Key"},
                {"step": "verify", "ok": False, "detail": "未就绪"},
            ],
            "verification": {"ready": False},
        }

    monkeypatch.setattr(bridge, "apply_link", fake_apply)
    with TestClient(create_app()) as c:
        r = c.post("/api/link/apply", json={})
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert "API Key" in r.json()["error"]


def test_state_does_not_leak_secrets(client):
    body = client.get("/api/state").json()
    cfg = body["config"]
    assert cfg["astrbot"]["webui_password"] == "***"
    assert cfg["snowluma"]["webui_password"] == "***"
    assert cfg["link"]["token"] == "***"
    assert cfg["astrbot"]["platform"]["ws_reverse_token"] == "***"


def test_link_status_shape(client):
    body = client.get("/api/link/status").json()
    assert body["ok"] is True
    for key in ("snowluma", "astrbot", "onebot", "bridge"):
        assert key in body
    assert "mode" in body
    assert isinstance(body.get("suggestions"), list)


def test_config_port_change_propagates_to_both_sides(client):
    r = client.put("/api/config", json={"link": {"ws_port": 6299}})
    cfg = r.json()["config"]
    assert cfg["astrbot"]["platform"]["ws_reverse_port"] == 6299
    assert cfg["snowluma"]["onebot_url"].endswith(":6299/ws")
    # 还原，避免影响其它用例
    client.put("/api/config", json={"link": {"ws_port": 6199}})


def test_events_sse_hello(client):
    with client.stream("GET", "/api/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        line = next(resp.iter_lines())
        assert line.startswith("data: ")
        assert '"type": "hello"' in line or '"type":"hello"' in line


def test_shutdown_endpoint_stops_everything(monkeypatch):
    """打包版靠这个接口"退出"：返回 ok 后停止服务并结束进程（测试里替换成记录调用）。"""

    import os
    import time as _time

    called: dict = {}
    monkeypatch.setattr(os, "_exit", lambda code=0: called.setdefault("code", code), raising=False)
    # 退出流程里会扫"残留进程"并 taskkill，测试环境必须挡住：
    # 否则跑一次 pytest 就会把开发者本机正在运行的 SnowLuma / AstrBot 一起杀掉。
    import slab.orphans as _orphans

    monkeypatch.setattr(_orphans, "cleanup", lambda *a, **kw: {"ok": True, "items": [], "killed": []})
    with TestClient(create_app()) as c:
        r = c.post("/api/system/shutdown")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        _time.sleep(1.6)  # 等后台任务把 stop_all + exit 走完
    assert called.get("code") == 0
