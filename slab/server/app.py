"""FastAPI 应用组装：REST + SSE + 静态前端。"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import paths
from ..config import get_config
from ..events import bus
from ..supervisor import supervisor
from . import routes_console

log = logging.getLogger("slab")


def _register_services() -> None:
    """注册三个服务定义（部署模块 + 桥接模块提供）。"""

    try:
        from ..installer import register_services as _reg
        _reg()
    except Exception as exc:  # noqa: BLE001
        log.warning("注册部署服务失败: %s", exc)
    try:
        from ..bridge import register_service as _reg_bridge
        _reg_bridge()
    except Exception as exc:  # noqa: BLE001
        log.warning("注册桥接服务失败: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    paths.ensure_dirs()
    bus.bind_loop(asyncio.get_running_loop())
    _register_services()
    bus.publish("service", name="console", status="running", detail="控制台已就绪")
    log.info("控制台已启动: http://%s:%s", get_config().app.host, get_config().app.port)
    try:
        yield
    finally:
        await supervisor.stop_all()
        log.info("控制台已关闭，所有子服务已停止")


def create_app() -> FastAPI:
    app = FastAPI(title=paths.APP_NAME, version=paths.APP_VERSION, lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")

    app.include_router(routes_console.router)

    from . import routes_deploy, routes_link, routes_qq, routes_upgrade  # 延迟导入，避免循环依赖

    app.include_router(routes_deploy.router)
    app.include_router(routes_link.router)
    app.include_router(routes_qq.router)
    app.include_router(routes_upgrade.router)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):  # noqa: ANN001
        return JSONResponse(status_code=exc.status_code,
                            content={"ok": False, "error": str(exc.detail)})

    @app.exception_handler(Exception)
    async def any_error(request: Request, exc: Exception):  # noqa: ANN001
        log.exception("未处理异常: %s", exc)
        return JSONResponse(status_code=500,
                            content={"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    web_dir = paths.WEB_DIR
    if web_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(web_dir)), name="assets")

    @app.get("/", include_in_schema=False)
    async def index():
        index_file = web_dir / "index.html"
        if not index_file.exists():
            return JSONResponse({"ok": False, "error": "前端文件缺失：slab/web/index.html"}, 500)
        # {{V}} 替换成版本号：升级后浏览器必然会重新拉取 app.css / theme.js（避免旧缓存）
        html = index_file.read_text("utf-8").replace("{{V}}", paths.APP_VERSION)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        icon = web_dir / "favicon.svg"
        if icon.exists():
            return FileResponse(str(icon), media_type="image/svg+xml")
        return JSONResponse({}, 204)

    return app


app = create_app()
