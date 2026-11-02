"""部署引擎（T1）：下载/解压 SnowLuma、克隆+建虚拟环境+装依赖 AstrBot、服务定义注册。

设计要点
--------
* **可中断可续传**：HTTP 下载走 ``<name>.part``，支持 ``Range`` 续传与 3 次重试；
* **原子落盘**：压缩包先解压到 ``<data>/tmp`` 的临时目录，校验通过后再改名到
  ``runtime/snowluma``；替换旧目录前把旧目录移到 ``data/backups``；
* **升级不丢配置**：重新部署时把旧的 ``config/ data/ logs/`` 迁移到新目录，
  只覆盖 ``config/runtime.json`` 里的 webuiPort / webuiHost；
* **幂等**：已安装且版本一致时直接跳过下载（``skipped=True``）；
* **中文可定位错误**：所有失败都抛 :class:`DeployError`，消息里带「下一步怎么做」；
* **进度**：所有长任务通过 ``bus.publish("deploy", ...)`` 推 SSE，同时写日志环形缓冲；
* **安全**：路径必须落在 ``paths.RUNTIME_DIR`` / ``paths.DATA_DIR`` 或显式配置的
  install_dir 内，拒绝 ``..``；下载 URL 只允许 GitHub 域名白名单。

对外 API
--------
``deploy_snowluma`` / ``deploy_astrbot`` / ``deploy_both`` / ``uninstall``
``register_services`` / ``install_status`` / ``list_versions``
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from . import paths, plat
from .config import AppConfig, get_config
from .events import bus
from .procutil import hidden_kwargs
from .supervisor import ServiceDef, supervisor

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
SNOWLUMA_REPO = "SnowLuma/SnowLuma"
ASTRBOT_REPO = "AstrBotDevs/AstrBot"
GITHUB_API = "https://api.github.com"
GITHUB_WEB = "https://github.com"
ASTRBOT_GIT_URL = f"{GITHUB_WEB}/{ASTRBOT_REPO}.git"
CODELOAD = "https://codeload.github.com"

# AstrBot 仓库默认分支是 master；配置里历史默认值写的是 main，两者都接受。
ASTRBOT_DEFAULT_BRANCH = "master"
DEFAULT_REF_ALIASES = {"", "main", "master", "latest", "default"}

# 下载 URL 白名单（GitHub 官方域名）
DOWNLOAD_HOSTS = {
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
    "release-assets.githubusercontent.com",
}

USER_AGENT = f"slab-console/{paths.APP_VERSION}"
CHUNK = 256 * 1024
DOWNLOAD_RETRIES = 3
DOWNLOAD_TIMEOUT = 60.0
API_TIMEOUT = 15.0
CMD_TIMEOUT = 300
CLONE_TIMEOUT = 600
DEPS_TIMEOUT = 15 * 60
VERSIONS_TTL = 2 * 60 * 60  # 版本列表缓存 2 小时
VERSIONS_FAIL_BACKOFF = 60.0

SNOWLUMA_ENTRY = "index.mjs"
SNOWLUMA_READY_RE = r"\[WebUI\] listening http://"
# uvicorn 监听行（剥离 ANSI 后匹配）；aiocqhttp 1.4.4 不会打印「适配器已连接」
ASTRBOT_READY_RE = r"Running on http://"
#: 可执行文件后缀（Windows ``.exe``，POSIX 空串）——平台差异统一取自 :mod:`slab.plat`
EXE_SUFFIX = plat.exe_suffix()

NODE_REQUIRED = "^24.22.1 || >=26.12.1"
ASTRBOT_MIN_PYTHON = (3, 12)  # pyproject requires-python >= 3.12
ASTRBOT_PREFERRED_PYTHON = "3.12"
PYTHON_DOWNLOAD_URL = "https://www.python.org/downloads/"
UV_INSTALL_URL = "https://docs.astral.sh/uv/"

FALLBACK_SNOWLUMA_TAGS = ["v2.2.1"]
DEFAULT_SNOWLUMA_RUNTIME_CONFIG: Dict[str, Any] = {
    "webuiPort": 5099,
    "hookAutoLoad": False,
    "webuiHost": "127.0.0.1",
    "webuiTls": {"enabled": False},
    "trustProxy": "",
    "logMaxTotalMb": 1024,
    "logRetainDays": 7,
    "logPerUin": False,
}

ASTRBOT_DASHBOARD_NOTE = (
    "AstrBot 首次启动会联网下载前端面板包（data/dashboard.zip，约 5 MB）。"
    "若下载失败，WebUI 面板可能打不开，但不影响 OneBot 消息转发；"
    "网络恢复后重启 AstrBot 即可补下载。"
)

_GIT_HINT = (
    "请安装 Git for Windows（https://git-scm.com/download/win）后重开控制台；"
    "或保持网络可用，控制台会自动改用源码压缩包（zipball）安装。"
    if plat.IS_WINDOWS
    else "请先安装 git（Ubuntu/Debian：sudo apt install -y git）后重开控制台；"
    "或保持网络可用，控制台会自动改用源码压缩包（zipball）安装。"
)

#: 安装目录示例（报错文案里给用户照着填）
INSTALL_DIR_EXAMPLE = r"D:\SnowLuma" if plat.IS_WINDOWS else "/opt/SnowLuma"

# SnowLuma 官方发行目标（release.yml 矩阵）→ 资产名后缀。
# 键是 release.json 里的 ``target``（= Node 的 ``process.platform-process.arch``）：
# Windows 是 ``win32-x64``、Linux 是 ``linux-x64`` / ``linux-arm64``；
# 同时接受 ``win-x64`` 这种写法（1.x 的旧包与手工改过的清单里出现过）。
_TARGET_SUFFIX = {
    "win32-x64": "win-x64",
    "win-x64": "win-x64",
    "win32-arm64": "win-arm64",
    "win-arm64": "win-arm64",
    "linux-x64": "linux-x64",
    "linux-arm64": "linux-arm64",
}

#: 各发行目标对应的压缩包格式（Windows 官方只发 zip，Linux 只发 tar.gz）
_TARGET_EXT = {"win-x64": ".zip", "win-arm64": ".zip",
               "linux-x64": ".tar.gz", "linux-arm64": ".tar.gz"}


# --------------------------------------------------------------------------- #
# 错误与进度
# --------------------------------------------------------------------------- #
class DeployError(RuntimeError):
    """可直接展示给最终用户的中文部署错误（消息里应包含下一步建议）。"""


class _HttpStatusError(Exception):
    """内部使用：HTTP 状态码异常（区分可重试/不可重试）。"""

    def __init__(self, status: int, url: str = "") -> None:
        super().__init__(f"HTTP {status}")
        self.status = int(status)
        self.url = url


class DeployProgress:
    """统一的进度上报器：推 SSE（type=deploy）+ 写日志缓冲 + 回调给调用方。"""

    def __init__(self, target: str, callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        self.target = target
        self._cb = callback
        self._last_frac = -1.0
        self._last_step = ""
        self._line_budget = 0
        self._line_ts = 0.0

    def __call__(
        self,
        step: str,
        progress: float,
        message: str,
        level: str = "info",
        throttle: bool = False,
    ) -> Dict[str, Any]:
        try:
            frac = max(0.0, min(1.0, float(progress)))
        except (TypeError, ValueError):
            frac = 0.0
        if throttle and frac < 1.0 and abs(frac - self._last_frac) < 0.01:
            return {}
        same_step = step == self._last_step
        self._last_frac = frac
        self._last_step = step
        event = bus.publish(
            "deploy", target=self.target, step=step, progress=round(frac, 4), message=message
        )
        # 下载进度只写每 5% 一条日志，避免刷屏
        if not throttle or not same_step or frac >= 1.0 or int(frac * 20) != int(max(0.0, self._last_frac) * 20):
            bus.log(self.target, f"[{step}] {message}", level=level)
        if self._cb is not None:
            try:
                self._cb(
                    {
                        "target": self.target,
                        "step": step,
                        "progress": round(frac, 4),
                        "message": message,
                    }
                )
            except Exception:  # noqa: BLE001 - 回调异常绝不影响部署
                pass
        return event

    def log_line(self, line: str, level: str = "info") -> None:
        """把子进程输出写进日志缓冲（限速，避免装依赖时刷爆环形缓冲）。

        每次部署都会在「终端」页实时滚动，限速太狠会显得卡顿，这里放宽到
        每秒 40 行（环形容量 2000 行，够看也够回溯）。
        """

        now = time.time()
        if now - self._line_ts >= 1.0:
            self._line_ts = now
            self._line_budget = 40
        if self._line_budget <= 0:
            return
        self._line_budget -= 1
        bus.log(self.target, line.rstrip("\r\n"), level=level)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _which(name: str) -> str:
    """``shutil.which`` 的薄封装（测试可 monkeypatch）。"""

    return shutil.which(name) or ""


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = path.read_text("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(tmp, path)


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


# --------------------------------------------------------------------------- #
# 路径安全
# --------------------------------------------------------------------------- #
def _reject_traversal(raw: str, label: str) -> None:
    text = str(raw)
    if "\x00" in text:
        raise DeployError(f"{label}路径非法：包含空字符。请检查设置里的目录。")
    normalized = text.replace("\\", "/")
    if any(part == ".." for part in normalized.split("/")):
        raise DeployError(
            f"{label}路径不能包含「..」({raw})。请在设置里填写完整的绝对路径，例如 {INSTALL_DIR_EXAMPLE}。"
        )


def ensure_within(base: Path, target: Path, label: str = "路径") -> Path:
    """确保 ``target`` 位于 ``base`` 之内（解析符号链接后比较），否则抛错。"""

    try:
        base_r = Path(base).resolve()
        target_r = Path(target).resolve()
    except OSError as exc:  # pragma: no cover - 极端文件系统错误
        raise DeployError(f"{label}无法解析：{target}（{exc}）") from exc
    if target_r == base_r or base_r in target_r.parents:
        return target_r
    raise DeployError(
        f"{label}越界：{target_r} 不在允许的目录 {base_r} 之内。"
        "为安全起见控制台只允许把文件写入 data/ 或 runtime/ 目录。"
    )


def _assert_allowed_root(p: Path, label: str) -> None:
    allowed_roots = []
    for root in (paths.RUNTIME_DIR, paths.DATA_DIR):
        try:
            allowed_roots.append(Path(root).resolve())
        except OSError:  # pragma: no cover
            continue
    for root in allowed_roots:
        if p == root or root in p.parents:
            return
    if p.parent == p:
        raise DeployError(
            f"{label}安装目录不能是磁盘根目录（{p}）。请选择一个子目录，例如 {INSTALL_DIR_EXAMPLE}。"
        )
    low = str(p).lower().replace("\\", "/")
    # Windows：\windows、\system32、\program files；POSIX：/bin、/etc、/usr、/sys、/boot、/lib
    system_like = ("/windows", "/system32", "/program files",
                   "/bin", "/sbin", "/etc", "/usr", "/sys", "/boot", "/lib")
    if any(seg in low for seg in system_like):
        raise DeployError(
            f"{label}安装目录不能是系统目录（{p}）。请改到普通数据目录，例如 {INSTALL_DIR_EXAMPLE}。"
        )


def resolve_install_dir(raw: str, default_dir: Path, label: str) -> Path:
    """把配置里的安装目录解析为绝对路径并做安全校验。"""

    text = (raw or "").strip()
    if text:
        _reject_traversal(text, label)
        p = Path(text).expanduser()
        if not p.is_absolute():
            raise DeployError(
                f"{label}安装目录必须填绝对路径（当前填的是「{text}」）。"
                f"请打开「设置」改成例如 {INSTALL_DIR_EXAMPLE}，或留空使用默认的 runtime 目录。"
            )
    else:
        p = Path(default_dir)
    try:
        p = p.resolve()
    except OSError as exc:  # pragma: no cover
        raise DeployError(f"{label}安装目录无法解析：{text or p}（{exc}）") from exc
    _assert_allowed_root(p, label)
    return p


def snowluma_dir(cfg: Optional[AppConfig] = None) -> Path:
    cfg = cfg or get_config()
    return resolve_install_dir(cfg.snowluma.install_dir, paths.RUNTIME_DIR / "snowluma", "SnowLuma ")


def astrbot_dir(cfg: Optional[AppConfig] = None) -> Path:
    cfg = cfg or get_config()
    return resolve_install_dir(cfg.astrbot.install_dir, paths.RUNTIME_DIR / "astrbot", "AstrBot ")


# --------------------------------------------------------------------------- #
# 版本号
# --------------------------------------------------------------------------- #
_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+]?([0-9A-Za-z.\-+]+))?$")


def parse_version(raw: Any) -> Optional[Tuple[int, int, int, str]]:
    """解析 ``v2.2.1`` / ``2.2`` / ``v2.2.1-beta.1`` → ``(2, 2, 1, 'beta.1')``。"""

    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    m = _VERSION_RE.match(text)
    if not m:
        return None
    major = int(m.group(1))
    minor = int(m.group(2) or 0)
    patch = int(m.group(3) or 0)
    pre = (m.group(4) or "").strip()
    # 去掉纯数字构建元数据（2.2.1-0 视为正式版）
    if pre and re.fullmatch(r"\d+", pre):
        pre = ""
    return major, minor, patch, pre


def compare_versions(a: Any, b: Any) -> int:
    """``a < b`` 返回 -1，相等 0，``a > b`` 返回 1；无法解析时按字符串比较。"""

    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        sa, sb = str(a or ""), str(b or "")
        return (sa > sb) - (sa < sb)
    if pa[:3] != pb[:3]:
        return (pa[:3] > pb[:3]) - (pa[:3] < pb[:3])
    # 正式版(无 pre) > 预发布版
    if pa[3] == pb[3]:
        return 0
    if not pa[3]:
        return 1
    if not pb[3]:
        return -1
    return (pa[3] > pb[3]) - (pa[3] < pb[3])


def tag_for(version: Any) -> str:
    """``2.2.1`` → ``v2.2.1``；已经是 ``vX`` 的原样返回。"""

    text = str(version or "").strip()
    if not text or text.lower() in ("latest", "default", ""):
        return ""
    return text if text.lower().startswith("v") else f"v{text}"


def same_version(a: Any, b: Any) -> bool:
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return str(a or "").strip().lstrip("v") == str(b or "").strip().lstrip("v")
    return pa[:3] == pb[:3] and pa[3] == pb[3]


def node_version_supported(raw: Any) -> bool:
    """对齐 ``check-node-version.cjs``：``^24.22.1 || >=26.12.1``。"""

    text = str(raw or "").strip().lstrip("v")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", text)
    if not m:
        return False
    major, minor, patch = (int(m.group(i)) for i in (1, 2, 3))
    if major == 24:
        return minor > 22 or (minor == 22 and patch >= 1)
    if major == 26:
        return minor > 12 or (minor == 12 and patch >= 1)
    return major > 26


# --------------------------------------------------------------------------- #
# 命令执行
# --------------------------------------------------------------------------- #
@dataclass
class CmdResult:
    ok: bool
    code: int
    out: str
    argv: List[str]

    def tail(self, n: int = 8) -> str:
        lines = [ln for ln in str(self.out).splitlines() if ln.strip()]
        return "\n".join(lines[-n:])


def _kill_proc(proc: "subprocess.Popen[str]") -> None:
    """结束一个超时的子进程（Windows: taskkill /T /F；POSIX: 进程组信号 + kill）。"""

    try:
        plat.kill_tree(proc.pid, graceful=0)
    except Exception:  # noqa: BLE001
        pass
    with contextlib.suppress(Exception):
        if proc.poll() is None:
            proc.kill()


def _run(
    argv: Iterable[str],
    *,
    cwd: Optional[Path] = None,
    timeout: float = CMD_TIMEOUT,
    env: Optional[Dict[str, str]] = None,
    on_line: Optional[Callable[[str], None]] = None,
) -> CmdResult:
    """同步执行外部命令并逐行回收输出（磁盘安装/克隆/装依赖都走这里）。"""

    args = [str(a) for a in argv]
    full_env = os.environ.copy()
    full_env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    if env:
        full_env.update({k: str(v) for k, v in env.items()})
    kwargs: Dict[str, Any] = dict(hidden_kwargs())
    try:
        proc = subprocess.Popen(  # noqa: S603
            args,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **kwargs,
        )
    except OSError as exc:
        return CmdResult(False, -1, f"无法执行 {args[0]}：{exc}", args)

    state = {"timeout": False}

    def _on_timeout() -> None:
        state["timeout"] = True
        _kill_proc(proc)

    timer = threading.Timer(float(timeout), _on_timeout)
    timer.daemon = True
    timer.start()
    lines: List[str] = []
    code = -1
    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\r\n")
            if not line:
                continue
            lines.append(line)
            if len(lines) > 4000:
                del lines[:2000]
            if on_line is not None:
                with contextlib.suppress(Exception):
                    on_line(line)
        with contextlib.suppress(subprocess.TimeoutExpired):
            code = proc.wait(timeout=30)
    except Exception as exc:  # noqa: BLE001
        _kill_proc(proc)
        lines.append(f"{type(exc).__name__}: {exc}")
    finally:
        timer.cancel()

    if state["timeout"]:
        lines.append(f"[超时] 命令执行超过 {int(timeout)} 秒，已强制结束")
        return CmdResult(False, -9, "\n".join(lines), args)
    return CmdResult(code == 0, code, "\n".join(lines), args)


def _run_checked(
    argv: Iterable[str],
    *,
    cwd: Optional[Path] = None,
    timeout: float = CMD_TIMEOUT,
    env: Optional[Dict[str, str]] = None,
    what: str = "命令",
    hint: str = "",
    on_line: Optional[Callable[[str], None]] = None,
) -> CmdResult:
    result = _run(argv, cwd=cwd, timeout=timeout, env=env, on_line=on_line)
    if not result.ok:
        parts = [f"{what}失败（退出码 {result.code}）。"]
        tail = result.tail()
        if tail:
            parts.append(f"最后输出：\n{tail}")
        if hint:
            parts.append(f"下一步：{hint}")
        raise DeployError("\n".join(parts))
    return result


def _py_version(exe: str) -> Optional[Tuple[int, int]]:
    if not exe:
        return None
    result = _run([exe, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"], timeout=30)
    if not result.ok:
        return None
    m = re.search(r"(\d+)\.(\d+)", result.out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _git(args: List[str], cwd: Optional[Path] = None, timeout: float = 60) -> str:
    """尽力而为地跑一条 git 查询命令，失败返回空串（用于读 commit 等）。"""

    if not _which("git"):
        return ""
    result = _run([_which("git") or "git"] + args, cwd=cwd, timeout=timeout)
    return result.out.strip() if result.ok else ""


# --------------------------------------------------------------------------- #
# 环境探测
# --------------------------------------------------------------------------- #
def _target_from(platform_name: str, arch: str) -> str:
    """把「平台名 + 架构」翻译成 SnowLuma 的发行目标名（``win32-x64`` 这种）。

    ``platform_name`` 是 ``sys.platform`` 风格（``win32`` / ``linux`` / ``darwin``）。
    """

    arch = (arch or "").lower()
    amd64 = arch in ("x86_64", "amd64", "x64", "x86-64")
    arm64 = arch in ("arm64", "aarch64")
    if platform_name.startswith("win") or platform_name == "nt":
        if amd64:
            return "win32-x64"
        if arm64:
            raise DeployError(
                "SnowLuma 官方暂未发布 Windows ARM64 版（当前架构 "
                f"{arch}）。下一步：换用 x64 的 Windows 电脑，或改用 SnowLuma 源码自行构建。"
            )
    if platform_name.startswith("linux"):
        if amd64:
            return "linux-x64"
        if arm64:
            return "linux-arm64"
    if platform_name.startswith("darwin") or platform_name.startswith("mac"):
        raise DeployError(
            "SnowLuma 官方发行包目前只提供 Windows x64 / Linux x64 / Linux arm64"
            "（macOS 暂无官方构建，插件与原生模块也不支持）。"
            "下一步：请在 Windows 或 Linux 电脑上部署本控制台，或自行从源码构建 SnowLuma。"
        )
    raise DeployError(
        f"当前系统 {platform_name or '未知'}/{arch or '未知'} 没有 SnowLuma 官方发行包。"
        "下一步：请在 Windows x64 或 Linux x64/arm64 上部署。"
    )


def detect_snowluma_target(
    sys_platform: Optional[str] = None, machine: Optional[str] = None
) -> str:
    """返回 SnowLuma 发行目标（``win32-x64`` / ``linux-x64`` / ``linux-arm64``）。

    不传参数时走 :func:`slab.plat.platform_tag`（平台差异只在 plat.py 里判定）；
    显式传 ``sys_platform`` / ``machine`` 时按参数判定（测试与离线包校验用）。
    """

    if sys_platform is None and machine is None:
        tag = plat.platform_tag()  # win-x64 / win-arm64 / linux-x64 / linux-arm64 / darwin-*
        if tag == "win-x64":
            return "win32-x64"
        if tag == "win-arm64":
            return _target_from("win32", "arm64")  # 抛「未发布 ARM64」的中文错误
        if tag in ("linux-x64", "linux-arm64"):
            return tag
        return _target_from(tag.split("-")[0], tag.split("-", 1)[1] if "-" in tag else "")

    # 只给了 machine 没给平台时，平台名也取自平台层（不在这里读 sys.platform）
    if sys_platform is None:
        if plat.IS_WINDOWS:
            sys_platform = "win32"
        elif plat.IS_LINUX:
            sys_platform = "linux"
        else:  # macOS / 其它：platform_tag() 形如 "darwin-arm64"
            sys_platform = plat.platform_tag().split("-")[0]
    platform_name = (sys_platform or "").lower()
    arch = (machine or os.environ.get("PROCESSOR_ARCHITECTURE") or "").lower()
    if not arch or arch in ("x86",):
        try:
            import platform as _platform

            arch = _platform.machine().lower()
        except Exception:  # noqa: BLE001  # pragma: no cover
            arch = "x86_64"
    return _target_from(platform_name, arch)


def snowluma_asset_ext(target: Optional[str] = None) -> str:
    """发行包扩展名：Windows 是 ``.zip``，Linux 是 ``.tar.gz``。"""

    tgt = target or detect_snowluma_target()
    return _TARGET_EXT.get(_TARGET_SUFFIX.get(tgt, ""), ".zip")


def snowluma_asset_name(
    version: Any,
    edition: str = "full",
    *,
    target: Optional[str] = None,
    sys_platform: Optional[str] = None,
    machine: Optional[str] = None,
) -> str:
    """按「平台 × 版本类型」生成官方资产名。

    ``SnowLuma-v2.2.1-win-x64.zip`` / ``SnowLuma-v2.2.1-win-x64-lite.zip`` /
    ``SnowLuma-v2.2.1-linux-x64.tar.gz`` / ``...-linux-arm64-lite.tar.gz``
    """

    tgt = target or detect_snowluma_target(sys_platform, machine)
    if tgt not in _TARGET_SUFFIX:
        raise DeployError(
            f"不支持的安装目标 {tgt}。可选：{', '.join(sorted(_TARGET_SUFFIX))}。"
        )
    tag = tag_for(version)
    if not tag:
        raise DeployError("请先指定 SnowLuma 版本（latest 或 v2.2.1 这样的 tag）。")
    suffix = _TARGET_SUFFIX[tgt]
    ext = _TARGET_EXT[suffix]
    lite = "-lite" if str(edition or "full").strip().lower() == "lite" else ""
    return f"SnowLuma-{tag}-{suffix}{lite}{ext}"


def snowluma_asset_url(tag: str, asset_name: str) -> str:
    """不需要 API 也能拼出的官方资产下载地址。"""

    return f"{GITHUB_WEB}/{SNOWLUMA_REPO}/releases/download/{tag_for(tag) or tag}/{asset_name}"


def pick_asset(assets: Iterable[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    items = [a for a in assets or [] if isinstance(a, dict)]
    for item in items:
        if str(item.get("name") or "") == name:
            return item
    lowered = name.lower()
    for item in items:
        if str(item.get("name") or "").lower() == lowered:
            return item
    return None


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
def check_download_url(url: str) -> str:
    """校验下载地址：必须 https + GitHub 域名白名单。"""

    text = str(url or "").strip()
    if not text:
        raise DeployError("下载地址为空。下一步：在部署页刷新版本列表后重试。")
    parsed = urlsplit(text)
    if parsed.scheme != "https":
        raise DeployError(
            f"下载地址必须是 https（当前「{text}」）。为防中间人篡改，控制台拒绝非加密下载。"
        )
    host = (parsed.hostname or "").lower()
    if host not in DOWNLOAD_HOSTS:
        raise DeployError(
            f"下载地址不在白名单内（主机 {host or '未知'}）：{text}\n"
            "只允许 github.com / api.github.com / objects.githubusercontent.com / "
            "codeload.github.com / release-assets.githubusercontent.com。\n"
            "下一步：在部署页填写可信的 GitHub 镜像前缀，或改用「离线安装」导入官方压缩包。"
        )
    return text


def with_mirror(url: str, mirror: str = "") -> str:
    """可选镜像前缀（例如 ``https://ghfast.top``）→ ``https://ghfast.top/https://github.com/...``。"""

    prefix = (mirror or "").strip().rstrip("/")
    if not prefix:
        return url
    if not prefix.startswith("https://"):
        raise DeployError(
            f"镜像前缀必须是 https 地址（当前「{prefix}」）。下一步：填写类似 https://ghfast.top 的前缀，或留空直连。"
        )
    return f"{prefix}/{url}"


def _response_total(resp: httpx.Response, have: int, expect_size: Optional[int]) -> int:
    content_range = resp.headers.get("content-range") or ""
    m = re.search(r"/(\d+)\s*$", content_range)
    if m:
        return int(m.group(1))
    length = resp.headers.get("content-length") or ""
    if length.isdigit():
        return int(length) + (have if resp.status_code == 206 else 0)
    if expect_size:
        return int(expect_size)
    return 0


def _download_once(
    url: str,
    part: Path,
    *,
    have: int,
    expect_size: Optional[int],
    on_progress: Optional[Callable[[int, int], None]],
    client: Optional[httpx.Client],
    timeout: float,
) -> int:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/octet-stream"}
    if have:
        headers["Range"] = f"bytes={have}-"
    ctx = contextlib.nullcontext(client) if client is not None else httpx.Client(follow_redirects=True, timeout=timeout)
    with ctx as cli:  # type: ignore[union-attr]
        assert cli is not None
        with cli.stream("GET", url, headers=headers, follow_redirects=True, timeout=timeout) as resp:
            code = resp.status_code
            if code == 416:  # Range 越界：本地 .part 已经完整
                return have
            if code not in (200, 206):
                raise _HttpStatusError(code, url)
            if code == 200 and have:
                have = 0
                with contextlib.suppress(OSError):
                    part.unlink()
            total = _response_total(resp, have, expect_size)
            done = have
            with open(part, "ab" if have else "wb") as fh:
                for chunk in resp.iter_bytes(CHUNK):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    if on_progress is not None:
                        on_progress(done, total or done)
            if on_progress is not None:
                on_progress(done, max(total, done))
            return done


def http_download(
    url: str,
    dest: Path,
    *,
    on_progress: Optional[Callable[[int, int], None]] = None,
    mirror: str = "",
    expect_size: Optional[int] = None,
    retries: int = DOWNLOAD_RETRIES,
    client: Optional[httpx.Client] = None,
    timeout: float = DOWNLOAD_TIMEOUT,
    label: str = "下载",
) -> Path:
    """流式下载到 ``dest``（先写 ``.part``，成功后原子改名），支持断点续传与重试。"""

    checked = check_download_url(url)
    target_url = with_mirror(checked, mirror)
    final = Path(dest)
    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + ".part")
    attempts = max(1, int(retries))
    last_error = ""
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        have = part.stat().st_size if part.exists() else 0
        if expect_size and have == int(expect_size) and have > 0:
            os.replace(part, final)
            return final
        try:
            done = _download_once(
                target_url,
                part,
                have=have,
                expect_size=expect_size,
                on_progress=on_progress,
                client=client,
                timeout=timeout,
            )
            size = part.stat().st_size if part.exists() else done
            if expect_size and size != int(expect_size):
                raise DeployError(
                    f"{label}文件大小不符：期望 {expect_size} 字节，实际 {size} 字节。\n"
                    f"下一步：删除 {final} 与 {part.name} 后重试；若多次失败请改用「离线安装」导入官方压缩包。"
                )
            os.replace(part, final)
            return final
        except DeployError:
            raise
        except _HttpStatusError as exc:
            if exc.status == 404:
                raise DeployError(
                    f"{label}失败：服务器返回 404（文件不存在）。\n"
                    f"链接：{checked}\n"
                    "下一步：确认该版本下存在匹配的平台/版本类型（可在部署页刷新版本列表查看），"
                    "或改用「离线安装」导入官方压缩包。"
                ) from exc
            if exc.status in (401, 403):
                raise DeployError(
                    f"{label}失败：GitHub 拒绝访问（HTTP {exc.status}）。\n"
                    "下一步：稍后重试；企业网络可配置镜像前缀；也可改用「离线安装」。"
                ) from exc
            last_error = f"HTTP {exc.status}"
        except Exception as exc:  # noqa: BLE001 - 网络类异常都重试
            last_exc = exc
            last_error = _short_error(exc)
        if attempt < attempts:
            time.sleep(min(0.5 * attempt, 3.0))
    hint = net_error_hint(last_exc, label) if last_exc is not None else (
        f"{label}失败（已重试 {attempts} 次）：{last_error or '未知错误'}"
    )
    raise DeployError(
        f"{hint}\n"
        f"（已重试 {attempts} 次，并保留未完成的 {part.name}，重试时会断点续传）"
    )


# --------------------------------------------------------------------------- #
# 压缩包处理
# --------------------------------------------------------------------------- #
def _safe_member(name: str) -> str:
    text = str(name or "").replace("\\", "/")
    if not text:
        raise DeployError("压缩包内含有空文件名，包可能已损坏。下一步：重新下载或改用离线安装。")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise DeployError(
            f"压缩包内含有绝对路径成员（{name}），为防止写到系统目录已中止。\n"
            "下一步：请只使用 SnowLuma / AstrBot 官方发行包。"
        )
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise DeployError(
            f"压缩包内含有路径穿越成员（{name}），已中止解压。\n下一步：请只使用官方发行包。"
        )
    return text


def verify_archive(path: Path) -> str:
    """校验压缩包完整性，返回 ``zip`` 或 ``tar``；损坏时抛中文错误。"""

    p = Path(path)
    if not p.exists():
        raise DeployError(f"找不到压缩包：{p}。下一步：重新下载，或确认「离线安装」选择的文件路径正确。")
    if p.stat().st_size == 0:
        raise DeployError(f"压缩包是空文件：{p}。下一步：删除后重新下载。")
    if zipfile.is_zipfile(p):
        try:
            with zipfile.ZipFile(p) as zf:
                bad = zf.testzip()
        except (zipfile.BadZipFile, OSError) as exc:
            raise DeployError(
                f"压缩包已损坏（{exc}）。下一步：删除 {p.name} 后重新下载，或改用「离线安装」导入官方包。"
            ) from exc
        if bad:
            raise DeployError(
                f"压缩包校验失败：{bad} 的内容已损坏。\n"
                f"下一步：删除 {p} 后重新下载；反复失败请改用「离线安装」。"
            )
        return "zip"
    try:
        with tarfile.open(p, "r:*") as tf:
            members = tf.getmembers()
    except (tarfile.TarError, OSError) as exc:
        raise DeployError(
            f"无法识别压缩包格式（{exc}）：{p.name}。\n"
            "下一步：确认下载的是 SnowLuma 官方 zip/tar.gz 发行包（不是源码包），或删除后重新下载。"
        ) from exc
    if not members:
        raise DeployError(f"压缩包内没有任何文件：{p.name}。下一步：重新下载或改用离线安装。")
    return "tar"


def _single_root(dest: Path) -> Path:
    """兼容「压缩包内多套了一层目录」的情况，返回真正的发行包根目录。"""

    if (dest / SNOWLUMA_ENTRY).is_file():
        return dest
    children = [c for c in dest.iterdir() if c.is_dir()]
    if len(children) == 1 and (children[0] / SNOWLUMA_ENTRY).is_file():
        return children[0]
    return dest


def _chmod_binaries(root: Path) -> List[str]:
    """给发行包里的可执行文件补上 ``+x``（POSIX；Windows 是空操作）。

    tar.gz 一般保留权限位，但「离线安装」常常是别人从 Windows 转存/解压再打包一遍的
    zip（Windows 没有权限位），解出来 node / launcher.sh 会不可执行，启动时只报
    ``Permission denied``。这里兜底 chmod 一次，失败不致命。
    """

    if plat.IS_WINDOWS:
        return []
    fixed: List[str] = []
    candidates = [root / plat.node_bin_name(), root / "launcher.sh"]
    for pattern in ("*.sh", "bin/*"):
        candidates.extend(sorted(root.glob(pattern)))
    for item in candidates:
        if not item.is_file():
            continue
        plat.chmod_exec(item)
        fixed.append(item.name)
    return fixed


def extract_archive(archive: Path, dest: Path) -> Path:
    """解压到 ``dest``（调用方负责 dest 是新建的临时目录），返回实际根目录。"""

    kind = verify_archive(archive)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if kind == "zip":
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                _safe_member(info.filename)
            zf.extractall(dest)
    else:
        with tarfile.open(archive, "r:*") as tf:
            for member in tf.getmembers():
                _safe_member(member.name)
                if member.issym() or member.islnk():
                    raise DeployError(
                        f"压缩包内含有符号链接（{member.name}），出于安全考虑已中止解压。"
                        "下一步：请只使用官方发行包。"
                    )
            try:
                tf.extractall(dest, filter="data")  # Python 3.12+
            except TypeError:  # pragma: no cover - 旧版本 Python 兜底
                tf.extractall(dest)
    root = _single_root(dest)
    _chmod_binaries(root)
    return root


def archive_release_info(archive: Path) -> Dict[str, Any]:
    """不完整解压就从压缩包里读 ``release.json``（离线安装用）。"""

    info: Dict[str, Any] = {"version": "", "target": ""}
    p = Path(archive)
    if not p.exists():
        return info
    raw: Optional[bytes] = None
    try:
        if zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as zf:
                names = sorted(
                    (n for n in zf.namelist() if n == "release.json" or n.endswith("/release.json")),
                    key=lambda n: n.count("/"),
                )
                if names:
                    raw = zf.read(names[0])
        elif tarfile.is_tarfile(p):
            with tarfile.open(p, "r:*") as tf:
                targets = sorted(
                    (m for m in tf.getmembers() if m.name == "release.json" or m.name.endswith("/release.json")),
                    key=lambda m: m.name.count("/"),
                )
                if targets:
                    fh = tf.extractfile(targets[0])
                    if fh is not None:
                        raw = fh.read()
    except (OSError, tarfile.TarError, zipfile.BadZipFile):
        return info
    if raw:
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            return info
        if isinstance(data, dict):
            info["version"] = str(data.get("version") or "")
            info["target"] = str(data.get("target") or "")
    return info


# --------------------------------------------------------------------------- #
# SnowLuma 运行时配置
# --------------------------------------------------------------------------- #
def write_runtime_config(install_dir: Path, webui_port: int, webui_host: str = "127.0.0.1",
                         hook_autoload: Optional[bool] = None) -> Path:
    """写 ``<install>/config/runtime.json``（保留既有其它键与用户改过的值）。

    ``hook_autoload`` 显式控制"自动注入所有 QQ"。实测某些 SnowLuma 版本把环境变量
    ``SNOWLUMA_HOOK_AUTOLOAD=0`` 当成真值，照样注入所有 QQ，所以开关同时落到文件里双保险。
    """

    try:
        port = int(webui_port)
    except (TypeError, ValueError) as exc:
        raise DeployError(f"WebUI 端口不是合法数字：{webui_port}。请在设置里改成 1024-65535 之间的数字。") from exc
    if not (1 <= port <= 65535):
        raise DeployError(f"WebUI 端口必须在 1-65535 之间（当前 {port}）。请在设置里修改。")
    root = Path(install_dir)
    path = root / "config" / "runtime.json"
    data: Dict[str, Any] = dict(DEFAULT_SNOWLUMA_RUNTIME_CONFIG)
    data["webuiTls"] = {"enabled": False}
    existing = _read_json(path)
    if existing:
        data.update(existing)
    data["webuiPort"] = port
    data["webuiHost"] = webui_host or "127.0.0.1"
    if hook_autoload is not None:
        data["hookAutoLoad"] = bool(hook_autoload)
    if not isinstance(data.get("webuiTls"), dict):
        data["webuiTls"] = {"enabled": False}
    _write_json_atomic(path, data)
    return path


def parse_listening_port(line: str) -> Optional[int]:
    """从 ``[WebUI] listening http://127.0.0.1:5199`` 解析真实端口。"""

    m = re.search(r"\[WebUI\]\s+listening\s+(https?://\S+)", str(line or ""))
    if not m:
        return None
    try:
        port = urlsplit(m.group(1)).port
    except ValueError:
        return None
    return int(port) if port else None


def _tail_lines(path: Path, limit: int = 600) -> List[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 512 * 1024))
            raw = fh.read()
    except OSError:
        return []
    return raw.decode("utf-8", "replace").splitlines()[-limit:]


def detect_snowluma_port(cfg: Optional[AppConfig] = None) -> Optional[int]:
    """探测 SnowLuma 实际运行的 WebUI 端口（日志里的真实端口，可能被自动改用）。"""

    for event in reversed(bus.logs("snowluma", 400)):
        port = parse_listening_port(str(event.get("line") or ""))
        if port:
            return port
    cfg = cfg or get_config()
    candidates: List[Path] = [paths.LOG_DIR / "snowluma.log"]
    home = snowluma_dir(cfg)
    own_logs = home / "logs"
    if own_logs.is_dir():
        candidates.extend(sorted(own_logs.glob("snowluma-*.log"), reverse=True)[:2])
    for path in candidates:
        for line in reversed(_tail_lines(path)):
            port = parse_listening_port(line)
            if port:
                return port
    return None


# --------------------------------------------------------------------------- #
# 已安装状态
# --------------------------------------------------------------------------- #
def installed_snowluma(cfg: Optional[AppConfig] = None) -> Dict[str, Any]:
    cfg = cfg or get_config()
    home = snowluma_dir(cfg)
    release = _read_json(home / "release.json") or {}
    package = _read_json(home / "package.json") or {}
    version = str(release.get("version") or package.get("version") or "").strip()
    node_exe = home / plat.node_bin_name()
    entry = (home / SNOWLUMA_ENTRY).is_file()
    has_node = node_exe.is_file()
    runtime_port = detect_snowluma_port(cfg) if home.is_dir() else None
    return {
        "installed": bool(version) and entry,
        "dir": str(home),
        "version": version,
        "tag": tag_for(version),
        "target": str(release.get("target") or ""),
        "entry": entry,
        "edition": "full" if has_node else ("lite" if entry else ""),
        "has_node": has_node,
        "node": str(node_exe) if has_node else "",
        "webui_port": int(cfg.snowluma.webui_port),
        "runtime_port": runtime_port,
        "port_mismatch": bool(runtime_port and int(cfg.snowluma.webui_port) != runtime_port),
    }


def astrbot_python(cfg: Optional[AppConfig] = None) -> Path:
    cfg = cfg or get_config()
    custom = (cfg.astrbot.python or "").strip()
    if custom:
        _reject_traversal(custom, "AstrBot Python ")
        return Path(custom).expanduser().resolve()
    home = astrbot_dir(cfg)
    # Windows: .venv/Scripts/python.exe；Linux/macOS: .venv/bin/python（平台层统一）
    return plat.venv_python(home)


def installed_astrbot(cfg: Optional[AppConfig] = None) -> Dict[str, Any]:
    cfg = cfg or get_config()
    home = astrbot_dir(cfg)
    py = astrbot_python(cfg)
    commit = branch = ""
    version = ""
    if home.is_dir():
        if (home / ".git").exists():
            commit = _git(["-C", str(home), "rev-parse", "--short", "HEAD"], timeout=20)
            branch = _git(["-C", str(home), "rev-parse", "--abbrev-ref", "HEAD"], timeout=20)
        pyproject = home / "pyproject.toml"
        if pyproject.is_file():
            with contextlib.suppress(OSError):
                m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject.read_text("utf-8", errors="replace"))
                if m:
                    version = m.group(1)
    return {
        "installed": bool((home / "main.py").is_file() and py.is_file()),
        "dir": str(home),
        "version": version,
        "branch": branch,
        "commit": commit,
        "source": "git" if (home / ".git").exists() else ("zip" if home.is_dir() and commit == "" else ""),
        "python": str(py),
        "venv": py.is_file(),
        "main": (home / "main.py").is_file(),
        "webui_port": int(cfg.astrbot.webui_port),
    }


def install_status(cfg: Optional[AppConfig] = None) -> Dict[str, Any]:
    cfg = cfg or get_config()
    return {"snowluma": installed_snowluma(cfg), "astrbot": installed_astrbot(cfg)}


# --------------------------------------------------------------------------- #
# 版本查询（GitHub API，2 小时缓存 + 失败降级）
# --------------------------------------------------------------------------- #
_versions_lock = threading.Lock()
_versions_state: Dict[str, Any] = {"data": None, "ts": 0.0, "error": "", "fail_ts": 0.0}
_branches_state: Dict[str, Any] = {"data": None, "ts": 0.0, "error": "", "fail_ts": 0.0}


def _http_get_json(url: str, timeout: float = API_TIMEOUT) -> Any:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    with httpx.Client(timeout=timeout, follow_redirects=True) as cli:
        resp = cli.get(url, headers=headers)
    if resp.status_code == 404:
        raise DeployError(f"GitHub 上找不到该资源（HTTP 404）：{url}")
    if resp.status_code >= 400:
        raise _HttpStatusError(resp.status_code, url)
    return resp.json()


def _exception_chain(exc: BaseException) -> List[BaseException]:
    """把异常连同 __cause__/__context__ 串起来（httpx 会把 ssl 错误包在 ConnectError 里）。"""

    chain: List[BaseException] = []
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return chain


def is_ssl_error(exc: BaseException) -> bool:
    """判断是不是 TLS 证书类错误（证书链/校验失败）。"""

    for item in _exception_chain(exc):
        if isinstance(item, ssl.SSLError):
            return True
        text = str(item).lower()
        if "certificate_verify_failed" in text or "certificate verify failed" in text:
            return True
    return False


def is_timeout_error(exc: BaseException) -> bool:
    for item in _exception_chain(exc):
        if isinstance(item, (httpx.TimeoutException, TimeoutError, socket.timeout)):
            return True
        if "timed out" in str(item).lower() or "timeout" in type(item).__name__.lower():
            return True
    return False


def _short_error(exc: BaseException) -> str:
    """取最内层异常的类型+消息，避免中间层重复包裹。"""

    chain = _exception_chain(exc)
    deepest = chain[-1] if chain else exc
    text = str(deepest).strip()
    return f"{type(deepest).__name__}: {text[:200]}" if text else type(deepest).__name__


def net_error_hint(exc: BaseException, action: str = "下载") -> str:
    """把网络类异常翻译成「可操作的中文提示」。"""

    detail = _short_error(exc)
    if is_ssl_error(exc):
        return (
            f"{action}失败：HTTPS 证书校验不通过（{detail}）。\n"
            "常见于公司网络/代理/安软替换了 HTTPS 证书。控制台已优先使用系统证书库，"
            "若仍失败：1) 关闭代理或把根证书导入系统证书库后重试；"
            "2) 或在部署页填写镜像前缀；3) 或改用「离线安装」导入官方压缩包。"
        )
    if is_timeout_error(exc):
        return (
            f"{action}超时（{detail}）。\n"
            "下一步：1) 检查网络是否稳定（GitHub 在部分网络较慢）；2) 重试（支持断点续传）；"
            "3) 或在部署页填写镜像前缀 / 改用「离线安装」。"
        )
    return (
        f"{action}失败：无法连接 GitHub（{detail}）。\n"
        "下一步：1) 检查网络与系统代理设置后重试；2) 在部署页填写镜像前缀；"
        "3) 或手动下载官方压缩包用「离线安装」。"
    )


def _friendly_net_error(exc: Exception) -> str:
    return net_error_hint(exc, "获取版本信息")


def check_github(url: str = f"{GITHUB_API}/", timeout: float = 5.0) -> Dict[str, Any]:
    """探测 GitHub 是否**真的可用**（能完成 HTTPS 握手并拿到响应）。

    返回 ``{"ok": bool, "reason": 中文原因, "status": int, "error": str}``。
    注意：TCP 连通不代表可用，这里直接发一次 HTTP 请求。
    """

    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as cli:
            resp = cli.head(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    except Exception as exc:  # noqa: BLE001 - 任何网络/TLS 异常都算不可达
        return {"ok": False, "reason": net_error_hint(exc, "连接 GitHub"), "status": 0, "error": _short_error(exc)}
    if int(resp.status_code) >= 500:
        return {
            "ok": False,
            "reason": f"GitHub 返回 HTTP {resp.status_code}（服务端异常，稍后重试）",
            "status": int(resp.status_code),
            "error": f"HTTP {resp.status_code}",
        }
    return {"ok": True, "reason": "", "status": int(resp.status_code), "error": ""}


def _build_release_index(raw: Any) -> Dict[str, Any]:
    releases: Dict[str, Dict[str, Any]] = {}
    tags: List[str] = []
    latest = ""
    latest_key: Optional[Tuple[int, int, int, str]] = None
    for item in raw or []:
        if not isinstance(item, dict) or item.get("draft"):
            continue
        tag = str(item.get("tag_name") or "").strip()
        if not tag:
            continue
        assets = [
            {
                "name": str(a.get("name") or ""),
                "url": str(a.get("browser_download_url") or ""),
                "size": int(a.get("size") or 0),
            }
            for a in (item.get("assets") or [])
            if isinstance(a, dict)
        ]
        releases[tag] = {
            "tag": tag,
            "assets": assets,
            "prerelease": bool(item.get("prerelease")),
            "published_at": str(item.get("published_at") or ""),
            "name": str(item.get("name") or ""),
        }
        tags.append(tag)
        key = parse_version(tag)
        if not item.get("prerelease") and any(a["name"] for a in assets) and key is not None:
            if latest_key is None or key > latest_key:
                latest_key, latest = key, tag
    # 保持 GitHub 返回顺序（本身就是按发布时间倒序），不二次排序：
    # 版本号排序会把 v2.3.0-rc.1 这类预发布排到正式版前面，容易误导用户
    if not latest and tags:
        latest = tags[0]
    return {
        "tags": tags,
        "releases": releases,
        "latest": latest,
        "fetched_at": time.time(),
        "stale": False,
        "error": "",
    }


def _release_index(force: bool = False) -> Dict[str, Any]:
    now = time.time()
    with _versions_lock:
        data = _versions_state["data"]
        ts = float(_versions_state["ts"] or 0)
        error = str(_versions_state["error"] or "")
        fail_ts = float(_versions_state["fail_ts"] or 0)
    if data and not force and now - ts < VERSIONS_TTL:
        return data
    if not force and fail_ts and now - fail_ts < VERSIONS_FAIL_BACKOFF:
        if data:
            stale = dict(data)
            stale.update(stale=True, error=error)
            return stale
        return _fallback_index(error)
    try:
        raw = _http_get_json(f"{GITHUB_API}/repos/{SNOWLUMA_REPO}/releases?per_page=20")
    except Exception as exc:  # noqa: BLE001 - 任何异常都降级
        message = _friendly_net_error(exc)
        with _versions_lock:
            _versions_state["error"] = message
            _versions_state["fail_ts"] = now
        if data:
            stale = dict(data)
            stale.update(stale=True, error=message)
            return stale
        return _fallback_index(message)
    index = _build_release_index(raw)
    with _versions_lock:
        _versions_state.update(data=index, ts=now, error="", fail_ts=0.0)
    return index


def _fallback_index(error: str) -> Dict[str, Any]:
    return {
        "tags": list(FALLBACK_SNOWLUMA_TAGS),
        "releases": {},
        "latest": FALLBACK_SNOWLUMA_TAGS[0],
        "fetched_at": 0.0,
        "stale": True,
        "error": error,
        "fallback": True,
    }


def _astrbot_branches(force: bool = False) -> Dict[str, Any]:
    now = time.time()
    with _versions_lock:
        data = _branches_state["data"]
        ts = float(_branches_state["ts"] or 0)
        error = str(_branches_state["error"] or "")
        fail_ts = float(_branches_state["fail_ts"] or 0)
    if data and not force and now - ts < VERSIONS_TTL:
        return data
    if not force and fail_ts and now - fail_ts < VERSIONS_FAIL_BACKOFF:
        return data or {"default": ASTRBOT_DEFAULT_BRANCH, "branches": [ASTRBOT_DEFAULT_BRANCH], "error": error}
    try:
        raw = _http_get_json(f"{GITHUB_API}/repos/{ASTRBOT_REPO}/branches?per_page=50")
        names = [str(b.get("name")) for b in raw or [] if isinstance(b, dict) and b.get("name")]
        if ASTRBOT_DEFAULT_BRANCH in names:
            names.remove(ASTRBOT_DEFAULT_BRANCH)
        info = {"default": ASTRBOT_DEFAULT_BRANCH, "branches": [ASTRBOT_DEFAULT_BRANCH] + names, "error": ""}
    except Exception as exc:  # noqa: BLE001
        message = _friendly_net_error(exc)
        with _versions_lock:
            _branches_state.update(error=message, fail_ts=now)
        return {"default": ASTRBOT_DEFAULT_BRANCH, "branches": [ASTRBOT_DEFAULT_BRANCH], "error": message}
    with _versions_lock:
        _branches_state.update(data=info, ts=now, error="", fail_ts=0.0)
    return info


def resolve_release(version: Any = "latest", *, force: bool = False) -> Dict[str, Any]:
    """解析目标发行版：``latest`` 需要 API；指定 tag 时即使离线也能拼出下载地址。"""

    want = str(version or "latest").strip()
    if want.lower() in ("", "latest"):
        index = _release_index(force=force)
        tag = str(index.get("latest") or "")
        if not tag:
            raise DeployError(
                "无法获取 SnowLuma 最新版本号（GitHub 不可达）。\n"
                "下一步：1) 在部署页填写 GitHub 镜像前缀；2) 或直接选择具体版本（如 v2.2.1）；"
                "3) 或改用「离线安装」导入官方压缩包。"
            )
        release = dict(index.get("releases", {}).get(tag) or {"tag": tag, "assets": []})
        release.setdefault("tag", tag)
        release.setdefault("assets", [])
        release["stale"] = bool(index.get("stale"))
        release["error"] = str(index.get("error") or "")
        return release
    tag = tag_for(want)
    index = _release_index(force=force)
    release = dict(index.get("releases", {}).get(tag) or {"tag": tag, "assets": []})
    release.setdefault("tag", tag)
    release.setdefault("assets", [])
    release["stale"] = bool(index.get("stale"))
    release["error"] = str(index.get("error") or "")
    return release


def list_versions(force: bool = False) -> Dict[str, Any]:
    """给前端用的版本列表（SnowLuma tag + AstrBot 分支），失败时降级不抛错。"""

    index = _release_index(force=force)
    tags = list(index.get("tags") or FALLBACK_SNOWLUMA_TAGS)
    branches = _astrbot_branches(force=force)
    status = install_status()
    return {
        "snowluma": tags,
        "snowluma_latest": str(index.get("latest") or (tags[0] if tags else "")),
        "astrbot": str(branches.get("default") or ASTRBOT_DEFAULT_BRANCH),
        "astrbot_branches": list(branches.get("branches") or [ASTRBOT_DEFAULT_BRANCH]),
        "fetched_at": float(index.get("fetched_at") or 0),
        "age": round(time.time() - float(index.get("fetched_at") or time.time()), 1),
        "stale": bool(index.get("stale")),
        "error": str(index.get("error") or ""),
        "installed": {"snowluma": status["snowluma"]["version"], "astrbot": status["astrbot"]["commit"]},
    }


# --------------------------------------------------------------------------- #
# Node / Python 预检
# --------------------------------------------------------------------------- #
_node_version_cache: Dict[str, str] = {}


def node_version(exe: Optional[str] = None) -> str:
    node = exe or _which("node")
    if not node:
        return ""
    cached = _node_version_cache.get(node)
    if cached:
        return cached
    result = _run([node, "--version"], timeout=30)
    if not result.ok:
        return ""
    m = re.search(r"v?(\d+\.\d+\.\d+)", result.out)
    version = m.group(1) if m else ""
    if version:
        _node_version_cache[node] = version
    return version


def check_node_version(install_dir: Optional[Path] = None, node: Optional[str] = None) -> str:
    """lite 版启动/安装前的 Node 预检（与包内 check-node-version.cjs 判定一致）。"""

    node_exe = node or _which("node")
    node_name = plat.node_bin_name()
    if not node_exe:
        raise DeployError(
            "未找到 Node.js，无法运行 SnowLuma lite 版（lite 不带内置 Node）。\n"
            f"下一步：1) 改用 full 版（自带 {node_name}，无需安装 Node）；2) 或安装 Node.js "
            f"{NODE_REQUIRED} 后重启控制台。"
        )
    version = node_version(node_exe)
    cjs = (Path(install_dir) / "check-node-version.cjs") if install_dir else None
    if cjs is not None and cjs.is_file():
        result = _run([node_exe, str(cjs)], cwd=Path(install_dir or "."), timeout=60)
        if not result.ok:
            raise DeployError(
                f"SnowLuma 自检脚本报告 Node.js 版本不受支持（当前 {version or '未知'}，"
                f"要求 {NODE_REQUIRED}）。\n"
                f"下一步：1) 改用 full 版（自带 {node_name}）；2) 或把 Node.js 升级到 24.22.1+ / 26.12.1+。\n"
                f"自检输出：{result.tail(3)}"
            )
        return version
    if not node_version_supported(version):
        raise DeployError(
            f"当前 Node.js 版本 {version or '未知'} 不受支持：要求 {NODE_REQUIRED}。\n"
            f"下一步：1) 改用 full 版（自带 {node_name}，推荐小白使用）；2) 或升级 Node.js 后重试。"
        )
    return version


def _base_python() -> str:
    """挑一个「拿它建 venv 就行」的基础 Python。

    Linux 发行版的 ``python3`` 可能是 3.10（Ubuntu 22.04），所以 POSIX 上**先按版本号找**
    ``python3.12`` / ``python3.13``…，再退回 ``python3`` / ``python``；Windows 老样子。
    """

    names = ("python", "python3") if plat.IS_WINDOWS else (
        "python3.12", "python3.13", "python3.14", "python3", "python",
    )
    for name in names:
        exe = _which(name)
        if exe:
            return exe
    return sys.executable


def _python_install_hint() -> str:
    """「怎么搞到 Python 3.12」的可执行建议（按平台给命令）。"""

    if plat.IS_WINDOWS:
        return (
            f"从 {PYTHON_DOWNLOAD_URL} 安装 Python 3.12+（安装时勾选 Add to PATH），"
            f"或安装 uv（{UV_INSTALL_URL}）后重启控制台重试。"
        )
    return (
        "安装 Python 3.12（Ubuntu 24.04 自带 python3.12；Ubuntu 22.04 需要先加源）：\n"
        "    sudo apt update && sudo apt install -y python3.12 python3.12-venv python3.12-dev\n"
        "    # Ubuntu 22.04 若提示找不到包：sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt update\n"
        f"或者安装 uv（它会自动下载 Python 3.12）：curl -LsSf https://astral.sh/uv/install.sh | sh\n"
        f"（也可以从 {PYTHON_DOWNLOAD_URL} 源码/安装包自己装）；装好后重启控制台重试。"
    )


def python_report() -> Dict[str, Any]:
    """报告 AstrBot 能用的 Python（uv 可自动下载 3.12，则始终可用）。"""

    uv = _which("uv")
    base = _base_python()
    version = _py_version(base)
    ok = bool(uv) or (version is not None and version >= ASTRBOT_MIN_PYTHON)
    info: Dict[str, Any] = {
        "ok": ok,
        "uv": uv,
        "base": base,
        "version": f"{version[0]}.{version[1]}" if version else "",
        "min_version": f"{ASTRBOT_MIN_PYTHON[0]}.{ASTRBOT_MIN_PYTHON[1]}",
        "preferred": ASTRBOT_PREFERRED_PYTHON,
        "download_url": PYTHON_DOWNLOAD_URL,
    }
    if not ok:
        info["message"] = (
            f"系统 Python 版本过低（{info['version'] or '未知'}，AstrBot 需要 "
            f"{info['min_version']}+），并且未检测到 uv。\n"
            f"下一步：{_python_install_hint()}"
        )
    return info


# --------------------------------------------------------------------------- #
# 部署：SnowLuma
# --------------------------------------------------------------------------- #
def github_reachable(url: str = f"{GITHUB_API}/", timeout: float = 5.0) -> bool:
    """``check_github`` 的布尔快捷方式。"""

    return bool(check_github(url, timeout).get("ok"))


def _ensure_not_running(name: str, display: str = "") -> None:
    state = supervisor.state(name)
    if state.status in ("running", "starting"):
        raise DeployError(
            f"{display or name} 正在运行，无法替换文件。\n"
            f"下一步：先在控制台点「停止」{display or name}，再重新部署。"
        )


def _move_dir(src: Path, dst: Path, label: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(dst))
    except OSError as exc:
        raise DeployError(
            f"{label}目录无法移动/替换：{src} → {dst}（{exc}）。\n"
            "下一步：确认相关服务已停止、关闭正在占用文件的程序（杀毒软件/资源管理器预览），然后重试。"
        ) from exc


def _dir_size(path: Path) -> int:
    """目录总字节数（读不到的文件忽略）。"""

    total = 0
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _remove_dir(path: Path, label: str) -> None:
    """删除目录（兼容 POSIX 上的只读文件/目录，例如 git 的 objects）。"""

    def _retry(func: Any, target: Any, _exc: Any = None) -> None:
        # 只读目录/文件（git objects 常常是 r-x）先放宽权限再删
        with contextlib.suppress(OSError):
            os.chmod(target, 0o700)
            func(target)

    try:
        try:
            shutil.rmtree(path, onexc=_retry)  # Python 3.12+
        except TypeError:  # pragma: no cover - 老版本 Python 兜底
            shutil.rmtree(path, onerror=lambda f, p, e: _retry(f, p, e))
    except OSError as exc:
        raise DeployError(
            f"{label}目录无法删除：{path}（{exc}）。\n"
            "下一步：先停掉对应服务、关掉占用文件的程序，然后重试；也可以手动删除该目录"
            + ("" if plat.IS_WINDOWS else "（例如：sudo rm -rf " + str(path) + "）") + "。"
        ) from exc



def _migrate_user_state(old: Path, new: Path) -> List[str]:
    """升级时把旧的 config/ data/ logs/ 搬进新目录，避免丢 WebUI 密码与 OneBot 配置。"""

    moved: List[str] = []
    for name in ("config", "data", "logs"):
        src = old / name
        if not src.is_dir():
            continue
        dst = new / name
        try:
            if dst.exists():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copytree(src, dst)
            moved.append(name)
        except OSError:
            continue
    return moved


def _swap_directory(staged: Path, final: Path, name: str, label: str) -> Optional[Path]:
    """把暂存目录原子替换到最终位置，旧目录备份到 data/backups。"""

    paths.ensure_dirs()
    final.parent.mkdir(parents=True, exist_ok=True)
    backup: Optional[Path] = None
    if final.exists():
        ensure_within(final.parent, final, f"{label}安装目录")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = Path(paths.BACKUP_DIR) / f"{name}-{stamp}-{uuid.uuid4().hex[:4]}"
        _move_dir(final, backup, label)
    try:
        os.replace(staged, final)
    except OSError:
        _move_dir(staged, final, label)
    return backup


def _staging_dir(prefix: str) -> Path:
    base = Path(paths.DATA_DIR) / "tmp"
    staging = base / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    staging.mkdir(parents=True, exist_ok=False)
    return staging


def _env_default(name: str) -> str:
    """环境变量兜底：命令行/无界面部署时不必改 CLI 也能指定离线包或镜像。"""

    return os.environ.get(name, "").strip()


def deploy_snowluma(
    cfg: Optional[AppConfig] = None,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    *,
    offline_zip: Optional[str] = None,
    mirror: str = "",
    force: bool = False,
    edition: Optional[str] = None,
    version: Optional[str] = None,
) -> Dict[str, Any]:
    """部署 SnowLuma：选择资产 → 下载/校验/解压 → 原子替换 → 写 runtime.json。"""

    cfg = cfg or get_config()
    # 无界面部署可用 SLAB_SNOWLUMA_OFFLINE_ZIP / SLAB_GITHUB_MIRROR 兜底
    offline_zip = (offline_zip or "").strip() or _env_default("SLAB_SNOWLUMA_OFFLINE_ZIP") or None
    mirror = (mirror or "").strip() or _env_default("SLAB_GITHUB_MIRROR")
    pr = DeployProgress("snowluma", progress)
    paths.ensure_dirs()
    _ensure_not_running("snowluma", "SnowLuma")
    target_dir = snowluma_dir(cfg)
    current = installed_snowluma(cfg)
    want_edition = str(edition or cfg.snowluma.edition or "full").strip().lower()
    if want_edition not in ("full", "lite"):
        want_edition = "full"
    try:
        target = detect_snowluma_target()
    except DeployError:
        # 已有安装时仍允许走离线安装，所以把错误延后到真正需要下载时再抛
        target = ""
    want = str(version or cfg.snowluma.version or "latest").strip()

    pr(
        "check",
        0.02,
        f"检查安装状态……当前：{('v' + current['version']) if current['version'] else '未安装'}"
        f"（目标：{'latest' if want.lower() == 'latest' else want}，{want_edition} 版）",
    )

    # ---- 1) 确定压缩包来源 ------------------------------------------------
    archive: Optional[Path] = None
    tag = ""
    release: Dict[str, Any] = {}
    offline = Path(str(offline_zip)).expanduser() if offline_zip else None
    if offline is not None:
        _reject_traversal(str(offline), "离线安装包")
        if not offline.is_file():
            raise DeployError(
                f"离线安装包不存在：{offline}\n下一步：重新选择 SnowLuma 官方 zip 压缩包，或检查文件是否被移动。"
            )
        meta = archive_release_info(offline)
        tag = tag_for(meta.get("version") or "") or tag_for(want) or ""
        if meta.get("target") and target and meta["target"] != target:
            raise DeployError(
                f"这个离线安装包是 {meta['target']} 版，不适用于当前系统（{target}）。\n"
                "下一步：请到 https://github.com/SnowLuma/SnowLuma/releases 下载匹配平台的发行包。"
                f"（当前系统应选 {snowluma_asset_name(tag or 'v2.0.0', want_edition, target=target)}）"
            )
        pr("prepare", 0.05, f"使用离线安装包：{offline.name}（{_mb(offline.stat().st_size)}）")
        archive = offline
    elif want.lower() in ("", "latest"):
        try:
            release = resolve_release("latest", force=force)
            tag = str(release.get("tag") or "")
            pr("prepare", 0.05, f"GitHub 最新版本：{tag or '未知'}")
        except DeployError as exc:
            if current["installed"] and not force:
                pr(
                    "done",
                    1.0,
                    f"无法检查 SnowLuma 更新，沿用已安装的 v{current['version']}（{str(exc).splitlines()[0]}）",
                    level="warn",
                )
                result = dict(current)
                result.update(skipped=True, ok=True, warning=str(exc), edition=current["edition"] or want_edition)
                return result
            raise
    else:
        tag = tag_for(want)
        release = resolve_release(tag, force=force)

    # ---- 2) 幂等：已安装且版本/版本类型一致则跳过 -------------------------
    if not force and current["installed"] and not offline:
        installed_edition = current["edition"] or want_edition
        if same_version(current["version"], tag) and installed_edition == want_edition:
            pr("done", 1.0, f"已安装 SnowLuma {tag}（{want_edition} 版），无需重复下载（跳过）")
            result = dict(current)
            result.update(skipped=True, ok=True, dir=str(target_dir))
            return result
    if not force and current["installed"] and offline is not None:
        if same_version(current["version"], tag):
            pr("done", 1.0, f"已安装 SnowLuma {current['tag'] or tag}，离线包版本相同，跳过安装")
            result = dict(current)
            result.update(skipped=True, ok=True, dir=str(target_dir))
            return result

    # ---- 3) lite 版先预检 Node，避免下载完才发现跑不起来 -------------------
    if want_edition == "lite":
        node_version_value = check_node_version(target_dir)
        pr("check", 0.04, f"Node.js {node_version_value} 满足 lite 版要求（{NODE_REQUIRED}）")
    else:
        node_version_value = ""

    # ---- 4) 下载 / 复用压缩包 --------------------------------------------
    if archive is None:
        if not target:
            target = detect_snowluma_target()  # 这里会抛出带指引的中文错误
        asset_name = snowluma_asset_name(tag, want_edition, target=target)
        asset = pick_asset(release.get("assets") or [], asset_name) or {
            "name": asset_name,
            "url": snowluma_asset_url(tag, asset_name),
            "size": 0,
        }
        if not asset.get("url"):
            asset["url"] = snowluma_asset_url(tag, asset_name)
        expect_size = int(asset.get("size") or 0) or None
        dest = Path(paths.DOWNLOAD_DIR) / asset_name
        if dest.exists() and expect_size and dest.stat().st_size == expect_size:
            pr("download", 0.25, f"复用已下载的 {asset_name}（{_mb(expect_size)}），跳过下载")
            archive = dest
        else:
            pr(
                "download",
                0.06,
                f"开始下载 {asset_name}"
                + (f"（{_mb(expect_size)}）" if expect_size else "")
                + ("，支持断点续传" if dest.with_name(dest.name + ".part").exists() else ""),
            )

            def _on_progress(done: int, total: int) -> None:
                frac = (done / total) if total else 0.0
                pr(
                    "download",
                    0.06 + 0.74 * min(1.0, frac),
                    f"已下载 {_mb(done)}" + (f" / {_mb(total)}" if total else "") + f"（{frac * 100:.0f}%）",
                    throttle=True,
                )

            archive = http_download(
                asset["url"],
                dest,
                on_progress=_on_progress,
                mirror=mirror,
                expect_size=expect_size,
                label="下载 SnowLuma 发行包",
            )
            pr("download", 0.82, f"下载完成：{archive.name}（{_mb(archive.stat().st_size)}）")
    assert archive is not None

    # ---- 5) 校验 + 解压到暂存目录 ----------------------------------------
    pr("verify", 0.84, "校验压缩包完整性……")
    kind = verify_archive(archive)
    pr("extract", 0.88, f"解压（{kind}）到临时目录……解压约需 10-30 秒")
    staging = _staging_dir("install-snowluma")
    backup: Optional[Path] = None
    try:
        root = extract_archive(archive, staging)
        if not (root / SNOWLUMA_ENTRY).is_file():
            meta_target = archive_release_info(archive).get("target") or ""
            hint = ""
            if meta_target:
                hint = f"\n（这个包的目标平台是 {meta_target}，当前系统是 {target or '未知'}）"
            # 给用户一个「本机该下哪个包」的示例（拿不到 target 时按平台给字面量）
            example = ("SnowLuma-v2.2.1-linux-x64.tar.gz" if plat.IS_LINUX
                       else "SnowLuma-v2.2.1-win-x64.zip")
            if target:
                with contextlib.suppress(DeployError):
                    example = snowluma_asset_name(tag or "v2.2.1", want_edition, target=target)
            raise DeployError(
                f"压缩包内容异常：解压后没有找到 {SNOWLUMA_ENTRY}。{hint}\n"
                f"下一步：确认下载的是 SnowLuma 官方发行包（文件名形如 {example}），"
                "而不是源码包；或重新下载后再试。"
            )

        # 保留旧安装的用户状态（WebUI 密码、OneBot 配置、日志）
        if target_dir.is_dir():
            pr("install", 0.93, "保留旧的 config/ data/ logs/（升级不丢配置）……")
            migrated = _migrate_user_state(target_dir, root)
            if migrated:
                pr("install", 0.94, f"已迁移旧配置：{', '.join(migrated)}")

        # ---- 6) 写 runtime.json ------------------------------------------
        pr("config", 0.95, f"写入 config/runtime.json（webuiPort={int(cfg.snowluma.webui_port)}）……")
        write_runtime_config(root, int(cfg.snowluma.webui_port), "127.0.0.1")

        # ---- 7) 原子替换 --------------------------------------------------
        pr("install", 0.97, f"安装到 {target_dir}……")
        backup = _swap_directory(root, target_dir, "snowluma", "SnowLuma ")
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(staging, ignore_errors=True)

    version_value = str((_read_json(target_dir / "release.json") or {}).get("version") or tag.lstrip("v"))
    node_exe = target_dir / plat.node_bin_name()
    pr(
        "done",
        1.0,
        f"SnowLuma {version_value}（{want_edition} 版）安装完成：{target_dir}",
    )
    if target_dir.is_dir() and not node_exe.is_file():
        pr(
            "done",
            1.0,
            f"注意：安装目录里没有内置 {plat.node_bin_name()}，启动时会使用系统 Node.js。"
            f"（要求 {NODE_REQUIRED}）",
            level="warn",
        )
    return {
        "ok": True,
        "installed": True,
        "skipped": False,
        "version": version_value,
        "tag": tag_for(version_value),
        "edition": "full" if node_exe.is_file() else "lite",
        "target": target or str((_read_json(target_dir / "release.json") or {}).get("target") or ""),
        "dir": str(target_dir),
        "entry": str(target_dir / SNOWLUMA_ENTRY),
        "node": str(node_exe) if node_exe.is_file() else (_which("node") or ""),
        "node_version": node_version_value,
        "webui_port": int(cfg.snowluma.webui_port),
        "download": str(archive),
        "backup": str(backup) if backup else "",
        "source": "offline" if offline is not None else "github",
    }


# --------------------------------------------------------------------------- #
# 部署：AstrBot
# --------------------------------------------------------------------------- #
def resolve_astrbot_ref(ref: Any) -> str:
    text = str(ref or "").strip()
    if text.lower() in DEFAULT_REF_ALIASES:
        return ASTRBOT_DEFAULT_BRANCH
    return text


def _astrbot_zipball_url(ref: str) -> str:
    if ref == ASTRBOT_DEFAULT_BRANCH or not parse_version(ref):
        return f"{CODELOAD}/{ASTRBOT_REPO}/zip/refs/heads/{ref}"
    tag = tag_for(ref)
    return f"{CODELOAD}/{ASTRBOT_REPO}/zip/refs/tags/{tag}"


def _clone_astrbot(home: Path, url: str, ref: str, pr: DeployProgress) -> None:
    git = _which("git")
    if not git:
        raise DeployError(
            "未检测到 git，将改用源码压缩包安装 AstrBot。\n" + _GIT_HINT
        )
    pr("clone", 0.08, f"克隆 AstrBot 源码（{ref}）……首次约 1-3 分钟")
    _run_checked(
        [git, "clone", "--depth", "1", "--branch", ref, url, str(home)],
        timeout=CLONE_TIMEOUT,
        what=f"克隆 AstrBot 源码（{ref}）",
        hint=(
            "检查网络能否访问 github.com；国内网络可在路由/VPN 侧放行，"
            "或先手动下载 AstrBot 源码压缩包并解压到该目录后重试。"
        ),
        on_line=lambda line: pr.log_line(line),
    )


def _update_astrbot(home: Path, ref: str, pr: DeployProgress) -> None:
    git = _which("git")
    if not git:
        raise DeployError("未检测到 git，无法更新 AstrBot 源码。\n" + _GIT_HINT)
    pr("update", 0.08, f"更新 AstrBot 源码（{ref}）……")
    _run_checked(
        [git, "-C", str(home), "fetch", "--depth", "1", "origin", ref],
        timeout=CLONE_TIMEOUT,
        what=f"拉取 AstrBot 源码（{ref}）",
        hint=f"确认分支/标签 {ref} 存在（可用 main/master/具体 tag），或检查网络。",
        on_line=lambda line: pr.log_line(line),
    )
    _run_checked(
        [git, "-C", str(home), "reset", "--hard", "FETCH_HEAD"],
        timeout=CLONE_TIMEOUT,
        what="切换 AstrBot 源码到最新版本",
        hint="若提示文件被占用，请先停止 AstrBot 再重试。",
    )


def _install_astrbot_zipball(home: Path, ref: str, pr: DeployProgress, mirror: str = "") -> Dict[str, Any]:
    """无 git 时的兜底：下载 codeload zipball → 解压 → 提升内容到安装目录。"""

    url = _astrbot_zipball_url(ref)
    pr("download", 0.10, f"下载 AstrBot 源码压缩包（{ref}）……约 10-20 MB")
    paths.ensure_dirs()
    dest = Path(paths.DOWNLOAD_DIR) / f"astrbot-{ref}-{uuid.uuid4().hex[:6]}.zip"

    def _on_progress(done: int, total: int) -> None:
        frac = (done / total) if total else 0.0
        pr(
            "download",
            0.10 + 0.20 * min(1.0, frac),
            f"已下载 {_mb(done)}" + (f" / {_mb(total)}" if total else ""),
            throttle=True,
        )

    archive = http_download(
        url,
        dest,
        on_progress=_on_progress,
        mirror=mirror,
        label="下载 AstrBot 源码包",
    )
    pr("extract", 0.32, "解压 AstrBot 源码包……")
    staging = _staging_dir("install-astrbot")
    try:
        root = extract_archive(archive, staging)
        if not (root / "main.py").is_file():
            candidates = [p for p in staging.rglob("main.py") if p.parent != staging]
            if candidates:
                root = candidates[0].parent
        if not (root / "main.py").is_file():
            raise DeployError(
                "AstrBot 源码包内容异常：解压后没有找到 main.py。\n"
                "下一步：检查分支/标签名是否正确，或改用 git 方式安装。"
            )
        if home.exists() and any(home.iterdir()):
            pr("install", 0.38, "保留旧的 AstrBot data/ 目录（升级不丢配置）……")
            if (home / "data").is_dir():
                with contextlib.suppress(OSError):
                    shutil.copytree(home / "data", root / "data", dirs_exist_ok=True)
        pr("install", 0.40, f"安装到 {home}……")
        _swap_directory(root, home, "astrbot", "AstrBot ")
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(staging, ignore_errors=True)
        with contextlib.suppress(OSError):
            archive.unlink()
    return {"source": "zip"}


def _create_venv(home: Path, py: Path, pr: DeployProgress) -> Dict[str, Any]:
    """建 .venv：优先 uv（能自动下载 Python 3.12），否则系统 python + 版本校验。

    为什么优先 uv：AstrBot 要求 Python≥3.12，而 Ubuntu 22.04 仓库里只有 3.10；
    uv 能自己下 3.12，不需要用户手动加 PPA。
    """

    python_info = python_report()
    if not python_info["ok"]:
        raise DeployError(str(python_info.get("message") or "Python 环境不满足要求"))
    uv = python_info["uv"]
    venv_dir = home / ".venv"
    if uv:
        pr("venv", 0.46, f"准备 Python {ASTRBOT_PREFERRED_PYTHON}（uv）……")
        install = _run([uv, "python", "install", ASTRBOT_PREFERRED_PYTHON], timeout=600,
                       on_line=lambda line: pr.log_line(line))
        if not install.ok:
            pr("venv", 0.47, "uv 无法自动下载 Python，改用本机 Python 创建虚拟环境", level="warn")
        pr("venv", 0.49, f"创建虚拟环境：{venv_dir}")
        created = _run([uv, "venv", "--python", ASTRBOT_PREFERRED_PYTHON, str(venv_dir)], cwd=home, timeout=600,
                       on_line=lambda line: pr.log_line(line))
        if not created.ok or not py.is_file():
            created = _run([uv, "venv", str(venv_dir)], cwd=home, timeout=600,
                           on_line=lambda line: pr.log_line(line))
    else:
        base = str(python_info["base"])
        pr("venv", 0.49, f"用系统 Python 创建虚拟环境：{venv_dir}")
        created = _run(
            [base, "-m", "venv", str(venv_dir)],
            cwd=home,
            timeout=900,
            on_line=lambda line: pr.log_line(line),
        )
        if not created.ok and not py.is_file() and not plat.IS_WINDOWS:
            # Debian/Ubuntu 的 python3-venv 没装时，`-m venv` 会失败在 ensurepip 上
            pr(
                "venv",
                0.50,
                "创建虚拟环境失败，可能是没装 python3-venv；"
                f"下一步：sudo apt install -y python3-venv（或 python{ASTRBOT_PREFERRED_PYTHON}-venv）后重试",
                level="warn",
            )

    if not py.is_file():
        base_version = python_info.get("version") or "未知"
        raise DeployError(
            f"创建 Python 虚拟环境失败（{venv_dir}）。\n"
            f"系统 Python：{python_info.get('base')}（{base_version}），要求 "
            f"{python_info['min_version']}+。\n"
            f"下一步：{_python_install_hint()}"
        )
    version = _py_version(str(py)) or (0, 0)
    if version < ASTRBOT_MIN_PYTHON:
        raise DeployError(
            f"虚拟环境里的 Python 版本过低（{version[0]}.{version[1]}，要求 "
            f"{python_info['min_version']}+）。\n"
            f"下一步：{_python_install_hint()}删除 {venv_dir} 后重新部署。"
        )
    return {"venv": str(venv_dir), "python": str(py), "version": f"{version[0]}.{version[1]}", "uv": uv}


#: pip 输出里能说明「缺编译工具链」的特征串
_BUILD_MARKERS = ("gcc", "g++", "cc1plus", "error: command", "Failed building wheel",
                  "subprocess-exited-with-error", "Python.h", "compilation terminated")


def _deps_failure_hint(out: str) -> str:
    """依赖安装失败时的「下一步」（按平台给可执行命令）。

    Linux 上很多包要现场编译（pydantic-core / aiohttp 之类），缺 build-essential 时
    pip 只会甩一大段 gcc 报错，普通用户看不懂，所以单独点出来。
    """

    pip_mirror = "pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/"
    if plat.IS_WINDOWS:
        return (
            f"1) 检查网络能否访问 PyPI（可配置国内镜像：{pip_mirror}）；"
            "2) 确认磁盘剩余空间 > 3GB；3) 重试一次。"
        )
    build_cmd = "sudo apt install -y build-essential python3.12-dev python3.12-venv"
    text = str(out or "").lower()
    if any(marker.lower() in text for marker in _BUILD_MARKERS):
        return (
            "1) 日志里出现了编译错误：先装编译工具链再重试：\n"
            f"    {build_cmd}\n"
            f"2) 检查网络能否访问 PyPI（可配置国内镜像：{pip_mirror}）；"
            "3) 确认磁盘剩余空间 > 3GB；4) 重试一次。"
        )
    return (
        f"1) 检查网络能否访问 PyPI（可配置国内镜像：{pip_mirror}）；"
        "2) 确认磁盘剩余空间 > 3GB；"
        "3) 日志里若出现 gcc / 'Failed building wheel' / 'Python.h' 之类的编译报错，"
        f"先装编译工具链：{build_cmd}；"
        "4) 重试一次。"
    )


def _install_astrbot_deps(home: Path, py: Path, pr: DeployProgress, uv: str = "") -> None:
    requirements = home / "requirements.txt"
    if not requirements.is_file():
        raise DeployError(
            f"AstrBot 源码里没有 requirements.txt（{requirements}）。\n"
            "下一步：确认安装目录是 AstrBot 仓库根目录（含 main.py），或在控制台重新部署。"
        )
    pr("deps", 0.55, "正在安装依赖（首次约 3-10 分钟，请耐心等待，不要关闭窗口）……")
    on_line = lambda line: pr.log_line(line)  # noqa: E731
    if uv:
        result = _run(
            [uv, "pip", "install", "--python", str(py), "-r", "requirements.txt"],
            cwd=home,
            timeout=DEPS_TIMEOUT,
            on_line=on_line,
        )
        if result.ok:
            pr("deps", 0.94, "依赖安装完成（uv）")
            return
        # 仓库里没有 uv.lock，所以只能用不带 --frozen 的 uv sync
        pr("deps", 0.80, f"uv pip install 失败（退出码 {result.code}），改试 uv sync……", level="warn")
        sync = _run([uv, "sync"], cwd=home, timeout=DEPS_TIMEOUT, on_line=on_line)
        if sync.ok:
            pr("deps", 0.94, "依赖安装完成（uv sync）")
            return
        pr("deps", 0.86, "uv sync 也失败，改用 pip 重试……", level="warn")
    pip_args = [str(py), "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"]
    _run([str(py), "-m", "pip", "install", "--upgrade", "pip"], cwd=home, timeout=600, on_line=on_line)
    result = _run(
        pip_args,
        cwd=home,
        timeout=DEPS_TIMEOUT,
        on_line=on_line,
    )
    if not result.ok:
        parts = [f"安装 AstrBot 依赖失败（退出码 {result.code}）。"]
        tail = result.tail()
        if tail:
            parts.append(f"最后输出：\n{tail}")
        parts.append(f"下一步：{_deps_failure_hint(result.out)}")
        raise DeployError("\n".join(parts))
    pr("deps", 0.94, "依赖安装完成（pip）")


def deploy_astrbot(
    cfg: Optional[AppConfig] = None,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    *,
    source: Optional[str] = None,
    ref: Optional[str] = None,
    version: Optional[str] = None,
    force: bool = False,
    mirror: str = "",
) -> Dict[str, Any]:
    """部署 AstrBot：git clone/fetch（无 git 退化为 zipball）→ .venv → 依赖。"""

    cfg = cfg or get_config()
    # 无界面部署可用 SLAB_ASTRBOT_SOURCE（本地源码目录或 git URL）兜底
    source = (source or "").strip() or _env_default("SLAB_ASTRBOT_SOURCE") or None
    mirror = (mirror or "").strip() or _env_default("SLAB_GITHUB_MIRROR")
    pr = DeployProgress("astrbot", progress)
    paths.ensure_dirs()
    _ensure_not_running("astrbot", "AstrBot")
    home = astrbot_dir(cfg)
    current = installed_astrbot(cfg)
    ref_name = resolve_astrbot_ref(ref or version or cfg.astrbot.version)
    url = (source or "").strip() or ASTRBOT_GIT_URL
    if source and ("://" not in url and not Path(url).expanduser().exists()):
        raise DeployError(
            f"指定的源码地址既不是 URL 也不存在：{source}\n下一步：留空使用官方仓库，或填写本地已解压的 AstrBot 目录。"
        )

    # 端口占用前置检查（避免启动时才发现面板端口被占）
    port = int(cfg.astrbot.webui_port)
    if not _port_free(port):
        from .envcheck import port_owner

        owner = port_owner(port)
        raise DeployError(
            f"AstrBot 面板端口 {port} 已被占用（{owner or '未知进程'}）。\n"
            f"下一步：关闭占用该端口的程序，或在「设置」里把 AstrBot 面板端口改成其它值（如 6285）后重试。"
        )

    pr("check", 0.02, f"检查 AstrBot 安装状态……当前：{current['commit'] or (current['version'] or '未安装')}")

    # ---- 1) 源码：git clone / fetch 或 zipball ----------------------------
    result_source = "git"
    if source and Path(str(source)).expanduser().is_dir():
        local = Path(str(source)).expanduser().resolve()
        pr("copy", 0.08, f"从本地目录安装：{local}")
        if home.exists() and any(home.iterdir()) and (home / ".git").exists():
            raise DeployError(
                f"安装目录已存在 git 仓库：{home}。\n下一步：先在控制台卸载 AstrBot，或把本地源码放到其它路径。"
            )
        staging = _staging_dir("install-astrbot-local")
        try:
            shutil.copytree(local, staging / "src", dirs_exist_ok=True)
            src = staging / "src"
            if not (src / "main.py").is_file():
                raise DeployError(f"本地目录不是 AstrBot 源码（缺少 main.py）：{local}")
            if home.exists() and (home / "data").is_dir():
                with contextlib.suppress(OSError):
                    shutil.copytree(home / "data", src / "data", dirs_exist_ok=True)
            _swap_directory(src, home, "astrbot", "AstrBot ")
        finally:
            with contextlib.suppress(OSError):
                shutil.rmtree(staging, ignore_errors=True)
        result_source = "local"
    elif (home / ".git").exists():
        _update_astrbot(home, ref_name, pr)
    elif home.exists() and any(home.iterdir()):
        if (home / "main.py").is_file():
            pr("update", 0.06, f"目录已存在 AstrBot 源码但无 git 版本信息，直接复用：{home}", level="warn")
            result_source = "existing"
        else:
            raise DeployError(
                f"安装目录已存在且非空，但不是 AstrBot 源码：{home}\n"
                "下一步：在控制台点「卸载 AstrBot」备份并清空该目录，或把「AstrBot 安装目录」改到其它位置。"
            )
    else:
        try:
            _clone_astrbot(home, url, ref_name, pr)
        except DeployError as exc:
            pr(
                "clone",
                0.10,
                f"git 克隆失败，改用源码压缩包安装（{str(exc).splitlines()[0]}）……",
                level="warn",
            )
            with contextlib.suppress(OSError):
                shutil.rmtree(home, ignore_errors=True)
            info = _install_astrbot_zipball(home, ref_name, pr, mirror=mirror)
            result_source = str(info.get("source") or "zip")

    pr("source", 0.44, f"AstrBot 源码就绪（{result_source}）")

    # ---- 2) 虚拟环境 ------------------------------------------------------
    py = astrbot_python(cfg)
    venv_info: Dict[str, Any] = {"python": str(py), "venv": str(home / ".venv"), "version": ""}
    if py.is_file() and not force:
        pr("venv", 0.46, f"复用已有虚拟环境：{py.parent.parent.name}")
        version = _py_version(str(py))
        venv_info["version"] = f"{version[0]}.{version[1]}" if version else ""
    else:
        venv_info = _create_venv(home, py, pr)

    # ---- 3) 依赖 ----------------------------------------------------------
    _install_astrbot_deps(home, py, pr, uv=str(venv_info.get("uv") or ""))

    # ---- 4) 收尾 ----------------------------------------------------------
    commit = _git(["-C", str(home), "rev-parse", "--short", "HEAD"], timeout=20)
    branch = _git(["-C", str(home), "rev-parse", "--abbrev-ref", "HEAD"], timeout=20)
    status = installed_astrbot(cfg)
    pr("done", 1.0, f"AstrBot 部署完成：{home}（{commit or result_source}）")
    pr("done", 1.0, ASTRBOT_DASHBOARD_NOTE, level="info")
    return {
        "ok": True,
        "installed": True,
        "skipped": False,
        "dir": str(home),
        "python": str(py),
        "venv": venv_info.get("venv"),
        "python_version": venv_info.get("version"),
        "commit": commit,
        "branch": branch,
        "version": status.get("version"),
        "ref": ref_name,
        "source": result_source,
        "webui_port": port,
        "notes": [ASTRBOT_DASHBOARD_NOTE],
    }


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.4):
            return False
    except OSError:
        return True


def deploy_both(
    cfg: Optional[AppConfig] = None,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """先装 SnowLuma 再装 AstrBot；任一失败不影响另一个，结果里逐项报告。"""

    cfg = cfg or get_config()
    results: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    try:
        results["snowluma"] = deploy_snowluma(
            cfg,
            progress=progress,
            mirror=str(kwargs.get("mirror") or ""),
            offline_zip=kwargs.get("offline_zip"),
            force=bool(kwargs.get("force")),
        )
    except DeployError as exc:
        errors["snowluma"] = str(exc)
    try:
        results["astrbot"] = deploy_astrbot(
            cfg,
            progress=progress,
            source=kwargs.get("source"),
            ref=kwargs.get("ref"),
            force=bool(kwargs.get("force")),
            mirror=str(kwargs.get("mirror") or ""),
        )
    except DeployError as exc:
        errors["astrbot"] = str(exc)
    return {"ok": not errors, "results": results, "errors": errors}


def uninstall(target: str, cfg: Optional[AppConfig] = None, purge: bool = False) -> Dict[str, Any]:
    """卸载：停止服务后处理安装目录。

    * ``purge=False``（默认）：目录改名备份到 ``data/backups``，可回滚；
    * ``purge=True``：直接删除目录，并顺带清掉该目标的历史备份（彻底腾空间）。
    """

    cfg = cfg or get_config()
    if target not in ("snowluma", "astrbot"):
        raise DeployError(f"未知卸载目标：{target}（只支持 snowluma / astrbot）")
    display = "SnowLuma" if target == "snowluma" else "AstrBot"
    _ensure_not_running(target, display)
    home = snowluma_dir(cfg) if target == "snowluma" else astrbot_dir(cfg)
    paths.ensure_dirs()

    if purge:
        freed = 0
        if home.exists():
            freed += _dir_size(home)
            _remove_dir(home, display)
        removed_backups = 0
        backup_root = Path(paths.BACKUP_DIR)
        if backup_root.is_dir():
            for item in backup_root.glob(f"{target}-*"):
                try:
                    if item.is_dir():
                        freed += _dir_size(item)
                        _remove_dir(item, f"{display} 备份")
                        removed_backups += 1
                except (OSError, DeployError):
                    continue
        freed_mb = round(freed / 1048576, 1)
        bus.log(target, f"{display} 已彻底删除，释放 {freed_mb} MB（含 {removed_backups} 个旧备份）", level="warn")
        return {
            "ok": True,
            "removed": True,
            "purged": True,
            "dir": str(home),
            "freed_mb": freed_mb,
            "removed_backups": removed_backups,
            "message": f"{display} 已彻底删除，释放约 {freed_mb} MB"
                       + (f"（含 {removed_backups} 个旧备份）" if removed_backups else ""),
        }

    if not home.exists():
        return {"ok": True, "removed": False, "purged": False, "dir": str(home),
                "message": f"{display} 尚未安装，无需卸载。"}
    backup = Path(paths.BACKUP_DIR) / f"{target}-{time.strftime('%Y%m%d-%H%M%S')}"
    _move_dir(home, backup, display)
    bus.log(target, f"{display} 已卸载，原目录备份到 {backup}", level="warn")
    return {
        "ok": True,
        "removed": True,
        "purged": False,
        "dir": str(home),
        "backup": str(backup),
        "message": f"{display} 已卸载；原目录备份在 {backup}（需要恢复时把该目录改回原名即可）。",
    }


# --------------------------------------------------------------------------- #
# 服务定义
# --------------------------------------------------------------------------- #
def snowluma_cwd(cfg: AppConfig) -> Path:
    return snowluma_dir(cfg)


def snowluma_argv(cfg: AppConfig) -> List[str]:
    """full → 自带 node（Windows: node.exe，Linux: 包内 ./node）；lite → PATH 里的 node。"""

    home = snowluma_dir(cfg)
    entry = home / SNOWLUMA_ENTRY
    if not entry.is_file():
        raise DeployError(
            f"SnowLuma 尚未安装或不完整（缺少 {entry}）。\n"
            "下一步：打开「部署」页点「安装 SnowLuma」，或检查安装目录是否被移动。"
        )
    edition = str(cfg.snowluma.edition or "full").strip().lower()
    node_exe = home / plat.node_bin_name()
    if edition == "full":
        if not node_exe.is_file():
            raise DeployError(
                f"当前选用 full 版，但安装目录里没有 {node_exe.name}（{home}）。\n"
                "下一步：重新部署 SnowLuma 选择 full 版，或到设置里把「版本类型」改成 lite（需要系统 Node.js）。"
            )
        # 离线 zip 转存会丢可执行位，这里兜底补一次（Windows 上是空操作）
        plat.chmod_exec(node_exe)
        return [str(node_exe), SNOWLUMA_ENTRY]
    node = _which("node")
    if not node:
        raise DeployError(
            "当前选用 lite 版，但系统里没有找到 Node.js。\n"
            f"下一步：改用 full 版（自带 {plat.node_bin_name()}，无需安装 Node），或安装 Node.js "
            f"{NODE_REQUIRED} 后重启控制台。"
        )
    check_node_version(home, node)
    return [node, SNOWLUMA_ENTRY]


def _accept_eula(cfg: AppConfig) -> bool:
    """是否跳过 SnowLuma 首次启动的协议门禁（SNOWLUMA_ACCEPT_EULA/PRIVACY）。

    默认开启：控制台的安装向导里已提供 EULA/隐私政策链接，点「安装」即视为同意。
    如需强制让用户在 SnowLuma 面板里亲自点同意，可设置环境变量
    ``SLAB_ACCEPT_SNOWLUMA_EULA=0``，或在配置里加 ``snowluma.accept_eula=false``。
    """

    flag = getattr(cfg.snowluma, "accept_eula", None)
    if isinstance(flag, bool):
        return flag
    raw = os.environ.get("SLAB_ACCEPT_SNOWLUMA_EULA", "").strip().lower()
    return raw not in ("0", "false", "no")


def _hook_autoload(cfg: Optional[AppConfig] = None) -> str:
    """是否让 SnowLuma 自动注入所有 QQ。

    默认 ``manual``：不自动注入，由控制台只注入用户选中的进程（避免接管用户自己的 QQ）；
    配置 ``snowluma.hook_mode = auto`` 或环境变量 ``SLAB_SNOWLUMA_HOOK_AUTOLOAD=1`` 才自动注入。
    """

    raw = os.environ.get("SLAB_SNOWLUMA_HOOK_AUTOLOAD", "").strip().lower()
    if raw:
        return "0" if raw in ("0", "false", "no") else "1"
    mode = str(getattr(getattr(cfg, "snowluma", None), "hook_mode", "manual") or "manual")
    return "1" if mode == "auto" else "0"


def snowluma_env(cfg: AppConfig) -> Dict[str, str]:
    """SnowLuma 只认环境变量，不解析任何命令行参数。"""

    home = snowluma_dir(cfg)
    port = int(cfg.snowluma.webui_port)
    env = {
        # 端口/主机；被占用时 SnowLuma 会 +1 递增试到 +50，真实端口以日志为准
        "SNOWLUMA_WEBUI_PORT": str(port),
        "SNOWLUMA_WEBUI_HOST": "127.0.0.1",
        "SNOWLUMA_LOG_LEVEL": "info",
        # 默认 manual：不自动注入，只注入控制台选中的进程（见 slab/qq.py）
        "SNOWLUMA_HOOK_AUTOLOAD": _hook_autoload(cfg),
    }
    if (home / "logs").is_dir():
        env["SNOWLUMA_LOG_DIR"] = str(home / "logs")
    password = (cfg.snowluma.webui_password or "").strip()
    if len(password) >= 8:  # 仅在首次启动（还没有 config/webui.json）时生效
        env["SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD"] = password
    if _accept_eula(cfg):
        # 否则 WebUI 会锁在「请先阅读并同意用户协议与隐私政策」页面（不影响 OneBot 运行）
        env["SNOWLUMA_ACCEPT_EULA"] = "1"
        env["SNOWLUMA_ACCEPT_PRIVACY"] = "1"
        bus.log("snowluma", "启动参数：已注入 SNOWLUMA_ACCEPT_EULA/PRIVACY=1（跳过协议门禁）")
    return env


def astrbot_argv(cfg: AppConfig) -> List[str]:
    home = astrbot_dir(cfg)
    if not (home / "main.py").is_file():
        raise DeployError(
            f"AstrBot 尚未安装或不完整（缺少 {home / 'main.py'}）。\n"
            "下一步：打开「部署」页点「安装 AstrBot」后重试。"
        )
    py = astrbot_python(cfg)
    if not py.is_file():
        raise DeployError(
            f"未找到 AstrBot 的 Python 虚拟环境：{py}\n"
            "下一步：在「部署」页重新安装 AstrBot（会自动创建 .venv 并安装依赖）。"
        )
    return [str(py), "main.py"]


def astrbot_env(cfg: AppConfig) -> Dict[str, str]:
    """AstrBot 的启动环境。

    显式给 ``ASTRBOT_ROOT``：AstrBot 默认按 ``main.py`` 的位置推自己的根目录，
    一旦控制台用软链/自定义安装目录启动，推出来的路径就可能不是安装目录
    （Linux 上尤其容易踩），所以直接把根目录钉死。
    """

    env = {
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "ASTRBOT_DASHBOARD_PORT": str(int(cfg.astrbot.webui_port)),
    }
    with contextlib.suppress(DeployError, OSError):
        env["ASTRBOT_ROOT"] = str(astrbot_dir(cfg))
    return env


def snowluma_service_def() -> ServiceDef:
    return ServiceDef(
        name="snowluma",
        display="SnowLuma",
        argv=snowluma_argv,
        cwd=snowluma_cwd,
        env=snowluma_env,
        # 端口被占用时 SnowLuma 会自动换端口（日志里是真实端口），
        # 因此以「监听成功」的日志行为准，不绑定配置里的端口。
        ready_regex=SNOWLUMA_READY_RE,
        ready_port=None,
        stop_timeout=20,
    )


def astrbot_service_def() -> ServiceDef:
    return ServiceDef(
        name="astrbot",
        display="AstrBot",
        argv=astrbot_argv,
        cwd=astrbot_dir,
        env=astrbot_env,
        ready_regex=ASTRBOT_READY_RE,
        ready_port=lambda cfg: int(cfg.astrbot.webui_port),
        stop_timeout=25,
    )


def register_services() -> None:
    """把 snowluma / astrbot 两个服务注册进 supervisor（可重复调用）。"""

    supervisor.register(snowluma_service_def())
    supervisor.register(astrbot_service_def())
    bus.log("console", "已注册服务：snowluma、astrbot")
