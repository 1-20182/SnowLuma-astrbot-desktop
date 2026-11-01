"""升级路由：检查升级包 / 预览影响 / 执行升级（数据一律不动）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException

from .. import upgrade
from ..events import bus

router = APIRouter(prefix="/api/upgrade", tags=["upgrade"])


@router.get("/plan")
async def get_plan() -> Dict[str, Any]:
    """升级会动什么、不会动什么（程序目录/数据/安装/缓存）。"""

    return upgrade.plan()


@router.post("/inspect")
async def inspect(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """看一眼这个 zip：版本号、是否更新、里面有没有 exe。"""

    raw = str((payload or {}).get("zip") or "").strip().strip('"')
    if not raw:
        raise HTTPException(400, "请先选择升级包（.zip）")
    info = await asyncio.to_thread(upgrade.inspect_zip, Path(raw))
    if not info.get("ok"):
        raise HTTPException(400, info.get("error") or "升级包无法识别")
    return info


@router.post("/apply")
async def apply(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """解压 + 生成升级助手。前端拿到结果后要主动退出程序，助手才能覆盖文件。"""

    data = payload or {}
    raw = str(data.get("zip") or "").strip().strip('"')
    if not raw:
        raise HTTPException(400, "请先选择升级包（.zip）")
    force = bool(data.get("force"))
    result = await asyncio.to_thread(upgrade.start_upgrade, Path(raw), force=force)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "升级未能开始")
    bus.log("console", f"升级已排队：v{result.get('version')}，程序即将退出并自动重启", level="warn")
    return result


@router.get("/log")
async def log_tail(limit: int = 120) -> Dict[str, Any]:
    """升级日志（覆盖/回滚过程都在里面）。"""

    path = upgrade.upgrade_dir() / "upgrade.log"
    if not path.is_file():
        return {"ok": True, "lines": []}
    try:
        lines = path.read_text("utf-8", "replace").splitlines()[-max(1, min(limit, 500)):]
    except OSError as exc:
        raise HTTPException(500, f"读不到升级日志：{exc}") from exc
    return {"ok": True, "lines": lines}
