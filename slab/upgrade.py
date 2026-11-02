"""从 zip 升级：把新版本安全地盖到旧程序上，数据与安装目录一律不动。

为什么要一个"助手进程"
--------------------
程序文件正在运行时不能覆盖（Windows 是文件锁；Linux 上覆盖正在执行的二进制会
``ETXTBSY`` / 让进程读到半截文件），所以流程是：

1. 本程序解压新 zip 到 ``data/upgrade/staged``，校验版本与文件；
2. 生成升级助手脚本并**脱离**启动它（Windows: ``apply-upgrade.cmd`` + PowerShell；
   Linux: ``apply-upgrade.sh``）；
3. 本程序退出；助手等进程彻底退出后：
   - 把旧程序目录备份到 ``data/backups/program-<旧版本>-<时间>``；
   - 用新文件覆盖旧文件（Windows: ``robocopy /E``；Linux: 先在旁边拼好新目录再
     ``mv`` 两个 rename，做到「原子替换」）；
   - 失败就回滚备份并启动旧版本；
   - 成功后启动新版本，日志写到 ``data/upgrade/upgrade.log``。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths, plat
from .events import bus

#: 这些目录属于"用户数据"，升级时绝不覆盖、也不参与备份清理
PROTECTED = ("data", "runtime", "downloads", "vendor")  # vendor 里有注入组件，跟版本走 → 单独处理
PROTECTED = ("data", "runtime", "downloads")
#: 入口程序名：Windows 是 exe；Linux 是可执行脚本/二进制（无扩展名）
_WINDOWS_ENTRIES = ("SnowLuma-Desktop.exe", "SnowLuma-AstrBot-Console.exe")
_POSIX_ENTRIES = ("SnowLuma-Desktop", "SnowLuma-AstrBot-Console", "snowluma-astrbot-console")
ENTRY_HINTS = _WINDOWS_ENTRIES if plat.IS_WINDOWS else _WINDOWS_ENTRIES + _POSIX_ENTRIES


def upgrade_dir() -> Path:
    d = paths.DATA_DIR / "upgrade"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _version_tuple(value: str) -> tuple:
    parts = re.findall(r"\d+", str(value or "0"))
    nums = [int(p) for p in parts[:3]] + [0, 0, 0]
    return tuple(nums[:3])


def read_zip_version(zf: zipfile.ZipFile) -> str:
    """zip 里任意层级的 ``VERSION`` 文件（我们是 ``<根目录>/VERSION``），取最浅的那个。

    必须**先精确匹配大写 ``VERSION``**，没有再退回大小写不敏感：
    Electron 的 dist 里自带一个叫 ``version`` 的文件（内容 ``33.4.11``），
    如果一开始就大小写不敏感，它比 ``resources/backend/VERSION`` 更浅，会把升级版本号
    污染成 Electron 的版本（实测过）。同理也挡住 AstrBot 的 ``version.txt`` 之类。
    """

    names = [name for name in zf.namelist() if not name.endswith("/")]
    candidates = [name for name in names if Path(name).name == "VERSION"]
    if not candidates:  # 退回大小写不敏感（老包/手工打的包）
        candidates = [name for name in names if Path(name).name.upper() == "VERSION"]
    candidates.sort(key=lambda name: (name.count("/"), name))
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
            # 桌面版 zip 里会有两个 basename 命中 ENTRY_HINTS：
            #   SnowLuma-Desktop/SnowLuma-Desktop        ← 壳（该重启用它）
            #   SnowLuma-Desktop/resources/backend/SnowLuma-AstrBot-Console  ← 内嵌后端
            # namelist 顺序 = 写入顺序（不可控），所以按层级深度 + 名字排序，取最浅的那个。
            entries = sorted((n for n in names if Path(n).name in ENTRY_HINTS),
                             key=lambda n: (n.count("/"), n))
            root = _single_root(names)
    except zipfile.BadZipFile:
        return {"ok": False, "error": "这个文件不是有效的 zip，可能下载不完整"}
    if not entries:
        return {"ok": False, "error": "压缩包里没有 SnowLuma 控制台的入口程序（exe / 可执行文件），"
                                      "确认下是不是下错了包"}

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
        # 桌面版的真实布局：<壳根>/SnowLuma-Desktop(+.exe) 与 <壳根>/resources/backend/<后端>，
        # 而控制台进程的 APP_ROOT 指向的是 resources/backend（后端目录）。
        # 所以要**先看壳根**（program.parent.parent）：桌面版该重启的是 Electron 壳；
        # 重启内嵌后端会被 main.js 判成「后端意外退出」→ 弹窗 + app.quit()，
        # 用户升级完窗口直接消失（新后端还可能变孤儿进程）。
        for name in ENTRY_HINTS:
            for base in (program.parent.parent, program, program.parent):
                candidate = base / name
                if candidate.is_file():
                    restart = str(candidate)
                    break
            if restart:
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


def _safe_relpath(name: str) -> Optional[Path]:
    """把 zip 成员名转成安全的相对路径；绝对路径 / 穿越 / 空名返回 ``None``（跳过）。"""

    text = str(name or "").replace("\\", "/")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    if any(p == ".." for p in parts):
        return None
    return Path(*parts)


def _extract_zip_preserving(zf: zipfile.ZipFile, target: Path) -> None:
    """解压 zip 并**按 ``external_attr`` 还原权限位与软链**（POSIX 专用）。

    为什么不能直接用 ``extractall``：Python 的 ``zipfile`` 只写文件内容，
    **既不还原 st_mode 也不还原软链**（``unzip`` 两者都会）。我们的发布产物是按
    unzip 语义打的（入口 / ``start.sh`` 是 755，可能带软链），直接 extractall 会得到：
    * 启动器变 ``0644`` → 升级后用户跑 ``start.sh`` 直接 ``Permission denied``；
    * 软链变成"内容=目标路径"的普通文件 → dlopen 时炸。

    失败只跳过该成员（升级不该因为一个附属文件就整体失败）。
    """

    for item in zf.infolist():
        rel = _safe_relpath(item.filename)
        if rel is None:
            continue
        dest = target / rel
        mode = (item.external_attr >> 16) & 0xFFFF
        try:
            if item.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                if mode & 0o777:
                    with contextlib.suppress(OSError):
                        dest.chmod(mode & 0o777)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(mode):
                link_target = zf.read(item).decode("utf-8", "replace")
                if dest.is_symlink() or dest.exists():
                    dest.unlink()
                os.symlink(link_target, dest)
                continue
            with zf.open(item) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            perm = mode & 0o777
            if perm:
                dest.chmod(perm)
        except (OSError, zipfile.BadZipFile):
            continue


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
            if plat.IS_WINDOWS:
                # Windows 保持原语义：符号链接需要额外权限，且 zip 里的 st_mode 没有意义
                zf.extractall(target)
            else:
                _extract_zip_preserving(zf, target)
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


def helper_script_sh(staged_root: Path, *, restart: str, pid: int, keep: List[str],
                     backup_dir: Path) -> Path:
    """生成 POSIX 升级助手（bash）：等旧进程退出 → 备份 → 原子替换 → 重启（失败回滚）。

    为什么用「拼好新目录 + 两次 rename」而不是就地覆盖：
    * 就地覆盖到一半失败会留下"半新半旧"的程序目录，很难恢复；
    * rename 在同一个文件系统内是原子的，替换瞬间新旧二选一，失败就把备份改回来；
    * 用户的 data/ runtime/ downloads/ 先 ``cp -a`` 进新目录，绝不丢。

    日志写到 ``data/upgrade/upgrade.log``，排查升级失败时先看它。
    """

    program = Path(paths.APP_ROOT)
    log = upgrade_dir() / "upgrade.log"
    script = upgrade_dir() / "apply-upgrade.sh"
    keep_names = [Path(p).name for p in keep if _is_inside(Path(p), program)]
    entry = Path(restart).name

    body = f'''#!/usr/bin/env bash
# 由 SnowLuma 控制台生成：等待旧进程退出 → 备份 → 原子替换 → 重启（失败回滚）
set -u
LOG={shlex.quote(str(log))}
SRC={shlex.quote(str(staged_root))}
DST={shlex.quote(str(program))}
BAK={shlex.quote(str(backup_dir))}
WAIT_PID={int(pid)}
RESTART={shlex.quote(str(restart))}
ENTRY={shlex.quote(entry)}
KEEP=({" ".join(shlex.quote(name) for name in keep_names) or ""})

log() {{ printf '%s %s\\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }}

start_app() {{
    if [ -e "$RESTART" ]; then
        chmod +x "$RESTART" 2>/dev/null || true
        if command -v setsid >/dev/null 2>&1; then
            setsid "$RESTART" >/dev/null 2>&1 &
        else
            nohup "$RESTART" >/dev/null 2>&1 &
        fi
        log "已启动：$RESTART"
    else
        log "找不到启动目标：$RESTART（请手动运行启动脚本）"
    fi
}}

log "升级开始：等待 pid $WAIT_PID 退出"
DEADLINE=$(( $(date +%s) + 180 ))
while kill -0 "$WAIT_PID" 2>/dev/null; do
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
        log "等待超时（进程仍在运行），放弃覆盖"
        exit 2
    fi
    sleep 0.5
done
log "旧进程已退出，开始校验升级包"

if [ ! -e "$SRC/$ENTRY" ]; then
    log "校验失败：升级包里没有 $ENTRY"
    exit 3
fi

NEW="$DST.slab-new.$$"
rm -rf "$NEW"
if ! mkdir -p "$NEW"; then
    log "无法创建临时目录：$NEW"
    exit 4
fi
if ! tar -C "$SRC" -cf - . | tar -C "$NEW" -xf -; then
    log "解包到新目录失败：$NEW（可能是磁盘空间不足）"
    rm -rf "$NEW"
    exit 4
fi
# 用户数据跟着进新目录（新版本自带的同名目录优先，绝不覆盖用户数据）
for name in $KEEP; do
    if [ -d "$DST/$name" ] && [ ! -e "$NEW/$name" ]; then
        cp -a "$DST/$name" "$NEW/$name" 2>>"$LOG" || log "警告：$name 没拷全，已保留旧目录"
    fi
done
chmod +x "$NEW/$ENTRY" 2>/dev/null || true
# 双保险：解压环节若丢了可执行位（zip 转存 / 老版本 stage），这里把启动脚本补回来。
# 不写 -type f：万一某个 .sh 是软链，-type f 不会匹配（chmod 会跟随软链，正是想要的）。
find "$NEW" -name '*.sh' -exec chmod +x {{}} + 2>/dev/null || true

log "备份程序目录 → $BAK"
mkdir -p "$(dirname "$BAK")" 2>/dev/null || true
if [ -d "$DST" ]; then
    if ! mv "$DST" "$BAK" 2>>"$LOG"; then
        # 跨文件系统时报错：退化为"同一个父目录里改名"，保证 mv 是原子的
        BAK="$DST.slab-old.$(date +%Y%m%d-%H%M%S)"
        log "备份目录不可写，改为原地备份：$BAK"
        if ! mv "$DST" "$BAK" 2>>"$LOG"; then
            log "备份失败（目录被占用？），放弃升级"
            rm -rf "$NEW"
            exit 5
        fi
    fi
else
    mkdir -p "$DST"
fi

if ! mv "$NEW" "$DST" 2>>"$LOG"; then
    log "替换失败，开始回滚"
    if [ -e "$BAK" ]; then
        mv "$BAK" "$DST" 2>>"$LOG" || log "回滚失败，请手动把 $BAK 改回 $DST"
    fi
    start_app
    exit 1
fi
log "替换完成，启动新版本"
start_app
log "升级结束"
'''
    script.write_text(body, encoding="utf-8")
    with contextlib.suppress(OSError):
        plat.chmod_exec(script)
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
        entry_text = "程序 exe" if plat.IS_WINDOWS else "启动脚本 / 可执行文件"
        return {"ok": False, "error": f"找不到要重启的{entry_text}，无法自动完成升级；请手动解压覆盖。"}

    backup = paths.DATA_DIR / "backups" / f"program-v{paths.APP_VERSION}-{time.strftime('%Y%m%d-%H%M%S')}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    kwargs: Dict[str, Any] = {
        "restart": info["restart"],
        "pid": os.getpid(),
        "keep": info["keep"],
        "backup_dir": backup,
    }
    if plat.IS_WINDOWS:
        script = helper_script(Path(staged["staged_root"]), **kwargs)
        argv = ["cmd", "/c", str(script)]
        popen_kwargs: Dict[str, Any] = {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x00000008,
        }
    else:
        script = helper_script_sh(Path(staged["staged_root"]), **kwargs)
        argv = ["bash", str(script)]
        popen_kwargs = {"start_new_session": True}
    bus.log("console", f"开始升级到 v{staged['version']}：备份 → 覆盖 → 重启（日志 {upgrade_dir() / 'upgrade.log'}）",
            level="warn")
    try:
        subprocess.Popen(  # noqa: S603
            argv,
            cwd=str(upgrade_dir()),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **popen_kwargs,
        )
    except OSError as exc:
        return {"ok": False, "error": f"无法启动升级助手：{exc}"}

    return {
        "ok": True,
        "version": staged["version"],
        "backup": str(backup),
        "log": str(upgrade_dir() / "upgrade.log"),
        "script": str(script),
        "message": f"正在升级到 v{staged['version']}：程序会自动退出、覆盖并重启，过程中别手动关窗口。",
    }
