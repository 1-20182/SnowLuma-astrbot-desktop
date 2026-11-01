"""从 zip 升级：把新版本安全地盖到旧程序上，数据与安装目录一律不动。

为什么要一个"助手进程"
--------------------
Windows 不允许覆盖正在运行的程序文件（exe / dll 被占用），所以流程是：

1. 本程序解压新 zip 到 ``data/upgrade/staged``，校验版本与文件；
2. 生成 ``data/upgrade/apply-upgrade.cmd`` 并**脱离**启动它；
3. 本程序退出；助手等进程彻底退出后：
   - 把旧程序目录备份到 ``data/backups/program-<旧版本>-<时间>``（排除数据/安装/缓存）；
   - 用 ``robocopy /E`` 把新文件盖上去（不 purge，绝不删用户目录）；
   - 失败就回滚备份并启动旧版本；
   - 成功后启动新版本，日志写到 ``data/upgrade/upgrade.log``。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths
from .events import bus

#: 这些目录属于"用户数据"，升级时绝不覆盖、也不参与备份清理
PROTECTED = ("data", "runtime", "downloads", "vendor")  # vendor 里有注入组件，跟版本走 → 单独处理
PROTECTED = ("data", "runtime", "downloads")
ENTRY_HINTS = ("SnowLuma-Desktop.exe", "SnowLuma-AstrBot-Console.exe")


def upgrade_dir() -> Path:
    d = paths.DATA_DIR / "upgrade"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _version_tuple(value: str) -> tuple:
    parts = re.findall(r"\d+", str(value or "0"))
    nums = [int(p) for p in parts[:3]] + [0, 0, 0]
    return tuple(nums[:3])


def read_zip_version(zf: zipfile.ZipFile) -> str:
    """zip 里任意层级的 VERSION 文件（我们是 <根目录>/VERSION），取最浅的那个。"""

    candidates = [
        name for name in zf.namelist()
        if not name.endswith("/") and Path(name).name.upper() == "VERSION"
    ]
    candidates.sort(key=lambda name: name.count("/"))
    for name in candidates:
        try:
            text = zf.read(name).decode("utf-8", "replace").strip()
        except (KeyError, OSError):
            continue
        if text:
            return text
    return ""


def _single_root(names: List[str]) -> str:
    """zip 里通常有一层顶层目录（SnowLuma-Desktop/…），返回它；没有就空串。"""

    tops = {n.split("/")[0] for n in names if n.strip("/")}
    if len(tops) == 1:
        only = tops.pop()
        if any(n.startswith(only + "/") for n in names):
            return only
    return ""


def inspect_zip(zip_path: Path) -> Dict[str, Any]:
    """只看不动：判断这个 zip 是不是新版本、里面有没有可执行文件。"""

    zip_path = Path(zip_path)
    if not zip_path.is_file():
        return {"ok": False, "error": f"找不到升级包：{zip_path}"}
    if zip_path.suffix.lower() != ".zip":
        return {"ok": False, "error": "升级包必须是 .zip（就是我们发布的那个压缩包）"}
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            version = read_zip_version(zf)
            entries = [n for n in names if Path(n).name in ENTRY_HINTS]
            root = _single_root(names)
    except zipfile.BadZipFile:
        return {"ok": False, "error": "这个文件不是有效的 zip，可能下载不完整"}
    if not entries:
        return {"ok": False, "error": "压缩包里没有 SnowLuma 控制台的 exe，确认下是不是下错了包"}

    current = paths.APP_VERSION
    newer = _version_tuple(version) > _version_tuple(current)
    return {
        "ok": True,
        "version": version or "未知",
        "current": current,
        "newer": newer,
        "same": _version_tuple(version) == _version_tuple(current),
        "root": root,
        "entry": Path(entries[0]).name,
        "files": len(names),
        "size_mb": round(zip_path.stat().st_size / 1048576, 1),
        "zip": str(zip_path),
    }


def plan() -> Dict[str, Any]:
    """升级会影响什么、不会动什么 —— 给前端摊开给用户看。"""

    program = Path(paths.APP_ROOT)
    data = Path(paths.DATA_DIR)
    runtime = Path(paths.RUNTIME_DIR)
    downloads = Path(paths.DOWNLOAD_DIR)
    desktop = bool(os.environ.get("SLAB_DESKTOP"))
    restart = ""
    if desktop:
        # 桌面壳在程序目录下；后端在 resources/backend
        for candidate in (program / "SnowLuma-Desktop.exe", program.parent / "SnowLuma-Desktop.exe"):
            if candidate.is_file():
                restart = str(candidate)
                break
    if not restart:
        for name in ENTRY_HINTS:
            if (program / name).is_file():
                restart = str(program / name)
                break

    return {
        "ok": True,
        "current_version": paths.APP_VERSION,
        "program_dir": str(program),
        "data_dir": str(data),
        "runtime_dir": str(runtime),
        "downloads_dir": str(downloads),
        "keep": [str(data), str(runtime), str(downloads)],
        "data_inside_program": _is_inside(data, program),
        "restart": restart,
        "desktop": desktop,
        "staged_dir": str(upgrade_dir() / "staged"),
    }


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def stage(zip_path: Path) -> Dict[str, Any]:
    """解压到 data/upgrade/staged（放在程序目录外，升级过程中不会被覆盖）。"""

    info = inspect_zip(zip_path)
    if not info.get("ok"):
        return info
    target = upgrade_dir() / "staged"
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(Path(zip_path)) as zf:
            zf.extractall(target)
    except (zipfile.BadZipFile, OSError) as exc:
        return {"ok": False, "error": f"解压失败：{exc}"}

    root = target / info["root"] if info.get("root") else target
    if not root.is_dir():
        return {"ok": False, "error": "解压后的目录结构不对，找不到程序文件"}
    info.update({"ok": True, "staged_root": str(root), "staged": str(target)})
    bus.log("console", f"升级包已解压：v{info['version']} → {root}", level="info")
    return info


def helper_script(staged_root: Path, *, restart: str, pid: int, keep: List[str], backup_dir: Path) -> Path:
    """生成升级助手：PowerShell 脚本 + cmd 外壳（cmd 只负责把 PS 拉起来）。

    为什么用 PowerShell 而不是纯批处理：批处理里 `tasklist | find` 的 errorlevel 在不同语言版本下
    不稳定（实测会卡在等待循环里），而且 robocopy 的返回码、中文日志都容易踩坑。
    PowerShell 对进程、文件锁、日志的处理都更省心。
    """

    program = Path(paths.APP_ROOT)
    log = upgrade_dir() / "upgrade.log"
    excludes = " ".join(f'"{Path(p).name}"' for p in keep if _is_inside(Path(p), program))
    ps1 = upgrade_dir() / "apply-upgrade.ps1"
    script = upgrade_dir() / "apply-upgrade.cmd"

    ps = f'''# 由 SnowLuma 控制台生成：等待旧进程退出 → 备份 → 覆盖 → 重启（失败回滚）
$ErrorActionPreference = "Continue"
$Log = "{log}"
$Src = "{staged_root}"
$Dst = "{program}"
$Bak = "{backup_dir}"
$WaitPid = {pid}
$Restart = "{restart}"

function Write-Log([string]$m) {{
    try {{ ("{{0}} {{1}}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m) | Out-File -FilePath $Log -Append -Encoding utf8 }} catch {{}}
}}

Write-Log "升级开始：等待 pid $WaitPid 退出"
$deadline = (Get-Date).AddMinutes(3)
while (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue) {{
    if ((Get-Date) -gt $deadline) {{ Write-Log "等待超时（进程仍在运行），放弃覆盖"; exit 2 }}
    Start-Sleep -Milliseconds 500
}}
Write-Log "旧进程已退出，开始备份程序目录"

robocopy $Dst $Bak /E /NFL /NDL /NJH /NJS /XD {excludes} | Out-Null
Write-Log "备份完成：$Bak"

robocopy $Src $Dst /E /NFL /NDL /NJH /NJS /XD {excludes} | Out-Null
$code = $LASTEXITCODE
if ($code -ge 8) {{
    Write-Log "覆盖失败（robocopy=$code），开始回滚"
    robocopy $Bak $Dst /E /NFL /NDL /NJH /NJS /XD {excludes} | Out-Null
    Write-Log "回滚完成（robocopy=$LASTEXITCODE），启动旧版本"
    if (Test-Path $Restart) {{ Start-Process -FilePath $Restart }}
    exit 1
}}

Write-Log "覆盖完成（robocopy=$code），启动新版本"
if (Test-Path $Restart) {{
    Start-Process -FilePath $Restart
    Write-Log "已启动：$Restart"
}} else {{
    Write-Log "找不到启动目标：$Restart（请手动打开程序）"
}}
Write-Log "升级结束"
'''
    ps1.write_text(ps, encoding="utf-8-sig")   # PS 5.1 需要 BOM，否则中文注释会乱码
    shim = "\r\n".join([
        "@echo off",
        "chcp 65001 >nul",
        f'powershell -NoProfile -ExecutionPolicy Bypass -File "{ps1}"',
    ]) + "\r\n"
    script.write_text(shim, encoding="utf-8")
    return script

def start_upgrade(zip_path: Path, *, force: bool = False) -> Dict[str, Any]:
    """执行升级：解压 → 生成助手脚本 → 让调用方退出 → 助手接管覆盖与重启。"""

    staged = stage(zip_path)
    if not staged.get("ok"):
        return staged
    if not staged.get("newer") and not force and not staged.get("same"):
        return {"ok": False, "error": f"这个包是 v{staged['version']}，不比你现在的 v{paths.APP_VERSION} 新；"
                                     "要降级请勾选强制。"}

    info = plan()
    if not info["restart"]:
        return {"ok": False, "error": "找不到要重启的程序 exe，无法自动完成升级；请手动解压覆盖。"}

    backup = paths.DATA_DIR / "backups" / f"program-v{paths.APP_VERSION}-{time.strftime('%Y%m%d-%H%M%S')}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    script = helper_script(
        Path(staged["staged_root"]),
        restart=info["restart"],
        pid=os.getpid(),
        keep=info["keep"],
        backup_dir=backup,
    )
    bus.log("console", f"开始升级到 v{staged['version']}：备份 → 覆盖 → 重启（日志 {upgrade_dir() / 'upgrade.log'}）",
            level="warn")
    try:
        subprocess.Popen(  # noqa: S603
            ["cmd", "/c", str(script)],
            cwd=str(upgrade_dir()),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x00000008,
        )
    except OSError as exc:
        return {"ok": False, "error": f"无法启动升级助手：{exc}"}

    return {
        "ok": True,
        "version": staged["version"],
        "backup": str(backup),
        "log": str(upgrade_dir() / "upgrade.log"),
        "message": f"正在升级到 v{staged['version']}：程序会自动退出、覆盖并重启，过程中别手动关窗口。",
    }
