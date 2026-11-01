"""Lead 维护：下载源测速 / 自定义安装路径的部署入口 / 卸载与清理。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from slab import installer, sources
from slab.server import routes_deploy
from slab.server.app import create_app


# --------------------------------------------------------------------------- #
# 下载源与测速
# --------------------------------------------------------------------------- #
def test_sources_catalog():
    with TestClient(create_app()) as c:
        body = c.get("/api/deploy/sources").json()
    assert body["ok"] is True
    ids = [m["id"] for m in body["mirrors"]]
    assert "official" in ids and "ghfast" in ids
    assert body["current"] in ids or body["current"] == "auto"
    assert body["test_asset"].startswith("https://github.com/")
    # 镜像都是 https 前缀（官方为空）
    for m in body["mirrors"]:
        assert m["prefix"] == "" or m["prefix"].startswith("https://")


def test_mirror_rewrite():
    official = "https://github.com/a/b/releases/download/v1/x.zip"
    assert sources.rewrite(official, "official") == official
    mirrored = sources.rewrite(official, "ghfast")
    assert mirrored == "https://ghfast.top/" + official
    # 未知 id 走直连
    assert sources.rewrite(official, "nope") == official


def test_speedtest_ranks_and_recommends(monkeypatch):
    async def fake_measure(mirror, asset):  # noqa: ANN001
        speed = {"official": 0.4, "ghfast": 8.2, "ghproxy_net": 0.0}[mirror["id"]]
        return {
            **mirror,
            "url": sources.rewrite(asset, mirror["id"]),
            "ok": speed > 0,
            "mbps": speed,
            "latency_ms": 120 if speed else 0,
            "bytes": 3 * 1024 * 1024 if speed else 0,
            "error": "" if speed else "timeout",
        }

    monkeypatch.setattr(sources, "_measure", fake_measure)
    monkeypatch.setattr(sources, "MIRRORS", [
        {"id": "official", "label": "官方", "prefix": "", "note": ""},
        {"id": "ghfast", "label": "ghfast", "prefix": "https://ghfast.top/", "note": ""},
        {"id": "ghproxy_net", "label": "ghproxy", "prefix": "https://ghproxy.net/", "note": ""},
    ])
    with TestClient(create_app()) as c:
        body = c.post("/api/deploy/sources/speedtest").json()
    assert body["ok"] is True
    assert body["recommended"] == "ghfast"
    assert body["results"][0]["id"] == "ghfast"
    assert [r["id"] for r in body["results"]][-1] == "ghproxy_net"


def test_auto_mirror_picks_fastest(monkeypatch):
    async def fake_measure(mirror, asset):  # noqa: ANN001
        speed = {"official": 0.2, "ghfast": 5.0}[mirror["id"]]
        return {**mirror, "url": asset, "ok": True, "mbps": speed, "latency_ms": 80,
                "bytes": 1024, "error": ""}

    monkeypatch.setattr(sources, "_measure", fake_measure)
    monkeypatch.setattr(sources, "MIRRORS", [
        {"id": "official", "label": "官方", "prefix": "", "note": ""},
        {"id": "ghfast", "label": "ghfast", "prefix": "https://ghfast.top/", "note": ""},
    ])
    sources._AUTO_CACHE.update({"ts": 0.0, "mirror": "official", "result": None})
    from slab.config import get_config

    picked = asyncio.run(sources.pick_fastest(get_config(), force=True))
    assert picked["mirror"] == "ghfast"


def test_config_default_mirror_is_auto():
    from slab.config import AppConfig

    cfg = AppConfig()
    assert cfg.app.download_mirror == "auto"


# --------------------------------------------------------------------------- #
# 磁盘占用 / 卸载
# --------------------------------------------------------------------------- #
def test_usage_reports_sizes():
    with TestClient(create_app()) as c:
        body = c.get("/api/deploy/usage").json()
    assert body["ok"] is True
    for key in ("snowluma", "astrbot", "data", "backups", "downloads"):
        assert key in body["items"]
        assert "path" in body["items"][key] and "mb" in body["items"][key]
    assert isinstance(body["backups"], list)
    assert body["total_mb"] >= 0


def test_uninstall_purge_flag(monkeypatch):
    calls = {}

    def fake_uninstall(target, cfg=None, purge=False):  # noqa: ANN001
        calls["target"] = target
        calls["purge"] = purge
        return {"ok": True, "removed": True, "purged": purge,
                "message": f"{target} purged={purge}"}

    monkeypatch.setattr(installer, "uninstall", fake_uninstall)
    with TestClient(create_app()) as c:
        c.delete("/api/deploy/snowluma?purge=true")
    assert calls["purge"] is True

    calls.clear()
    with TestClient(create_app()) as c:
        c.delete("/api/deploy/snowluma")
    assert calls["purge"] is False


def test_uninstall_purge_removes_dir(tmp_path, monkeypatch):
    """purge=True 真删目录（含旧备份），删除后目录不存在。"""

    home = tmp_path / "snowluma"
    home.mkdir()
    (home / "index.mjs").write_text("x" * 2048, "utf-8")
    backup_root = tmp_path / "backups"
    (backup_root / "snowluma-20260101-000000").mkdir(parents=True)
    (backup_root / "snowluma-20260101-000000" / "old.bin").write_text("y" * 1024, "utf-8")

    from slab import paths
    from slab.config import AppConfig

    cfg = AppConfig()
    cfg.snowluma.install_dir = str(home)
    monkeypatch.setattr(paths, "BACKUP_DIR", backup_root)
    monkeypatch.setattr(installer, "_ensure_not_running", lambda *a, **k: None)

    result = installer.uninstall("snowluma", cfg, purge=True)
    assert result["purged"] is True
    assert not home.exists()
    assert not (backup_root / "snowluma-20260101-000000").exists()
    assert result["removed_backups"] == 1
    assert result["freed_mb"] >= 0


# --------------------------------------------------------------------------- #
# 前端接线
# --------------------------------------------------------------------------- #
def test_deploy_page_has_mirror_path_and_uninstall_ui():
    html = (Path(__file__).resolve().parents[1] / "slab" / "web" / "index.html").read_text("utf-8")
    for needle in ('id="card-mirror"', 'id="mirror-select"', 'id="mirror-test"',
                   'id="dep-snowluma-dir"', 'id="dep-astrbot-dir"',
                   'data-pick-input="dep-snowluma-dir"',
                   'id="card-uninstall"', 'id="usage-refresh"', 'id="drop-backups"'):
        assert needle in html, f"部署页缺少 {needle}"
    js = (Path(__file__).resolve().parents[1] / "slab" / "web" / "app.js").read_text("utf-8")
    for needle in ("/api/deploy/sources", "/api/deploy/sources/speedtest", "/api/deploy/usage",
                   "/api/deploy/backups/clear", "runSpeedtest", "loadUsage", "uninstallTarget", "/api/deploy/sources/speedtest"):
        assert needle in js, f"app.js 缺少 {needle}"
