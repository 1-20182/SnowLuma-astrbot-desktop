from fastapi.testclient import TestClient

from slab.server.app import create_app


def _client():
    return TestClient(create_app())


def test_health():
    with _client() as c:
        r = c.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["app"].startswith("SnowLuma")


def test_state_shape():
    with _client() as c:
        body = c.get("/api/state").json()
    assert body["ok"] is True
    assert "config" in body and "services" in body and "env" in body
    assert body["config"]["link"]["token"] == "***"
    assert body["env"]["tools"]["python"]["ok"] is True


def test_config_update_and_mask():
    with _client() as c:
        r = c.put("/api/config", json={"astrbot": {"webui_port": 6285}, "link": {"trigger_mode": "all"}})
        assert r.status_code == 200
        cfg = r.json()["config"]
        assert cfg["astrbot"]["webui_port"] == 6285
        assert cfg["link"]["trigger_mode"] == "all"
        # 恢复
        c.put("/api/config", json={"astrbot": {"webui_port": 6185}, "link": {"trigger_mode": "at_or_prefix"}})


def test_unknown_service_404():
    with _client() as c:
        r = c.post("/api/services/notexist/start")
    assert r.status_code == 404
    assert r.json()["ok"] is False


def test_logs_endpoint():
    with _client() as c:
        body = c.get("/api/logs?limit=10").json()
    assert body["ok"] is True
    assert isinstance(body["logs"], list)
    assert isinstance(body["services"], list)


def test_index_served():
    with _client() as c:
        r = c.get("/")
    assert r.status_code in (200, 500)
