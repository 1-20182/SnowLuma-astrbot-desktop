"""残留进程清理。

真实故障（用户现场）：控制台被强杀 / 崩溃后，AstrBot 的 ``python.exe main.py`` 子进程没人收尸，
它继续占着 6185；下次启动就报「端口 6185 已被占用」，然后整个 AstrBot 起不来。

处理原则：
* **只清理自己的进程**：exe 或命令行落在 ``runtime/astrbot`` / ``runtime/snowluma`` 下面的才算
  （Windows 是反斜杠、Linux 是正斜杠，两种标记都认）；
* 别人家的程序占端口不碰，只报给用户看；
* 一律杀整棵进程树（venv 的 python 会再 exec 一个真 python，只杀父进程会留孤儿）。
  具体怎么杀由 :func:`slab.plat.kill_tree` 决定：Windows 是 ``taskkill /T /F``，
  Linux 是"进程组 SIGTERM → 超时 SIGKILL"。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths, plat
from .events import bus


def _runtime_roots() -> List[Path]:
    """两个项目的安装目录：既看 paths.RUNTIME_DIR，也看配置里指定的 install_dir。"""

    roots: List[Path] = []
    for name in ("astrbot", "snowluma"):
        d = Path(paths.RUNTIME_DIR) / name
        if d.exists():
            roots.append(d)
    try:
        from . import installer
        from .config import get_config

        cfg = get_config()
        for d in (installer.astrbot_dir(cfg), installer.snowluma_dir(cfg)):
            if Path(d).exists() and Path(d) not in roots:
                roots.append(Path(d))
    except Exception:  # noqa: BLE001 - 拿不到配置就只按运行时目录判断
        pass
    return roots


#: 路径特征：任何以 runtime\astrbot / runtime\snowluma 结尾的路径都算自家进程
#: （Windows 用反斜杠，POSIX 用正斜杠；比对前两边都会 lowercase）
_PATH_MARKERS = ("\\runtime\\astrbot", "/runtime/astrbot", "\\runtime\\snowluma", "/runtime/snowluma")


def _proc_cmd(proc: Any) -> str:
    try:
        return " ".join(proc.cmdline() or [])
    except Exception:  # noqa: BLE001
        return ""


def _proc_exe(proc: Any) -> str:
    try:
        return str(proc.exe() or "")
    except Exception:  # noqa: BLE001
        return ""


#: 只有这几种可执行文件可能是我们启的（python / node）。
#: POSIX 上 psutil 给的是 ``python3`` / ``python3.12`` / ``node``，所以用"主干名"比对。
_OUR_EXE_NAMES = ("python", "python3", "pythonw", "node", "nodejs")


def _exe_stem(name: str) -> str:
    """可执行文件名 → 主干：``python.exe`` / ``python3.12`` → ``python3``。"""

    text = (name or "").strip().lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    text = text.replace(".exe", "")
    return text.split(".")[0]


def is_ours(pid: int) -> bool:
    """这个进程是不是我们装的那两个项目启起来的。

    判据（从严，避免误杀别的程序）：
    1. 可执行文件路径里带 ``runtime/astrbot`` / ``runtime/snowluma``；
    2. 或者：进程名是 python/node（含 ``python3.12`` 这种带版本号的），
       且 **命令行或工作目录** 落在这两个目录里
       （AstrBot 的 venv python 会再 exec 一个 uv 里的真 python，exe 在 uv 目录下，
        只能靠 ``main.py`` + cwd 认出来）。
    """

    try:
        import psutil

        proc = psutil.Process(pid)
    except Exception:  # noqa: BLE001
        return False

    exe = _proc_exe(proc).lower()
    cmd = _proc_cmd(proc).lower()
    try:
        name = (proc.name() or "").lower()
    except Exception:  # noqa: BLE001
        name = ""
    try:
        cwd = str(proc.cwd()).lower()
    except Exception:  # noqa: BLE001
        cwd = ""

    if any(marker in exe for marker in _PATH_MARKERS):
        return True
    if _exe_stem(name) not in _OUR_EXE_NAMES:
        return False
    if any(marker in cmd for marker in _PATH_MARKERS):
        return True
    if any(marker in cwd for marker in _PATH_MARKERS):
        return True
    # venv 的 python 会 exec 到 uv 的 python：命令行里可能只剩 main.py
    if _exe_stem(name).startswith("python") and ("main.py" in cmd or cmd.strip().endswith("main.py")):
        for root in _runtime_roots():
            if cwd.startswith(str(root).lower()):
                return True
    return False

def port_owner(port: int) -> Optional[int]:
    """拿监听某个端口的 PID（平台差异在 plat：Windows GetExtendedTcpTable / Linux /proc）。"""

    return plat.port_owner_pid(port)


def find_orphans() -> List[Dict[str, Any]]:
    """找出占着端口、且属于我们自己的残留进程。"""

    from .config import get_config

    cfg = get_config()
    ports = {
        "astrbot": int(cfg.astrbot.webui_port),
        "astrbot_onebot": int(cfg.link.ws_port),
        "snowluma": int(cfg.snowluma.webui_port),
    }
    found: Dict[int, Dict[str, Any]] = {}
    for label, port in ports.items():
        pid = port_owner(port)
        if not pid or pid in found or pid == os.getpid():
            continue
        if not is_ours(pid):
            continue
        try:
            import psutil

            proc = psutil.Process(pid)
            name = proc.name()
            cmd = _proc_cmd(proc)[:200]
        except Exception:  # noqa: BLE001
            name, cmd = "", ""
        found[pid] = {"pid": pid, "port": port, "kind": label, "name": name, "cmdline": cmd}

    # 端口没占，但进程还在（比如 AstrBot 卡在启动阶段）
    try:
        import psutil

        for proc in psutil.process_iter(["pid"]):
            pid = proc.info["pid"]
            if pid in found or pid == os.getpid():
                continue
            if is_ours(pid):
                found[pid] = {"pid": pid, "port": 0, "kind": "no-port", "name": "",
                              "cmdline": _proc_cmd(proc)[:200]}
    except Exception:  # noqa: BLE001
        pass
    return list(found.values())


def _parent_pid(pid: int) -> int:
    try:
        import psutil

        return int(psutil.Process(pid).ppid() or 0)
    except Exception:  # noqa: BLE001
        return 0


def _parent_alive(pid: int) -> bool:
    ppid = _parent_pid(pid)
    if ppid <= 0:
        return False
    try:
        import psutil

        parent = psutil.Process(ppid)
        return parent.is_running() and parent.status() != psutil.STATUS_ZOMBIE
    except Exception:  # noqa: BLE001
        return False


def startup_cleanup() -> Dict[str, Any]:
    """控制台启动时"擦屁股"：只清理**父进程已经没了**的自家进程。

    父进程还在的（比如另一个控制台实例正开着）不动，避免两个实例互杀。
    """

    items = [i for i in find_orphans() if not _parent_alive(i["pid"])]
    killed: List[int] = []
    for item in items:
        if kill_tree(item["pid"]):
            killed.append(item["pid"])
    if killed:
        bus.log("console", f"启动自检：清理了 {len(killed)} 个上次残留的进程（{', '.join(map(str, killed))}）",
                level="warn")
    return {"ok": True, "killed": killed, "items": items}


def kill_tree(pid: int) -> bool:
    """结束整棵进程树（平台差异在 :mod:`slab.plat`）。

    Windows：``taskkill /PID <pid> /T /F``；
    POSIX：进程组 ``SIGTERM`` → 等待 → ``SIGKILL``（SnowLuma 有优雅关停路径，别一上来就强杀）。
    """

    return plat.kill_tree(pid, graceful=5.0)


def cleanup(dry_run: bool = False) -> Dict[str, Any]:
    """杀掉我们自己的残留进程；返回清单给界面用。"""

    items = find_orphans()
    try:
        from .supervisor import supervisor

        managed = supervisor.managed_pids()
    except Exception:  # noqa: BLE001
        managed = set()
    for item in items:
        item["managed"] = item["pid"] in managed
    items = [i for i in items if not i["managed"]]
    if dry_run:
        return {"ok": True, "dry_run": True, "items": items, "killed": []}
    killed: List[int] = []
    for item in items:
        if kill_tree(item["pid"]):
            killed.append(item["pid"])
    if killed:
        bus.log("console", f"已清理 {len(killed)} 个残留进程：{', '.join(map(str, killed))}", level="warn")
    return {"ok": True, "items": items, "killed": killed,
            "message": f"清理了 {len(killed)} 个残留进程" if killed else "没有发现残留进程"}


def free_port(port: int, *, label: str = "") -> Dict[str, Any]:
    """启动服务前确保端口空闲：占着的是我们自己的旧实例就杀掉，别人家的就报错。"""

    pid = port_owner(port)
    if not pid:
        return {"ok": True, "port": port}
    if is_ours(pid):
        kill_tree(pid)
        bus.log("console", f"端口 {port} 被上次没退干净的进程占用（pid={pid}），已清理", level="warn")
        return {"ok": True, "port": port, "killed": pid,
                "message": f"清理了占用 {port} 的残留进程 pid={pid}"}
    return {"ok": False, "port": port, "pid": pid,
            "message": f"端口 {port} 被其他程序占用（pid={pid}，不是本控制台启动的）：{label}"
                       "请先关掉那个程序，或在配置页换个端口。"}
