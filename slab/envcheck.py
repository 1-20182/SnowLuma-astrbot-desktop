"""环境自检：外部命令、运行时版本、磁盘空间、端口占用。

平台差异**只在** :mod:`slab.plat` 里判断；这里只负责"检查什么"和"怎么把结论讲给用户看"。
Linux 上还会检查 QQ 注入的前置条件（ptrace_scope / node 的 cap_sys_ptrace / 图形会话），
一条都不满足也不抛异常——拿不到就记成"未知"（``ok: None``）。
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import paths, plat
from .procutil import hidden_kwargs
from .supervisor import port_open

CMD_TIMEOUT = 8

#: POSIX 上 ``python`` 往往只是 ``python3`` 的软链、甚至根本没装
#: （Ubuntu 24.04 默认只有 python3），环境自检要按这个顺序找，
#: 否则 Linux 上会误报"未检测到 Python"。
_TOOL_ALIASES: Dict[str, Tuple[str, ...]] = {"python": ("python", "python3", "python3.12")}

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
    exe = ""
    for candidate in _TOOL_ALIASES.get(name, (name,)):
        exe = shutil.which(candidate) or ""
        if exe:
            break
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


# --------------------------------------------------------------------------- #
# 版本判定（Python / Node）
# --------------------------------------------------------------------------- #
def _parse_version(text: Any) -> Tuple[int, ...]:
    """把 ``v24.22.1`` / ``Python 3.12.3`` 之类的字符串拆成 (24, 22, 1)；认不出返回 ()。"""

    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", str(text or ""))
    if not match:
        return ()
    return tuple(int(group) for group in match.groups() if group is not None)


def min_python() -> Tuple[int, int]:
    """AstrBot 要求的最低 Python（与 ``installer.ASTRBOT_MIN_PYTHON`` 对齐）。"""

    try:
        from .installer import ASTRBOT_MIN_PYTHON

        return (int(ASTRBOT_MIN_PYTHON[0]), int(ASTRBOT_MIN_PYTHON[1]))
    except Exception:  # noqa: BLE001 - installer 拿不到就按文档口径
        return (3, 12)


def python_supported(version_text: str = "") -> bool:
    """Python 是否满足 AstrBot 的下限（默认 3.12+）。"""

    parts = _parse_version(version_text or platform.python_version())
    need = min_python()
    return len(parts) >= 2 and tuple(parts[:2]) >= need


def node_required() -> str:
    """SnowLuma lite 版要求的 Node 版本区间文本。"""

    try:
        from .installer import NODE_REQUIRED

        return str(NODE_REQUIRED)
    except Exception:  # noqa: BLE001
        return "^24.22.1 || >=26.12.1"


def node_version_text(raw: Any) -> str:
    """``node --version`` 的输出 → ``24.22.1``（拿不到返回空串）。"""

    match = re.search(r"v?(\d+\.\d+\.\d+)", str(raw or ""))
    return match.group(1) if match else ""


def node_supported(version: str) -> bool:
    """Node 版本是否满足 ``^24.22.1 || >=26.12.1``（判定复用 installer，保持一致）。"""

    if not version:
        return False
    try:
        from .installer import node_version_supported

        return bool(node_version_supported(version))
    except Exception:  # noqa: BLE001 - 退化判定：主版本够新就算过
        parts = _parse_version(version)
        return bool(parts) and (parts[0] > 24 or (parts[0] == 24 and tuple(parts[:3]) >= (24, 22, 1)))


def bundled_node() -> Path:
    """SnowLuma full 版自带的 node 可执行文件路径（不存在也没关系，调用方会退 PATH）。"""

    return paths.RUNTIME_DIR / "snowluma" / plat.node_bin_name()


def node_state() -> Dict[str, Any]:
    """Node 版本状态：优先 SnowLuma full 包自带的 node，其次 PATH 上的 node。"""

    candidates: List[str] = []
    bundled = bundled_node()
    if bundled.is_file():
        candidates.append(str(bundled))
    which = shutil.which("node")
    if which:
        candidates.append(which)
    for exe in candidates:
        version = node_version_text(_run([exe, "--version"]))
        if version:
            return {"exe": exe, "version": version, "ok": node_supported(version)}
    return {"exe": candidates[0] if candidates else "", "version": "", "ok": False}


def venv_ok() -> Optional[bool]:
    """``python3 -m venv`` 能不能用（AstrBot 要建 venv 装依赖；Ubuntu 上常见缺 python3-venv）。"""

    if plat.IS_WINDOWS:
        return True
    exe = shutil.which("python3") or sys.executable
    try:
        proc = subprocess.run(  # noqa: S603
            [exe, "-c", "import venv, ensurepip"],
            capture_output=True,
            timeout=CMD_TIMEOUT,
            **hidden_kwargs(),
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001
        return None


def _check(checks: List[Dict[str, Any]], key: str, label: str, ok: Optional[bool],
           value: str = "", fix: str = "", level: str = "") -> None:
    """往渲染清单里追加一条（``ok=None`` 表示"未知"，按 warn 展示但不下错误结论）。"""

    if not level:
        level = "ok" if ok is True else ("warn" if ok is None else "error")
    checks.append({"key": key, "label": label, "ok": ok, "value": value, "fix": fix, "level": level})


def _linux_section(node: Dict[str, Any], checks: List[Dict[str, Any]],
                   warnings: List[str]) -> Dict[str, Any]:
    """Linux 专属检查：QQ 注入前置条件、python3-venv、图形会话。拿不到就记"未知"。"""

    node_exe = Path(str(node["exe"])) if node.get("exe") else None
    cap = plat.binary_has_cap_ptrace(node_exe) if node_exe is not None else None
    scope = plat.ptrace_scope()
    display = (os.environ.get("DISPLAY") or "").strip()
    wayland = (os.environ.get("WAYLAND_DISPLAY") or "").strip()
    venv = venv_ok()
    py_ok = python_supported(platform.python_version())
    readiness = plat.qq_injection_readiness(node_exe)

    # 1. Yama ptrace_scope：3 直接判死，1/2 需要 file capability 配合
    scope_ok: Optional[bool] = None if scope is None else scope < 3
    _check(checks, "ptrace_scope", "Yama ptrace_scope（QQ 注入）", scope_ok,
           "未启用 Yama" if scope is None else f"ptrace_scope={scope}",
           "" if scope_ok in (True, None)
           else "sudo sysctl -w kernel.yama.ptrace_scope=1（重启失效，永久改 /etc/sysctl.d/99-ptrace.conf）")

    # 2. node 的 cap_sys_ptrace（注入 QQ 必需）
    _check(checks, "node_cap_ptrace", "node 的 cap_sys_ptrace", cap,
           "未知（未装 getcap）" if cap is None else ("有" if cap else "没有"),
           "sudo apt install -y libcap2-bin（装 getcap 才能检测）" if cap is None
           else ("" if cap else "sudo setcap cap_sys_ptrace=ep '<node 绝对路径>'"))

    # 3. 图形会话：没有它 QQ 起来了也没法扫码
    has_display = bool(display or wayland)
    _check(checks, "display", "图形会话（QQ 扫码登录）", has_display,
           display or wayland or "无 DISPLAY / WAYLAND_DISPLAY",
           "" if has_display else "在桌面会话里启动控制台；无头服务器用 Xvfb + VNC（见 docs/LINUX.md）")

    # 4. python3-venv：AstrBot 用 venv 装依赖
    _check(checks, "venv", "python3-venv 可用", venv,
           "未知" if venv is None else ("可用" if venv else "缺少 venv/ensurepip"),
           "" if venv in (True, None) else "sudo apt install -y python3-venv")
    if venv is False:
        warnings.append("缺少 python3-venv（python3 -m venv 不可用）：AstrBot 装依赖会失败，"
                        "请先 sudo apt install -y python3-venv")

    # 5. 注入总体结论汇总进 warnings（明细在 checks / linux 里，前端别重复拼接）
    if str(readiness.get("level") or "") in ("warn", "error") and readiness.get("message"):
        warnings.append(str(readiness["message"]))

    return {
        "ptrace_scope": scope,
        "display": display,
        "wayland": wayland,
        "node_cap_ptrace": cap,
        "venv_ok": venv,
        "node_ok": bool(node.get("ok")),
        "node_version": str(node.get("version") or ""),
        "python_ok": py_ok,
        "python_version": platform.python_version(),
        "injection": readiness,
    }


def env_report() -> Dict[str, Any]:
    """返回给前端的环境自检报告。

    * 公共字段：``platform/os/arch/is_windows/is_linux/tag``、``tools``、``free_space_gb``、
      ``installed``、``warnings``；
    * ``checks``：渲染用清单 ``[{key,label,ok,value,fix,level}]``（两个平台都给，顺序即展示顺序，
      ``ok`` 可能为 ``None`` = 未知）；
    * ``linux``：Linux 专属检查（Windows 上是空 dict）。
    """

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

    node = node_state()
    min_py = min_python()
    py_ver = platform.python_version()
    free_gb = free_space_gb(paths.DATA_DIR)

    report: Dict[str, Any] = {
        "platform": platform.platform(),
        "os": os.name,
        "arch": platform.machine(),
        "is_windows": plat.IS_WINDOWS,
        "is_linux": plat.IS_LINUX,
        "tag": plat.platform_tag(),
        "python_executable": sys.executable,
        "python_version": py_ver,
        "app_root": str(paths.APP_ROOT),
        "data_dir": str(paths.DATA_DIR),
        "runtime_dir": str(paths.RUNTIME_DIR),
        "storage_base": str(paths.STORAGE.get("base") or plat.default_storage_base() or ""),
        "free_space_gb": free_gb,
        "tools": tools,
        "warnings": [],
        "installed": {
            "snowluma": (paths.RUNTIME_DIR / "snowluma").exists(),
            "astrbot": (paths.RUNTIME_DIR / "astrbot").exists(),
        },
        "linux": {},
        "checks": [],
    }
    checks: List[Dict[str, Any]] = report["checks"]
    warnings: List[str] = report["warnings"]

    # ---------------- 公共检查 ----------------
    py_ok = python_supported(py_ver)
    _check(checks, "python", f"Python ≥ {min_py[0]}.{min_py[1]}", py_ok, f"当前 {py_ver}",
           "" if py_ok else f"安装 Python {min_py[0]}.{min_py[1]}+"
                            "（Linux: sudo apt install -y python3.12 python3.12-venv）")
    if not py_ok:
        warnings.append(f"当前 Python {py_ver} 低于 AstrBot 要求的 {min_py[0]}.{min_py[1]}+")

    git_ok = bool(tools.get("git", {}).get("ok"))
    _check(checks, "git", "git", git_ok, str(tools.get("git", {}).get("version") or ""),
           "" if git_ok else ("安装 Git：sudo apt install -y git" if not plat.IS_WINDOWS
                              else "安装 Git for Windows"))
    if not git_ok:
        warnings.append("未检测到 git：AstrBot 需要 git 克隆，或可改用离线包")

    disk_ok = free_gb >= 3
    _check(checks, "disk", "可用磁盘空间 ≥ 3 GB", disk_ok, f"{free_gb} GB",
           "" if disk_ok else "清理磁盘或换一个剩余空间更大的数据目录（配置页可改）")
    if not disk_ok:
        warnings.append(f"磁盘剩余空间仅 {free_gb} GB，建议至少 3 GB")

    node_ok = bool(node.get("ok"))
    # Node 只是 SnowLuma **lite** 版的要求（full 版自带 node），所以缺了也只是 warn，不是错误
    _check(checks, "node", f"Node.js {node_required()}（仅 lite 版需要）", node_ok,
           str(node.get("version") or "未安装"),
           "" if node_ok else "用 SnowLuma full 版（自带 node，推荐），或把 Node.js 升到 "
                              "24.22.1+ / 26.12.1+",
           level="ok" if node_ok else "warn")

    if plat.IS_LINUX:
        report["linux"] = _linux_section(node, checks, warnings)
    return report


def port_owner(port: int) -> str:
    """返回占用端口的进程信息（``PID 1234 python3``，拿不到返回空串）。

    平台差异全在 :mod:`slab.plat`：Windows 走 ``GetExtendedTcpTable``，
    Linux 走 ``/proc/net/tcp`` → inode → ``/proc/<pid>/fd``。
    """

    return plat.port_owner_text(port)


_PORTS_CACHE: Dict[str, Any] = {"ts": 0.0, "key": "", "data": None}
_PORTS_TTL = 3.0


def _listen_map() -> Dict[int, str]:
    """一次拿到所有 LISTEN 端口的 ``port → PID``（平台差异在 plat；拿不到返回空 dict）。"""

    return plat.listen_map()


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
