"""QQ 深度分身：复用 AppClone（D:\\fenshen）的 sandbox DLL，在 Python 里完成
"挂起创建 → 注入 → 恢复"，做到**独立数据目录 + 多开解锁 + 已知文件夹重定向**。

与 ``slab/qq.py`` 里"轻量隔离"的区别：
* 轻量隔离只改环境变量：Electron 类应用（QQ NT 就是）用 shell32 的
  ``SHGetKnownFolderPath`` 取路径，不读 APPDATA，所以登录态会串；
* 深度分身额外注入 ``inject64.dll``，它会
  - Hook 命名互斥体/信号量（``FENSHEN_MUTEX_TAG``）→ 允许第二个 QQ 实例；
  - Hook 已知文件夹 / ``GetUserProfileDirectory`` / ``GetUserName``（``FENSHEN_CLONE_HOME`` 等）；
  - 可选：``RegOverridePredefKey`` 重定向 HKCU（``FENSHEN_REG_SUBKEY``，需要先复制注册表快照，默认不用）。

**平台差异**（只在 :mod:`slab.plat` 里判定）：
* Windows：上面那套 CreateRemoteThread/DLL 注入，逻辑一字未改；
* Linux/macOS：没有 ``ctypes.wintypes``、也没有可注入的 Win32 API，改成
  「独立 HOME 分身」——每个 ``clone_id`` 一个 HOME（``$base/qq/clones/<id>/home``），
  复用 :func:`slab.qq.spawn_posix_isolated`（QQ 的单实例锁在 ``$HOME/.config/QQ``，
  换 HOME 才能多开）。所以**顶层 import 不能依赖 ctypes.wintypes**。

安全边界：只注入我们自己创建的进程；DLL 来自用户自己的 AppClone 项目。
"""

from __future__ import annotations

import ctypes
import os
import struct
import time
from pathlib import Path
from typing import Any, Dict

from . import paths, plat, qq
from .events import bus

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_UNICODE_ENVIRONMENT = 0x00000400
PROCESS_ALL_ACCESS = 0x1F0FFF
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
PAGE_READWRITE = 0x04
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0
DLL_VENDOR_FALLBACK = Path(r"D:\fenshen\dist\sandbox")


# --------------------------------------------------------------------------- #
# 沙箱 DLL 位置
# --------------------------------------------------------------------------- #
def sandbox_dir() -> Path:
    """优先用打包进程序里的副本，其次回退到本机 AppClone 安装目录。"""

    bundled = paths.SANDBOX_DIR
    if (bundled / "inject64.dll").is_file():
        return bundled
    if (DLL_VENDOR_FALLBACK / "inject64.dll").is_file():
        return DLL_VENDOR_FALLBACK
    return bundled


def clone_root(clone_id: str = "bot") -> Path:
    base = Path(str(paths.STORAGE.get("base") or paths.APP_ROOT))
    return base / "qq" / "clones" / clone_id


def clone_dirs(clone_id: str = "bot") -> Dict[str, Path]:
    root = clone_root(clone_id)
    return {
        "home": root / "home",
        "roaming": root / "AppData" / "Roaming",
        "local": root / "AppData" / "Local",
        "local_low": root / "AppData" / "LocalLow",
        "temp": root / "Temp",
        "user_data": root / "UserData",
    }


# --------------------------------------------------------------------------- #
# 环境块
# --------------------------------------------------------------------------- #
def build_env_block(extra: Dict[str, str]) -> bytes:
    """构造 CreateProcessW 需要的 Unicode 环境块（大小写不敏感合并父进程环境）。"""

    merged: Dict[str, str] = {}
    for key, value in os.environ.items():
        merged[key.upper()] = f"{key}={value}"
    for key, value in extra.items():
        if value is None:
            merged.pop(key.upper(), None)
        else:
            merged[key.upper()] = f"{key}={value}"
    items = [merged[k] for k in sorted(merged)]
    blob = "\0".join(items) + "\0\0"
    return blob.encode("utf-16le")


def build_clone_env(clone_id: str = "bot", *, device_fake: bool = False,
                    device_name: str = "", taskbar_group: str = "") -> Dict[str, str]:
    """Windows 深度分身的环境变量（POSIX 用 :func:`slab.qq.posix_isolated_env`）。"""

    dirs = clone_dirs(clone_id)
    tag = clone_id.replace("-", "")[:12]
    # 注意：**不要**重定向 USERPROFILE —— 实测（QQ NT 9.9.36）会把启动器直接卡死，
    # 连子进程都不拉起；只重定向 APPDATA/LOCALAPPDATA/TEMP 就足以隔离登录态与缓存。
    env: Dict[str, str] = {
        "APPDATA": str(dirs["roaming"]),
        "LOCALAPPDATA": str(dirs["local"]),
        "TEMP": str(dirs["temp"]),
        "TMP": str(dirs["temp"]),
        # 深度隔离：互斥体改名 + 已知文件夹重定向（Electron 只认 shell32）
        "FENSHEN_CLONE": f"QQNT:{clone_id}",
        "FENSHEN_MUTEX_TAG": tag,
        "FENSHEN_CLONE_HOME": str(dirs["home"]),
        "FENSHEN_ROAMING": str(dirs["roaming"]),
        "FENSHEN_LOCAL": str(dirs["local"]),
        "FENSHEN_LOCAL_LOW": str(dirs["local_low"]),
        # 明确不启用注册表重定向（没做 HKCU 快照，启用会让 QQ 读到空注册表）
        "FENSHEN_REG_SUBKEY": "",
        "FENSHEN_DEVICE_FAKE": "1" if device_fake else "",
        "FENSHEN_DEVICE_NAME": device_name or "",
        "FENSHEN_TASKBAR_GROUP": taskbar_group or f"Slab.QQ.{clone_id}",
    }
    return {k: v for k, v in env.items() if v != ""}


# --------------------------------------------------------------------------- #
# PE 架构探测（纯文件解析，两个平台都能跑）
# --------------------------------------------------------------------------- #
def _is_64bit_exe(exe: Path) -> bool:
    """读 PE 头判断 64 位（决定注入 inject64 还是 inject32）。"""

    try:
        with open(exe, "rb") as fh:
            head = fh.read(0x400)
        if head[:2] != b"MZ":
            return True
        offset = struct.unpack_from("<I", head, 0x3C)[0]
        if offset + 6 > len(head):
            return True
        machine = struct.unpack_from("<H", head, offset + 4)[0]
        return machine == 0x8664
    except OSError:
        return True


# --------------------------------------------------------------------------- #
# Windows 专属：进程创建 + DLL 注入
#
# 这一段是 Windows 的唯一实现，逻辑与 1.7.x 完全一致；
# 顶层 import 全部放在 ``if plat.IS_WINDOWS`` 里，否则 Linux 上 import 本模块就会
# 因为 ``ctypes.wintypes`` 不存在而 ImportError。
# --------------------------------------------------------------------------- #
if plat.IS_WINDOWS:  # pragma: no branch - 平台常量
    import ctypes.wintypes as wt

    class _STARTUPINFO(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD),
            ("lpReserved", wt.LPWSTR),
            ("lpDesktop", wt.LPWSTR),
            ("lpTitle", wt.LPWSTR),
            ("dwX", wt.DWORD),
            ("dwY", wt.DWORD),
            ("dwXSize", wt.DWORD),
            ("dwYSize", wt.DWORD),
            ("dwXCountChars", wt.DWORD),
            ("dwYCountChars", wt.DWORD),
            ("dwFillAttribute", wt.DWORD),
            ("dwFlags", wt.DWORD),
            ("wShowWindow", wt.WORD),
            ("cbReserved2", wt.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wt.HANDLE),
            ("hStdOutput", wt.HANDLE),
            ("hStdError", wt.HANDLE),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wt.HANDLE),
            ("hThread", wt.HANDLE),
            ("dwProcessId", wt.DWORD),
            ("dwThreadId", wt.DWORD),
        ]

    def _create_suspended(exe: Path, cmdline: str, cwd: Path, env_block: bytes):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateProcessW.restype = wt.BOOL
        si = _STARTUPINFO()
        si.cb = ctypes.sizeof(si)
        pi = _PROCESS_INFORMATION()
        env_buf = ctypes.create_string_buffer(env_block, len(env_block))
        flags = CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_UNICODE_ENVIRONMENT
        ok = kernel32.CreateProcessW(
            ctypes.c_wchar_p(str(exe)),
            ctypes.c_wchar_p(cmdline),
            None, None, False, flags,
            ctypes.cast(env_buf, ctypes.c_void_p),
            ctypes.c_wchar_p(str(cwd)),
            ctypes.byref(si), ctypes.byref(pi),
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "CreateProcessW 失败")
        return kernel32, pi

    def _inject_dll(kernel32, pi: "_PROCESS_INFORMATION", dll: Path) -> None:
        """VirtualAllocEx + WriteProcessMemory(路径) + CreateRemoteThread(LoadLibraryW)。"""

        k32 = kernel32
        k32.OpenProcess.restype = wt.HANDLE
        k32.VirtualAllocEx.restype = ctypes.c_void_p
        k32.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
        k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                           ctypes.POINTER(ctypes.c_size_t)]
        k32.CreateRemoteThread.restype = wt.HANDLE
        k32.CreateRemoteThread.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                           ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        k32.GetProcAddress.restype = ctypes.c_void_p
        k32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]

        handle = k32.OpenProcess(PROCESS_ALL_ACCESS, False, pi.dwProcessId)
        if not handle:
            raise OSError(ctypes.get_last_error(), "OpenProcess 失败（可能需要管理员权限）")
        try:
            dll_bytes = (str(dll) + "\0").encode("utf-16le")
            remote = k32.VirtualAllocEx(handle, None, len(dll_bytes), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
            if not remote:
                raise OSError(ctypes.get_last_error(), "VirtualAllocEx 失败")
            written = ctypes.c_size_t(0)
            if not k32.WriteProcessMemory(handle, remote, dll_bytes, len(dll_bytes), ctypes.byref(written)):
                raise OSError(ctypes.get_last_error(), "WriteProcessMemory 失败")
            load_library = k32.GetProcAddress(k32.GetModuleHandleW("kernel32.dll"), b"LoadLibraryW")
            if not load_library:
                raise OSError(ctypes.get_last_error(), "找不到 LoadLibraryW")
            tid = wt.DWORD(0)
            thread = k32.CreateRemoteThread(handle, None, 0, load_library, remote, 0, ctypes.byref(tid))
            if not thread:
                raise OSError(ctypes.get_last_error(), "CreateRemoteThread 失败（杀软拦截？）")
            try:
                k32.WaitForSingleObject(thread, 10000)
            finally:
                k32.CloseHandle(thread)
        finally:
            k32.CloseHandle(handle)

    def _launch_clone_windows(exe: Path, clone_id: str) -> Dict[str, Any]:
        is64 = _is_64bit_exe(exe)
        dll = sandbox_dir() / ("inject64.dll" if is64 else "inject32.dll")
        if not dll.is_file():
            raise FileNotFoundError(
                f"缺少沙箱注入组件：{dll}\n"
                "请把 AppClone 的 sandbox 目录（inject64.dll / inject32.dll）放到 "
                f"{sandbox_dir()}，或保留 D:\\fenshen\\dist\\sandbox"
            )

        dirs = clone_dirs(clone_id)
        for key in ("home", "roaming", "local", "local_low", "temp", "user_data"):
            dirs[key].mkdir(parents=True, exist_ok=True)

        env = build_clone_env(clone_id)
        env_block = build_env_block(env)
        cmdline = f'"{exe}" --user-data-dir="{dirs["user_data"]}"'

        kernel32, pi = _create_suspended(exe, cmdline, exe.parent, env_block)
        try:
            _inject_dll(kernel32, pi, dll)
        except Exception:
            # 注入失败就别把它挂在那里：直接结束，避免留下一个空进程
            try:
                kernel32.TerminateProcess(pi.hProcess, 1)
            finally:
                kernel32.CloseHandle(pi.hThread)
                kernel32.CloseHandle(pi.hProcess)
            raise
        kernel32.ResumeThread(pi.hThread)
        kernel32.CloseHandle(pi.hThread)
        kernel32.CloseHandle(pi.hProcess)

        time.sleep(0.6)
        bus.log("qq", f"深度分身 QQ 已启动：pid={pi.dwProcessId}（clone={clone_id}，"
                      f"{'x64' if is64 else 'x86'}，数据 {dirs['home']}）", level="info")
        return {
            "ok": True,
            "pid": int(pi.dwProcessId),
            "clone_id": clone_id,
            "arch": "x64" if is64 else "x86",
            "home": str(dirs["home"]),
            "user_data": str(dirs["user_data"]),
            "injected": str(dll),
            "note": "首次启动需要在新窗口里扫码登录机器人号；它与你自己的 QQ 数据完全分开。",
        }


# --------------------------------------------------------------------------- #
# POSIX 分身：独立 HOME（QQ 单实例锁在 $HOME/.config/QQ/SingletonLock）
# --------------------------------------------------------------------------- #
def _launch_clone_posix(exe: Path, clone_id: str) -> Dict[str, Any]:
    exe = Path(exe)
    if not exe.is_file():
        raise FileNotFoundError(
            f"没有找到 QQ：{exe}\n下一步：" + qq.LINUX_INSTALL_HINT
        )
    dirs = clone_dirs(clone_id)
    for key in ("home", "roaming", "local", "temp", "user_data"):
        dirs[key].mkdir(parents=True, exist_ok=True)
    try:
        proc = qq.spawn_posix_isolated(exe, dirs["home"])
    except OSError as exc:
        raise OSError(
            f"启动分身 QQ 失败：{exc}\n下一步：确认 {exe} 有可执行权限（sudo chmod +x {exe}），"
            "并在桌面会话里运行控制台（需要 DISPLAY/WAYLAND_DISPLAY）。"
        ) from exc
    time.sleep(0.6)
    bus.log("qq", f"HOME 分身 QQ 已启动：pid={proc.pid}（clone={clone_id}，数据 {dirs['home']}）",
            level="info")
    return {
        "ok": True,
        "pid": int(proc.pid),
        "clone_id": clone_id,
        "arch": "arm64" if plat.platform_tag().endswith("arm64") else "x64",
        "home": str(dirs["home"]),
        "user_data": str(dirs["user_data"]),
        # Linux 没有注入这一步，字段保留是为了让返回结构与 Windows 一致
        "injected": "",
        "mode": "home",
        "note": ("首次启动需要在新窗口里扫码登录机器人号；它用的是独立 HOME"
                 f"（{dirs['home']}），和你自己的 QQ 数据完全分开。"),
    }


def launch_clone(source_exe: Path, clone_id: str = "bot") -> Dict[str, Any]:
    """分身启动。

    * Windows：挂起创建 → 注入 sandbox DLL → 恢复运行（深度分身）；
    * Linux：独立 HOME 分身（换 HOME 绕过单实例锁，--no-sandbox 由平台层补）。
    """

    if plat.IS_WINDOWS:
        exe = Path(source_exe)
        if not exe.is_file():
            raise FileNotFoundError(f"找不到 QQ.exe：{exe}")
        return _launch_clone_windows(exe, clone_id)
    return _launch_clone_posix(Path(source_exe), clone_id)


def status(clone_id: str = "bot") -> Dict[str, Any]:
    """分身准备情况（按平台返回）。

    ``sandbox_dir`` / ``sandbox_ready`` 描述的是「安装包里有没有带 Windows 注入组件」，
    Linux 上不使用它（Linux 走独立 HOME，见 ``mode`` 字段）。
    """

    dirs = clone_dirs(clone_id)
    sandbox = sandbox_dir()
    info: Dict[str, Any] = {
        "ok": True,
        "clone_id": clone_id,
        "platform": plat.platform_tag(),
        "mode": "inject" if plat.IS_WINDOWS else "home",
        "sandbox_dir": str(sandbox),
        "sandbox_ready": (sandbox / "inject64.dll").is_file() and (sandbox / "inject32.dll").is_file(),
        "home": str(dirs["home"]),
        "exists": dirs["home"].is_dir(),
    }
    if plat.IS_WINDOWS:
        info["note"] = "深度分身：挂起创建 + 注入 sandbox DLL（独立数据目录 / 互斥体改名）"
    else:
        info["note"] = (
            "Linux 没有可注入的 Win32 API，这里用「独立 HOME 分身」实现多开："
            "每个分身一个 HOME，QQ 的单实例锁各自独立。sandbox_ready 只是包里自带组件的状态。"
        )
    return info
