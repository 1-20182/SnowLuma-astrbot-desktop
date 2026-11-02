"""统一的子进程启动参数：**永远不弹控制台窗口**。

背景：打包成窗口模式（``console=False``）后，控制台自己是没有黑框的，但每次
``subprocess`` 启动一个控制台程序（git / uv / node / python / taskkill ...），
Windows 都会给子进程新建一个黑色 DOS 窗口——部署一次能看到十几个窗口闪出来，
用户会以为中了病毒。所以所有子进程都必须带 ``CREATE_NO_WINDOW`` + 隐藏
``STARTUPINFO``。

平台差异**只在** :mod:`slab.plat` 里判断；本模块只是它的一层薄壳（保持既有对外 API）。

用法::

    from .procutil import hidden_kwargs, run_hidden

    subprocess.run(argv, capture_output=True, **hidden_kwargs())
    run_hidden(argv, timeout=30)          # 等价于 run + hidden_kwargs + 文本输出
"""

from __future__ import annotations

import subprocess
from typing import Any, Dict, List, Sequence

from . import plat

IS_WINDOWS = plat.IS_WINDOWS
CREATE_NO_WINDOW = plat.CREATE_NO_WINDOW  # subprocess.CREATE_NO_WINDOW（这里写成常量，便于非 Windows 上引用）


def hidden_kwargs() -> Dict[str, Any]:
    """返回让子进程不显示窗口的 Popen 参数（POSIX 返回空 dict）。"""

    return plat.hidden_kwargs()


def hidden_console_kwargs() -> Dict[str, Any]:
    """给长驻服务用：**创建一个隐藏的**控制台（窗口不可见）。

    与 ``hidden_kwargs``（完全无控制台）的区别：服务自己再 ``subprocess`` 拉子进程时，
    子进程会继承这个隐藏控制台，不会各自新开黑窗（例如 AstrBot 装插件、T2I 渲染）。

    POSIX 上的等价物是"独立会话 + 独立进程组"（``start_new_session``），
    这样关窗/退出时 ``kill_tree`` 能按进程组收掉整棵树。
    """

    return plat.hidden_console_kwargs()


def run_hidden(argv: Sequence[str], *, timeout: float | None = 60, cwd: str | None = None,
               env: Dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """``subprocess.run`` 的隐藏窗口版本（文本输出、不抛 CalledProcessError）。"""

    return subprocess.run(  # noqa: S603
        list(argv),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=cwd,
        env=env,
        **hidden_kwargs(),
    )


def popen_hidden(argv: List[str], **kwargs: Any) -> subprocess.Popen:
    """``subprocess.Popen`` 的隐藏窗口版本。"""

    kwargs.update(hidden_kwargs())
    return subprocess.Popen(list(argv), **kwargs)  # noqa: S603
