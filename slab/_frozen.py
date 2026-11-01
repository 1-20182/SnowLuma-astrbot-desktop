"""打包（PyInstaller）运行时的小工具。

窗口模式（``--windowed``）下 ``sys.stdout`` / ``sys.stderr`` 是 ``None``，
而 uvicorn / logging 会往 stderr 写，直接跑会抛
``AttributeError: 'NoneType' object has no attribute 'write'``。
所以打包版启动第一件事就是把标准流重定向到日志文件，并准备好“启动失败也要
让用户看得见”的兜底（写崩溃日志 + 弹一个原生对话框）。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import Optional

FROZEN: bool = bool(getattr(sys, "frozen", False))
_EXE_DIR: Optional[Path] = Path(sys.executable).resolve().parent if FROZEN else None


def exe_dir() -> Optional[Path]:
    return _EXE_DIR


def ensure_stdio(log_dir: Path) -> Path:
    """把 stdout/stderr 兜底到日志文件，返回该文件路径。"""

    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = log_dir / "console.log"
    try:
        stream = open(logfile, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        return logfile

    if sys.stdout is None or getattr(sys.stdout, "closed", False):
        sys.stdout = stream  # type: ignore[assignment]
    if sys.stderr is None or getattr(sys.stderr, "closed", False):
        sys.stderr = stream  # type: ignore[assignment]
    # logging 的 StreamHandler 在 emit 时读 sys.stderr，这里再兜一层
    if sys.stderr is None:
        sys.stderr = stream  # type: ignore[assignment]
    return logfile


def show_error_box(message: str, title: str = "SnowLuma × AstrBot 联合控制台") -> None:
    """Windows 原生错误对话框；非 Windows / 非打包环境退化为打印。"""

    text = message.strip()
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR
            return
        except Exception:  # noqa: BLE001
            pass
    try:
        print(f"[{title}] {text}", file=sys.stderr or sys.stdout)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        pass


def report_fatal(exc: BaseException, log_dir: Path, *, box: bool = True) -> Path:
    """把未捕获异常写进日志并（可选）弹窗，返回日志路径。"""

    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "console-crash.log"
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n==== 启动/运行失败 ====\n")
            fh.write(detail)
    except OSError:
        pass
    if box:
        show_error_box(
            "控制台启动失败，已写入日志：\n"
            f"{path}\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            "常见原因：端口被占用（改 data/config.json 的 app.port）、"
            "杀毒软件拦截、或缺少运行库。"
        )
    return path
