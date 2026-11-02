"""平台层：Windows / Linux 差异的**唯一收口处**。

设计原则（重要，改这个文件前先读）：

1. **Windows 行为一个字都不许变**——现有 371 项测试与线上版本都钉着它。
   Windows 分支是把原来散在 ``procutil`` / ``supervisor`` / ``orphans`` / ``envcheck``
   里的逻辑**原样搬过来**，不是重写。
2. 其它模块（supervisor / orphans / envcheck / installer / qq / routes_*）**只许 import 这里的函数**，
   不许自己写 ``if os.name == "nt"`` 的平台分叉。
3. 失败要退化，不要抛：端口表拿不到、``/proc`` 读不了、``ss`` 不存在，都返回空并由调用方兜底。

Linux 侧的关键事实（来自上游侦察，别凭直觉改）：

* SnowLuma 在 Linux 上是 **ptrace 注入** QQ 进程：node 二进制要有 ``cap_sys_ptrace``，
  且必须**与 QQ 同 uid**；Yama ``ptrace_scope=3`` 直接判死（0/1/2 可配合 file capability）。
* 子进程必须 ``start_new_session=True`` 起，否则"关窗/退出"只能杀到自己，留下孤儿——
  Windows 那边靠 ``taskkill /T /F`` 弥补，Linux 上靠**进程组**。
* 端口 → PID：Linux 没有 ``GetExtendedTcpTable`` 也没 ``netstat``，走
  ``/proc/net/tcp{,6}``（state 0A = LISTEN）拿 socket inode，再扫 ``/proc/<pid>/fd`` 找 inode 的持有者。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")
IS_POSIX = os.name == "posix"

#: Windows 的 CREATE_NO_WINDOW（写成常量便于非 Windows 上引用）
CREATE_NO_WINDOW = 0x08000000

#: 强制结束用的信号（POSIX 是 SIGKILL；Windows 没有这个常量，退化成 SIGTERM 仅作占位）
SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

#: ``/proc`` 根目录。Linux 端口反查全走这里；测试可以 monkeypatch 成夹具目录。
PROC_ROOT = Path("/proc")
#: ``/proc/net`` 下的 TCP 连接表（IPv4 / IPv6），state=0A 即 LISTEN
PROC_NET_TCP_FILES = ("tcp", "tcp6")


# --------------------------------------------------------------------------- #
# 基本标识
# --------------------------------------------------------------------------- #
def platform_tag() -> str:
    """发行目标标识，与 SnowLuma 官方资产命名对齐：``win-x64`` / ``linux-x64`` / ``linux-arm64``。"""

    if IS_WINDOWS:
        arch = str(os.environ.get("PROCESSOR_ARCHITECTURE") or "").lower()
    else:
        try:
            arch = os.uname().machine.lower()
        except AttributeError:  # pragma: no cover - 只在奇怪的解释器下
            arch = ""
    if IS_WINDOWS:
        return "win-x64" if arch in ("amd64", "x86_64", "") else "win-arm64"
    if IS_LINUX:
        return "linux-arm64" if arch in ("aarch64", "arm64") else "linux-x64"
    return f"{sys.platform}-{arch or 'unknown'}"


def exe_suffix() -> str:
    return ".exe" if IS_WINDOWS else ""


def node_bin_name() -> str:
    """SnowLuma full 版自带的 node 可执行文件名。"""

    return f"node{exe_suffix()}"


def venv_bin_dir(home: Path) -> Path:
    """``<home>/.venv`` 里放可执行文件的目录（Windows: Scripts，POSIX: bin）。"""

    return Path(home) / ".venv" / ("Scripts" if IS_WINDOWS else "bin")


def venv_python(home: Path) -> Path:
    return venv_bin_dir(home) / f"python{exe_suffix()}"


def uid() -> int:
    """当前有效 uid（Windows 返回 0，仅用于"是否同用户"的判定分支）。"""

    try:
        return os.geteuid()  # type: ignore[attr-defined]
    except AttributeError:
        return 0


# --------------------------------------------------------------------------- #
# 子进程：隐藏窗口（Windows）/ 独立进程组（POSIX）
# --------------------------------------------------------------------------- #
def hidden_kwargs() -> Dict[str, Any]:
    """``subprocess`` 的"不弹窗"参数。

    Windows：``CREATE_NO_WINDOW`` + 隐藏 ``STARTUPINFO``（否则部署一次能看到十几个黑框）。
    POSIX：没有窗口概念，返回空 dict。
    """

    if not IS_WINDOWS:
        return {}
    kwargs: Dict[str, Any] = {"creationflags": CREATE_NO_WINDOW}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    kwargs["startupinfo"] = startupinfo
    return kwargs


def hidden_console_kwargs() -> Dict[str, Any]:
    """长驻服务用：Windows 下创建一个隐藏控制台，孙进程不会各自新开窗。

    POSIX 下等价物是"独立会话 + 独立进程组"，这样 ``kill_tree`` 能按组收掉整棵树。
    具体取值见 :func:`service_spawn_kwargs`。
    """

    return service_spawn_kwargs()


def service_spawn_kwargs() -> Dict[str, Any]:
    """**长驻服务**（AstrBot / SnowLuma / 中继）子进程的启动参数，平台差异就在这里收口。

    * Windows：``CREATE_NEW_CONSOLE`` + 隐藏 ``STARTUPINFO``，孙进程不再各开一个黑窗。
    * POSIX：``start_new_session=True`` —— 子进程成为新会话/新进程组组长。
      这是"关窗 / 退出 / 清理残留"能收掉整棵树的前提：没有它，控制台退出后
      ``node`` / ``python`` 会变成孤儿并一直占着 6185 / 5099，下次启动直接报端口占用。
    """

    if IS_WINDOWS:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        return {"creationflags": subprocess.CREATE_NEW_CONSOLE, "startupinfo": startupinfo}
    return {"start_new_session": True}


def detached_kwargs() -> Dict[str, Any]:
    """把一个后台服务拉成"自己的进程组"（POSIX 必需，Windows 返回空）。"""

    return {} if IS_WINDOWS else {"start_new_session": True}


# --------------------------------------------------------------------------- #
# 进程终止
# --------------------------------------------------------------------------- #
def _psutil():
    try:
        import psutil

        return psutil
    except Exception:  # noqa: BLE001 - psutil 缺失时退化到纯标准库
        return None


def _kill_tree_windows(pid: int) -> bool:
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=20, **hidden_kwargs())
        return True
    except Exception:  # noqa: BLE001
        return False


def _posix_targets(pid: int) -> List[int]:
    """要杀掉的目标：自己就是一个进程组的组长 → 整组；否则先自己，再递归子进程。"""

    targets: List[int] = []
    own_group: Optional[int] = None
    try:
        own_group = int(os.getpgid(0))
    except (OSError, AttributeError):
        own_group = None
    try:
        pgid = int(os.getpgid(pid))
        # 只对"自己单独成组"的目标发 -pgid；
        # pgid == 1 是 init/systemd 的组，负号打过去等于把系统杀穿，绝不能碰。
        if pgid == pid and pgid > 1 and pgid != own_group:
            targets.append(-pgid)  # 负数 = 整个进程组
    except OSError:
        pass
    targets.append(pid)

    ps = _psutil()
    if ps is not None:
        try:
            for child in ps.Process(pid).children(recursive=True):
                try:
                    targets.append(int(child.pid))
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001 - 进程已退出
            pass
    return list(dict.fromkeys(targets))


def _signal_targets(targets: Iterable[int], sig: int) -> List[int]:
    sent: List[int] = []
    for target in targets:
        try:
            os.kill(target, sig)
            sent.append(target)
        except OSError:
            continue
    return sent


def _wait_gone(pid: int, timeout: float, ps: Any) -> bool:
    """等 ``pid`` 消失（含僵尸态，僵尸已经不再持有端口/文件）；超时返回 False。"""

    deadline = time.time() + timeout
    while time.time() < deadline:
        if ps is not None:
            try:
                if not ps.pid_exists(pid) or ps.Process(pid).status() == ps.STATUS_ZOMBIE:
                    return True
            except Exception:  # noqa: BLE001
                return True
        else:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
        time.sleep(0.1)
    return False


def kill_tree(pid: int, *, graceful: float = 3.0, force: bool = False) -> bool:
    """结束整棵进程树。

    * Windows：``taskkill /PID <pid> /T /F``（强杀，历史行为；``force`` 不影响它）。
    * POSIX：先对"进程组 / 子进程"发 ``SIGTERM``——SnowLuma 有优雅关停路径
      （先 dispose OneBot 再关日志），强杀会丢收尾；``graceful`` 秒后还在的再 ``SIGKILL``。
      ``force=True`` 跳过 ``SIGTERM`` 直接 ``SIGKILL``（调用方已经等不及时的兜底）。

    返回是否"发过信号"（不代表对方一定退出，调用方自己 ``poll``）。
    """

    if not pid or pid <= 0:
        return False
    if pid == os.getpid():  # 保护：绝不把控制台自己当目标
        return False
    if IS_WINDOWS:
        return _kill_tree_windows(pid)

    targets = _posix_targets(pid)
    if force:
        return bool(_signal_targets(targets, SIGKILL))
    if not _signal_targets(targets, signal.SIGTERM):
        return False
    if graceful <= 0:
        return True
    ps = _psutil()
    if _wait_gone(pid, graceful, ps):
        return True
    _signal_targets(_posix_targets(pid), SIGKILL)
    return True


def process_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    ps = _psutil()
    if ps is not None:
        try:
            proc = ps.Process(pid)
            return proc.is_running() and proc.status() != ps.STATUS_ZOMBIE
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 端口 → PID
# --------------------------------------------------------------------------- #
def _listen_map_windows() -> Dict[int, str]:
    """GetExtendedTcpTable（毫秒级），失败返回空由调用方退 netstat。"""

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
    except Exception:  # noqa: BLE001
        return {}


def _listen_map_windows_netstat() -> Dict[int, str]:
    result: Dict[int, str] = {}
    try:
        out = subprocess.run(  # noqa: S603
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace", **hidden_kwargs(),
        ).stdout
    except Exception:  # noqa: BLE001
        return result
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3] != "LISTENING" or ":" not in parts[1]:
            continue
        try:
            result[int(parts[1].rsplit(":", 1)[1])] = parts[-1]
        except ValueError:
            continue
    return result


def _proc_net_listen_inodes() -> Dict[str, int]:
    """``/proc/net/tcp{,6}`` 里 state=0A(LISTEN) 的 ``socket inode → port``。

    行格式（内核文档 proc_net_tcp）：``sl local_address rem_address st ... uid timeout inode``，
    本地地址是 ``十六进制IP:十六进制端口``，第 10 个字段是 socket inode。
    """

    inodes: Dict[str, int] = {}
    for name in PROC_NET_TCP_FILES:
        try:
            with open(PROC_ROOT / "net" / name, "r", encoding="utf-8", errors="replace") as fh:
                next(fh, None)  # 表头
                for line in fh:
                    parts = line.split()
                    if len(parts) < 10 or parts[3] != "0A":  # 0A = TCP_LISTEN
                        continue
                    try:
                        port = int(parts[1].rsplit(":", 1)[1], 16)
                        inode = int(parts[9])
                    except (IndexError, ValueError):
                        continue
                    if inode <= 0:  # 0 = 内核没给 inode（TIME_WAIT 之类）
                        continue
                    inodes[str(inode)] = port
        except OSError:  # 文件不存在（非 Linux / 容器里没挂 proc）→ 退化
            continue
    return inodes


def _pid_of_inodes(targets: Dict[str, int]) -> Dict[int, int]:
    """扫 ``/proc/<pid>/fd`` 把 socket inode 反查回 pid（读不到 fd 的进程直接跳过）。

    同一个监听 socket 可能被父子进程同时持有（fork 后 exec），这里取**pid 最小**的那个
    ——通常是父进程/进程组组长，``kill_tree`` 按组收树时最合适。
    """

    if not targets:
        return {}
    owners: Dict[int, int] = {}
    expected = len(set(targets.values()))  # 端口数（tcp + tcp6 可能同一端口两个 inode）
    try:
        # 按 pid 升序扫：父进程 pids 通常更小，先扫到它就能直接收工；
        # psutil 的 /proc 顺序在内核里是按 pid 递增的，但别依赖实现细节。
        pid_names = sorted((d for d in os.listdir(PROC_ROOT) if d.isdigit()), key=int)
    except OSError:
        return {}
    for pid_text in pid_names:
        if len(owners) >= expected:
            break
        fd_dir = PROC_ROOT / pid_text / "fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:  # 别人的进程（hidepid / 权限不足）→ 跳过
            continue
        for fd in fds:
            try:
                link = os.readlink(fd_dir / fd)
            except OSError:
                continue
            if not link.startswith("socket:["):
                continue
            port = targets.get(link[8:-1])  # "socket:[12345]" → "12345"
            if port is None:
                continue
            pid = int(pid_text)
            prev = owners.get(port)
            if prev is None or pid < prev:
                owners[port] = pid
    return owners


def _ss_local_port(line: str) -> Optional[int]:
    """从 ``ss -ltnp`` 的一行里取本地端口（第 4 列，形如 ``127.0.0.1:6185`` / ``[::]:22``）。"""

    for token in line.split()[3:6]:
        if ":" not in token:
            continue
        tail = token.rsplit(":", 1)[1]
        if tail.isdigit():
            return int(tail)
    return None


def _listen_map_linux_ss() -> Dict[int, str]:
    """兜底：``ss -ltnp``（``/proc`` 拿不到 inode 持有者时用，例如 hidepid 或别的 netns）。

    没权限的进程 ``ss`` 只会给出端口、不给 pid，这种情况就查不到——返回空让调用方退化。
    """

    result: Dict[int, str] = {}
    exe = shutil.which("ss")
    if not exe:
        return result
    try:
        out = subprocess.run([exe, "-ltnp"], capture_output=True, text=True, timeout=10,
                             encoding="utf-8", errors="replace").stdout
    except Exception:  # noqa: BLE001
        return result
    for line in out.splitlines():
        match = re.search(r"pid=(\d+)", line)
        if not match:
            continue
        port = _ss_local_port(line)
        if port:
            result.setdefault(port, match.group(1))
    return result


def _listen_map_linux() -> Dict[int, str]:
    """Linux：``/proc/net/tcp{,6}`` → inode → ``/proc/<pid>/fd`` 反查 pid；拿不到再退 ``ss``。"""

    inodes = _proc_net_listen_inodes()
    if not inodes:
        return {}  # 真的没人监听：连 ss 都不用起，省一次 fork
    owners = _pid_of_inodes(inodes)
    if owners:
        return {port: str(pid) for port, pid in owners.items()}
    return _listen_map_linux_ss()


def listen_map() -> Dict[int, str]:
    """监听端口 → 占用它的 PID（``{port: "1234"}``），拿不到就返回空 dict。"""

    if IS_WINDOWS:
        fast = _listen_map_windows()
        return fast or _listen_map_windows_netstat()
    if IS_LINUX:
        return _listen_map_linux()
    return {}


def _proc_name(pid: int) -> str:
    """进程名（psutil 优先，Linux 再退 ``/proc/<pid>/comm``），拿不到返回空串。"""

    ps = _psutil()
    if ps is not None:
        try:
            return str(ps.Process(pid).name() or "")
        except Exception:  # noqa: BLE001
            return ""
    try:
        return (PROC_ROOT / str(pid) / "comm").read_text("utf-8", errors="replace").strip()
    except (OSError, AttributeError):
        return ""


def port_owner_pid(port: int) -> Optional[int]:
    """占用监听端口的 PID（没人占/拿不到返回 ``None``）。"""

    try:
        pid = listen_map().get(int(port))
    except (TypeError, ValueError):
        return None
    try:
        return int(pid) if pid else None
    except (TypeError, ValueError):
        return None


def port_owner_text(port: int) -> str:
    """占用端口的进程的可读描述（``PID 1234 python3``），拿不到返回空串。"""

    pid = port_owner_pid(port)
    if not pid:
        return ""
    name = _proc_name(pid)
    return f"PID {pid} {name}".strip()


# --------------------------------------------------------------------------- #
# 文件 / 桌面
# --------------------------------------------------------------------------- #
def chmod_exec(path: Path) -> None:
    """确保可执行位（POSIX）；Windows 无操作。"""

    if IS_WINDOWS:
        return
    try:
        mode = Path(path).stat().st_mode
        Path(path).chmod(mode | 0o111)
    except OSError:
        pass


def open_path(path: Path) -> bool:
    """在文件管理器里打开目录。"""

    target = str(path)
    try:
        if IS_WINDOWS:
            # 与 1.7.x 的 routes_deploy 逐字一致：explorer 只把请求发出去、不等结果，
            # 也不加 CREATE_NO_WINDOW（explorer 是 GUI 子系统程序，控制台标志对它无效，
            # 加了反而在"资源管理器没在跑、要新起一个壳"的场景下徒增变量）。
            subprocess.Popen(["explorer", target], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            return True
        if sys.platform == "darwin":
            subprocess.run(["open", target], timeout=10)
            return True
        opener = shutil.which("xdg-open")
        if opener:
            subprocess.Popen([opener, target], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             start_new_session=True)
            return True
        if hasattr(os, "startfile"):  # pragma: no cover - 老式 Windows 兜底
            os.startfile(target)  # type: ignore[attr-defined]
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def desktop_dirs() -> List[Tuple[str, Path]]:
    """可选目录选择框的兜底（Linux 下没装 zenity 时前端让用户手输路径）。"""

    if IS_WINDOWS:
        return []
    home = Path.home()
    return [("home", home), ("root", Path("/")), ("opt", Path("/opt"))]


# --------------------------------------------------------------------------- #
# 存储根
# --------------------------------------------------------------------------- #
def default_storage_base() -> Optional[Path]:
    """默认"把数据放在哪"。

    Windows 返回 ``None``——交给 ``slab.storage.plan()`` 枚举盘符挑非 C 盘。
    POSIX 用 XDG：``$XDG_DATA_HOME/SnowLumaAstrBot``，没有就 ``~/.local/share/SnowLumaAstrBot``。
    """

    if IS_WINDOWS:
        return None
    env = (os.environ.get("SLAB_STORAGE_ROOT") or "").strip()
    if env:
        return Path(env).expanduser()
    xdg = (os.environ.get("XDG_DATA_HOME") or "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "SnowLumaAstrBot"


def home_dir() -> Path:
    return Path.home()


# --------------------------------------------------------------------------- #
# QQ 客户端（Linux：官方 deb 装到 /opt/QQ；Windows：注册表 + Program Files）
# --------------------------------------------------------------------------- #
def qq_install_dirs() -> List[Path]:
    if IS_WINDOWS:
        return []  # Windows 侧由 slab.qq._common_install_paths / _registry_installs 负责
    return [Path("/opt/QQ"), Path("/usr/share/QQ"), Path("/usr/lib/QQ"),
            Path("/opt/linuxqq"), Path("/usr/share/linuxqq")]


def qq_exe_names() -> Tuple[str, ...]:
    if IS_WINDOWS:
        return ("QQ.exe", "QQEX.exe", "QQScLauncher.exe")
    return ("qq", "linuxqq", "QQ")


def qq_process_names() -> set:
    """进程名（小写）白名单：用于找"正在跑的 QQ"。"""

    if IS_WINDOWS:
        return {"qq.exe"}
    return {"qq", "linuxqq"}


def qq_main_arg() -> List[str]:
    """Linux QQ 启动时用于"隔离多开"的参数（独立 HOME 由调用方注入环境变量）。"""

    return ["--no-sandbox"] if not IS_WINDOWS else []


# --------------------------------------------------------------------------- #
# Linux 注入前置条件
# --------------------------------------------------------------------------- #
def ptrace_scope() -> Optional[int]:
    try:
        text = Path("/proc/sys/kernel/yama/ptrace_scope").read_text("utf-8").strip()
        return int(text)
    except (OSError, ValueError):
        return None


def binary_has_cap_ptrace(path: Path) -> Optional[bool]:
    """二进制是否带 ``cap_sys_ptrace``；没有 getcap 命令时返回 ``None``（未知）。"""

    getcap = shutil.which("getcap")
    if not getcap:
        return None
    try:
        out = subprocess.run([getcap, str(path)], capture_output=True, text=True,
                             timeout=8).stdout.lower()
    except Exception:  # noqa: BLE001
        return None
    return "cap_sys_ptrace" in out


def running_qq_uids() -> List[int]:
    """正在运行的 QQ 进程的 uid（拿不到就返回空列表）。

    ptrace 注入要求**注入方与被注入方同 uid**，这是 Linux 上最常见的"注入没反应"原因：
    控制台用 sudo 起、QQ 用普通用户起（或反过来），ptrace 直接 EPERM。
    """

    if IS_WINDOWS:
        return []
    ps = _psutil()
    if ps is None:
        return []
    names = qq_process_names()
    uids: List[int] = []
    try:
        for proc in ps.process_iter(["pid", "name"]):
            try:
                if str(proc.info.get("name") or "").lower() not in names:
                    continue
                real_uid = int(proc.uids().real)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - 进程已退出 / 无权限
                continue
            if real_uid not in uids:
                uids.append(real_uid)
    except Exception:  # noqa: BLE001
        return uids
    return uids


def qq_injection_readiness(node_exe: Optional[Path] = None) -> Dict[str, Any]:
    """Linux 上"能不能接管 QQ"的自检（Windows 恒返回 ok=True，由 Windows 逻辑自理）。

    检查项（都是实测会踩的坑）：

    1. Yama ``ptrace_scope``：``>=3`` 直接判死；``1/2`` 需要 node 带 ``cap_sys_ptrace``；
    2. ``node`` 是否有 ``cap_sys_ptrace``（没装 libcap2-bin 时只能说"未知"）；
    3. 正在跑的 QQ 是否与控制台**同 uid**（不同 uid 注入必然 EPERM）；
    4. 有没有图形会话（DISPLAY / WAYLAND_DISPLAY）——没有就扫不了码。

    返回 ``{"ok": bool, "level": "ok|warn|error", "message": str, "fix": str}``，
    ``ok`` 的含义是"没有致命问题"（warn 仍算 ok，只是要提示用户）。
    """

    if IS_WINDOWS:
        return {"ok": True, "level": "ok", "message": "", "fix": ""}

    problems: List[str] = []
    fixes: List[str] = []
    level = "ok"

    def _raise_level(new: str) -> None:
        """只升不降：error 一旦出现就不再被 warn 盖掉。"""

        nonlocal level
        order = {"ok": 0, "warn": 1, "error": 2}
        if order.get(new, 0) > order.get(level, 0):
            level = new

    scope = ptrace_scope()
    has_cap: Optional[bool] = None
    node_path = Path(node_exe) if node_exe is not None else None
    if node_path is not None:
        has_cap = binary_has_cap_ptrace(node_path)

    # 1) 硬性失败项：ptrace_scope=3、node 明确没有 cap、与 QQ 不同 uid
    if scope is not None and scope >= 3:
        _raise_level("error")
        problems.append(f"Yama ptrace_scope={scope}（3 = 完全禁止 ptrace 注入）")
        fixes.append("临时：sudo sysctl -w kernel.yama.ptrace_scope=1（重启后失效，"
                     "永久改 /etc/sysctl.d/99-ptrace.conf）")

    if has_cap is False and node_path is not None:
        _raise_level("error")
        problems.append(f"{node_path} 没有 cap_sys_ptrace，注入会 EPERM")
        fixes.append(f"sudo setcap cap_sys_ptrace=ep '{node_path.resolve()}'")
    elif has_cap is None and node_path is not None and scope is not None and scope >= 1:
        _raise_level("warn")
        problems.append("没装 libcap2-bin（getcap），无法确认 node 是否已有 ptrace 权限")
        fixes.append("sudo apt install -y libcap2-bin")
    elif has_cap is not True and scope is not None and 1 <= scope < 3:
        # 没给 node 路径时只能说"需要加 cap"（明细在调用方，envcheck 会带上 node 路径）
        _raise_level("warn")
        problems.append(f"Yama ptrace_scope={scope}：同 uid 进程之间默认不允许 ptrace，"
                        "注入需要给 node 加 cap_sys_ptrace")
        fixes.append(f"sudo setcap cap_sys_ptrace=ep '{node_path.resolve()}'" if node_path is not None
                     else "sudo setcap cap_sys_ptrace=ep '<SnowLuma 自带的 node 绝对路径>'")

    qq_uids = running_qq_uids()
    if qq_uids and uid() not in qq_uids:
        _raise_level("error")
        problems.append(f"控制台 uid={uid()} 与正在运行的 QQ uid={qq_uids} 不一致，"
                        "ptrace 注入要求同一个用户")
        fixes.append("用与 QQ 相同的用户启动控制台（别一边 sudo 一边普通用户）")

    display = (os.environ.get("DISPLAY") or "").strip()
    wayland = (os.environ.get("WAYLAND_DISPLAY") or "").strip()
    if not display and not wayland:
        _raise_level("warn")
        problems.append("当前没有图形会话（DISPLAY/WAYLAND_DISPLAY 都为空），QQ 起来也没法扫码登录")
        fixes.append("在桌面会话里启动控制台；无头服务器请用 Xvfb + VNC（见 docs/LINUX.md）")

    return {"ok": level != "error", "level": level,
            "message": "；".join(problems), "fix": "；".join(fixes)}


# --------------------------------------------------------------------------- #
# 其它
# --------------------------------------------------------------------------- #
def require_supported() -> None:
    """控制台支持范围（Windows 10/11 x64、Linux x64/arm64）。"""

    if IS_WINDOWS or IS_LINUX:
        return
    raise RuntimeError(
        f"当前系统（{sys.platform}）暂不支持：控制台目前只支持 Windows 10/11 与 Linux x64/arm64。"
    )


def summary() -> Dict[str, Any]:
    return {
        "os": os.name,
        "platform": sys.platform,
        "tag": platform_tag(),
        "is_windows": IS_WINDOWS,
        "is_linux": IS_LINUX,
        "uid": uid(),
        "ptrace_scope": ptrace_scope() if IS_LINUX else None,
        "display": (os.environ.get("DISPLAY") or "") if IS_POSIX else "",
        "storage_base": str(default_storage_base() or ""),
    }
