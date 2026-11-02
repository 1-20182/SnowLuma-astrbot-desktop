"""存储位置规划：默认**不要装到系统盘 / 系统分区**（数据、安装、下载缓存都一样）。

规则（按用户要求）：
1. 只要有非系统盘的**固定磁盘**（本地硬盘 / 本地挂载点），默认把东西放在剩余空间最大的那块；
2. 机器只有一块盘时，Windows 才用 C；Linux 按 XDG 放到 ``~/.local/share/SnowLumaAstrBot``
   （普通用户对 ``/`` 没有写权限，不能像 Windows 那样往根分区扔）；
3. 显式环境变量优先：``SLAB_DATA_DIR`` / ``SLAB_RUNTIME`` / ``SLAB_DOWNLOAD_DIR`` / ``SLAB_STORAGE_ROOT``；
4. 老用户不搬家：程序目录旁边已经有 ``runtime/``（装过东西）或 ``data/config.json``（存过配置），
   就继续用原位置，避免升级后"配置/安装在 C 盘"的旧用户突然丢状态。

目录结构（自动选的根目录下）::

    D:\\SnowLumaAstrBot\\        或  ~/.local/share/SnowLumaAstrBot/
      data/        配置、日志（很小）
      runtime/     SnowLuma / AstrBot 的实际安装
      downloads/   下载的发行包缓存

平台差异只在 :mod:`slab.plat`（``default_storage_base`` / ``IS_WINDOWS``）；
所有探测都是"失败就退化"，不会因为某块盘读不到容量就崩。
"""

from __future__ import annotations

import os
import shutil
import string
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import plat

APP_FOLDER = "SnowLumaAstrBot"
#: Windows 的系统盘（盘符）
SYSTEM_DRIVE = "C"
#: POSIX 的系统挂载点
SYSTEM_MOUNT = "/"

#: POSIX 上算"本地固定磁盘"的文件系统；tmpfs / overlay / 9p / nfs 之类一律不算，
#: 否则容器的 /dev/shm、WSL 的 /mnt/wslg 会被当成"第二块盘"，把数据放到临时目录里。
_FIXED_FSTYPES = frozenset({
    "ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "jfs", "reiserfs",
    "vfat", "exfat", "ntfs", "ntfs3", "hfs", "hfsplus", "apfs", "zfs",
})


def _free_gb(path: Path) -> float:
    try:
        return round(shutil.disk_usage(str(path)).free / (1024**3), 1)
    except OSError:
        return -1.0


def _total_gb(path: Path) -> float:
    try:
        return round(shutil.disk_usage(str(path)).total / (1024**3), 1)
    except OSError:
        return -1.0


def _is_fixed_drive(letter: str) -> bool:
    """Windows：DRIVE_FIXED(3) 才算本地硬盘（排除 U 盘 / 光驱 / 网络盘）。"""

    if not plat.IS_WINDOWS:
        return False
    try:
        import ctypes

        return ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") == 3
    except Exception:  # noqa: BLE001 - 读不到就当不是固定盘
        return False


def _mount_table() -> List[Tuple[str, str, str]]:
    """Linux 的挂载表：``[(设备, 挂载点, 文件系统类型)]``，只保留本地固定磁盘；读不到返回空。

    ``/proc/mounts`` 里空格等字符是八进制转义（``\\040``），这里顺手还原。
    """

    points: List[Tuple[str, str, str]] = []
    try:
        text = Path("/proc/mounts").read_text("utf-8", errors="replace")
    except OSError:
        return points
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mountpoint, fstype = parts[0], parts[1], parts[2].lower()
        if fstype not in _FIXED_FSTYPES:
            continue
        mountpoint = (mountpoint.replace("\\040", " ").replace("\\011", "\t")
                      .replace("\\012", "\n").replace("\\134", "\\"))
        points.append((device, mountpoint, fstype))
    return points


def _list_mounts() -> List[Dict[str, object]]:
    """POSIX：把本地挂载点当作"盘"列出来（按设备去重，bind mount 只算一次）。"""

    drives: List[Dict[str, object]] = []
    seen: set = set()
    # "/" 永远排第一：按设备去重时先认系统分区，别让 bind mount（WSL 的 /mnt/wslg/distro）
    # 或 btrfs subvol 把根分区挤掉
    table = sorted(_mount_table(), key=lambda item: (item[1] != SYSTEM_MOUNT, len(item[1])))
    for device, mountpoint, fstype in table:
        if device in seen:
            continue
        seen.add(device)
        free = _free_gb(Path(mountpoint))
        if free < 0:  # 读不到容量（权限/已卸载）→ 跳过
            continue
        drives.append({
            "letter": mountpoint,  # 前端"盘符"列直接显示挂载点
            "root": mountpoint,
            "device": device,
            "fstype": fstype,
            "is_system": mountpoint == SYSTEM_MOUNT,
            "free_gb": free,
            "total_gb": _total_gb(Path(mountpoint)),
        })
    if not drives:  # 容器 / 非 Linux：只剩根分区这一条
        root = Path(SYSTEM_MOUNT)
        drives.append({"letter": SYSTEM_MOUNT, "root": SYSTEM_MOUNT, "device": "", "fstype": "",
                       "is_system": True, "free_gb": _free_gb(root), "total_gb": _total_gb(root)})
    return drives


def list_drives() -> List[Dict[str, object]]:
    """列出可用磁盘：Windows 枚举盘符；POSIX 枚举本地挂载点（都带剩余/总容量）。"""

    if not plat.IS_WINDOWS:
        return _list_mounts()

    drives: List[Dict[str, object]] = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        if not root.exists() or not _is_fixed_drive(letter):
            continue
        free = _free_gb(root)
        if free < 0:
            continue
        drives.append({
            "letter": letter,
            "root": str(root),
            "is_system": letter.upper() == SYSTEM_DRIVE,
            "free_gb": free,
            "total_gb": _total_gb(root),
        })
    return drives


def _largest_non_system(drives: List[Dict[str, object]]) -> Optional[Dict[str, object]]:
    candidates = [d for d in drives if not d.get("is_system") and float(d.get("free_gb") or 0) > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda d: float(d.get("free_gb") or 0))  # type: ignore[arg-type]


def _system_root(drives: List[Dict[str, object]]) -> str:
    """系统盘 / 系统挂载点（拿不到返回空串）。"""

    system = next((d for d in drives if d.get("is_system")), None)
    return str((system or {}).get("root") or "")


def _system_root_is_posix(drives: List[Dict[str, object]]) -> bool:
    """系统"盘"是不是 POSIX 挂载点（``/`` 而不是 ``C:\\``）。

    只有真·POSIX 根才走 XDG 那条路；测试里灌进来的 Windows 样式盘符
    （``C:\\``）不满足"以 / 开头"，会继续按盘符逻辑算。

    注意：不能用 ``Path(root).is_absolute()`` 判断——Windows 上 ``Path("/")``
    不是绝对路径（缺盘符），会让 Linux 逻辑在 Windows 上跑测试时失效。
    """

    root = _system_root(drives)
    return bool(root) and root.startswith("/")


def _has_legacy_runtime(app_root: Path) -> bool:
    legacy = app_root / "runtime"
    return legacy.is_dir() and any((legacy / name).exists() for name in ("snowluma", "astrbot"))


def _has_legacy_data(app_root: Path) -> bool:
    legacy = app_root / "data"
    return (legacy / "config.json").exists() or (legacy / "logs").is_dir()


def plan(app_root: Path) -> Dict[str, object]:
    """算出数据 / 运行时 / 下载缓存该放哪（纯计算，不创建目录）。

    选择顺序：环境变量 → 剩余空间最大的非系统盘/挂载点 → 系统盘（Windows）/
    XDG 用户数据目录（POSIX）。
    """

    app_root = Path(app_root)
    drives = list_drives()
    env_root = (os.environ.get("SLAB_STORAGE_ROOT") or "").strip()
    reasons: List[str] = []

    if env_root:
        base = Path(env_root).expanduser()
        reason = f"使用环境变量 SLAB_STORAGE_ROOT（{base}）"
        source = "env"
    else:
        chosen = _largest_non_system(drives)
        xdg_base = plat.default_storage_base()  # Windows 恒为 None
        if chosen is not None:
            base = Path(str(chosen["root"])) / APP_FOLDER
            reason = f"自动选择非系统盘 {chosen['root']}（剩余 {chosen['free_gb']} GB）"
            source = "auto"
        elif xdg_base is not None and _system_root_is_posix(drives):
            # POSIX 单盘：按 XDG 放用户目录（普通用户对 / 没有写权限）
            base = Path(xdg_base)
            reason = f"只有一块盘，按 XDG 规范使用用户数据目录（{base}）"
            source = "xdg"
        else:
            system_root = _system_root(drives) or f"{SYSTEM_DRIVE}:\\"
            base = Path(system_root) / APP_FOLDER
            reason = "只有一块盘，使用系统盘"
            source = "auto-single-drive"

    legacy_runtime = _has_legacy_runtime(app_root)
    legacy_data = _has_legacy_data(app_root)

    runtime_dir = app_root / "runtime" if legacy_runtime else base / "runtime"
    data_dir = app_root / "data" if legacy_data else base / "data"
    downloads_dir = base / "downloads"

    if legacy_runtime:
        reasons.append(f"检测到已有安装，沿用 {runtime_dir}")
    if legacy_data:
        reasons.append(f"检测到已有配置，沿用 {data_dir}")
    if reasons:
        reason = reason + "；" + "；".join(reasons)

    return {
        "base": str(base),
        "data_dir": str(data_dir),
        "runtime_dir": str(runtime_dir),
        "downloads_dir": str(downloads_dir),
        "reason": reason,
        "source": source,
        "drives": drives,
        "legacy_runtime": legacy_runtime,
        "legacy_data": legacy_data,
    }


def describe(app_root: Path) -> Dict[str, object]:
    """给前端看的说明（含每块盘的剩余空间）。"""

    app_root = Path(app_root)
    info = plan(app_root)
    info["app_root"] = str(app_root)
    # 语义按平台：Windows 是盘符（C），POSIX 是挂载点（/）
    info["system_drive"] = SYSTEM_DRIVE if plat.IS_WINDOWS else SYSTEM_MOUNT
    info["is_windows"] = plat.IS_WINDOWS
    return info
