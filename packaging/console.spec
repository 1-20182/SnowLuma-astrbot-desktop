# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

用法（一般由 build-exe.ps1 调用）：
    set SLAB_EXE_CONSOLE=1     # 可选：保留黑框控制台，便于排查
    set SLAB_EXE_ONEFILE=1     # 可选：打成单文件 exe（启动稍慢）
    pyinstaller --clean --noconfirm packaging/console.spec
"""

import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - SPECPATH 由 PyInstaller 注入

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

icon_path = os.path.join(ROOT, "packaging", "app.ico")
icon = icon_path if os.path.exists(icon_path) else None
version_file = os.path.join(ROOT, "packaging", "version_info.txt")

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
    version=version_file if os.path.exists(version_file) else None,
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
