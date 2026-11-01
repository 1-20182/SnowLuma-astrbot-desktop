"""命令行入口： python -m slab [serve|install|doctor|bridge|version]"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
import webbrowser

from . import paths
from .config import get_config, store


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from ._frozen import FROZEN
    from .server import routes_console
    from .server.app import app
    from .supervisor import port_open

    cfg = get_config()
    host = args.host or cfg.app.host
    explicit = bool(args.host or args.port)
    port = int(args.port or cfg.app.port)
    shifted = False
    # 端口被占用时自动避让（不想被改就用 --port 显式指定，或设 SLAB_STRICT_PORT=1）
    if not explicit and port_open(port) and os.environ.get("SLAB_STRICT_PORT") != "1":
        for candidate in range(port + 1, port + 40):
            if not port_open(candidate):
                print(f"[warn] 端口 {port} 已被占用，自动改用 {candidate}"
                      f"（想固定端口：改配置里的 app.port，或启动时加 --port {port}）", flush=True)
                port, shifted = candidate, True
                break
        else:
            print(f"[error] 端口 {port} 被占用，且 {port + 1}~{port + 39} 也都不可用", flush=True)
            return 1
    if explicit:
        # 命令行覆盖写入配置，保证重启后一致
        store.apply_patch({"app": {"host": host, "port": port}})
    routes_console.set_runtime_port(port)

    # 启动时擦屁股：上次被强杀 / 崩溃留下的自家进程（父进程已不在）先清掉，
    # 否则它们会一直占着 6185 / 6199 / 5099，导致新实例启动直接失败。
    try:
        from . import orphans

        threading.Thread(target=orphans.startup_cleanup, name="orphan-cleanup", daemon=True).start()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 启动自检清理失败（不影响使用）：{exc}", flush=True)

    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    print("=" * 68)
    print(f" {paths.APP_NAME} v{paths.APP_VERSION}")
    print(f" 控制台地址 : {url}" + ("   （端口已自动避让）" if shifted else ""))
    print(f" 数据目录   : {paths.DATA_DIR}")
    print(" Ctrl+C 退出；关闭控制台会同时停止由它启动的服务")
    print("=" * 68, flush=True)
    # 打包版默认打开浏览器（用户双击 exe 就是想要界面）
    if not args.no_browser and (FROZEN or cfg.app.open_browser):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level=args.log_level.lower(),
                access_log=False, log_config=None)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    from . import installer

    cfg = get_config()

    def progress(evt: dict) -> None:
        print(f"[{evt.get('target', '')}] {evt.get('message', '')}", flush=True)

    if args.target == "both":
        result = installer.deploy_both(cfg, progress=progress)
    elif args.target == "snowluma":
        result = {"snowluma": installer.deploy_snowluma(cfg, progress=progress)}
    else:
        result = {"astrbot": installer.deploy_astrbot(cfg, progress=progress)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import envcheck

    cfg = get_config()
    report = envcheck.env_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    issues = cfg.validate()
    print("\n配置检查:")
    for item in issues or ["全部通过"]:
        print(f"  - {item}")
    print("\n端口检查:")
    for label, info in envcheck.check_ports(
        {"console": cfg.app.port, "astrbot": cfg.astrbot.webui_port,
         "snowluma": cfg.snowluma.webui_port, "onebot": cfg.link.ws_port}
    ).items():
        state = "被占用" if info["in_use"] else "空闲"
        print(f"  {label:<16}{info['port']:<7}{state} {info['owner']}")
    return 0


def cmd_bridge(args: argparse.Namespace) -> int:
    import asyncio

    from . import bridge

    asyncio.run(bridge.run_forever())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="slab", description=paths.APP_NAME)
    p.add_argument("--version", action="version", version=f"{paths.APP_NAME} {paths.APP_VERSION}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="启动图形控制台（默认）")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--log-level", default="INFO")
    s.set_defaults(func=cmd_serve)

    i = sub.add_parser("install", help="命令行安装（无界面）")
    i.add_argument("target", nargs="?", default="both", choices=["both", "snowluma", "astrbot"])
    i.set_defaults(func=cmd_install)

    d = sub.add_parser("doctor", help="环境与配置自检")
    d.set_defaults(func=cmd_doctor)

    b = sub.add_parser("bridge", help="仅前台运行桥接进程（调试用）")
    b.set_defaults(func=cmd_bridge)
    return p


def main(argv: list[str] | None = None) -> int:
    from ._frozen import FROZEN, ensure_stdio, report_fatal

    if FROZEN:
        # 窗口模式下没有标准流，先兜底到 data/logs/console.log
        try:
            paths.ensure_dirs()
            ensure_stdio(paths.LOG_DIR)
        except Exception:  # noqa: BLE001
            pass

    _setup_logging()
    paths.ensure_dirs()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["serve"] + (argv or []))
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("slab").exception("启动失败")
        report_fatal(exc, paths.LOG_DIR, box=FROZEN)
        return 1


if __name__ == "__main__":
    sys.exit(main())
