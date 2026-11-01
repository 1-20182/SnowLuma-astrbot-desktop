"""Lead 维护：桌面窗口（无边框 + 长按电源键）+ 启动性能优化。"""

from __future__ import annotations

import time
from pathlib import Path

from slab import envcheck

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "slab" / "web" / "index.html"
APP_JS = ROOT / "slab" / "web" / "app.js"
MAIN = ROOT / "desktop" / "main.js"
PRELOAD = ROOT / "desktop" / "preload.js"


# --------------------------------------------------------------------------- #
# 窗口：无边框 + 自定义关闭
# --------------------------------------------------------------------------- #
def test_main_window_is_frameless():
    src = MAIN.read_text("utf-8")
    assert "frame: false" in src
    assert "win:action" in src
    for action in ("minimize", "maximize", "pin", "quit"):
        assert f"case '{action}'" in src


def test_preload_exposes_win_action():
    assert "winAction" in PRELOAD.read_text("utf-8")


def test_ui_has_power_controls():
    html = INDEX.read_text("utf-8")
    for needle in ('id="win-controls"', 'id="win-min"', 'id="win-pin"',
                   'id="win-power"', 'id="power-dialog"', 'id="power-quit"', 'id="power-min"'):
        assert needle in html
    js = APP_JS.read_text("utf-8")
    for needle in ("bindWinControls", "bindLongPress", "power-dialog", "winAction"):
        assert needle in js
    # 长按时间：1 秒，给足误触保护
    assert "bindLongPress($('#win-power'), 1000" in js


# --------------------------------------------------------------------------- #
# 启动性能：端口探测从"每端口一次 netstat+tasklist"改成"一次系统调用"
# --------------------------------------------------------------------------- #
def test_listen_map_is_fast():
    start = time.perf_counter()
    mapping = envcheck._listen_map()
    elapsed = time.perf_counter() - start
    assert isinstance(mapping, dict)
    # netstat 老实现要 ~1.2s；新实现走 GetExtendedTcpTable，几十毫秒内
    assert elapsed < 0.35, f"端口表太慢：{elapsed:.2f}s"


def test_check_ports_is_cached_and_correct():
    ports = {"console": 65530, "astrbot_webui": 65531}
    envcheck._PORTS_CACHE.update({"ts": 0.0, "key": "", "data": None})
    start = time.perf_counter()
    first = envcheck.check_ports(ports)
    cold = time.perf_counter() - start
    start = time.perf_counter()
    second = envcheck.check_ports(ports)
    warm = time.perf_counter() - start
    assert first == second
    assert warm < 0.02, f"缓存没生效：{warm * 1000:.1f}ms"
    assert cold < 0.35, f"冷启动太慢：{cold * 1000:.0f}ms"
    for label, info in first.items():
        assert info["port"] == ports[label]
        assert isinstance(info["in_use"], bool)


def test_env_report_disk_cache_roundtrip(tmp_path, monkeypatch):
    """自检结果落盘：下次启动不用等 git/uv/node 探测。"""

    monkeypatch.setattr(envcheck, "_cache_file", lambda: tmp_path / "cache" / "env.json")
    envcheck._write_disk_cache({"ok": True, "tools": {}, "installed": {"snowluma": True}})
    loaded = envcheck._read_disk_cache()
    assert loaded and loaded["ok"] is True
    assert loaded["installed"]["snowluma"] is True


def test_state_endpoint_is_fast():
    from fastapi.testclient import TestClient

    from slab.server.app import create_app

    with TestClient(create_app()) as c:
        c.get("/api/state")           # 预热自检缓存
        start = time.perf_counter()
        res = c.get("/api/state")
        elapsed = time.perf_counter() - start
    assert res.status_code == 200
    assert elapsed < 0.35, f"/api/state 太慢：{elapsed * 1000:.0f}ms"

def test_splash_shows_progress_and_tips():
    splash = (ROOT / "desktop" / "splash.html").read_text("utf-8")
    for needle in ('id="tick"', 'id="tip"', 'id="phase"', "setInterval"):
        assert needle in splash
    assert "杀软" in splash  # 告知用户首次启动慢的原因
