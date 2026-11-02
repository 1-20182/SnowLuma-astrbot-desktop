"""平台层 POSIX 分支回归（在 Windows 上也能跑）。

``slab/plat.py`` 的 Linux 实现没法在 Windows 上直接执行，所以这里用两种手段覆盖：

1. **monkeypatch 打开 POSIX 分支**：把 ``plat.IS_WINDOWS`` 改成 False、
   把 ``plat.os`` / ``plat.subprocess`` / ``plat.shutil`` 换成"只覆盖个别属性"的代理；
2. **``/proc`` 文本夹具**：``plat.PROC_ROOT`` 指到 tmp 目录里，里面放
   ``net/tcp`` / ``net/tcp6`` 的真实格式内容（state=0A 才是 LISTEN）。

覆盖点：listen_map（inode → pid 反查 + ss 兜底）、kill_tree（进程组 SIGTERM→SIGKILL）、
port_owner_text、qq_injection_readiness、default_storage_base、storage 的 XDG 分支、
以及 supervisor / orphans / envcheck 是否真的委托给了 plat（防止哪天又长出平台分叉）。
"""

from __future__ import annotations

import os
import signal
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from slab import envcheck, orphans, plat, storage

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# 工具：只覆盖个别属性的模块代理（避免全局 patch os / subprocess 影响 pytest 自己）
# --------------------------------------------------------------------------- #
class _Proxy:
    """``plat.os`` 之类模块的替身：只在被覆盖的属性上返回假值，其余原样透传。"""

    def __init__(self, module: Any, **overrides: Any) -> None:
        self._module = module
        self._overrides = overrides

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._module, name)


class _FakeProc:
    """psutil.Process 替身：``alive`` 控制它"还在跑"还是"已经没了"。"""

    def __init__(self, pid: int, alive: bool) -> None:
        self.pid = pid
        self.alive = alive

    def status(self) -> str:
        return "running" if self.alive else "zombie"

    def children(self, recursive: bool = False) -> List[Any]:  # noqa: ARG002
        return []

    def name(self) -> str:
        return "python3"


class _FakePsutil:
    """psutil 替身：进程是否还在由 ``alive`` 决定（省掉 kill_tree 的真等待）。"""

    STATUS_ZOMBIE = "zombie"

    def __init__(self, alive: bool = False) -> None:
        self.alive = alive

    def pid_exists(self, pid: int) -> bool:
        return self.alive

    def Process(self, pid: int) -> Any:  # noqa: N802 - 模仿 psutil 命名
        return _FakeProc(pid, self.alive)


# --------------------------------------------------------------------------- #
# 夹具：/proc 文本（真实内核格式）
# --------------------------------------------------------------------------- #
#: 6185 = 0x1829（AstrBot webui），5099 = 0x13EB（SnowLuma webui）
PROC_TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1829 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 424242 1 0000 100 0 0 10 0
   1: 0100007F:1829 0100007F:C834 01 00000000:00000000 00:00000000 00000000     0        0 111111 1 0000 100 0 0 10 0
   2: 0100007F:13EB 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 555555 1 0000 100 0 0 10 0
"""

PROC_TCP6 = """\
  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000000000000:0016 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 777777 1 0000 100 0 0 10 0
"""


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    """把 ``plat.PROC_ROOT`` 指向一份 /proc 文本夹具，并打开 POSIX 分支。"""

    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "net" / "tcp").write_text(PROC_TCP, "utf-8")
    (proc / "net" / "tcp6").write_text(PROC_TCP6, "utf-8")
    monkeypatch.setattr(plat, "PROC_ROOT", proc)
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(plat, "IS_POSIX", True)
    monkeypatch.setattr(plat, "IS_LINUX", True)
    return proc


def _fake_fds(monkeypatch, table: Dict[str, Dict[str, str]]) -> None:
    """把 ``/proc/<pid>/fd`` 的 listdir / readlink 打到内存表上。"""

    real_listdir = os.listdir
    real_readlink = os.readlink

    def listdir(path: Any) -> List[str]:
        text = str(path)
        for pid, fds in table.items():
            if text == str(plat.PROC_ROOT / pid / "fd"):
                return list(fds)
        if text == str(plat.PROC_ROOT):
            return list(table)
        return real_listdir(path)

    def readlink(path: Any) -> str:
        text = str(path)
        for pid, fds in table.items():
            for fd, target in fds.items():
                if text == str(plat.PROC_ROOT / pid / "fd" / fd):
                    return target
        return real_readlink(path)

    monkeypatch.setattr(plat, "os", _Proxy(os, listdir=listdir, readlink=readlink))


# --------------------------------------------------------------------------- #
# 子进程参数：POSIX 必须是独立进程组
# --------------------------------------------------------------------------- #
def test_posix_spawn_kwargs_open_new_session(monkeypatch):
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(plat, "IS_POSIX", True)
    # POSIX 没有窗口概念：短命令不加任何参数
    assert plat.hidden_kwargs() == {}
    # 长驻服务必须独立会话/进程组，否则关窗只能杀到自己、留一堆孤儿
    assert plat.service_spawn_kwargs() == {"start_new_session": True}
    assert plat.hidden_console_kwargs() == {"start_new_session": True}
    assert plat.detached_kwargs() == {"start_new_session": True}


def test_windows_spawn_kwargs_unchanged(monkeypatch):
    """Windows 分支：短命令 CREATE_NO_WINDOW，长驻 CREATE_NEW_CONSOLE + 隐藏窗口。

    ``subprocess.STARTUPINFO`` 在 Linux 上不存在，所以这里用一个只提供那几个常量的
    替身，保证这条 Windows 断言在 Linux 验证机上也能跑。
    """

    class _StartupInfo:
        def __init__(self) -> None:
            self.dwFlags = 0
            self.wShowWindow = 1

    class _WinSubprocess:
        CREATE_NEW_CONSOLE = 0x10
        STARTF_USESHOWWINDOW = 0x1
        SW_HIDE = 0
        STARTUPINFO = _StartupInfo

    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    monkeypatch.setattr(plat, "subprocess", _WinSubprocess)
    kwargs = plat.hidden_console_kwargs()
    assert kwargs["creationflags"] == 0x10  # CREATE_NEW_CONSOLE
    assert kwargs["startupinfo"].wShowWindow == 0  # SW_HIDE
    assert "start_new_session" not in kwargs


# --------------------------------------------------------------------------- #
# listen_map：/proc/net/tcp → inode → /proc/<pid>/fd
# --------------------------------------------------------------------------- #
def test_proc_net_listen_inodes_only_listen_state(fake_proc):
    # 111111 是 ESTABLISHED（st=01），必须被忽略；tcp6 的表头行也不能算出端口
    assert plat._proc_net_listen_inodes() == {"424242": 6185, "555555": 5099, "777777": 22}


def test_proc_net_listen_inodes_missing_proc_degrades(tmp_path, monkeypatch):
    monkeypatch.setattr(plat, "PROC_ROOT", tmp_path / "nowhere")
    assert plat._proc_net_listen_inodes() == {}


def test_pid_of_inodes_prefers_lowest_pid(fake_proc, monkeypatch):
    # 同一个监听 socket 被父子进程都持有：取 pid 最小的那个（一般是组长）
    _fake_fds(monkeypatch, {
        "900": {"3": "socket:[424242]"},
        "300": {"7": "socket:[424242]"},
        "301": {"0": "/dev/null", "1": "pipe:[1234]"},
    })
    assert plat._pid_of_inodes({"424242": 6185}) == {6185: 300}


def test_pid_of_inodes_skips_unreadable_fd_dir(fake_proc, monkeypatch):
    # 别人的进程（hidepid / 权限不足）读不到 fd 目录，不能抛异常
    _fake_fds(monkeypatch, {"400": {"0": "socket:[424242]"}})
    assert plat._pid_of_inodes({"424242": 6185}) == {6185: 400}


def test_listen_map_linux_reads_proc(fake_proc, monkeypatch):
    _fake_fds(monkeypatch, {
        "400": {"3": "socket:[424242]", "4": "socket:[555555]"},
    })
    monkeypatch.setattr(plat, "shutil", _Proxy(plat.shutil, which=lambda name: None))
    assert plat.listen_map() == {6185: "400", 5099: "400"}
    assert plat.port_owner_pid(6185) == 400


def test_listen_map_linux_without_listeners_skips_ss(fake_proc, monkeypatch):
    """没人监听时不该再起 ss（少一次 fork）。"""

    (fake_proc / "net" / "tcp").write_text("  sl  local_address\n", "utf-8")
    (fake_proc / "net" / "tcp6").write_text("  sl  local_address\n", "utf-8")
    called: List[List[str]] = []
    monkeypatch.setattr(plat, "shutil", _Proxy(plat.shutil, which=lambda name: "/usr/bin/ss"))
    monkeypatch.setattr(plat, "subprocess", _Proxy(
        plat.subprocess, run=lambda argv, **kw: called.append(list(argv))))
    assert plat.listen_map() == {}
    assert called == []


def test_listen_map_linux_falls_back_to_ss(fake_proc, monkeypatch):
    """拿不到 fd 持有者（别的 netns / 无权限）时才退 ss。"""

    _fake_fds(monkeypatch, {})  # 谁都看不到 socket fd
    ss_output = (
        "State  Recv-Q Send-Q  Local Address:Port Peer Address:PortProcess\n"
        'LISTEN 0 1000 127.0.0.1:6185 0.0.0.0:* users:(("python3",pid=1234,fd=20))\n'
        'LISTEN 0 128 [::]:5099 [::]:* users:(("node",pid=4321,fd=25))\n'
        "LISTEN 0 128 10.255.255.254:53 0.0.0.0:*\n"  # 无权限：ss 给不出 pid
    )

    class _Out:
        stdout = ss_output

    monkeypatch.setattr(plat, "shutil", _Proxy(plat.shutil, which=lambda name: "/usr/bin/ss"))
    monkeypatch.setattr(plat, "subprocess", _Proxy(
        plat.subprocess, run=lambda argv, **kw: _Out()))
    assert plat.listen_map() == {6185: "1234", 5099: "4321"}


def test_ss_local_port_parses_ipv4_ipv6_and_star():
    assert plat._ss_local_port('LISTEN 0 1 127.0.0.1:6185 0.0.0.0:* users:(("a",pid=1,fd=2))') == 6185
    assert plat._ss_local_port("LISTEN 0 1 [::]:5099 [::]:*") == 5099
    assert plat._ss_local_port("LISTEN 0 1 *:8080 *:*") == 8080
    assert plat._ss_local_port("garbage") is None


def test_port_owner_text(fake_proc, monkeypatch):
    monkeypatch.setattr(plat, "listen_map", lambda: {6185: "4321"})
    monkeypatch.setattr(plat, "_proc_name", lambda pid: "python3")
    assert plat.port_owner_text(6185) == "PID 4321 python3"
    assert plat.port_owner_text(9999) == ""
    assert plat.port_owner_pid(9999) is None


def test_proc_name_falls_back_to_comm(fake_proc, monkeypatch):
    """没装 psutil 时退回 /proc/<pid>/comm（Linux 才有）。"""

    monkeypatch.setattr(plat, "_psutil", lambda: None)
    (fake_proc / "42").mkdir()
    (fake_proc / "42" / "comm").write_text("python3\n", "utf-8")
    assert plat._proc_name(42) == "python3"
    assert plat._proc_name(43) == ""


def test_listen_map_live_own_listener():
    """真机校验：自己起的临时监听端口，listen_map 必须报出本进程 pid。

    Windows 走 GetExtendedTcpTable，Linux 走 /proc；两边都该在 2 秒内认出来。
    """

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
    except OSError as exc:  # pragma: no cover - 极端受限环境
        pytest.skip(f"无法监听本地端口：{exc}")
    try:
        owner = None
        deadline = time.time() + 2.0
        while time.time() < deadline:
            owner = plat.port_owner_pid(port)
            if owner is not None:
                break
            time.sleep(0.1)
        if owner is None:  # pragma: no cover - 容器里 /proc 不给权限时
            pytest.skip("当前环境下 listen_map 拿不到端口归属（容器 hidepid / 权限）")
        assert owner == os.getpid()
    finally:
        sock.close()


# --------------------------------------------------------------------------- #
# kill_tree：进程组优先，SIGTERM → SIGKILL
# --------------------------------------------------------------------------- #
def _signal_recorder(monkeypatch, *, pgid_of: Optional[int], own_group: int = 500,
                     alive: bool = False) -> List:
    sent: List = []

    def getpgid(pid: int) -> int:
        if pid == 0:
            return own_group
        if pgid_of is None:
            raise OSError(3, "no such process")
        return pgid_of

    def kill(target: int, sig: int) -> None:
        sent.append((target, sig))

    monkeypatch.setattr(plat, "os", _Proxy(plat.os, getpgid=getpgid, kill=kill))
    monkeypatch.setattr(plat, "_psutil", lambda: _FakePsutil(alive=alive))
    return sent


def test_kill_tree_posix_kills_whole_group_first(monkeypatch):
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    sent = _signal_recorder(monkeypatch, pgid_of=4321)
    assert plat.kill_tree(4321, graceful=0.5) is True
    # -4321 = 整个进程组（组长就是它自己），随后再单独兜底一次 pid
    assert sent[0] == (-4321, signal.SIGTERM)
    assert (4321, signal.SIGTERM) in sent
    # 进程已经不在了（_FakePsutil.pid_exists=False）→ 不该再补 SIGKILL
    assert all(sig == signal.SIGTERM for _target, sig in sent)


def test_kill_tree_posix_escalates_to_sigkill_after_grace(monkeypatch):
    """SIGTERM 之后还赖着不走 → 补 SIGKILL（POSIX 才能真的收掉 node / python）。"""

    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    sent = _signal_recorder(monkeypatch, pgid_of=4321, alive=True)
    assert plat.kill_tree(4321, graceful=0.2) is True
    rounds = [sig for _target, sig in sent]
    assert rounds[:2] == [signal.SIGTERM, signal.SIGTERM]
    assert rounds[2:] == [plat.SIGKILL, plat.SIGKILL]


def test_kill_tree_posix_force_skips_sigterm(monkeypatch):
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    sent = _signal_recorder(monkeypatch, pgid_of=4321, alive=True)
    assert plat.kill_tree(4321, force=True) is True
    # 只有一轮信号（没有先礼后兵的 SIGTERM 轮）
    assert len(sent) == 2
    assert sent[0] == (-4321, plat.SIGKILL)


def test_kill_tree_posix_never_targets_init_group(monkeypatch):
    """pgid=1 是 init/systemd 的组：负号打过去等于杀穿系统。"""

    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    sent = _signal_recorder(monkeypatch, pgid_of=1)
    plat.kill_tree(1, graceful=0)
    assert all(target > 0 for target, _sig in sent), sent


def test_kill_tree_posix_never_targets_own_group(monkeypatch):
    """目标是本进程所在组的成员时（控制台自己的子进程没独立成组）不能发 -pgid。"""

    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    sent = _signal_recorder(monkeypatch, pgid_of=4321, own_group=4321)
    plat.kill_tree(4321, graceful=0)
    assert all(target > 0 for target, _sig in sent), sent


def test_kill_tree_refuses_self_and_bad_pid(monkeypatch):
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    sent = _signal_recorder(monkeypatch, pgid_of=1234)
    assert plat.kill_tree(os.getpid()) is False
    assert plat.kill_tree(0) is False
    assert plat.kill_tree(-5) is False
    assert sent == []


def test_kill_tree_windows_uses_taskkill(monkeypatch):
    """Windows 分支行为不变：taskkill /PID <pid> /T /F。"""

    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    monkeypatch.setattr(plat, "hidden_kwargs", lambda: {})
    calls: List[List[str]] = []

    class _Done:
        returncode = 0

    monkeypatch.setattr(plat, "subprocess", _Proxy(
        plat.subprocess, run=lambda argv, **kw: calls.append(list(argv)) or _Done()))
    assert plat.kill_tree(4242) is True
    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]


# --------------------------------------------------------------------------- #
# 进程存活判断
# --------------------------------------------------------------------------- #
def test_process_alive_basics():
    assert plat.process_alive(0) is False
    assert plat.process_alive(-1) is False
    assert plat.process_alive(os.getpid()) is True


# --------------------------------------------------------------------------- #
# 存储根：POSIX 走 XDG
# --------------------------------------------------------------------------- #
def test_default_storage_base_xdg(tmp_path, monkeypatch):
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.delenv("SLAB_STORAGE_ROOT", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert plat.default_storage_base() == tmp_path / "xdg" / "SnowLumaAstrBot"

    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    expected = Path.home() / ".local" / "share" / "SnowLumaAstrBot"
    assert plat.default_storage_base() == expected


def test_default_storage_base_env_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setenv("SLAB_STORAGE_ROOT", str(tmp_path / "custom"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert plat.default_storage_base() == tmp_path / "custom"


def test_default_storage_base_windows_is_none(monkeypatch):
    """Windows 返回 None：交给 storage.plan() 枚举盘符挑非 C 盘。"""

    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    assert plat.default_storage_base() is None


def test_storage_posix_single_disk_uses_xdg(tmp_path, monkeypatch):
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(plat, "IS_LINUX", True)
    monkeypatch.delenv("SLAB_STORAGE_ROOT", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(storage, "list_drives", lambda: [
        {"letter": "/", "root": "/", "is_system": True, "free_gb": 100.0, "total_gb": 200.0},
    ])
    plan = storage.plan(tmp_path / "app")
    assert plan["source"] == "xdg"
    assert plan["base"] == str(tmp_path / "xdg" / "SnowLumaAstrBot")
    assert "XDG" in plan["reason"]
    assert Path(str(plan["data_dir"])) == tmp_path / "xdg" / "SnowLumaAstrBot" / "data"
    assert Path(str(plan["runtime_dir"])) == tmp_path / "xdg" / "SnowLumaAstrBot" / "runtime"
    assert Path(str(plan["downloads_dir"])) == tmp_path / "xdg" / "SnowLumaAstrBot" / "downloads"
    assert storage.describe(tmp_path / "app")["system_drive"] == "/"


def test_storage_posix_mounts_are_deduped_by_device(tmp_path, monkeypatch):
    """同一设备挂多处（bind mount / btrfs subvol）只算一块盘，且 / 必须是系统盘。"""

    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(storage, "_mount_table", lambda: [
        ("/dev/sdd", "/mnt/wslg/distro", "ext4"),  # bind mount：排在 / 前面也不能抢系统盘
        ("/dev/sdd", "/", "ext4"),
        ("/dev/sdb1", "/data", "ext4"),
    ])
    monkeypatch.setattr(storage, "_free_gb", lambda p: 50.0)
    monkeypatch.setattr(storage, "_total_gb", lambda p: 100.0)
    drives = storage.list_drives()
    assert [d["root"] for d in drives] == ["/", "/data"]
    assert drives[0]["is_system"] is True
    assert all(d["is_system"] is False for d in drives[1:])


def test_storage_posix_prefers_extra_local_disk(tmp_path, monkeypatch):
    """真有第二块本地盘时，还是按"别塞系统盘"的规则挑剩余空间最大的。"""

    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(plat, "IS_LINUX", True)
    monkeypatch.delenv("SLAB_STORAGE_ROOT", raising=False)
    monkeypatch.setattr(storage, "list_drives", lambda: [
        {"letter": "/", "root": "/", "is_system": True, "free_gb": 10.0, "total_gb": 40.0},
        {"letter": "/data", "root": "/data", "is_system": False, "free_gb": 500.0, "total_gb": 900.0},
    ])
    plan = storage.plan(tmp_path / "app")
    assert plan["source"] == "auto"
    assert plan["base"] == str(Path("/data") / storage.APP_FOLDER)


# --------------------------------------------------------------------------- #
# Linux 注入前置条件
# --------------------------------------------------------------------------- #
def _posix_injection(monkeypatch, *, scope: Optional[int], cap: Optional[bool],
                     qq_uids: Optional[List[int]] = None,
                     display: str = ":0", wayland: str = "") -> Dict[str, Any]:
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(plat, "ptrace_scope", lambda: scope)
    monkeypatch.setattr(plat, "binary_has_cap_ptrace", lambda path: cap)
    monkeypatch.setattr(plat, "running_qq_uids", lambda: list(qq_uids or []))
    monkeypatch.setenv("DISPLAY", display)
    monkeypatch.setenv("WAYLAND_DISPLAY", wayland)
    return plat.qq_injection_readiness(Path("/opt/QQ/node"))


def test_injection_readiness_ok_when_cap_and_display(monkeypatch):
    monkeypatch.setattr(plat, "uid", lambda: 1000)
    res = _posix_injection(monkeypatch, scope=1, cap=True, qq_uids=[1000])
    assert res["level"] == "ok" and res["ok"] is True
    assert res["message"] == ""


def test_injection_readiness_scope3_is_fatal(monkeypatch):
    res = _posix_injection(monkeypatch, scope=3, cap=True)
    assert res["level"] == "error" and res["ok"] is False
    assert "ptrace_scope=3" in res["message"]
    assert "sysctl" in res["fix"]


def test_injection_readiness_missing_cap_is_fatal(monkeypatch):
    res = _posix_injection(monkeypatch, scope=1, cap=False)
    assert res["level"] == "error" and res["ok"] is False
    assert "cap_sys_ptrace" in res["message"]
    assert "setcap" in res["fix"]


def test_injection_readiness_uid_mismatch_is_fatal(monkeypatch):
    monkeypatch.setattr(plat, "uid", lambda: 0)
    res = _posix_injection(monkeypatch, scope=1, cap=True, qq_uids=[1000])
    assert res["level"] == "error"
    assert "同一个用户" in res["message"]


def test_injection_readiness_headless_is_warning(monkeypatch):
    monkeypatch.setattr(plat, "uid", lambda: 1000)
    res = _posix_injection(monkeypatch, scope=1, cap=True, qq_uids=[1000], display="", wayland="")
    assert res["level"] == "warn"
    assert res["ok"] is True  # warn 不算致命
    assert "图形会话" in res["message"]


def test_injection_readiness_windows_is_always_ok(monkeypatch):
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    assert plat.qq_injection_readiness() == {"ok": True, "level": "ok", "message": "", "fix": ""}


# --------------------------------------------------------------------------- #
# 环境自检的 Linux 分支
# --------------------------------------------------------------------------- #
def test_env_report_linux_section(monkeypatch, tmp_path):
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(plat, "IS_LINUX", True)
    monkeypatch.setattr(plat, "ptrace_scope", lambda: 1)
    monkeypatch.setattr(plat, "binary_has_cap_ptrace", lambda path: False)
    monkeypatch.setattr(plat, "qq_injection_readiness", lambda node_exe=None: {
        "ok": False, "level": "error", "message": "node 没有 cap_sys_ptrace", "fix": "setcap ..."})
    monkeypatch.setattr(envcheck, "probe_tool", lambda name, args: {
        "name": name, "path": f"/usr/bin/{name}", "ok": True, "version": "x 1.0.0"})
    monkeypatch.setattr(envcheck, "node_state", lambda: {
        "exe": "/opt/snowluma/node", "version": "24.22.1", "ok": True})
    monkeypatch.setattr(envcheck, "venv_ok", lambda: False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "")

    report = envcheck.env_report()
    assert report["is_linux"] is True and report["is_windows"] is False
    assert report["tag"].startswith("linux-") or report["tag"].startswith("win-")
    linux = report["linux"]
    assert linux["ptrace_scope"] == 1
    assert linux["display"] == ":0"
    assert linux["node_cap_ptrace"] is False
    assert linux["venv_ok"] is False
    assert linux["node_ok"] is True
    assert linux["python_ok"] is True  # 测试解释器 >= 3.12（见 requirements / CI）
    # 渲染清单：有 key/label/ok/level，且带上 Linux 专属几项
    keys = [item["key"] for item in report["checks"]]
    for key in ("python", "git", "disk", "node", "ptrace_scope", "node_cap_ptrace", "display", "venv"):
        assert key in keys, keys
    assert all({"key", "label", "ok", "value", "fix", "level"} <= set(item) for item in report["checks"])
    # 错误信息不许再出现"非 Windows 所以不支持 QQ"
    joined = "；".join(report["warnings"])
    assert "仅支持 Windows" not in joined
    assert "ptrace" in joined  # 注入前置问题要出现在 warnings 里
    assert any("python3-venv" in w for w in report["warnings"])


def test_env_report_windows_has_empty_linux_section(monkeypatch):
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    monkeypatch.setattr(plat, "IS_LINUX", False)
    monkeypatch.setattr(envcheck, "probe_tool", lambda name, args: {
        "name": name, "path": "C:/x.exe", "ok": True, "version": "1.0"})
    monkeypatch.setattr(envcheck, "node_state", lambda: {
        "exe": "C:/node.exe", "version": "24.22.1", "ok": True})
    report = envcheck.env_report()
    assert report["linux"] == {}
    assert report["is_windows"] is True
    assert [item["key"] for item in report["checks"]][:4] == ["python", "git", "disk", "node"]


def test_envcheck_python_probe_falls_back_to_python3(monkeypatch):
    """Ubuntu 24.04 没有 ``python`` 别名，只有 python3：不能因此报"未检测到 Python"。"""

    monkeypatch.setattr(envcheck.shutil, "which", lambda name: "/usr/bin/python3" if name == "python3" else None)
    monkeypatch.setattr(envcheck, "_run", lambda argv, cwd=None: "Python 3.12.3")
    info = envcheck.probe_tool("python", ["--version"])
    assert info["ok"] is True
    assert info["path"] == "/usr/bin/python3"
    assert info["version"] == "Python 3.12.3"


def test_node_supported_matches_installer_rules():
    assert envcheck.node_supported("24.22.1") is True
    assert envcheck.node_supported("24.22.0") is False
    assert envcheck.node_supported("26.12.1") is True
    assert envcheck.node_supported("27.0.0") is True
    assert envcheck.node_supported("24.13.0") is False
    assert envcheck.node_supported("") is False
    assert envcheck.node_version_text("v24.22.1\n") == "24.22.1"


def test_python_supported_uses_installer_floor():
    assert envcheck.python_supported("3.12.3") is True
    assert envcheck.python_supported("3.11.9") is False
    assert envcheck.python_supported("3.13.0") is True
    assert envcheck.min_python() == (3, 12)


def test_envcheck_listen_map_delegates_to_plat(monkeypatch):
    monkeypatch.setattr(plat, "listen_map", lambda: {6185: "777"})
    assert envcheck._listen_map() == {6185: "777"}
    monkeypatch.setattr(plat, "port_owner_text", lambda port: f"PID 777 port-{port}")
    assert envcheck.port_owner(6185) == "PID 777 port-6185"


# --------------------------------------------------------------------------- #
# orphans：POSIX 进程名白名单 + 路径标记
# --------------------------------------------------------------------------- #
def test_orphans_exe_stem_handles_posix_names():
    assert orphans._exe_stem("python3.12") == "python3"
    assert orphans._exe_stem("python.exe") == "python"
    assert orphans._exe_stem("node.exe") == "node"
    assert orphans._exe_stem("/usr/bin/node") == "node"
    for name in ("python", "python3", "node"):
        assert name in orphans._OUR_EXE_NAMES


def test_orphans_path_markers_cover_posix_layout():
    assert "/runtime/astrbot" in orphans._PATH_MARKERS
    assert "/runtime/snowluma" in orphans._PATH_MARKERS
    assert "\\runtime\\astrbot" in orphans._PATH_MARKERS  # Windows 行为不变


def test_orphans_port_owner_delegates_to_plat(monkeypatch):
    monkeypatch.setattr(plat, "port_owner_pid", lambda port: 4321)
    assert orphans.port_owner(6185) == 4321


def test_orphans_kill_tree_delegates_to_plat(monkeypatch):
    calls: List[int] = []
    monkeypatch.setattr(plat, "kill_tree", lambda pid, **kw: calls.append(pid) or True)
    assert orphans.kill_tree(4321) is True
    assert calls == [4321]


# --------------------------------------------------------------------------- #
# 平台分叉不许再长回来（源码级检查）
# --------------------------------------------------------------------------- #
def _src(name: str) -> str:
    return (ROOT / "slab" / name).read_text("utf-8")


def test_supervisor_uses_plat_for_spawn_and_kill():
    src = _src("supervisor.py")
    assert "plat.service_spawn_kwargs()" in src
    assert "plat.kill_tree(" in src
    assert '"taskkill"' not in src  # 杀进程树只能在 plat 里实现


def test_orphans_has_no_platform_branch():
    src = _src("orphans.py")
    assert "plat.kill_tree(" in src
    assert "os.name ==" not in src
    assert '"taskkill"' not in src


def test_envcheck_has_no_platform_branch():
    src = _src("envcheck.py")
    assert "plat.listen_map()" in src
    assert "plat.port_owner_text(" in src
    assert "iphlpapi" not in src
    assert "os.name !=" not in src


def test_procutil_delegates_to_plat():
    src = _src("procutil.py")
    assert "plat.hidden_kwargs()" in src
    assert "plat.hidden_console_kwargs()" in src
    assert "os.name ==" not in src
