# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（Windows / Linux 共用）。

用法（一般由 build-exe.ps1 / build-linux.sh 调用）：
    set SLAB_EXE_CONSOLE=1     # 可选：保留黑框控制台，便于排查（仅 Windows 有意义）
    set SLAB_EXE_ONEFILE=1     # 可选：打成单文件（启动稍慢，Linux 上不推荐）
    pyinstaller --clean --noconfirm packaging/console.spec

平台差异（只在下面这一处收口，别往别处掺）：
  * icon / version 是 **Windows 专有**资源：.ico + VERSIONINFO 都只有 Windows 的 PE 文件
    才有，Linux 上 PyInstaller 会忽略或直接报错，所以 Linux 明确传 None。
  * console：Windows 的 --windowed 决定有没有黑框；Linux 下没有这个概念（无实际影响），
    打包版靠 slab/_frozen.py 把 stdout/stderr 兜底到 logs/console.log。
  * onedir（默认）而不是 onefile：Linux 上 onefile 每次启动都要把整个包解到 /tmp，
    在 noexec 挂载的 /tmp（容器、加固发行版很常见）里直接起不来，且冷启动多几秒。
"""

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - SPECPATH 由 PyInstaller 注入
IS_WINDOWS = sys.platform.startswith("win")

APP_NAME = "SnowLuma-AstrBot-Console"
CONSOLE = os.environ.get("SLAB_EXE_CONSOLE") == "1"
ONE_FILE = os.environ.get("SLAB_EXE_ONEFILE") == "1"

# 前端静态资源必须随包走（源码模式下是按仓库路径找的）
datas = [
    (os.path.join(ROOT, "VERSION"), "."),
    (os.path.join(ROOT, "vendor", "sandbox"), "vendor/sandbox"),
    (os.path.join(ROOT, "slab", "web"), "slab/web"),
    (os.path.join(ROOT, "README.md"), "."),
    # 只带手册，不进 docs/images（9 张 PNG 约 6MB，运行时根本用不到，只会拖慢解压与杀软扫描）
    (os.path.join(ROOT, "docs", "MANUAL.md"), "docs"),
    (os.path.join(ROOT, "docs", "UPGRADE.md"), "docs"),
    (os.path.join(ROOT, "CHANGELOG.md"), "."),
]

hiddenimports = []
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("websockets")
hiddenimports += collect_submodules("anyio")
hiddenimports += [
    "truststore",
    "psutil",
    "httpx",
    "h11",
    "h2",
    "sniffio",
    "slab.server.routes_console",
    "slab.server.routes_deploy",
    "slab.server.routes_link",
    "slab.installer",
    "slab.bridge",
    "slab.snowluma_admin",
    "slab.astrbot_admin",
    "slab.onebot.client",
    "slab.onebot.reverse",
    "slab.onebot.actions",
    "slab.onebot.model",
]

excludes = [
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "IPython",
    "pytest",
    "_pytest",
    "pytest_asyncio",
    "notebook",
    "sqlite3",
    "PIL",
]

# --------------------------------------------------------------------------- #
# Windows 专有资源：图标（PE 资源）与版本信息（VERSIONINFO）。
# Linux 的 ELF 里没有这两样东西，显式给 None，免得 PyInstaller 报警告或被当成错误。
# --------------------------------------------------------------------------- #
icon = None
version_file = None
if IS_WINDOWS:
    icon_path = os.path.join(ROOT, "packaging", "app.ico")
    icon = icon_path if os.path.exists(icon_path) else None
    candidate = os.path.join(ROOT, "packaging", "version_info.txt")
    version_file = candidate if os.path.exists(candidate) else None

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "packaging", "entry.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

EXE_COMMON = dict(
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=CONSOLE,
    icon=icon,
    version=version_file,
)

if ONE_FILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        exclude_binaries=False,
        runtime_tmpdir=None,
        **EXE_COMMON,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        **EXE_COMMON,
    )
    coll = COLLECT(  # noqa: F821
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name=APP_NAME,
    )
