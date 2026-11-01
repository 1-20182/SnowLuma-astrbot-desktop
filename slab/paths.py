"""集中管理控制台自身用到的目录/路径。

所有路径都可以用环境变量覆盖，方便打包与便携运行：
    SLAB_HOME     项目根目录（默认：源码模式=仓库根；打包模式=exe 所在目录）
    SLAB_DATA_DIR 数据目录（默认：<root>/data）
    SLAB_RUNTIME  运行时目录（默认：<root>/runtime）

打包（PyInstaller）说明：
* ``APP_ROOT`` 取 **exe 所在目录**，所以 data/、runtime/ 会落在 exe 旁边，整个
  文件夹拷走即可迁移；
* 只读资源（前端静态文件等）在 ``_MEIPASS`` 里，用 ``WEB_DIR`` 访问。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import storage

APP_NAME = "SnowLuma × AstrBot 联合控制台"
APP_SLUG = "snowluma-astrbot-console"

FROZEN: bool = bool(getattr(sys, "frozen", False))

# 源码根目录 / PyInstaller 解包目录（onefile 是临时目录）
_CODE_DIR: Path = Path(__file__).resolve().parents[1]
_BUNDLE_DIR: Path = Path(getattr(sys, "_MEIPASS", _CODE_DIR))
# exe 旁边的目录（仅打包模式有意义）
_EXE_DIR: Path = Path(sys.executable).resolve().parent if FROZEN else _CODE_DIR


def _read_version() -> str:
    """版本号唯一来源：仓库根目录的 VERSION 文件（打包时一起带上）。"""

    candidates = [
        Path(os.environ["SLAB_VERSION_FILE"]) if os.environ.get("SLAB_VERSION_FILE") else None,
        _BUNDLE_DIR / "VERSION",
        _CODE_DIR / "VERSION",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            try:
                return candidate.read_text("utf-8").strip() or "0.0.0"
            except OSError:
                continue
    return "0.0.0"


APP_VERSION = _read_version()


def _p(env: str, default: Path) -> Path:
    raw = os.environ.get(env, "").strip()
    return Path(raw).expanduser().resolve() if raw else default


APP_ROOT: Path = _p("SLAB_HOME", _EXE_DIR)

# 存储规划：默认避开 C 盘（只有一块盘时才用 C），老用户沿用原目录
STORAGE: dict = storage.plan(APP_ROOT)

DATA_DIR: Path = _p("SLAB_DATA_DIR", Path(STORAGE["data_dir"]))
RUNTIME_DIR: Path = _p("SLAB_RUNTIME", Path(STORAGE["runtime_dir"]))
DOWNLOAD_DIR: Path = _p("SLAB_DOWNLOAD_DIR", Path(STORAGE["downloads_dir"]))
LOG_DIR: Path = DATA_DIR / "logs"
BACKUP_DIR: Path = DATA_DIR / "backups"
# 前端静态资源：源码模式在仓库里，打包模式在 _MEIPASS 里
_BUNDLED_WEB = _BUNDLE_DIR / "slab" / "web"
SANDBOX_DIR: Path = _BUNDLE_DIR / "vendor" / "sandbox" if (_BUNDLE_DIR / "vendor" / "sandbox").is_dir() else APP_ROOT / "vendor" / "sandbox"
WEB_DIR: Path = _p("SLAB_WEB_DIR", _BUNDLED_WEB if _BUNDLED_WEB.exists() else APP_ROOT / "slab" / "web")

CONFIG_PATH: Path = DATA_DIR / "config.json"


def ensure_dirs() -> None:
    """幂等地创建控制台运行所需的目录。"""

    for d in (DATA_DIR, RUNTIME_DIR, DOWNLOAD_DIR, LOG_DIR, BACKUP_DIR):
        d.mkdir(parents=True, exist_ok=True)


def snowluma_home(cfg_value: str = "") -> Path:
    raw = (cfg_value or "").strip()
    return Path(raw).expanduser().resolve() if raw else RUNTIME_DIR / "snowluma"


def astrbot_home(cfg_value: str = "") -> Path:
    raw = (cfg_value or "").strip()
    return Path(raw).expanduser().resolve() if raw else RUNTIME_DIR / "astrbot"
