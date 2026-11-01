"""Lead 维护：首次启动向导 + 端口避让 + 日志清理 + 多语言接线。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from slab import housekeeping, paths
from slab.server import routes_console
from slab.server.app import create_app

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "slab" / "web"


# --------------------------------------------------------------------------- #
# 端口：health 暴露真实端口 / 冲突自动避让
# --------------------------------------------------------------------------- #
def test_health_reports_runtime_port():
    with TestClient(create_app()) as c:
        body = c.get("/api/health").json()
    assert "port" in body and "configured_port" in body
    assert "port_shifted" in body


def test_fix_ports_avoids_busy_ports(monkeypatch):
    busy = {8790, 6199}

    monkeypatch.setattr(routes_console, "port_open", lambda port, *a, **k: int(port) in busy)
    with TestClient(create_app()) as c:
        before = c.get("/api/state").json()["config"]
        res = c.post("/api/system/fix-ports").json()
        assert res["ok"] is True
        assert res["changes"], "应该报告至少两处端口冲突"
        fields = {ch["field"] for ch in res["changes"]}
        assert "app.port" in fields and "link.ws_port" in fields
        assert res["restart_required"] is True
        new_cfg = res["config"]
        assert new_cfg["app"]["port"] not in busy
        assert new_cfg["link"]["ws_port"] not in busy
        # 恢复原配置，别影响其它用例
        c.put("/api/config", json={
            "app": {"port": before["app"]["port"]},
            "astrbot": {"webui_port": before["astrbot"]["webui_port"]},
            "snowluma": {"webui_port": before["snowluma"]["webui_port"]},
            "link": {"ws_port": before["link"]["ws_port"]},
        })


def test_fix_ports_noop_when_free(monkeypatch):
    monkeypatch.setattr(routes_console, "port_open", lambda port, *a, **k: False)
    with TestClient(create_app()) as c:
        res = c.post("/api/system/fix-ports").json()
    assert res["ok"] is True
    assert res["changes"] == []
    assert "无需调整" in res["message"]


# --------------------------------------------------------------------------- #
# 日志清理
# --------------------------------------------------------------------------- #
def _old_file(path: Path, days: int = 10) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * 128, "utf-8")
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_cleanup_removes_old_logs_and_downloads():
    old_log = paths.LOG_DIR / "test-old.log"
    new_log = paths.LOG_DIR / "test-new.log"
    old_pkg = paths.DOWNLOAD_DIR / "test-old.zip"
    _old_file(old_log)
    _old_file(new_log, days=0)
    _old_file(old_pkg)

    report = housekeeping.cleanup_all(log_days=7, download_days=7)
    assert not old_log.exists()
    assert not old_pkg.exists()
    assert new_log.exists()
    assert report.freed_bytes > 0
    new_log.unlink(missing_ok=True)


def test_cleanup_endpoint_and_preview():
    stale = paths.LOG_DIR / "test-endpoint.log"
    _old_file(stale)
    with TestClient(create_app()) as c:
        preview = c.get("/api/system/cleanup/preview").json()
        assert preview["ok"] is True
        res = c.post("/api/system/cleanup").json()
    assert res["ok"] is True
    assert res["removed_count"] >= 1
    assert not stale.exists()


def test_huge_log_is_truncated():
    big = paths.LOG_DIR / "test-huge.log"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"A" * (21 * 1024 * 1024))
    housekeeping._rotate_huge_log(big)
    assert big.stat().st_size < 2 * 1024 * 1024
    big.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 向导 / 多语言：静态资源与接线
# --------------------------------------------------------------------------- #
def test_wizard_markup_and_script():
    html = (WEB / "index.html").read_text("utf-8")
    for needle in ('id="wizard"', 'data-pane="1"', 'data-pane="4"', 'id="wiz-install"',
                   'id="wiz-next"', 'id="wiz-checks"', 'wizard.js?v={{V}}'):
        assert needle in html, f"缺少向导元素 {needle}"
    js = (WEB / "wizard.js").read_text("utf-8")
    assert "slab-wizard-done" in js
    assert "/api/deploy/both" in js
    assert "/api/system/fix-ports" in js
    assert "/api/link/apply" in js


def test_i18n_wired():
    html = (WEB / "index.html").read_text("utf-8")
    assert 'id="btn-lang"' in html
    assert 'i18n.js?v={{V}}' in html and 'i18n-extra.js?v={{V}}' in html
    assert "slabI18n" in html
    i18n = (WEB / "i18n.js").read_text("utf-8")
    assert "window.slabI18n" in i18n
    assert "MutationObserver" in i18n


def test_maintenance_card_and_fields():
    html = (WEB / "index.html").read_text("utf-8")
    assert 'id="card-maint"' in html
    assert 'data-cfg="app.log_retain_days"' in html
    assert 'data-cfg="app.download_retain_days"' in html
    assert 'id="maint-cleanup"' in html
    js = (WEB / "app.js").read_text("utf-8")
    assert "/api/system/cleanup" in js
    assert "/api/system/fix-ports" in js
