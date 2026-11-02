"""QQ 客户端路由：检测 / 进程列表 / 隔离启动 / 只注入选中进程。

平台差异说明（前端按 ``/api/qq/summary`` 里的 ``platform`` / ``clone_mode`` 渲染）：
* Windows：``clone_mode="inject"``，深度分身 = 挂起创建 + 注入 sandbox DLL；
* Linux：``clone_mode="home"``，没有 DLL 注入这回事，分身 = 独立 HOME 多开。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException

from .. import plat, qq, qqclone
from ..config import get_config
from ..events import bus
from ..snowluma_admin import SnowLumaWebUI

router = APIRouter(prefix="/api/qq", tags=["qq"])


def _webui(cfg) -> SnowLumaWebUI:
    return SnowLumaWebUI(
        f"http://127.0.0.1:{int(cfg.snowluma.webui_port)}",
        password=str(cfg.snowluma.webui_password or ""),
        timeout=8.0,
    )


@router.get("/summary")
async def summary() -> Dict[str, Any]:
    """本机装了哪些 QQ、正在跑哪些 QQ 进程、隔离目录在哪。"""

    return await asyncio.to_thread(qq.summary)


@router.post("/launch-isolated")
async def launch_isolated(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """用独立数据目录启动一个 QQ（机器人专用，和你日常用的 QQ 分开）。"""

    source = str((payload or {}).get("source_exe") or "").strip()
    try:
        return await asyncio.to_thread(qq.launch_isolated, source)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


@router.get("/clone/status")
async def clone_status() -> Dict[str, Any]:
    """分身准备情况（Windows: 沙箱 DLL 是否带齐；Linux: 独立 HOME 目录与 mode=home）。"""

    return await asyncio.to_thread(qqclone.status)


@router.post("/clone/launch")
async def clone_launch(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """启动分身。

    * Windows：深度分身（独立数据目录 + 互斥体改名 + 已知文件夹重定向，注入 sandbox DLL）；
    * Linux：独立 HOME 分身（换 HOME 绕过 QQ 的单实例锁）。
    """

    data = payload or {}
    clone_id = str(data.get("clone_id") or "bot").strip() or "bot"
    source = str(data.get("source_exe") or "").strip()
    if not source:
        installs = await asyncio.to_thread(qq.list_installs)
        if not installs:
            if plat.IS_WINDOWS:
                raise HTTPException(404, "没有找到 QQ.exe：请先安装 QQ（NT 版），或在「QQ 客户端」里指定路径")
            raise HTTPException(404, "没有找到 QQ。下一步：" + qq.LINUX_INSTALL_HINT)
        source = installs[0]["exe"]
    try:
        result = await asyncio.to_thread(qqclone.launch_clone, source, clone_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except OSError as exc:
        if plat.IS_WINDOWS:
            hint = "（杀软拦截注入时请把安装目录加入白名单）"
        else:
            hint = "（Linux 上请确认可执行位与桌面会话：chmod +x /opt/QQ/qq、有 DISPLAY/WAYLAND_DISPLAY）"
        raise HTTPException(500, f"启动分身失败：{exc}{hint}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"启动分身失败：{exc}") from exc
    return result


@router.post("/inject")
async def inject(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """只把这一个 QQ 进程注入 SnowLuma（需要 SnowLuma 正在运行且为非自动注入模式）。"""

    data = payload or {}
    try:
        pid = int(data.get("pid"))
    except (TypeError, ValueError):
        raise HTTPException(400, "缺少有效的 pid") from None
    cfg = get_config()
    expect = str(data.get("expect_uin") or cfg.snowluma.qq_uin or "").strip()
    ui = _webui(cfg)
    try:
        async with ui:
            result = await ui.request("POST", f"/api/processes/{pid}/load")
            # 注入后回读账号：不是小号就立刻解除，避免把用户自己的号接管进来
            await asyncio.sleep(1.2)
            actual = ""
            try:
                for item in await ui.processes():
                    if int(item.get("pid") or 0) == pid:
                        actual = str((item.get("account") or {}).get("uin") or item.get("uin") or "")
                        break
            except Exception:  # noqa: BLE001
                actual = ""
            if expect and actual and actual not in ("0", expect):
                try:
                    await ui.request("POST", f"/api/processes/{pid}/unload")
                except Exception:  # noqa: BLE001
                    pass
                bus.log("qq", f"注入的是 {actual}，不是小号 {expect}，已自动解除", level="warn")
                raise HTTPException(
                    409,
                    f"这个进程登录的是 {actual}，不是配置里的机器人小号 {expect}。已自动解除注入；"
                    "请在 QQ 里登录小号后再试，或把「QQ 号」改成实际要当机器人的号。",
                )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            502,
            f"注入失败：{exc}。请确认 SnowLuma 正在运行、面板密码正确，并且该 PID 仍是 QQ 进程。",
        ) from exc
    bus.log("qq", f"已注入 QQ 进程 pid={pid}（账号 {actual or expect or '未知'}）", level="info")
    return {"ok": True, "pid": pid, "uin": actual or expect, "result": result}


@router.post("/unload")
async def unload(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """从 SnowLuma 卸载某个 QQ 进程（解除注入）。"""

    data = payload or {}
    try:
        pid = int(data.get("pid"))
    except (TypeError, ValueError):
        raise HTTPException(400, "缺少有效的 pid") from None
    cfg = get_config()
    ui = _webui(cfg)
    try:
        async with ui:
            result = await ui.request("POST", f"/api/processes/{pid}/unload")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"解除注入失败：{exc}") from exc
    return {"ok": True, "pid": pid, "result": result}


@router.post("/hook-mode")
async def set_hook_mode(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """切换注入模式：manual（只注入选中的）/ auto（自动注入所有 QQ）。"""

    from ..config import store

    mode = str((payload or {}).get("mode") or "").strip().lower()
    if mode not in ("manual", "auto"):
        raise HTTPException(400, "mode 只能是 manual 或 auto")
    cfg = store.apply_patch({"snowluma": {"hook_mode": mode}})
    return {
        "ok": True,
        "mode": cfg.snowluma.hook_mode,
        "restart_required": True,
        "message": "已切换；重启 SnowLuma 后生效" if mode == "auto" else "已切换为手动注入；重启 SnowLuma 后生效",
    }
