"""Lead 维护：残留进程（端口被上次没退干净的实例占住）。"""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from slab.server.app import create_app

# --------------------------------------------------------------------------- #
# 残留进程清理（端口被上次没退干净的实例占住）
# --------------------------------------------------------------------------- #
def test_is_ours_ignores_foreign_processes():
    from slab import orphans

    # 自己进程、无关程序都不能被判定成"我们的"
    assert orphans.is_ours(os.getpid()) is False
    assert orphans.is_ours(999999) is False


def test_find_orphans_shape(monkeypatch):
    from slab import orphans

    monkeypatch.setattr(orphans, "port_owner", lambda port: 4242)
    monkeypatch.setattr(orphans, "is_ours", lambda pid: True)
    items = orphans.find_orphans()
    assert items and items[0]["pid"] == 4242
    assert "port" in items[0] and "kind" in items[0]


def test_cleanup_dry_run_does_not_kill(monkeypatch):
    from slab import orphans

    called = []
    monkeypatch.setattr(orphans, "find_orphans", lambda: [{"pid": 4242, "port": 6185, "kind": "astrbot"}])
    monkeypatch.setattr(orphans, "kill_tree", lambda pid: called.append(pid) or True)
    res = orphans.cleanup(dry_run=True)
    assert res["killed"] == []
    assert called == []


def test_cleanup_skips_managed(monkeypatch):
    from slab import orphans

    killed = []
    monkeypatch.setattr(orphans, "find_orphans", lambda: [{"pid": 4242, "port": 6185, "kind": "astrbot"}])
    monkeypatch.setattr(orphans, "kill_tree", lambda pid: killed.append(pid) or True)

    class FakeSup:
        def managed_pids(self):
            return {4242}

    import slab.supervisor as sup_mod

    monkeypatch.setattr(sup_mod, "supervisor", FakeSup())
    res = orphans.cleanup()
    assert res["killed"] == []
    assert killed == []


def test_free_port_reports_foreign_owner(monkeypatch):
    from slab import orphans

    monkeypatch.setattr(orphans, "port_owner", lambda port: 999)
    monkeypatch.setattr(orphans, "is_ours", lambda pid: False)
    res = orphans.free_port(6185)
    assert res["ok"] is False
    assert "不是本控制台启动的" in res["message"]


def test_free_port_kills_our_leftover(monkeypatch):
    from slab import orphans

    killed = []
    monkeypatch.setattr(orphans, "port_owner", lambda port: 4242)
    monkeypatch.setattr(orphans, "is_ours", lambda pid: True)
    monkeypatch.setattr(orphans, "kill_tree", lambda pid: killed.append(pid) or True)
    res = orphans.free_port(6185)
    assert res["ok"] is True
    assert killed == [4242]


def test_cleanup_route_and_button():
    from pathlib import Path

    with TestClient(create_app()) as c:
        body = c.post("/api/system/cleanup-orphans?dry_run=true").json()
    assert body["ok"] is True
    assert "items" in body
    html = (Path(__file__).resolve().parents[1] / "slab" / "web" / "index.html").read_text("utf-8")
    assert 'id="maint-orphans"' in html
    js = (Path(__file__).resolve().parents[1] / "slab" / "web" / "app.js").read_text("utf-8")
    assert "/api/system/cleanup-orphans" in js

# --------------------------------------------------------------------------- #
# 启动自清 + 退出清端口
# --------------------------------------------------------------------------- #
def test_startup_cleanup_only_parentless(monkeypatch):
    from slab import orphans

    killed = []
    monkeypatch.setattr(orphans, "find_orphans", lambda: [
        {"pid": 11, "port": 6185, "kind": "astrbot"},
        {"pid": 22, "port": 0, "kind": "no-port"},
    ])
    # 11 的父进程还活着（另一个控制台在跑）→ 不动；22 的父进程没了 → 清掉
    monkeypatch.setattr(orphans, "_parent_alive", lambda pid: pid == 11)
    monkeypatch.setattr(orphans, "kill_tree", lambda pid: killed.append(pid) or True)
    result = orphans.startup_cleanup()
    assert killed == [22]
    assert result["killed"] == [22]


def test_shutdown_cleans_leftovers(monkeypatch):
    """退出控制台时必须把残留进程一起清掉，否则端口一直被占。"""

    import asyncio

    from slab import orphans
    from slab.server import routes_console

    called = {"cleanup": 0, "stop_all": 0}

    async def fake_stop_all():
        called["stop_all"] += 1

    def fake_cleanup(dry_run=False):
        called["cleanup"] += 1
        return {"ok": True, "killed": [4242], "items": []}

    monkeypatch.setattr(routes_console.supervisor, "stop_all", fake_stop_all)
    monkeypatch.setattr(orphans, "cleanup", fake_cleanup)

    # 不真的退出进程
    exits = []
    monkeypatch.setattr(routes_console.os, "_exit", lambda code: exits.append(code))

    with TestClient(create_app()) as c:
        res = c.post("/api/system/shutdown")
        assert res.status_code == 200
        # 等后台任务跑完（0.5s 延迟 + 处理）
        for _ in range(50):
            if exits:
                break
            import time as _t

            _t.sleep(0.1)
    assert called["stop_all"] >= 1
    assert called["cleanup"] >= 1
    assert exits == [0]


def test_desktop_window_controls_wired():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    main = (root / "desktop" / "main.js").read_text("utf-8")
    # 关闭必须真的关窗口：统一走 quitApp，并带 9 秒兜底
    assert "function quitApp" in main
    assert "win.on('close'" in main
    assert "app.exit(0)" in main
    assert "9000" in main
    # 窗口必须可最小化/最大化
    assert "minimizable: true" in main
    assert "maximizable: true" in main
    # 最小化对隐藏窗口无效 → 要有兜底
    assert "fallback to hide()" in main
    html = (root / "slab" / "web" / "index.html").read_text("utf-8")
    assert 'id="win-max"' in html
    js = (root / "slab" / "web" / "app.js").read_text("utf-8")
    assert "winAction('maximize')" in js
    assert "winAction('minimize')" in js


def test_serve_does_startup_cleanup():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "slab" / "__main__.py").read_text("utf-8")
    assert "startup_cleanup" in src

# --------------------------------------------------------------------------- #
# 启动顺序：AstrBot 先就绪，SnowLuma 再连（否则会出现 "能收不能发"）
# --------------------------------------------------------------------------- #
def test_start_all_orders_astrbot_first(monkeypatch):
    import asyncio

    from slab import supervisor as sup_mod

    calls = []

    class FakeSup(sup_mod.Supervisor):
        async def start(self, name, cfg=None):  # noqa: ANN001
            calls.append(name)
            st = self.state(name)
            st.status = "running"
            st.ready = (name == "astrbot")
            return st.as_dict()

        def state(self, name):
            st = sup_mod.ServiceState(name=name)
            st.status = "running"
            st.ready = True
            return st

    sup = FakeSup()
    for name in ("astrbot", "snowluma"):
        sup._defs[name] = sup_mod.ServiceDef(name=name, display=name, kind="process", argv=lambda c: [])
    result = asyncio.run(sup.start_all(wait_ready=2))
    assert calls[0] == "astrbot", calls
    assert "astrbot_wait" in result
