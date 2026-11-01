"""Lead 维护：所有子进程都不得弹出可见的控制台窗口（用户反馈"部署时满屏 DOS 窗口"）。

验证方式：让子进程自己问 Windows「我的控制台窗口可见吗」，把结果写进文件。
* 短命令（git / uv / netstat / taskkill）用 ``CREATE_NO_WINDOW`` → 根本没有控制台（hwnd=0）
* 长驻服务（node.exe / python.exe）用隐藏控制台 → 有控制台但不可见（visible=0），
  这样服务自己再拉子进程时也不会各开一个黑窗
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from slab import procutil
from slab.config import AppConfig
from slab.supervisor import ServiceDef, Supervisor

pytestmark = pytest.mark.skipif(os.name != "nt", reason="仅 Windows 有控制台窗口概念")

PROBE = """\
import ctypes, pathlib, sys, time
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
hwnd = kernel32.GetConsoleWindow()
visible = 0
if hwnd:
    visible = 1 if user32.IsWindowVisible(hwnd) else 0
pathlib.Path(sys.argv[1]).write_text(f"{hwnd} {visible}", encoding="utf-8")
time.sleep(2)
"""


def _wait_file(path, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if path.exists():
            text = path.read_text("utf-8").strip()
            if text:
                return text
        time.sleep(0.1)
    return ""


def _visible_flag(text: str) -> int:
    parts = (text or "").split()
    return int(parts[1]) if len(parts) == 2 else -1


def test_hidden_kwargs_requests_no_window():
    kwargs = procutil.hidden_kwargs()
    assert kwargs["creationflags"] & procutil.CREATE_NO_WINDOW
    assert kwargs["startupinfo"].dwFlags & 0x1  # STARTF_USESHOWWINDOW
    assert kwargs["startupinfo"].wShowWindow == 0  # SW_HIDE


def test_hidden_console_kwargs_are_hidden():
    kwargs = procutil.hidden_console_kwargs()
    assert kwargs["creationflags"] & 0x10  # CREATE_NEW_CONSOLE
    assert kwargs["startupinfo"].wShowWindow == 0  # SW_HIDE


def test_run_hidden_child_has_no_console(tmp_path):
    script = tmp_path / "probe.py"
    script.write_text(PROBE, "utf-8")
    out_file = tmp_path / "win.txt"
    procutil.run_hidden([sys.executable, str(script), str(out_file)], timeout=30)
    text = _wait_file(out_file)
    assert text.split()[0] == "0", f"短命令竟然拿到了控制台窗口: {text}"


def test_supervisor_service_window_is_invisible(tmp_path):
    script = tmp_path / "probe.py"
    script.write_text(PROBE, "utf-8")
    out_file = tmp_path / "win.txt"

    sup = Supervisor()
    sup.register(ServiceDef(
        name="probe", display="Probe",
        argv=lambda cfg: [sys.executable, str(script), str(out_file)],
        cwd=lambda cfg: tmp_path,
    ))

    async def _run():
        await sup.start("probe", AppConfig())
        try:
            return await asyncio.to_thread(_wait_file, out_file)
        finally:
            await sup.stop("probe")

    assert _visible_flag(asyncio.run(_run())) == 0, "被控制台启动的服务弹出了可见黑框"


def test_popen_hidden_forwards_flags(monkeypatch):
    captured = {}

    class FakeProc:
        pid = 1

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(procutil.subprocess, "Popen", fake_popen)
    procutil.popen_hidden(["cmd", "/c", "echo", "hi"], stdout=None)
    assert captured["creationflags"] & procutil.CREATE_NO_WINDOW
