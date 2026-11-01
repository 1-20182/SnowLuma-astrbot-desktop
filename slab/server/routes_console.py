"""控制台核心 REST + SSE 路由（Lead 维护）。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from .. import envcheck, housekeeping, paths
from ..config import get_config, store
from ..events import bus
from ..supervisor import port_open, supervisor

router = APIRouter(prefix="/api", tags=["console"])
STARTED_AT = time.time()
# 真实监听端口（配置端口被占用时会自动避让，由 __main__ 在启动前写入）
RUNTIME_PORT: int = 0


def set_runtime_port(port: int) -> None:
    global RUNTIME_PORT
    RUNTIME_PORT = int(port or 0)


@router.get("/health")
async def health() -> Dict[str, Any]:
    cfg = get_config()
    return {
        "ok": True,
        "app": paths.APP_NAME,
        "version": paths.APP_VERSION,
        "uptime": round(time.time() - STARTED_AT, 1),
        # 前端用 started_at 自己每秒推算运行时长，避免"跳秒"
        "started_at": round(STARTED_AT, 3),
        "pid": os.getpid(),
        "port": RUNTIME_PORT or cfg.app.port,
        "configured_port": cfg.app.port,
        "port_shifted": bool(RUNTIME_PORT and RUNTIME_PORT != cfg.app.port),
    }


@router.get("/state")
async def state() -> Dict[str, Any]:
    cfg = get_config()
    ports = {
        "console": cfg.app.port,
        "astrbot_webui": cfg.astrbot.webui_port,
        "snowluma_webui": cfg.snowluma.webui_port,
        "onebot": cfg.link.ws_port,
    }
    # env_report/check_ports 都是阻塞调用（子进程 + netstat），放线程池避免卡住事件循环
    env, port_info = await asyncio.gather(
        asyncio.to_thread(envcheck.env_report_cached),
        asyncio.to_thread(envcheck.check_ports, ports),
    )
    return {
        "ok": True,
        "version": paths.APP_VERSION,
        "uptime": round(time.time() - STARTED_AT, 1),
        "started_at": round(STARTED_AT, 3),
        "config": cfg.masked(),
        "issues": cfg.validate(),
        "services": supervisor.states(),
        "env": env,
        "ports": port_info,
        "paths": {
            "data": str(paths.DATA_DIR),
            "runtime": str(paths.RUNTIME_DIR),
            "downloads": str(paths.DOWNLOAD_DIR),
            "snowluma": str(paths.snowluma_home(cfg.snowluma.install_dir)),
            "astrbot": str(paths.astrbot_home(cfg.astrbot.install_dir)),
        },
        # 存储规划：默认避开 C 盘，前端用来展示"会装到哪、为什么"
        "storage": {
            "base": paths.STORAGE.get("base"),
            "data_dir": str(paths.DATA_DIR),
            "runtime_dir": str(paths.RUNTIME_DIR),
            "downloads_dir": str(paths.DOWNLOAD_DIR),
            "reason": paths.STORAGE.get("reason"),
            "drives": paths.STORAGE.get("drives"),
        },
    }


@router.put("/config")
async def update_config(patch: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    cfg = store.apply_patch(patch)
    bus.publish("config", changed=list(patch.keys()))
    return {"ok": True, "config": cfg.masked(), "issues": cfg.validate()}


@router.post("/config/reset")
async def reset_config() -> Dict[str, Any]:
    cfg = store.reset()
    return {"ok": True, "config": cfg.masked()}


@router.get("/config/export")
async def export_config() -> Response:
    """把整份配置（含密钥）导出成 JSON 文件，方便换机器/备份。"""

    cfg = get_config()
    body = json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="slab-config-{stamp}.json"'},
    )


@router.post("/config/import")
async def import_config(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """导入配置（整份替换）。用于迁移到新机器或恢复备份。"""

    try:
        cfg = store.replace(payload)
    except ValueError as exc:
        raise HTTPException(400, f"导入失败：{exc}") from exc
    bus.publish("config", changed=["import"])
    return {"ok": True, "config": cfg.masked(), "issues": cfg.validate()}


@router.post("/services/all/{action}")
async def services_all(action: str, request: Request) -> Dict[str, Any]:
    return await _service_action("all", action, request)


@router.post("/services/{name}/{action}")
async def services_one(name: str, action: str, request: Request) -> Dict[str, Any]:
    return await _service_action(name, action, request)


async def _service_action(name: str, action: str, request: Request) -> Dict[str, Any]:
    cfg = get_config()
    if name == "all":
        try:
            if action == "start":
                result = await supervisor.start_all(cfg)
            elif action == "stop":
                await supervisor.stop_all()
                result = {k: "stopped" for k in supervisor.definitions()}
            elif action == "restart":
                await supervisor.stop_all()
                await asyncio.sleep(0.8)
                result = await supervisor.start_all(cfg)
            else:
                raise HTTPException(400, f"未知动作: {action}")
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"操作失败: {exc}") from exc
        return {"ok": True, "action": action, "results": result,
                "services": supervisor.states()}

    if name not in supervisor.definitions():
        raise HTTPException(404, f"未知服务: {name}")
    try:
        if action == "start":
            await supervisor.start(name, cfg)
        elif action == "stop":
            await supervisor.stop(name)
        elif action == "restart":
            await supervisor.restart(name, cfg)
        else:
            raise HTTPException(400, f"未知动作: {action}")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"操作失败: {exc}") from exc
    return {"ok": True, "action": action, "services": supervisor.states()}


@router.post("/system/shutdown")
async def shutdown(request: Request) -> Dict[str, Any]:
    """停止所有服务并退出控制台进程（打包版靠这个"关闭按钮"）。"""

    async def _worker() -> None:
        await asyncio.sleep(0.5)  # 先让 HTTP 响应发出去
        try:
            await supervisor.stop_all()
        finally:
            # 停完再扫一遍：venv 的 python 会 exec 出真 python，父进程停了子进程可能还活着，
            # 不清掉就会一直占着 6185 / 6199 / 5099，下次启动直接报"端口已被占用"。
            try:
                from .. import orphans

                result = await asyncio.to_thread(orphans.cleanup)
                if result.get("killed"):
                    bus.log("console", f"退出前清理了 {len(result['killed'])} 个残留进程", level="warn")
            except Exception as exc:  # noqa: BLE001
                bus.log("console", f"退出前清理残留失败（忽略）：{exc}", level="warn")
            bus.log("console", "用户请求退出控制台，已停止所有服务")
            await asyncio.sleep(0.3)
            os._exit(0)

    asyncio.create_task(_worker())
    return {"ok": True, "message": "正在停止所有服务并退出控制台…"}


# --------------------------------------------------------------------------- #
# 维护：端口避让 / 清理日志
# --------------------------------------------------------------------------- #
def _find_free_port(start: int, taken, limit: int = 200) -> int:
    for port in range(int(start), int(start) + limit):
        if port in taken:
            continue
        if not port_open(port):
            return port
    return 0


@router.post("/system/fix-ports")
async def fix_ports() -> Dict[str, Any]:
    """检查四个端口，被占用/重复的就近换一个空闲端口并写回配置。"""

    cfg = get_config()
    taken: set[int] = set()
    changes: List[Dict[str, Any]] = []
    restart_required = False

    def pick(label: str, current: int, section: str, field: str, *, needs_restart: bool) -> int:
        nonlocal restart_required
        current = int(current)
        reason = ""
        if current in taken:
            reason = "与其它端口重复"
        elif port_open(current):
            reason = "已被其它程序占用"
        if not reason:
            taken.add(current)
            return current
        free = _find_free_port(current + 1, taken)
        if not free:
            raise HTTPException(500, f"{label}：{current} {reason}，附近也找不到空闲端口，请手动指定")
        taken.add(free)
        changes.append({
            "label": label, "field": f"{section}.{field}",
            "from": current, "to": free, "reason": reason,
        })
        if needs_restart:
            restart_required = True
        return free

    patch: Dict[str, Any] = {}
    console_port = pick("控制台端口", cfg.app.port, "app", "port", needs_restart=True)
    if console_port != cfg.app.port:
        patch.setdefault("app", {})["port"] = console_port
    astr_port = pick("AstrBot 面板端口", cfg.astrbot.webui_port, "astrbot", "webui_port", needs_restart=False)
    if astr_port != cfg.astrbot.webui_port:
        patch.setdefault("astrbot", {})["webui_port"] = astr_port
    snow_port = pick("SnowLuma 面板端口", cfg.snowluma.webui_port, "snowluma", "webui_port", needs_restart=False)
    if snow_port != cfg.snowluma.webui_port:
        patch.setdefault("snowluma", {})["webui_port"] = snow_port
    ws_port = pick("OneBot 联动端口", cfg.link.ws_port, "link", "ws_port", needs_restart=False)
    if ws_port != cfg.link.ws_port:
        patch.setdefault("link", {})["ws_port"] = ws_port
    if cfg.link.mode == "bridge":
        relay = pick("中继端口", cfg.snowluma.relay_port, "snowluma", "relay_port", needs_restart=False)
        if relay != cfg.snowluma.relay_port:
            patch.setdefault("snowluma", {})["relay_port"] = relay

    if patch:
        cfg = store.apply_patch(patch)
        bus.log("console", "端口冲突已自动避让：" + "；".join(
            f"{c['label']} {c['from']}→{c['to']}（{c['reason']}）" for c in changes
        ))
    return {
        "ok": True,
        "changes": changes,
        "restart_required": restart_required,
        "config": cfg.masked(),
        "message": "端口都空闲，无需调整" if not changes else
                   ("已换端口；控制台端口变了，需要手动重启控制台" if restart_required else "已换端口，重新「写入联动配置」后生效"),
    }


@router.post("/system/cleanup")
async def cleanup() -> Dict[str, Any]:
    """立刻清理过期日志与安装包（默认保留天数见配置）。"""

    cfg = get_config()
    report = await asyncio.to_thread(
        housekeeping.cleanup_all, int(cfg.app.log_retain_days), int(cfg.app.download_retain_days)
    )
    return {"ok": True, **report.as_dict(), "report": report.as_dict()}


@router.get("/system/cleanup/preview")
async def cleanup_preview() -> Dict[str, Any]:
    cfg = get_config()
    snap = await asyncio.to_thread(
        housekeeping.cleanup_snapshot, int(cfg.app.log_retain_days), int(cfg.app.download_retain_days)
    )
    return {"ok": True, "log_retain_days": cfg.app.log_retain_days,
            "download_retain_days": cfg.app.download_retain_days, **snap}


@router.post("/system/cleanup-orphans")
async def cleanup_orphans(dry_run: bool = Query(default=False)) -> Dict[str, Any]:
    """清理上次被强杀留下的残留进程（只动我们自己装的 AstrBot / SnowLuma）。"""

    from .. import orphans

    return await asyncio.to_thread(orphans.cleanup, dry_run)


@router.post("/panels/astrbot-password")
async def reset_astrbot_password(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """重置 AstrBot 面板密码并写回控制台配置（解决"一键登录密码不对"）。

    AstrBot 4.x 把密码存成 pbkdf2 散列，初始口令只在首次启动的日志里打印一次；
    我们直接按它的格式写散列，用户就不需要去翻日志了。
    """

    import secrets
    import string

    from .. import astrbot_admin
    from ..config import store

    cfg = get_config()
    raw = str((payload or {}).get("password") or "").strip()
    if not raw:
        alphabet = string.ascii_letters + string.digits
        raw = "".join(secrets.choice(alphabet) for _ in range(16))

    # 必须停掉 AstrBot：它退出时会用内存里的配置覆盖文件
    from ..supervisor import supervisor as _sup

    was_running = _sup.is_active("astrbot")
    if was_running:
        await _sup.stop("astrbot")
    try:
        result = await asyncio.to_thread(astrbot_admin.set_dashboard_password, cfg, raw)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"重置 AstrBot 面板密码失败：{exc}") from exc
    store.apply_patch({"astrbot": {"webui_password": result["password"],
                                   "webui_username": result.get("username") or cfg.astrbot.webui_username}})
    if was_running:
        try:
            await _sup.start("astrbot")
        except Exception as exc:  # noqa: BLE001
            bus.log("astrbot", f"重置密码后重启 AstrBot 失败：{exc}", level="warn")
    bus.log("console", "已重置 AstrBot 面板密码（控制台已记录新密码）", level="warn")
    return {
        "ok": True,
        "username": result.get("username") or "astrbot",
        "password": result["password"],
        "restarted": was_running,
        "message": f"AstrBot 面板密码已重置为 {result['password']}（已保存，可直接用「一键登录」）",
    }


@router.get("/panels")
async def panels() -> Dict[str, Any]:
    """两个网页面板的地址与登录凭据（本机使用，不必再去翻终端日志）。"""

    from .. import astrbot_admin

    cfg = get_config()
    snow = {
        "label": "SnowLuma 面板",
        "url": f"http://127.0.0.1:{int(cfg.snowluma.webui_port)}",
        "username": "admin",
        "password": str(cfg.snowluma.webui_password or ""),
        "source": "config",
        "ok": bool(cfg.snowluma.webui_password),
    }
    try:
        info = astrbot_admin.read_dashboard_password(cfg)
    except Exception as exc:  # noqa: BLE001 - 抓不到就退回配置值
        info = {"ok": False, "password": "", "hint": str(exc)}
    astr = {
        "label": "AstrBot 面板",
        "url": f"http://127.0.0.1:{int(cfg.astrbot.webui_port)}",
        "username": str(cfg.astrbot.webui_username or "astrbot"),
        "password": str(info.get("password") or cfg.astrbot.webui_password or ""),
        "source": info.get("source") or "config",
        "ok": bool(info.get("ok") or cfg.astrbot.webui_password),
        "hint": info.get("hint") or "",
    }
    return {"ok": True, "snowluma": snow, "astrbot": astr}


@router.get("/logs")
async def logs(service: str | None = Query(default=None), limit: int = Query(default=200, ge=1, le=2000)):
    return {
        "ok": True,
        "services": bus.log_services(),
        "logs": [
            {"seq": e["seq"], "ts": e["ts"], "service": e.get("service"), "line": e.get("line", ""),
             "level": e.get("level", "info")}
            for e in bus.logs(service, limit)
        ],
    }


# --------------------------------------------------------------------------- #
# 进程监视（前端「终端」页用）：控制台自己 + 它拉起的整棵进程树
# --------------------------------------------------------------------------- #
_PROC_CACHE: Dict[int, Any] = {}


def _service_pid_map() -> Dict[int, str]:
    """pid → 服务名（SnowLuma / AstrBot / 中继）"""

    out: Dict[int, str] = {}
    for name, st in (supervisor.states() or {}).items():
        pid = st.get("pid")
        if pid:
            out[int(pid)] = name
    return out


def _collect_procs() -> Dict[str, Any]:
    """采集控制台进程树（CPU/内存/命令行），供前端做「任务管理器」式展示。"""

    try:
        import psutil
    except Exception as exc:  # noqa: BLE001
        return {"processes": [], "error": f"缺少 psutil：{exc}"}

    root_pid = os.getpid()
    try:
        root = psutil.Process(root_pid)
        procs = [root] + root.children(recursive=True)
    except psutil.Error as exc:  # noqa: BLE001
        return {"processes": [], "error": f"读取进程树失败：{exc}"}

    svc_by_pid = _service_pid_map()
    alive: set[int] = set()
    rows = []
    for p in procs:
        try:
            pid = p.pid
            alive.add(pid)
            cached = _PROC_CACHE.get(pid)
            if cached is None or cached.pid != pid:
                cached = p
                _PROC_CACHE[pid] = p
            cpu = cached.cpu_percent(None)  # 依赖上一次采样，轮询后就有值
            mem = p.memory_info().rss / (1024 * 1024)
            cmdline = " ".join(p.cmdline())[:240]
            service = svc_by_pid.get(pid, "")
            if not service:  # 孙进程归到父服务名下
                parent = p.parent()
                while parent is not None and parent.pid != root_pid:
                    if parent.pid in svc_by_pid:
                        service = svc_by_pid[parent.pid]
                        break
                    parent = parent.parent()
            rows.append({
                "pid": pid,
                "ppid": p.ppid(),
                "name": p.name(),
                "service": service or ("console" if pid == root_pid else ""),
                "cpu": round(float(cpu or 0), 1),
                "mem_mb": round(mem, 1),
                "created": round(p.create_time(), 1),
                "status": p.status(),
                "cmdline": cmdline,
            })
        except Exception:  # noqa: BLE001 - 进程可能已退出
            continue

    for pid in list(_PROC_CACHE):
        if pid not in alive:
            _PROC_CACHE.pop(pid, None)

    rows.sort(key=lambda r: (r["service"] != "console", r["created"]))
    return {"root_pid": root_pid, "processes": rows}


@router.get("/procs")
async def procs() -> Dict[str, Any]:
    data = await asyncio.to_thread(_collect_procs)
    return {"ok": True, **data, "ts": time.time()}



@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    q = bus.subscribe()

    async def gen():
        try:
            snapshot = {"type": "hello", "ts": time.time(), "recent": bus.recent(30),
                        "services": supervisor.states(), "config": get_config().masked()}
            yield f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
