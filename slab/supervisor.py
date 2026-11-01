"""子进程 / 内部任务的服务管理（启动、停止、就绪探测、日志采集）。

* 所有服务都用同一套状态机：stopped → starting → running → stopping → stopped
* 就绪条件二选一：端口可连接 + 日志正则（两者都配则都要满足）
* Windows 下用 ``taskkill /T /F`` 杀掉整棵进程树（QQ 客户端会带子进程）
* 子进程日志逐行推入 EventBus，同时落盘到 data/logs/<service>.log
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .config import AppConfig, get_config
from .events import bus
from .procutil import hidden_console_kwargs, hidden_kwargs
from . import paths

IS_WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------- #
@dataclass
class ServiceDef:
    name: str
    display: str
    kind: str = "process"  # process | task
    argv: Optional[Callable[[AppConfig], List[str]]] = None
    cwd: Optional[Callable[[AppConfig], Path]] = None
    env: Optional[Callable[[AppConfig], Dict[str, str]]] = None
    ready_regex: str = ""
    ready_port: Optional[Callable[[AppConfig], Optional[int]]] = None
    task_factory: Optional[Callable[[], Awaitable[None]]] = None
    stop_timeout: float = 10.0


@dataclass
class ServiceState:
    name: str
    status: str = "stopped"  # stopped|starting|running|stopping|failed
    pid: Optional[int] = None
    started_at: Optional[float] = None
    stopped_at: Optional[float] = None
    exit_code: Optional[int] = None
    ready: bool = False
    restarts: int = 0
    last_error: str = ""
    ready_port: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "pid": self.pid,
            "started_at": self.started_at,
            "uptime": round(time.time() - self.started_at, 1) if self.started_at and self.status == "running" else 0,
            "exit_code": self.exit_code,
            "ready": self.ready,
            "restarts": self.restarts,
            "last_error": self.last_error,
            "ready_port": self.ready_port,
        }


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    if not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


class Supervisor:
    def __init__(self) -> None:
        self._defs: Dict[str, ServiceDef] = {}
        self._states: Dict[str, ServiceState] = {}
        self._procs: Dict[str, subprocess.Popen] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------- registry
    def register(self, d: ServiceDef) -> None:
        with self._lock:
            self._defs[d.name] = d
            self._states.setdefault(d.name, ServiceState(name=d.name))

    def definitions(self) -> Dict[str, ServiceDef]:
        return dict(self._defs)

    def state(self, name: str) -> ServiceState:
        with self._lock:
            st = self._states.setdefault(name, ServiceState(name=name))
            proc = self._procs.get(name)
            if proc is not None and st.status in ("running", "starting"):
                code = proc.poll()
                if code is not None:  # 进程已退出
                    st.status = "stopped" if code == 0 else "failed"
                    st.exit_code = code
                    st.pid = None
                    st.ready = False
                    st.stopped_at = time.time()
        return st

    def states(self) -> Dict[str, Dict[str, Any]]:
        out = {}
        for name in self._defs:
            st = self.state(name)
            d = self._defs[name]
            info = st.as_dict()
            info["display"] = d.display
            info["kind"] = d.kind
            out[name] = info
        return out

    def is_active(self, name: str) -> bool:
        st = self.state(name)
        return st.status in ("running", "starting")

    # ------------------------------------------------------------ lifecycle
    async def start(self, name: str, cfg: Optional[AppConfig] = None) -> Dict[str, Any]:
        d = self._defs.get(name)
        if d is None:
            raise KeyError(f"未知服务: {name}")
        cfg = cfg or get_config()
        st = self.state(name)
        if st.status in ("running", "starting"):
            return st.as_dict()

        # 启动前预检端口：上次被强杀留下的自家进程会一直占着端口（实测 astrbot 的 venv python
        # 会再 exec 一个真 python，只杀父进程就留孤儿，下次直接报"端口已被占用"）
        try:
            from . import orphans

            ports = []
            if name == "astrbot":
                ports = [int(cfg.astrbot.webui_port), int(cfg.link.ws_port)]
            elif name == "snowluma":
                ports = [int(cfg.snowluma.webui_port)]
            for port in ports:
                check = orphans.free_port(port, label=f"{d.display} 需要这个端口")
                if not check.get("ok"):
                    st.status = "failed"
                    st.last_error = str(check.get("message") or "")
                    bus.publish("service", name=name, status="failed", error=st.last_error)
                    raise RuntimeError(st.last_error)
        except RuntimeError:
            raise
        except Exception:  # noqa: BLE001 - 预检本身失败不要拦住启动
            pass
        st.status = "starting"
        st.ready = False
        st.exit_code = None
        st.last_error = ""
        st.started_at = time.time()
        st.stopped_at = None
        bus.publish("service", name=name, status=st.status, pid=None)
        if name == "snowluma":
            # 每次启动都把"自动注入"开关落到 runtime.json（环境变量在部分版本里不生效）
            try:
                from . import installer as _installer

                _installer.write_runtime_config(
                    _installer.snowluma_dir(cfg),
                    int(cfg.snowluma.webui_port),
                    hook_autoload=str(cfg.snowluma.hook_mode) == "auto",
                )
            except Exception as exc:  # noqa: BLE001 - 写不了就别拦着启动
                bus.log("snowluma", f"写入 runtime.json 失败（继续启动）：{exc}", level="warn")
        try:
            if d.kind == "task":
                if d.task_factory is None:
                    raise RuntimeError("任务型服务缺少 task_factory")
                self._tasks[name] = asyncio.create_task(self._run_task(d, st), name=f"svc-{name}")
            else:
                self._spawn(d, st, cfg)
        except Exception as exc:  # noqa: BLE001
            st.status = "failed"
            st.last_error = f"{type(exc).__name__}: {exc}"
            bus.publish("service", name=name, status=st.status, error=st.last_error)
            raise
        return st.as_dict()

    def _spawn(self, d: ServiceDef, st: ServiceState, cfg: AppConfig) -> None:
        assert d.argv is not None
        argv = [str(a) for a in d.argv(cfg)]
        if not argv:
            raise RuntimeError(f"{d.display} 启动命令为空（是否尚未部署？）")
        cwd = d.cwd(cfg) if d.cwd else Path.cwd()
        cwd.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "FORCE_COLOR": "0"})
        if d.env:
            env.update({k: str(v) for k, v in d.env(cfg).items()})
        if not Path(argv[0]).exists() and os.sep in argv[0]:
            raise RuntimeError(f"可执行文件不存在: {argv[0]}")

        logfile = paths.LOG_DIR / f"{d.name}.log"
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = open(logfile, "a", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(  # noqa: S603
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            # 关键：长驻服务用一个"隐藏的"控制台，既不弹黑框，孙进程也不会各自新开窗
            **hidden_console_kwargs(),
        )
        st.pid = proc.pid
        self._procs[d.name] = proc
        bus.publish("service", name=d.name, status="starting", pid=proc.pid,
                    detail=f"已启动进程 {proc.pid}: {' '.join(argv[:4])}")
        threading.Thread(target=self._pump, args=(d, st, proc, fh), daemon=True,
                         name=f"log-{d.name}").start()
        if d.ready_regex or d.ready_port:
            threading.Thread(target=self._watch_ready, args=(d, st, cfg), daemon=True,
                             name=f"ready-{d.name}").start()
        else:
            st.status = "running"
            st.ready = True
            bus.publish("service", name=d.name, status="running", pid=proc.pid)

    def _pump(self, d: ServiceDef, st: ServiceState, proc: subprocess.Popen, fh) -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                fh.write(line + "\n")
                fh.flush()
                bus.log(d.name, line)
        finally:
            fh.close()
            code = proc.wait()
            # 竞态：我们主动 stop() 时，kill 之后 _pump 可能晚于 stop() 收尾执行，
            # 此时 st.status 已经是 "stopped"，不能因为被强杀产生的 exit=1 判成 failed。
            if st.status not in ("stopping", "stopped"):
                st.status = "stopped" if code == 0 else "failed"
                if code != 0:
                    st.last_error = f"进程退出，退出码 {code}"
            st.exit_code = code
            st.stopped_at = time.time()
            st.pid = None
            st.ready = False
            bus.publish("service", name=d.name, status=st.status, pid=None, exit_code=code)

    def _watch_ready(self, d: ServiceDef, st: ServiceState, cfg: AppConfig) -> None:
        import re

        pattern = re.compile(d.ready_regex, re.IGNORECASE) if d.ready_regex else None
        port = None
        try:
            port = d.ready_port(cfg) if d.ready_port else None
        except Exception:  # noqa: BLE001
            port = None
        st.ready_port = port
        deadline = time.time() + 180
        while time.time() < deadline:
            proc = self._procs.get(d.name)
            if proc is None or proc.poll() is not None:
                return
            if pattern is None and port:
                if port_open(port):
                    self._mark_ready(d, st)
                    return
            elif pattern is not None:
                # 日志正则由 _pump 记录，这里读最近日志判断
                recent = bus.logs(d.name, 200)
                for ev in reversed(recent):
                    if pattern.search(ev.get("line", "")):
                        if port is None or port_open(port):
                            self._mark_ready(d, st)
                            return
                        break
            time.sleep(1.0)

    def _mark_ready(self, d: ServiceDef, st: ServiceState) -> None:
        st.ready = True
        st.status = "running"
        bus.publish("service", name=d.name, status="running", pid=st.pid, ready=True)

    async def _run_task(self, d: ServiceDef, st: ServiceState) -> None:
        assert d.task_factory is not None
        st.pid = None
        st.status = "running"
        st.ready = True
        bus.publish("service", name=d.name, status="running")
        try:
            await d.task_factory()
            st.status = "stopped"
        except asyncio.CancelledError:
            st.status = "stopped"
            raise
        except Exception as exc:  # noqa: BLE001
            st.status = "failed"
            st.last_error = f"{type(exc).__name__}: {exc}"
            bus.log(d.name, st.last_error, level="error")
        finally:
            st.ready = False
            st.stopped_at = time.time()
            bus.publish("service", name=d.name, status=st.status, error=st.last_error)

    async def stop(self, name: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        d = self._defs.get(name)
        st = self.state(name)
        if d is None:
            raise KeyError(f"未知服务: {name}")
        if st.status in ("stopped", "failed") and name not in self._tasks:
            return st.as_dict()
        st.status = "stopping"
        bus.publish("service", name=name, status="stopping")
        task = self._tasks.pop(name, None)
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=timeout or 5)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        proc = self._procs.pop(name, None)
        if proc is not None and proc.poll() is None:
            self._kill_tree(proc.pid)
            try:
                proc.wait(timeout=timeout or d.stop_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        st.status = "stopped"
        st.pid = None
        st.ready = False
        st.stopped_at = time.time()
        bus.publish("service", name=name, status="stopped")
        return st.as_dict()

    def managed_pids(self) -> set:
        """当前由本进程托管的 PID（清理残留时要跳过它们）。"""

        pids = set()
        for proc in self._procs.values():
            try:
                if proc.poll() is None:
                    pids.add(int(proc.pid))
            except Exception:  # noqa: BLE001
                continue
        return pids

    def _kill_tree(self, pid: int) -> None:
        try:
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=20, **hidden_kwargs())
            else:
                os.kill(pid, 15)
        except Exception as exc:  # noqa: BLE001
            bus.log("console", f"结束进程 {pid} 失败: {exc}", level="warn")

    async def restart(self, name: str, cfg: Optional[AppConfig] = None) -> Dict[str, Any]:
        await self.stop(name)
        await asyncio.sleep(0.6)
        self.state(name).restarts += 1
        return await self.start(name, cfg)

    async def stop_all(self) -> None:
        for name in list(self._defs):
            try:
                await self.stop(name)
            except Exception:  # noqa: BLE001
                pass

    async def start_all(self, cfg: Optional[AppConfig] = None, *, wait_ready: float = 45.0) -> Dict[str, str]:
        """按顺序启动：AstrBot → SnowLuma → 中继。

        顺序很重要：AstrBot 的 aiocqhttp 反向 WS 要先就绪，SnowLuma 作为 WS 客户端连上去
        才会注册成"可双向收发"的 bot；反过来（SnowLuma 先连上、AstrBot 后起）容易出现
        "能收到消息但发不回去"（aiocqhttp.ApiNotAvailable）。
        """

        cfg = cfg or get_config()
        result: Dict[str, str] = {}
        order = [n for n in ("astrbot", "snowluma", "bridge") if n in self._defs]
        order += [n for n in self._defs if n not in order]

        for name in order:
            d = self._defs[name]
            try:
                await self.start(name, cfg)
                result[name] = "started"
            except Exception as exc:  # noqa: BLE001
                result[name] = f"failed: {exc}"
                continue
            # AstrBot 起来了再等它就绪（ready 后才开始监听 6199），再拉 SnowLuma
            if name == "astrbot" and d.kind == "process":
                deadline = time.time() + max(5.0, wait_ready)
                while time.time() < deadline:
                    st = self.state("astrbot")
                    if st.ready or st.status in ("failed", "stopped"):
                        break
                    await asyncio.sleep(0.4)
                result["astrbot_wait"] = "ready" if self.state("astrbot").ready else "timeout"
        return result


supervisor = Supervisor()
