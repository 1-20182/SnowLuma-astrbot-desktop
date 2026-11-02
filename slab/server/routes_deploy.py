"""部署路由（T1 维护）：环境自检 / 版本列表 / 部署 / 卸载 / 任务状态。

接口约定见 docs/CONTRACTS.md：

* ``GET  /api/deploy/env``        环境自检（工具版本、磁盘、端口占用、已安装状态、可行性结论）
* ``GET  /api/deploy/versions``   SnowLuma 最近 tag + AstrBot 分支（2 小时缓存，失败降级不报错）
* ``POST /api/deploy/{target}``   target=snowluma|astrbot|both，**立刻返回**并在后台线程执行
* ``DELETE /api/deploy/{target}`` 卸载（停服务 → 目录备份到 data/backups）
* ``GET  /api/deploy/status``     后台任务快照（SSE 断线后前端可轮询兜底）

进度与结果统一通过 ``bus.publish("deploy", target=..., step=..., progress=..., message=...)``
推送，``step`` 取值：check/prepare/download/verify/extract/install/config/done/finished/error。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from .. import envcheck, installer, paths, plat
from ..config import get_config
from ..events import bus
from ..supervisor import supervisor

router = APIRouter(prefix="/api/deploy", tags=["deploy"])

TARGETS = ("snowluma", "astrbot", "both")
SERVICE_NAMES = ("snowluma", "astrbot")


class _PickerUnavailable(RuntimeError):
    """当前环境没有一个能弹目录选择框的程序（POSIX 上缺 zenity/kdialog）。"""


# --------------------------------------------------------------------------- #
# 目录选择（Windows: 原生"浏览文件夹"对话框；Linux: zenity / kdialog）
# --------------------------------------------------------------------------- #
_PICK_SCRIPT = """
Add-Type -AssemblyName System.Windows.Forms | Out-Null
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.WindowState = 'Minimized'
$dlg = New-Object System.Windows.Forms.FolderBrowserDialog
$dlg.Description = __TITLE__
$dlg.ShowNewFolderButton = $true
if (__INITIAL__ -ne '') { try { $dlg.SelectedPath = __INITIAL__ } catch {} }
$result = $dlg.ShowDialog($owner)
if ($result -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($dlg.SelectedPath) }
$owner.Dispose()
"""


def _ps_quote(value: str) -> str:
    return "'" + str(value or "").replace("'", "''") + "'"


def _pick_dir_windows(title: str, initial: str) -> Dict[str, Any]:
    """弹出 Windows 原生目录选择框（powershell 本身隐藏，只显示对话框）。"""

    import contextlib
    import tempfile

    from ..procutil import hidden_kwargs

    script = _PICK_SCRIPT.replace("__TITLE__", _ps_quote(title)).replace("__INITIAL__", _ps_quote(initial))
    fd, tmp_name = tempfile.mkstemp(prefix="slab-pick-dir-", suffix=".ps1")
    tmp = Path(tmp_name)
    os.close(fd)
    tmp.write_text(script, "utf-8-sig")  # BOM：Windows PowerShell 5.1 才认中文
    try:
        proc = subprocess.run(  # noqa: S603
            ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(tmp)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, **hidden_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "cancelled": True, "error": "等待选择超时（10 分钟）"}
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    chosen = (proc.stdout or "").strip()
    if not chosen:
        return {"ok": False, "cancelled": True}
    return {"ok": True, "path": chosen}


def _pick_dir_posix(title: str, initial: str) -> Dict[str, Any]:
    """POSIX：有 zenity 就用 zenity，其次 kdialog；都没有就抛 :class:`_PickerUnavailable`。

    浏览器拿不到本机绝对路径，无头/最小安装的 Linux 上也常常没装任何对话框程序，
    这时前端应退回"手动输入路径"（路由返回 501 并带上提示）。
    """

    zenity = shutil.which("zenity")
    kdialog = shutil.which("kdialog")
    if zenity:
        argv = [zenity, "--file-selection", "--directory", f"--title={title or '选择目录'}"]
        if initial:
            argv.append(f"--filename={str(initial).rstrip('/')}/")
    elif kdialog:
        argv = [kdialog, "--title", title or "选择目录", "--getexistingdirectory",
                str(initial or Path.home())]
    else:
        raise _PickerUnavailable(
            "当前环境没有图形目录选择框（缺 zenity / kdialog）。\n"
            "下一步：手动输入完整路径（Linux 常用 /opt/SnowLuma 或 "
            "$XDG_DATA_HOME/SnowLumaAstrBot）；"
            "想在网页上弹框可以安装：sudo apt install -y zenity"
        )
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)  # noqa: S603
    except subprocess.TimeoutExpired:
        return {"ok": False, "cancelled": True, "error": "等待选择超时（10 分钟）"}
    except OSError as exc:
        raise _PickerUnavailable(f"目录选择框启动失败：{exc}。下一步：手动输入路径，或安装 zenity 后重试。") from exc
    chosen = (proc.stdout or "").strip()
    if proc.returncode != 0 or not chosen:
        return {"ok": False, "cancelled": True}
    return {"ok": True, "path": chosen}


def _pick_dir_native(title: str, initial: str) -> Dict[str, Any]:
    """按平台弹出目录选择框（Windows: PowerShell；Linux: zenity/kdialog）。

    没有可用对话框时抛 :class:`_PickerUnavailable`，由路由翻译成 501。
    """

    if plat.IS_WINDOWS:
        return _pick_dir_windows(title, initial)
    return _pick_dir_posix(title, initial)


@router.post("/pick-dir")
async def pick_dir(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """让用户选一个目录（浏览器拿不到绝对路径，由服务端弹原生对话框）。"""

    data = payload or {}
    try:
        return await asyncio.to_thread(
            _pick_dir_native, str(data.get("title") or "选择目录"), str(data.get("initial") or "")
        )
    except _PickerUnavailable as exc:
        raise HTTPException(501, str(exc)) from exc

_jobs_lock = threading.RLock()
_jobs: Dict[str, Dict[str, Any]] = {}


# --------------------------------------------------------------------------- #
# 后台任务状态
# --------------------------------------------------------------------------- #
def _job(target: str) -> Dict[str, Any]:
    job = _jobs.get(target)
    if job is None:
        job = {
            "target": target,
            "running": False,
            "ok": None,
            "step": "idle",
            "progress": 0.0,
            "message": "",
            "error": "",
            "result": None,
            "started_at": 0.0,
            "finished_at": 0.0,
            "updated_at": 0.0,
        }
        _jobs[target] = job
    return job


def _snapshot() -> Dict[str, Dict[str, Any]]:
    with _jobs_lock:
        return {name: dict(job) for name, job in _jobs.items()}


def _job_update(target: str, **fields: Any) -> None:
    with _jobs_lock:
        job = _job(target)
        job.update(fields)
        job["updated_at"] = time.time()


def _running() -> List[str]:
    with _jobs_lock:
        return [name for name, job in _jobs.items() if job.get("running")]


def _progress_callback(name: str):
    def _cb(event: Dict[str, Any]) -> None:
        with _jobs_lock:
            job = _jobs.get(name)
            if job is None:
                return
            job.update(
                step=str(event.get("step") or job.get("step") or ""),
                progress=float(event.get("progress") or 0.0),
                message=str(event.get("message") or ""),
                updated_at=time.time(),
            )

    return _cb


def _deploy_worker(target: str, options: Dict[str, Any]) -> None:
    """后台线程：按目标顺序执行部署，逐项发布 deploy 事件。"""

    cfg = get_config()
    names = list(SERVICE_NAMES) if target == "both" else [target]
    results: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for name in names:
        _job_update(name, running=True, ok=None, step="start", progress=0.0,
                    message="开始部署", error="", result=None, started_at=time.time(), finished_at=0.0)
        bus.publish("deploy", target=name, step="start", progress=0.0,
                    message=f"开始部署 {'SnowLuma' if name == 'snowluma' else 'AstrBot'}")
        callback = _progress_callback(name)
        try:
            if name == "snowluma":
                results[name] = installer.deploy_snowluma(cfg, progress=callback, **options.get("snowluma", {}))
            else:
                results[name] = installer.deploy_astrbot(cfg, progress=callback, **options.get("astrbot", {}))
        except installer.DeployError as exc:
            message = str(exc)
            errors[name] = message
            _job_update(name, running=False, ok=False, step="error", progress=1.0,
                        message=message.splitlines()[0] if message else "部署失败",
                        error=message, finished_at=time.time())
            bus.publish("deploy", target=name, step="error", progress=1.0, ok=False, message=message)
            bus.log(name, f"部署失败：{message}", level="error")
            break
        except Exception as exc:  # noqa: BLE001 - 兜底，避免线程静默死掉
            message = f"{type(exc).__name__}: {exc}"
            errors[name] = message
            bus.log(name, f"部署异常：{traceback.format_exc()}", level="error")
            _job_update(name, running=False, ok=False, step="error", progress=1.0,
                        message=message, error=message, finished_at=time.time())
            bus.publish("deploy", target=name, step="error", progress=1.0, ok=False, message=message)
            break
        else:
            payload = results[name]
            skipped = bool(payload.get("skipped"))
            _job_update(name, running=False, ok=True, step="done", progress=1.0,
                        message=str(payload.get("message") or ("已是最新，跳过" if skipped else "部署完成")),
                        error="", result=payload, finished_at=time.time())
            bus.publish("deploy", target=name, step="finished", progress=1.0, ok=True,
                        skipped=skipped, result=payload,
                        message=f"{'SnowLuma' if name == 'snowluma' else 'AstrBot'} "
                                + ("已是最新，跳过" if skipped else "部署完成"))

    ok = not errors
    envcheck.invalidate_cache()  # 安装状态变了，别让 /api/deploy/env 返回 12 秒前的旧快照
    if target == "both":
        done = [k for k in results]
        summary = (
            f"一键部署完成：{', '.join(done)}"
            if ok
            else f"一键部署未全部成功（已完成 {', '.join(done) or '无'}）：" + "；".join(errors.values())
        )
    else:
        summary = (
            "部署完成"
            if ok
            else "部署失败：" + (errors.get(target) or "").splitlines()[0]
        )
    bus.publish("deploy", target=target, step="finished", progress=1.0, ok=ok,
                results=results, errors=errors, message=summary)
    bus.log("deploy", summary, level="info" if ok else "error")


# --------------------------------------------------------------------------- #
# 环境自检
# --------------------------------------------------------------------------- #
@router.get("/env")
async def deploy_env(
    net: bool = Query(default=False, description="是否顺带探测 GitHub 连通性（最多多花 2 秒）"),
) -> Dict[str, Any]:
    """环境自检：工具链、磁盘、端口占用、安装状态与「能不能装」的结论。"""

    cfg = get_config()
    # env_report 会 fork 若干子进程（git/uv/node/python/pnpm），放到线程里跑并走 12s 缓存
    report = await asyncio.to_thread(envcheck.env_report_cached)
    status = installer.install_status(cfg)
    ports = envcheck.check_ports(
        {
            "console": cfg.app.port,
            "astrbot_webui": cfg.astrbot.webui_port,
            "snowluma_webui": cfg.snowluma.webui_port,
            "onebot": cfg.link.ws_port,
        }
    )
    tools = report.get("tools") or {}
    node_tool = tools.get("node") or {}
    node_ver = str(node_tool.get("version") or "").strip().lstrip("v")
    lite_ok = installer.node_version_supported(node_ver) if node_ver else False
    git_tool = tools.get("git") or {}
    python_info = installer.python_report()
    github: Dict[str, Any] = {"checked": False, "reachable": None, "reason": ""}
    if net:
        probe = await asyncio.to_thread(installer.check_github)
        github = {
            "checked": True,
            "reachable": bool(probe.get("ok")),
            "reason": str(probe.get("reason") or ""),
            "status": probe.get("status", 0),
        }

    # 能不能装 AstrBot：git 在 → 一定可以；git 不在 → 需要能下 zipball
    python_reason = (
        "Python 版本不足（AstrBot 需要 3.12+；安装 uv 可让它自动下载 3.12）"
        if not python_info.get("ok")
        else ""
    )
    if git_tool.get("ok"):
        astrbot_method = "git"
        astrbot_ok = bool(python_info.get("ok"))
        astrbot_reason = python_reason
    elif github.get("checked") and github.get("reachable") is False:
        astrbot_method = ""
        astrbot_ok = False
        astrbot_reason = "未安装 git，且下载源码包需要访问 GitHub。\n" + str(github.get("reason") or "")
    else:
        astrbot_method = "zipball"
        astrbot_ok = bool(python_info.get("ok"))
        astrbot_reason = python_reason or (
            "未安装 git：将改用源码压缩包安装（需要能访问 codeload.github.com）"
        )

    # 配置端口 vs 实际端口（SnowLuma 端口被占用时会 +1 漂移，以日志为准）
    snowluma_status = status.get("snowluma") or {}
    runtime_port = snowluma_status.get("runtime_port")
    port_state = {
        "configured": int(cfg.snowluma.webui_port),
        "runtime": runtime_port,
        "mismatch": bool(snowluma_status.get("port_mismatch")),
    }

    warnings: List[str] = list(report.get("warnings") or [])
    warnings = [w for w in warnings if "未检测到 git" not in w]  # 已由 astrbot_method 说明
    if not lite_ok:
        warnings.append(
            f"当前 Node.js {node_ver or '未安装'} 不满足 SnowLuma lite 版要求（{installer.NODE_REQUIRED}）"
            f"，请使用 full 版（自带 {plat.node_bin_name()}，推荐）。"
        )
    if ports["snowluma_webui"]["in_use"]:
        warnings.append(
            f"端口 {ports['snowluma_webui']['port']} 已被占用（{ports['snowluma_webui']['owner'] or '未知进程'}）："
            "SnowLuma 会自动改用 +1 的端口，实际端口以运行日志为准。"
        )
    if port_state["mismatch"]:
        warnings.append(
            f"SnowLuma 实际运行在端口 {runtime_port}（配置里是 {port_state['configured']}）："
            f"端口被占用时会自动 +1，请以实际端口打开面板 http://127.0.0.1:{runtime_port}。"
        )
    if ports["astrbot_webui"]["in_use"]:
        warnings.append(
            f"端口 {ports['astrbot_webui']['port']} 已被占用（{ports['astrbot_webui']['owner'] or '未知进程'}）："
            "AstrBot 会启动失败，请先关闭占用进程或改端口。"
        )
    if ports["onebot"]["in_use"]:
        warnings.append(
            f"OneBot 联动端口 {ports['onebot']['port']} 已被占用（{ports['onebot']['owner'] or '未知进程'}）："
            "可能是上一次运行遗留的进程，建议先停止服务。"
        )
    if not python_info.get("ok"):
        warnings.append(str(python_info.get("message") or "Python 环境不满足 AstrBot 要求"))

    return {
        "ok": True,
        **report,
        "warnings": warnings,
        "ports": ports,
        "snowluma_port": port_state,
        "deploy": status,
        "recommend": {
            "snowluma_edition": "lite" if (lite_ok and cfg.snowluma.edition == "lite") else "full",
            "lite_supported": lite_ok,
            "node_version": node_ver,
            "node_required": installer.NODE_REQUIRED,
            "python": python_info,
            "astrbot_installable": astrbot_ok,
            "astrbot_method": astrbot_method,
            "astrbot_reason": astrbot_reason,
            "github": github,
            "notes": [
                f"SnowLuma full 版自带 {plat.node_bin_name()}，开箱即用；lite 版需要 Node "
                f"{installer.NODE_REQUIRED}。",
                installer.ASTRBOT_DASHBOARD_NOTE,
            ],
        },
    }


# --------------------------------------------------------------------------- #
# 版本列表
# --------------------------------------------------------------------------- #
@router.get("/versions")
async def deploy_versions(force: bool = Query(default=False, description="忽略 2 小时缓存强制刷新")) -> Dict[str, Any]:
    return {"ok": True, **installer.list_versions(force=force)}


@router.get("/status")
async def deploy_status() -> Dict[str, Any]:
    return {"ok": True, "running": _running(), "jobs": _snapshot()}


# --------------------------------------------------------------------------- #
# 部署 / 卸载
# --------------------------------------------------------------------------- #
def _parse_options(target: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """把前端请求体转成各 deploy 函数的 kwargs，顺带做前置校验（快速失败）。"""

    data = payload or {}
    if not isinstance(data, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象。")
    force = bool(data.get("force"))
    mirror = str(data.get("mirror") or "").strip().rstrip("/")
    if mirror and not mirror.startswith("https://"):
        raise HTTPException(400, f"镜像前缀必须是 https 地址（当前「{mirror}」），例如 https://ghfast.top 。")
    edition = str(data.get("edition") or "").strip().lower()
    if edition and edition not in ("full", "lite"):
        raise HTTPException(400, f"版本类型只能是 full 或 lite（当前「{edition}」）。")
    version = str(data.get("version") or "").strip()
    if version and (".." in version or "/" in version or "\\" in version):
        raise HTTPException(400, f"版本号/分支名不合法：{version}")
    offline = str(data.get("offline_zip") or "").strip()
    if offline:
        candidate = Path(offline).expanduser()
        if not candidate.is_file():
            raise HTTPException(
                400,
                f"离线安装包不存在：{candidate}。请重新选择 SnowLuma 官方 zip 压缩包，或检查文件是否被移动。",
            )
        offline = str(candidate)
    source = str(data.get("source") or data.get("astrbot_source") or "").strip()

    options: Dict[str, Dict[str, Any]] = {"snowluma": {}, "astrbot": {}}
    if target in ("snowluma", "both"):
        options["snowluma"] = {
            k: v
            for k, v in {
                "force": force,
                "mirror": mirror,
                "edition": edition or None,
                "version": version or None,
                "offline_zip": offline or None,
            }.items()
            if v not in (None, "")
        }
    if target in ("astrbot", "both"):
        options["astrbot"] = {
            k: v
            for k, v in {
                "force": force,
                "mirror": mirror,
                "ref": version or None,
                "source": source or None,
            }.items()
            if v not in (None, "")
        }
    return options


# --------------------------------------------------------------------------- #
# 打开安装目录（浏览器无法直接调资源管理器，这里代劳）
# --------------------------------------------------------------------------- #
OPEN_TARGETS = ("snowluma", "astrbot", "data")


def _under_any(path: Path, roots: List[Path]) -> bool:
    try:
        resolved = Path(path).resolve()
    except OSError:  # pragma: no cover
        return False
    for root in roots:
        base = Path(root).resolve()
        if resolved == base or base in resolved.parents:
            return True
    return False


def _open_dir_path(target: str, cfg) -> Path:
    """解析并校验要打开的目录，拒绝越界路径。"""

    if target == "data":
        path = Path(paths.DATA_DIR)
    elif target in ("snowluma", "astrbot"):
        module = installer.snowluma_dir if target == "snowluma" else installer.astrbot_dir
        try:
            path = module(cfg)
        except installer.DeployError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not _under_any(path, [paths.RUNTIME_DIR, paths.DATA_DIR]):
            # 允许用户显式配置在别处的安装目录（resolve_install_dir 已校验过绝对路径与系统目录）
            section = cfg.snowluma if target == "snowluma" else cfg.astrbot
            configured = Path(str(section.install_dir or "").strip() or ".").expanduser()
            try:
                configured = configured.resolve()
            except OSError:  # pragma: no cover
                configured = Path(".")
            if path != configured:
                raise HTTPException(400, f"拒绝打开未授权的目录：{path}")
    else:
        raise HTTPException(400, f"未知目标：{target}（可选 {' / '.join(OPEN_TARGETS)}）")
    display = {"snowluma": "SnowLuma", "astrbot": "AstrBot", "data": "数据"}[target]
    if not path.exists():
        raise HTTPException(400, f"{display}目录不存在，请先安装：{path}")
    if not path.is_dir():
        raise HTTPException(400, f"{display}目录不是一个文件夹：{path}")
    return path


def _open_path(path: Path) -> bool:
    """在系统文件管理器里打开目录（Windows: explorer；Linux: xdg-open）；平台层统一处理。"""

    return plat.open_path(Path(path))


@router.post("/open-dir")
async def open_dir(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """在系统文件管理器里打开安装/数据目录（浏览器没有这个权限，由本机进程代劳）。"""

    data = payload or {}
    target = str(data.get("target") or "").strip().lower()
    path = _open_dir_path(target, get_config())
    if not _open_path(path):
        raise HTTPException(
            501,
            "当前环境打不开文件管理器"
            + ("（explorer 启动失败）。" if plat.IS_WINDOWS else "（没装 xdg-open）。")
            + f"下一步：手动打开这个路径：{path}",
        )
    where = "资源管理器" if plat.IS_WINDOWS else "文件管理器"
    bus.log("console", f"已在{where}中打开：{path}")
    return {"ok": True, "path": str(path), "target": target}


@router.post("/{target}")
async def deploy(target: str, payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """立即返回 ``{"ok": true, "started": true}``，真正的下载/安装在后台线程执行。"""

    if target not in TARGETS:
        raise HTTPException(400, f"未知部署目标：{target}（可选 snowluma / astrbot / both）")
    names = list(SERVICE_NAMES) if target == "both" else [target]
    busy = [name for name in _running() if name in names]
    if busy:
        raise HTTPException(
            409, f"{'、'.join(busy)} 的部署任务正在进行中，请等待完成后再试。"
        )
    options = _parse_options(target, payload)

    # 下载源：请求里没指定就用配置。auto 只复用 30 分钟内的测速缓存，
    # 绝不在点"安装"时现场卡 12 秒测速；没有缓存就用官方直连。
    cfg_now = get_config()
    wanted = str((payload or {}).get("mirror") or "").strip() or str(cfg_now.app.download_mirror or "auto")
    from .. import sources as _sources

    if wanted == "auto":
        mirror_id = _sources.cached_fastest() or "official"
        if mirror_id == "official":
            bus.log("deploy", "下载源：自动（暂无测速缓存，先用官方源；可在部署页点「测试各源速度」）")
    elif wanted in _sources.MIRROR_IDS:
        mirror_id = wanted
    else:
        mirror_id = "official"
    mirror_prefix = _sources.mirror_prefix(mirror_id)
    for name in names:
        options.setdefault(name, {})
        options[name].setdefault("mirror", mirror_prefix)
    if mirror_id != "official":
        bus.publish("deploy", target=target, step="mirror", progress=0.0,
                    message=f"下载源：{_sources.mirror_label(mirror_id)}")
        bus.log("deploy", f"使用下载源：{_sources.mirror_label(mirror_id)}")

    # 部署前先停掉相关服务：Windows 上 node.exe / *.dll / *.pyd 被占用会导致替换失败
    for name in names:
        if supervisor.is_active(name):
            try:
                await supervisor.stop(name)
                bus.log(name, "部署前已停止服务")
            except Exception as exc:  # noqa: BLE001
                bus.log(name, f"停止服务失败（继续部署）：{exc}", level="warn")

    for name in names:
        _job_update(name, running=True, ok=None, step="prepare", progress=0.0,
                    message="排队中", error="", result=None, started_at=time.time(), finished_at=0.0)
    bus.publish("deploy", target=target, step="prepare", progress=0.0,
                message="部署任务已开始（后台执行，可在事件流里查看进度）")
    thread = threading.Thread(target=_deploy_worker, args=(target, options),
                              name=f"deploy-{target}", daemon=True)
    thread.start()
    return {
        "ok": True,
        "started": True,
        "target": target,
        "services": names,
        "options": {k: v for k, v in options.items() if v},
    }


@router.delete("/{target}")
async def undeploy(target: str, purge: bool = Query(default=False, description="true=连备份一起彻底删除")) -> Dict[str, Any]:
    """卸载：停服务 → 目录备份到 data/backups；``purge=true`` 时直接删除（含旧备份）。"""

    if target not in SERVICE_NAMES:
        raise HTTPException(400, f"卸载只支持 snowluma 或 astrbot（收到 {target}）。")
    if target in _running():
        raise HTTPException(409, f"{target} 的部署任务正在进行中，请等待完成后再卸载。")
    try:
        await supervisor.stop(target)
    except Exception as exc:  # noqa: BLE001
        bus.log(target, f"停止服务失败（继续卸载）：{exc}", level="warn")
    try:
        cfg_now = get_config()
        result = (installer.uninstall(target, cfg_now, purge=True) if purge
                  else installer.uninstall(target, cfg_now))
    except installer.DeployError as exc:
        raise HTTPException(400, str(exc)) from exc
    bus.publish("deploy", target=target, step="uninstall", progress=1.0,
                ok=bool(result.get("removed")), message=str(result.get("message") or ""))
    envcheck.invalidate_cache()
    _job_update(target, running=False, ok=True, step="uninstall", progress=1.0,
                message=str(result.get("message") or ""), result=result, finished_at=time.time())
    return {"ok": True, **result}


# --------------------------------------------------------------------------- #
# 下载源 / 测速 / 磁盘占用
# --------------------------------------------------------------------------- #
@router.get("/sources")
async def deploy_sources() -> Dict[str, Any]:
    """可选下载源清单 + 当前配置 + 测速用的样本 URL。"""

    from .. import sources

    cfg = get_config()
    return {
        "ok": True,
        "mirrors": sources.catalog(),
        "current": sources.effective_mirror(cfg),
        "test_asset": sources.test_asset_url(cfg),
        "sample_mb": round(sources.SAMPLE_BYTES / 1048576, 1),
    }


@router.post("/sources/speedtest")
async def deploy_speedtest(apply: bool = Query(default=False, description="true=把最快的源写进配置")) -> Dict[str, Any]:
    """并发测所有下载源，返回速率排序；``apply=true`` 时顺便把推荐源写进配置。"""

    from .. import sources

    cfg = get_config()
    data = await sources.speedtest(cfg)
    if apply and data.get("recommended"):
        store_cfg = cfg.clone()
        store_cfg.app.download_mirror = data["recommended"]
        from ..config import store as _store

        _store.save(store_cfg)
        data["applied"] = data["recommended"]
    return data


_USAGE_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
USAGE_TTL = 30.0


def _collect_usage() -> Dict[str, Any]:
    """同步遍历目录（几万个文件，必须放线程里跑，否则会卡住事件循环）。"""

    cfg = get_config()

    def _mb(path) -> float:
        return round(installer._dir_size(path) / 1048576, 1)  # noqa: SLF001 - 同包内部工具

    items = {
        "snowluma": {"path": str(installer.snowluma_dir(cfg)), "mb": _mb(installer.snowluma_dir(cfg))},
        "astrbot": {"path": str(installer.astrbot_dir(cfg)), "mb": _mb(installer.astrbot_dir(cfg))},
        "data": {"path": str(paths.DATA_DIR), "mb": _mb(paths.DATA_DIR)},
        "backups": {"path": str(paths.BACKUP_DIR), "mb": _mb(paths.BACKUP_DIR)},
        "downloads": {"path": str(paths.DOWNLOAD_DIR), "mb": _mb(paths.DOWNLOAD_DIR)},
    }
    backups = []
    if paths.BACKUP_DIR.is_dir():
        for item in sorted(paths.BACKUP_DIR.iterdir(), reverse=True):
            if item.is_dir():
                backups.append({"name": item.name, "mb": _mb(item)})
    total = sum(v["mb"] for k, v in items.items() if k not in ("backups",))
    return {"ok": True, "items": items, "backups": backups[:20], "total_mb": round(total, 1)}


@router.get("/usage")
async def deploy_usage() -> Dict[str, Any]:
    """磁盘占用（结果缓存 30 秒；遍历 600MB 目录比较慢，放线程里）。"""

    if _USAGE_CACHE["data"] and time.time() - _USAGE_CACHE["ts"] < USAGE_TTL:
        return _USAGE_CACHE["data"]
    data = await asyncio.to_thread(_collect_usage)
    _USAGE_CACHE.update({"ts": time.time(), "data": data})
    return data


@router.delete("/backups/clear")
async def drop_backups() -> Dict[str, Any]:
    """清空 data/backups（卸载留下的旧目录）。"""

    freed = 0
    removed = 0
    if paths.BACKUP_DIR.is_dir():
        for item in list(paths.BACKUP_DIR.iterdir()):
            try:
                if item.is_dir():
                    freed += installer._dir_size(item)  # noqa: SLF001
                    installer._remove_dir(item, "备份")  # noqa: SLF001
                else:
                    freed += item.stat().st_size
                    item.unlink()
                removed += 1
            except (OSError, installer.DeployError):
                continue
    bus.log("console", f"已清理 {removed} 个备份目录，释放 {round(freed / 1048576, 1)} MB", level="warn")
    envcheck.invalidate_cache()
    return {"ok": True, "removed": removed, "freed_mb": round(freed / 1048576, 1)}



