import asyncio
import socket
import sys
import time
from pathlib import Path

import pytest
from types import SimpleNamespace

from slab.config import AppConfig
from slab.supervisor import ServiceDef, Supervisor, port_open


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait(pred, timeout=15.0, interval=0.1):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return False


@pytest.mark.asyncio
async def test_process_lifecycle_and_logs(tmp_path):
    sup = Supervisor()
    port = _free_port()
    script = (
        "import http.server,socketserver,sys\n"
        f"httpd=socketserver.TCPServer(('127.0.0.1',{port}),http.server.SimpleHTTPRequestHandler)\n"
        "print('serving', flush=True)\n"
        f"print('listening on {port}', flush=True)\n"
        "httpd.serve_forever()\n"
    )
    script_path = tmp_path / "srv.py"
    script_path.write_text(script, "utf-8")

    sup.register(ServiceDef(
        name="fake", display="Fake",
        argv=lambda cfg: [sys.executable, str(script_path)],
        cwd=lambda cfg: tmp_path,
        ready_regex=r"listening on",
        ready_port=lambda cfg: port,
        stop_timeout=8,
    ))
    cfg = AppConfig()
    await sup.start("fake", cfg)
    assert sup.state("fake").status in ("starting", "running")
    assert _wait(lambda: sup.state("fake").ready), "服务未在超时时间内就绪"
    assert port_open(port)

    await sup.stop("fake")
    assert sup.state("fake").status == "stopped"
    assert _wait(lambda: not port_open(port), timeout=5)


@pytest.mark.asyncio
async def test_missing_executable_raises(tmp_path):
    sup = Supervisor()
    sup.register(ServiceDef(name="bad", display="Bad",
                            argv=lambda cfg: [str(tmp_path / "nope.exe")],
                            cwd=lambda cfg: tmp_path))
    with pytest.raises(RuntimeError):
        await sup.start("bad", AppConfig())
    assert sup.state("bad").status == "failed"


@pytest.mark.asyncio
async def test_intentional_stop_is_not_failed(tmp_path):
    """强制结束进程后不能把状态判成 failed（回归：真机联调时 SnowLuma 被停后显示"异常退出"）。"""

    sup = Supervisor()
    script = tmp_path / "loop.py"
    script.write_text("import time\nwhile True:\n    time.sleep(0.2)\n", "utf-8")
    sup.register(ServiceDef(
        name="loop", display="Loop",
        argv=lambda cfg: [sys.executable, str(script)],
        cwd=lambda cfg: tmp_path,
        stop_timeout=5,
    ))
    await sup.start("loop", AppConfig())
    assert _wait(lambda: sup.state("loop").status == "running", timeout=10)
    await sup.stop("loop")
    time.sleep(0.5)  # 给 _pump 线程收尾的机会
    st = sup.state("loop")
    assert st.status == "stopped", st.as_dict()
    assert st.last_error == ""


@pytest.mark.asyncio
async def test_task_service_lifecycle():
    sup = Supervisor()
    stop_evt = asyncio.Event()

    async def _worker():
        await stop_evt.wait()

    sup.register(ServiceDef(name="task", display="Task", kind="task", task_factory=_worker))
    await sup.start("task", AppConfig())
    await asyncio.sleep(0.1)
    st = sup.state("task")
    assert st.status == "running" and st.ready
    await sup.stop("task")
    assert sup.state("task").status == "stopped"


@pytest.mark.asyncio
async def test_state_reports_exited_process():
    sup = Supervisor()
    sup.register(ServiceDef(name="quick", display="Quick",
                            argv=lambda cfg: [sys.executable, "-c", "print('bye')"]))
    await sup.start("quick", AppConfig())
    assert _wait(lambda: sup.state("quick").status in ("stopped", "failed"), timeout=20)
    st = sup.state("quick")
    assert st.exit_code == 0
    assert st.pid is None


@pytest.mark.asyncio
async def test_start_all_waits_until_bridge_connects(monkeypatch):
    """「一键启动全部」要等中继真的连上 AstrBot/SnowLuma 才返回，界面才不用手动刷新。"""

    import slab.bridge as bridge_mod

    sup = Supervisor()
    stop_evt = asyncio.Event()

    async def _worker():
        await stop_evt.wait()

    sup.register(ServiceDef(name="bridge", display="中继", kind="task", task_factory=_worker))
    cfg = AppConfig()
    cfg.normalise()
    cfg.link.mode = "bridge"

    stub = SimpleNamespace(
        upstream=SimpleNamespace(connected=False),
        downstream=SimpleNamespace(connected=False),
    )
    monkeypatch.setattr(bridge_mod, "_bridge", stub, raising=False)

    async def _connect_later():
        await asyncio.sleep(0.5)
        stub.upstream.connected = True
        stub.downstream.connected = True

    connector = asyncio.create_task(_connect_later())
    result = await sup.start_all(cfg, wait_ready=10)
    await connector
    assert result["bridge_wait"] == "connected"
    await sup.stop("bridge")
    stop_evt.set()
