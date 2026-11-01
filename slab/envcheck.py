"""环境自检：外部命令、运行时版本、磁盘空间、端口占用。"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from . import paths
from .procutil import hidden_kwargs
from .supervisor import port_open

CMD_TIMEOUT = 8

# env_report 会 fork 若干子进程（git/uv/node...），比较慢；缓存一小段时间避免拖慢首屏
_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
CACHE_TTL = 12.0


_DISK_CACHE: Dict[str, Any] = {"loaded": False, "refreshing": False}


def _cache_file():
    return paths.DATA_DIR / "cache" / "env.json"


def _read_disk_cache() -> Optional[Dict[str, Any]]:
    """跨进程复用上次的自检结果（首屏不用等 git/uv/node 探测）。"""

    try:
        path = _cache_file()
        if not path.is_file():
            return None
        data = json.loads(path.read_text("utf-8"))
        if not isinstance(data, dict):
            return None
        if time.time() - float(data.get("_ts") or 0) > 7 * 86400:
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


def _write_disk_cache(data: Dict[str, Any]) -> None:
    try:
        path = _cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**data, "_ts": time.time()}, ensure_ascii=False), "utf-8")
    except OSError:
        pass


def _refresh_async() -> None:
    """后台刷新：先返回旧结果，算完再替换（不会让首屏等子进程）。"""

    with _CACHE_LOCK:
        if _DISK_CACHE["refreshing"]:
            return
        _DISK_CACHE["refreshing"] = True

    def _work() -> None:
        try:
            data = env_report()
            with _CACHE_LOCK:
                _CACHE["ts"] = time.time()
                _CACHE["data"] = data
            _write_disk_cache(data)
        except Exception:  # noqa: BLE001
            pass
        finally:
            with _CACHE_LOCK:
                _DISK_CACHE["refreshing"] = False

    threading.Thread(target=_work, name="env-refresh", daemon=True).start()


def env_report_cached(ttl: float = CACHE_TTL) -> Dict[str, Any]:
    now = time.time()
    with _CACHE_LOCK:
        data = _CACHE["data"]
        fresh = data is not None and (now - _CACHE["ts"]) < ttl
        if fresh:
            return dict(data or {})
    if data is not None:
        # 有旧数据：立刻返回，后台更新（典型场景：每 2 秒轮询状态）
        _refresh_async()
        return dict(data)
    disk = _read_disk_cache()
    if disk:
        with _CACHE_LOCK:
            _CACHE["ts"] = float(disk.get("_ts") or 0)
            _CACHE["data"] = disk
        _refresh_async()
        return dict(disk)
    # 首次运行：同步算一次（并行探测 ~0.5s），拿到就落盘
    data = env_report()
    with _CACHE_LOCK:
        _CACHE["ts"] = time.time()
        _CACHE["data"] = data
    _write_disk_cache(data)
    return dict(data)


def invalidate_cache() -> None:
    with _CACHE_LOCK:
        _CACHE["ts"] = 0.0
        _CACHE["data"] = None


def _run(argv: List[str], cwd: Path | None = None) -> str:
    try:
        p = subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            text=True,
            timeout=CMD_TIMEOUT,
            cwd=str(cwd) if cwd else None,
            encoding="utf-8",
            errors="replace",
            **hidden_kwargs(),
        )
        out = (p.stdout or "").strip() or (p.stderr or "").strip()
        return out.splitlines()[0] if out else ""
    except Exception as exc:  # noqa: BLE001
        return f"<失败: {exc.__class__.__name__}>"


def probe_tool(name: str, args: List[str]) -> Dict[str, Any]:
    exe = shutil.which(name)
    info: Dict[str, Any] = {"name": name, "path": exe or "", "ok": bool(exe), "version": ""}
    if exe:
        info["version"] = _run([exe] + args)
    return info


def free_space_gb(path: Path) -> float:
    try:
        usage = shutil.disk_usage(str(path))
        return round(usage.free / (1024**3), 1)
    except OSError:
        return -1.0


def env_report() -> Dict[str, Any]:
    """返回给前端的环境自检报告。"""

    # 五个 --version 子进程并行跑：串行要 ~800ms，并行 ~150ms
    spec = {
        "python": ["--version"],
        "git": ["--version"],
        "uv": ["--version"],
        "node": ["--version"],
        "pnpm": ["--version"],
    }
    tools: Dict[str, Any] = {}
    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=5, thread_name_prefix="envprobe") as pool:
            futures = {name: pool.submit(probe_tool, name, args) for name, args in spec.items()}
            for name, fut in futures.items():
                try:
                    tools[name] = fut.result(timeout=12)
                except Exception as exc:  # noqa: BLE001
                    tools[name] = {"ok": False, "version": "", "error": str(exc)}
    except Exception:  # noqa: BLE001 - 兜底：退回串行
        tools = {name: probe_tool(name, args) for name, args in spec.items()}
    report: Dict[str, Any] = {
        "platform": platform.platform(),
        "os": os.name,
        "arch": platform.machine(),
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "app_root": str(paths.APP_ROOT),
        "data_dir": str(paths.DATA_DIR),
        "runtime_dir": str(paths.RUNTIME_DIR),
        "free_space_gb": free_space_gb(paths.DATA_DIR),
        "tools": tools,
        "warnings": [],
        "installed": {
            "snowluma": (paths.RUNTIME_DIR / "snowluma").exists(),
            "astrbot": (paths.RUNTIME_DIR / "astrbot").exists(),
        },
    }
    if not tools["git"]["ok"]:
        report["warnings"].append("未检测到 git：AstrBot 需要 git 克隆，或可改用离线包")
    if report["free_space_gb"] < 3:
        report["warnings"].append(f"磁盘剩余空间仅 {report['free_space_gb']} GB，建议至少 3 GB")
    if os.name != "nt":
        report["warnings"].append("当前非 Windows 系统：SnowLuma 的 QQ 客户端接入仅支持 Windows")
    return report


def port_owner(port: int) -> str:
    """返回占用端口的进程信息（尽力而为，仅 Windows 有效）。"""

    if os.name != "nt":
        return ""
    try:
        out = subprocess.run(  # noqa: S603
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace", **hidden_kwargs(),
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
                pid = parts[-1]
                name = _run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]).replace('"', "")
                return f"PID {pid} {name.split(',')[0] if ',' in name else name}"
    except Exception:  # noqa: BLE001
        pass
    return ""


_PORTS_CACHE: Dict[str, Any] = {"ts": 0.0, "key": "", "data": None}
_PORTS_TTL = 3.0


def _listen_map_ctypes() -> Dict[int, str]:
    """用 GetExtendedTcpTable 拿监听端口→PID（几毫秒，比 netstat 快两个数量级）。"""

    if os.name != "nt":
        return {}
    try:
        import ctypes
        from ctypes import wintypes

        AF_INET, TCP_TABLE_OWNER_PID_LISTENER = 2, 3
        class MIB_TCPROW_OWNER_PID(ctypes.Structure):
            _fields_ = [("dwState", wintypes.DWORD), ("dwLocalAddr", wintypes.DWORD),
                        ("dwLocalPort", wintypes.DWORD), ("dwRemoteAddr", wintypes.DWORD),
                        ("dwRemotePort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD)]

        iphlpapi = ctypes.WinDLL("iphlpapi.dll")
        size = wintypes.DWORD(0)
        iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET,
                                     TCP_TABLE_OWNER_PID_LISTENER, 0)
        if not size.value:
            return {}
        buf = ctypes.create_string_buffer(size.value)
        ret = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, AF_INET,
                                           TCP_TABLE_OWNER_PID_LISTENER, 0)
        if ret != 0:
            return {}
        count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
        rows = ctypes.cast(ctypes.byref(buf, ctypes.sizeof(wintypes.DWORD)),
                           ctypes.POINTER(MIB_TCPROW_OWNER_PID))
        result: Dict[int, str] = {}
        for i in range(count):
            row = rows[i]
            port = ((row.dwLocalPort & 0xFF) << 8) | ((row.dwLocalPort >> 8) & 0xFF)
            result[int(port)] = str(row.dwOwningPid)
        return result
    except Exception:  # noqa: BLE001 - 任何异常都退回 netstat
        return {}


def _listen_map() -> Dict[int, str]:
    """一次 netstat 拿到所有 LISTENING 的 port → PID（原来每个端口跑一次，4 个端口就 1.3s）。"""

    fast = _listen_map_ctypes()
    if fast:
        return fast
    result: Dict[int, str] = {}
    if os.name != "nt":
        return result
    try:
        out = subprocess.run(  # noqa: S603
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace", **hidden_kwargs(),
        ).stdout
    except Exception:  # noqa: BLE001
        return result
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3] != "LISTENING":
            continue
        local = parts[1]
        if ":" not in local:
            continue
        try:
            port = int(local.rsplit(":", 1)[1])
        except ValueError:
            continue
        result[port] = parts[-1]
    return result


def _process_names(pids: Iterable[str]) -> Dict[str, str]:
    """一次 tasklist 拿全部进程名（原来是每个 PID 跑一次 tasklist）。"""

    wanted = {int(p) for p in pids if str(p).isdigit()}
    if not wanted:
        return {}
    names: Dict[str, str] = {}
    try:  # psutil 查名字是毫秒级；tasklist 要 1.5s，别用
        import psutil

        for pid in wanted:
            try:
                names[str(pid)] = psutil.Process(pid).name()
            except Exception:  # noqa: BLE001 - 进程可能已退出
                continue
    except ImportError:
        return {}
    return names


def check_ports(ports: Dict[str, int]) -> Dict[str, Any]:
    key = ",".join(f"{k}={v}" for k, v in sorted(ports.items()))
    now = time.time()
    if _PORTS_CACHE["data"] is not None and _PORTS_CACHE["key"] == key and (now - _PORTS_CACHE["ts"]) < _PORTS_TTL:
        return dict(_PORTS_CACHE["data"])  # type: ignore[arg-type]

    out: Dict[str, Any] = {}
    owners = _listen_map()
    used_pids = [owners[p] for p in ports.values() if p in owners]
    names = _process_names(used_pids) if used_pids else {}
    for label, port in ports.items():
        # owners 已经覆盖所有监听端口；port_open 单次 ~400ms，只在拿不到表时兜底
        used = port in owners or (not owners and port_open(port))
        pid = owners.get(port, "")
        owner = f"PID {pid} {names.get(pid, '')}".strip() if pid else (port_owner(port) if used else "")
        out[label] = {"port": port, "in_use": used, "owner": owner}
    _PORTS_CACHE.update({"ts": now, "key": key, "data": out})
    return out
