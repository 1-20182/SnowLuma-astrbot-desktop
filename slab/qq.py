"""QQ 客户端管理：检测安装、列出进程、隔离启动、只注入指定进程。

背景（用户反馈"乱连"）：SnowLuma 默认会把机器上**所有**像 QQ 的进程都 hook 进去，
用户自己正在用的 QQ 也会被接管。这里改成：
1. 默认 `snowluma.hook_mode = "manual"`：SnowLuma 不自动注入，由控制台**只注入你选中的那个 QQ 进程**；
2. 可以在软件里「隔离启动」一个独立数据目录的 QQ（环境变量重定向 + 独立 user-data-dir），
   让机器人号和你平时用的 QQ 分开登录，互不影响。

平台差异（Windows 不变）：
* **Windows**：QQ NT 装在 ``C:\\Program Files\\Tencent\\QQNT``，安装目录从注册表卸载表里找，
  版本号靠 PowerShell 读 ``VersionInfo.FileVersion``；隔离靠 ``USERPROFILE``/``APPDATA`` 等重定向。
* **Linux**：官方 deb 装到 ``/opt/QQ``（启动器 ``/opt/QQ/qq``），目录/可执行名/进程名一律取自
  :mod:`slab.plat`，版本号走 ``dpkg-query``；隔离靠**换 HOME**——QQ 的单实例锁是
  ``$HOME/.config/QQ/SingletonLock``，不换 HOME 就起不了第二个实例。
  另外 Linux QQ（Electron）在部分发行版上不加 ``--no-sandbox`` 会直接崩。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths, plat, storage
from .events import bus
from .procutil import hidden_kwargs

#: 可执行文件名（Windows: QQ.exe…；Linux: qq/linuxqq/QQ）——统一取自平台层
QQ_EXE_NAMES = plat.qq_exe_names()
#: 进程名白名单（小写）
_PROC_EXE_NAMES = plat.qq_process_names()

#: 官方 QQNT deb 的架构 token（Debian 命名：x64 是 amd64，不是 x86_64，也不是我们的 linux-x64）
DEB_ARCH = "arm64" if plat.platform_tag().endswith("arm64") else "amd64"

#: Linux 上没装 QQ 时的安装指引（前端直接展示）
LINUX_INSTALL_HINT = (
    "Linux 版 QQ（NT）没有官方 apt 源里的通用包，请从 https://im.qq.com/linuxqq/ 下载 "
    f"{DEB_ARCH} 的 .deb 包后执行：sudo apt install -y ./QQ_*_{DEB_ARCH}.deb（一般会装到 /opt/QQ）。"
)


def _qq_profile_root() -> Path:
    base = Path(str(paths.STORAGE.get("base") or paths.APP_ROOT))
    return base / "qq"


def isolated_profile_dir() -> Path:
    return _qq_profile_root() / "profile"


def isolated_app_dir() -> Path:
    return _qq_profile_root() / "app"


def isolated_home_dir() -> Path:
    """隔离实例的「家目录」。

    POSIX 上它就是 ``HOME``（QQ 的单实例锁 ``$HOME/.config/QQ/SingletonLock`` 在这里，
    换掉 HOME 才能多开）；Windows 上只是隔离数据的落脚点。
    """

    return isolated_profile_dir()


# --------------------------------------------------------------------------- #
# 检测安装
# --------------------------------------------------------------------------- #
def _windows_install_paths() -> List[Path]:
    """Windows 上 QQ NT 的常见安装位置（其他平台返回空）。"""

    if not plat.IS_WINDOWS:
        return []
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


def _common_install_paths() -> List[Path]:
    """各平台的默认安装目录（Windows: 常见目录 + 注册表；Linux: /opt/QQ 等）。"""

    found = _windows_install_paths()
    for folder in plat.qq_install_dirs():
        if folder.is_dir() and folder not in found:
            found.append(folder)
    return found


def _registry_installs() -> List[Path]:
    """从卸载表里找 QQ 的安装目录（HKLM/HKCU 都看）；只在 Windows 生效。"""

    if not plat.IS_WINDOWS:
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows
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


def _windows_file_version(exe: Path) -> str:
    """Windows：PowerShell 读 exe 的文件版本（失败返回空串）。"""

    try:
        info = subprocess.run(  # noqa: S603
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-Item '{exe}').VersionInfo.FileVersion"],
            capture_output=True, text=True, timeout=8, **hidden_kwargs(),
        )
        return (info.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _linux_package_version(exe: Path) -> str:
    """Linux：用 dpkg-query 查包版本（先问「这个文件属于哪个包」，退化为常见包名）。

    deb 装的 QQ 没有 Windows 那种文件版本资源，最靠谱的是包管理器里的版本号；
    ``dpkg -S`` 在 ``/opt/QQ`` 这种非标准前缀目录上也能查到归属包。
    """

    dpkg_query = shutil.which("dpkg-query")
    if not dpkg_query:
        return ""
    packages: List[str] = []
    dpkg = shutil.which("dpkg")
    if dpkg:
        with contextlib.suppress(Exception):
            owner = subprocess.run(  # noqa: S603
                [dpkg, "-S", str(exe)], capture_output=True, text=True, timeout=8,
            )
            for line in (owner.stdout or "").splitlines():
                if ":" in line:
                    packages.append(line.split(":", 1)[0].strip())
                if len(packages) >= 3:
                    break
    packages.extend(["linuxqq", "qq"])
    for package in dict.fromkeys(p for p in packages if p):
        with contextlib.suppress(Exception):
            result = subprocess.run(  # noqa: S603
                [dpkg_query, "-W", "-f=${Version}", package],
                capture_output=True, text=True, timeout=8,
            )
            if result.returncode == 0:
                version = (result.stdout or "").strip()
                if version:
                    return version
    # 最后兜底：QQ NT 的 resources/app/package.json 里有版本号
    for candidate in (exe.parent / "resources" / "app" / "package.json",
                      exe.parent / "resources" / "package.json"):
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(candidate.read_text("utf-8", errors="replace"))
            if isinstance(data, dict) and data.get("version"):
                return str(data["version"])
    return ""


def _exe_version(exe: Path) -> str:
    return _windows_file_version(exe) if plat.IS_WINDOWS else _linux_package_version(exe)


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
        version = _exe_version(exe)
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
def _process_home(pid: int) -> str:
    """读 ``/proc/<pid>/environ`` 里那个进程的 HOME（POSIX；读不到返回空串）。"""

    if plat.IS_WINDOWS:
        return ""
    try:
        raw = Path(f"/proc/{int(pid)}/environ").read_bytes()
    except (OSError, ValueError):
        return ""
    for item in raw.split(b"\0"):
        if item.startswith(b"HOME="):
            return item[5:].decode("utf-8", "replace").strip()
    return ""


def list_processes() -> List[Dict[str, Any]]:
    """列出正在跑的 QQ 进程（含是否来自我们的隔离目录）。"""

    try:
        import psutil
    except ImportError:
        return []

    isolated_root = str(isolated_app_dir()).lower()
    isolated_home = str(isolated_home_dir())
    rows: List[Dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "exe", "create_time", "username", "cmdline"]):
        try:
            info = proc.info
            name = str(info.get("name") or "").lower()
            if name not in _PROC_EXE_NAMES:
                continue
            exe = str(info.get("exe") or "")
            # Linux 的隔离实例换的是 HOME（不是 exe 路径），按 /proc/<pid>/environ 认
            isolated = bool(isolated_root and exe.lower().startswith(isolated_root))
            if not isolated and isolated_home:
                isolated = _process_home(int(info.get("pid") or 0)) == isolated_home
            rows.append({
                "pid": info.get("pid"),
                "exe": exe,
                "dir": str(Path(exe).parent) if exe else "",
                "started_at": info.get("create_time") or 0,
                "age_seconds": int(time.time() - (info.get("create_time") or time.time())),
                "user": str(info.get("username") or ""),
                "cmdline": " ".join(info.get("cmdline") or [])[:300],
                "isolated": isolated,
            })
        except Exception:  # noqa: BLE001 - 进程可能刚好退出
            continue
    rows.sort(key=lambda r: (not r["isolated"], -float(r["age_seconds"])))
    return rows


# --------------------------------------------------------------------------- #
# 隔离启动
# --------------------------------------------------------------------------- #
def _resolve_exe(source_exe: str = "") -> Optional[Path]:
    """确定要启动的 QQ 可执行文件（显式指定优先，其次自动探测）。"""

    candidates = [source_exe] if source_exe else []
    candidates += [item["exe"] for item in list_installs()]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def posix_isolated_env(home: Path) -> Dict[str, str]:
    """POSIX 隔离实例的环境变量：HOME 与 XDG_* 全部指到分身目录。

    为什么必须真的换 HOME：QQ（NT/Linux）的单实例锁是 ``$HOME/.config/QQ/SingletonLock``，
    只改 ``XDG_CONFIG_HOME`` 时它仍会去 ``$HOME`` 下拿锁，第二个实例直接"唤醒"第一个。
    ``TMPDIR`` 一并隔离，避免两个实例共用 socket。
    """

    home = Path(home)
    dirs = {
        "home": home,
        "config": home / ".config",
        "data": home / ".local" / "share",
        "state": home / ".local" / "state",
        "cache": home / ".cache",
        "tmp": home / "tmp",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(dirs["home"]),
        "XDG_CONFIG_HOME": str(dirs["config"]),
        "XDG_DATA_HOME": str(dirs["data"]),
        "XDG_STATE_HOME": str(dirs["state"]),
        "XDG_CACHE_HOME": str(dirs["cache"]),
        "TMPDIR": str(dirs["tmp"]),
        "SLAB_ISOLATED_QQ": "1",
    }


def spawn_posix_isolated(exe: Path, home: Path, extra_args: Optional[List[str]] = None) -> "subprocess.Popen[bytes]":
    """用独立 HOME 起一个 QQ（POSIX）。返回 Popen，调用方负责报告 pid。"""

    exe = Path(exe)
    env = os.environ.copy()
    env.update(posix_isolated_env(home))
    args = [str(exe), *plat.qq_main_arg(), *(extra_args or [])]
    return subprocess.Popen(  # noqa: S603
        args,
        env=env,
        cwd=str(exe.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # 独立会话：控制台退出不会连带收掉 QQ
    )


def launch_isolated(source_exe: str = "") -> Dict[str, Any]:
    """用独立数据目录启动一个 QQ（轻量隔离：环境变量 + 独立 user-data-dir/HOME）。

    注意：这是"让机器人号和你自己用的 QQ 分开"的最小方案，
    不修改注册表、不改设备指纹；Linux 上靠独立 HOME 绕过单实例锁，
    Windows 上如果 QQ 版本强制单实例，可能仍会被主实例拦下。
    """

    exe = _resolve_exe(source_exe)
    if exe is None:
        if plat.IS_WINDOWS:
            raise FileNotFoundError(
                "没有找到 QQ.exe：请先安装 QQ（NT 版），或在「QQ 客户端」里手动指定路径"
            )
        raise FileNotFoundError(
            "没有找到 QQ：请先安装 Linux 版 QQ（官方 deb，一般装到 /opt/QQ），"
            "或在「QQ 客户端」里手动指定路径。\n下一步：" + LINUX_INSTALL_HINT
        )

    if plat.IS_WINDOWS:
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
        note = ("隔离实例首次启动需要重新扫码登录；如果 QQ 版本强制单实例，可能只弹旧窗口，"
                "这时请先退出自己的 QQ 再试，或改用「使用本机已登录的 QQ」。")
    else:
        profile = isolated_home_dir()
        try:
            proc = spawn_posix_isolated(exe, profile)
        except OSError as exc:
            raise RuntimeError(
                f"启动隔离 QQ 失败：{exc}\n下一步：确认 /opt/QQ/qq 有可执行权限"
                f"（sudo chmod +x {exe}），或在桌面会话里运行控制台（需要 DISPLAY/WAYLAND_DISPLAY）。"
            ) from exc
        note = ("已用独立 HOME 启动一个 QQ 分身（数据在 "
                f"{profile}），首次启动需要重新扫码登录；"
                "它和你自己的 QQ 互不可见（单实例锁在各自的 $HOME/.config/QQ 下）。")

    bus.log("qq", f"已用隔离数据目录启动 QQ：pid={proc.pid}，数据在 {profile}", level="info")
    return {
        "ok": True,
        "pid": proc.pid,
        "exe": str(exe),
        "profile": str(profile),
        "home": str(profile),
        "isolation": "userprofile" if plat.IS_WINDOWS else "home",
        "note": note,
    }


def summary() -> Dict[str, Any]:
    """QQ 页首屏数据：装了哪些、跑着哪些、隔离目录在哪、能不能多开。"""

    injection: Dict[str, Any] = {"ok": True, "level": "ok", "message": "", "fix": ""}
    if not plat.IS_WINDOWS:
        with contextlib.suppress(Exception):
            injection = plat.qq_injection_readiness()
    return {
        "ok": True,
        "platform": plat.platform_tag(),
        "is_windows": plat.IS_WINDOWS,
        # Windows: 深度分身（DLL 注入）；Linux: 独立 HOME 多开
        "clone_mode": "inject" if plat.IS_WINDOWS else "home",
        "installs": list_installs(),
        "processes": list_processes(),
        "isolated_profile": str(isolated_profile_dir()),
        "isolated_home": str(isolated_home_dir()),
        "storage_base": str(storage.plan(paths.APP_ROOT).get("base")),
        "injection": injection,
        "install_hint": "" if plat.IS_WINDOWS else LINUX_INSTALL_HINT,
    }
