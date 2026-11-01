"""QQ 客户端管理：检测安装、列出进程、隔离启动、只注入指定进程。

背景（用户反馈"乱连"）：SnowLuma 默认会把机器上**所有**像 QQ 的进程都 hook 进去，
用户自己正在用的 QQ 也会被接管。这里改成：
1. 默认 `snowluma.hook_mode = "manual"`：SnowLuma 不自动注入，由控制台**只注入你选中的那个 QQ 进程**；
2. 可以在软件里「隔离启动」一个独立数据目录的 QQ（环境变量重定向 + 独立 user-data-dir），
   让机器人号和你平时用的 QQ 分开登录，互不影响。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths, storage
from .events import bus
from .procutil import hidden_kwargs

QQ_EXE_NAMES = ("QQ.exe", "QQEX.exe", "QQScLauncher.exe")
_PROC_EXE_NAMES = {"qq.exe"}


def _qq_profile_root() -> Path:
    base = Path(str(paths.STORAGE.get("base") or paths.APP_ROOT))
    return base / "qq"


def isolated_profile_dir() -> Path:
    return _qq_profile_root() / "profile"


def isolated_app_dir() -> Path:
    return _qq_profile_root() / "app"


# --------------------------------------------------------------------------- #
# 检测安装
# --------------------------------------------------------------------------- #
def _common_install_paths() -> List[Path]:
    env = os.environ
    candidates = [
        Path(r"C:\Program Files\Tencent\QQNT"),
        Path(r"C:\Program Files (x86)\Tencent\QQNT"),
        Path(r"D:\Program Files\Tencent\QQNT"),
        Path(r"E:\Program Files\Tencent\QQNT"),
        Path(env.get("LOCALAPPDATA", "")) / "Programs" / "QQNT",
        Path(env.get("ProgramFiles", "")) / "Tencent" / "QQNT",
        Path(env.get("ProgramFiles(x86)", "")) / "Tencent" / "QQNT",
    ]
    return [p for p in candidates if str(p) and p.is_dir()]


def _registry_installs() -> List[Path]:
    """从卸载表里找 QQ 的安装目录（HKLM/HKCU 都看）。"""

    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []

    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    found: List[Path] = []
    for hive, sub in roots:
        try:
            with winreg.OpenKey(hive, sub) as root:
                count = winreg.QueryInfoKey(root)[0]
                for i in range(count):
                    try:
                        name = winreg.EnumKey(root, i)
                        with winreg.OpenKey(root, name) as item:
                            display = str(winreg.QueryValueEx(item, "DisplayName")[0] or "")
                            if "QQ" not in display.upper() or "NT" not in display.upper():
                                continue
                            loc = str(winreg.QueryValueEx(item, "InstallLocation")[0] or "")
                            if loc:
                                candidate = Path(loc)
                                found.append(candidate if candidate.name.upper() == "QQNT" else candidate)
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def find_qq_exe(folder: Path) -> Optional[Path]:
    for name in QQ_EXE_NAMES:
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def list_installs() -> List[Dict[str, Any]]:
    """本机装了哪些 QQ（去重 + 标出版本与是否可用）。"""

    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for folder in _common_install_paths() + _registry_installs():
        exe = find_qq_exe(folder)
        if not exe:
            continue
        key = str(exe).lower()
        if key in seen:
            continue
        seen.add(key)
        version = ""
        try:
            import subprocess as _sp

            info = _sp.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-Item '{exe}').VersionInfo.FileVersion"],
                capture_output=True, text=True, timeout=8, **hidden_kwargs(),
            )
            version = (info.stdout or "").strip()
        except Exception:  # noqa: BLE001
            version = ""
        out.append({
            "exe": str(exe),
            "dir": str(exe.parent),
            "version": version,
            "isolated_copy": str(isolated_app_dir()) if str(exe).lower().startswith(str(isolated_app_dir()).lower()) else "",
        })
    # 我们自己的隔离副本放最前
    out.sort(key=lambda item: 0 if item["isolated_copy"] else 1)
    return out


# --------------------------------------------------------------------------- #
# 进程
# --------------------------------------------------------------------------- #
def list_processes() -> List[Dict[str, Any]]:
    """列出正在跑的 QQ 进程（含是否来自我们的隔离目录）。"""

    try:
        import psutil
    except ImportError:
        return []

    isolated_root = str(isolated_app_dir()).lower()
    rows: List[Dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "exe", "create_time", "username", "cmdline"]):
        try:
            info = proc.info
            name = str(info.get("name") or "").lower()
            if name not in _PROC_EXE_NAMES:
                continue
            exe = str(info.get("exe") or "")
            rows.append({
                "pid": info.get("pid"),
                "exe": exe,
                "dir": str(Path(exe).parent) if exe else "",
                "started_at": info.get("create_time") or 0,
                "age_seconds": int(time.time() - (info.get("create_time") or time.time())),
                "user": str(info.get("username") or ""),
                "cmdline": " ".join(info.get("cmdline") or [])[:300],
                "isolated": bool(isolated_root and exe.lower().startswith(isolated_root)),
            })
        except Exception:  # noqa: BLE001 - 进程可能刚好退出
            continue
    rows.sort(key=lambda r: (not r["isolated"], -float(r["age_seconds"])))
    return rows


# --------------------------------------------------------------------------- #
# 隔离启动
# --------------------------------------------------------------------------- #
def launch_isolated(source_exe: str = "") -> Dict[str, Any]:
    """用独立数据目录启动一个 QQ（轻量隔离：环境变量 + 独立 user-data-dir）。

    注意：这是"让机器人号和你自己用的 QQ 分开"的最小方案，
    不修改注册表、不改设备指纹；如果 QQ 版本强制单实例，可能仍会被主实例拦下。
    """

    candidates = [source_exe] if source_exe else []
    candidates += [item["exe"] for item in list_installs()]
    exe: Optional[Path] = None
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            exe = Path(candidate)
            break
    if exe is None:
        raise FileNotFoundError("没有找到 QQ.exe：请先安装 QQ（NT 版），或在「QQ 客户端」里手动指定路径")

    profile = isolated_profile_dir()
    app_dir = isolated_app_dir()
    for sub in ("AppData", "LocalAppData", "Temp", "Documents", "Downloads"):
        (profile / sub).mkdir(parents=True, exist_ok=True)
    app_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update({
        "USERPROFILE": str(profile),
        "APPDATA": str(profile / "AppData"),
        "LOCALAPPDATA": str(profile / "LocalAppData"),
        "TEMP": str(profile / "Temp"),
        "TMP": str(profile / "Temp"),
        "HOMEDRIVE": profile.drive or "C:",
        "HOMEPATH": str(profile)[len(profile.drive):] if profile.drive else str(profile),
        "SLAB_ISOLATED_QQ": "1",
    })

    args = [str(exe), f"--user-data-dir={profile / 'user-data'}"]
    try:
        proc = subprocess.Popen(  # noqa: S603
            args,
            env=env,
            cwd=str(exe.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x00000008,
        )
    except OSError as exc:
        raise RuntimeError(f"启动隔离 QQ 失败：{exc}") from exc

    bus.log("qq", f"已用隔离数据目录启动 QQ：pid={proc.pid}，数据在 {profile}", level="info")
    return {
        "ok": True,
        "pid": proc.pid,
        "exe": str(exe),
        "profile": str(profile),
        "note": "隔离实例首次启动需要重新扫码登录；如果 QQ 版本强制单实例，可能只弹旧窗口，"
                "这时请先退出自己的 QQ 再试，或改用「使用本机已登录的 QQ」。",
    }


def summary() -> Dict[str, Any]:
    return {
        "ok": True,
        "installs": list_installs(),
        "processes": list_processes(),
        "isolated_profile": str(isolated_profile_dir()),
        "storage_base": str(storage.plan(paths.APP_ROOT).get("base")),
    }
