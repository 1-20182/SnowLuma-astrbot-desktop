"""存储位置规划：默认**不要装到 C 盘**（数据、安装、下载缓存都一样）。

规则（按用户要求）：
1. 只要有非 C 盘的**固定磁盘**（本地硬盘），默认把东西放在剩余空间最大的那块；
2. 机器只有一块盘（只有 C）时，才用 C；
3. 显式环境变量优先：``SLAB_DATA_DIR`` / ``SLAB_RUNTIME`` / ``SLAB_DOWNLOAD_DIR`` / ``SLAB_STORAGE_ROOT``；
4. 老用户不搬家：程序目录旁边已经有 ``runtime\\``（装过东西）或 ``data\\config.json``（存过配置），
   就继续用原位置，避免升级后"配置/安装在 C 盘"的旧用户突然丢状态。

目录结构（自动选的根目录下）::

    D:\\SnowLumaAstrBot\\
      data\\        配置、日志（很小）
      runtime\\     SnowLuma / AstrBot 的实际安装
      downloads\\   下载的发行包缓存

所有探测都是"失败就退化"，不会因为某块盘读不到容量就崩。
"""

from __future__ import annotations

import os
import shutil
import string
from pathlib import Path
from typing import Dict, List, Optional

APP_FOLDER = "SnowLumaAstrBot"
SYSTEM_DRIVE = "C"


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

    if os.name != "nt":
        return False
    try:
        import ctypes

        return ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") == 3
    except Exception:  # noqa: BLE001 - 读不到就当不是固定盘
        return False


def list_drives() -> List[Dict[str, object]]:
    """列出可用磁盘（Windows 枚举盘符；其它系统只有根分区）。"""

    if os.name != "nt":
        root = Path("/")
        return [{"letter": "/", "root": str(root), "is_system": True,
                 "free_gb": _free_gb(root), "total_gb": _total_gb(root)}]

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


def _has_legacy_runtime(app_root: Path) -> bool:
    legacy = app_root / "runtime"
    return legacy.is_dir() and any((legacy / name).exists() for name in ("snowluma", "astrbot"))


def _has_legacy_data(app_root: Path) -> bool:
    legacy = app_root / "data"
    return (legacy / "config.json").exists() or (legacy / "logs").is_dir()


def plan(app_root: Path) -> Dict[str, object]:
    """算出数据 / 运行时 / 下载缓存该放哪（纯计算，不创建目录）。"""

    drives = list_drives()
    env_root = (os.environ.get("SLAB_STORAGE_ROOT") or "").strip()
    reasons: List[str] = []

    if env_root:
        base = Path(env_root).expanduser()
        reason = f"使用环境变量 SLAB_STORAGE_ROOT（{base}）"
        source = "env"
    else:
        chosen = _largest_non_system(drives)
        if chosen is not None:
            base = Path(str(chosen["root"])) / APP_FOLDER
            reason = f"自动选择非系统盘 {chosen['root']}（剩余 {chosen['free_gb']} GB）"
            source = "auto"
        else:
            system = next((d for d in drives if d.get("is_system")), {"root": "C:\\"})
            base = Path(str(system.get("root") or "C:\\")) / APP_FOLDER
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

    info = plan(app_root)
    info["app_root"] = str(app_root)
    info["system_drive"] = SYSTEM_DRIVE
    return info
