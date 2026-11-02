"""Linux 打包链路 + Electron 桌面壳 POSIX 行为的静态守门测试。

为什么是"静态断言"而不是真跑一遍构建：
  * 打包产物必须在 Linux 上构建（PyInstaller 不能交叉编译），Windows 开发机上跑不了；
    但**脚本结构 / 关键参数**是可以钉死的——本次最致命的 bug（关窗后后端还在跑）
    恰好就是 JS 里少写了一个 ``detached: true``，静态断言正是能在没有 Linux 桌面的
    环境里挡住它回归的手段。
  * 真正的"能不能跑"由 packaging/build-linux.sh 里的冒烟测试负责（起服务 → 打
    /api/health → SIGTERM → 查残留）；CI 在 Linux 上跑那个脚本才算端到端验证。

约定：
  * 只断言"契约"（shebang、严格模式、平台分支、字段齐全），不断言具体文案，
    免得改一句注释就红一片。
  * Windows 上 ``bash -n`` 那条会自动 skip（system32\\bash.exe 是 WSL 壳，路径不通）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_SH = ROOT / "一键启动.sh"
LAUNCHER_BAT = ROOT / "一键启动.bat"
PACKAGING_SH = ROOT / "packaging" / "build-linux.sh"
DESKTOP_SH = ROOT / "desktop" / "build-linux.sh"
BUMP_SH = ROOT / "scripts" / "bump.sh"
BUMP_PS1 = ROOT / "scripts" / "bump.ps1"
SPEC = ROOT / "packaging" / "console.spec"
MAIN_JS = ROOT / "desktop" / "main.js"
ICON_PNG = ROOT / "desktop" / "assets" / "icon.png"
ICON_ICO = ROOT / "desktop" / "assets" / "icon.ico"

SHELL_SCRIPTS = [LAUNCHER_SH, PACKAGING_SH, DESKTOP_SH, BUMP_SH]

#: Linux 构建脚本里绝不允许出现的 Windows-only 命令/写法（漏一个就会在 Linux 上蹦）
WINDOWS_ONLY_TOKENS = [
    "Compress-Archive",
    "Expand-Archive",
    "Start-Process",
    "Remove-Item",
    "Get-ChildItem",
    "taskkill",
    "powershell",
    "cmd /c",
    "cmd.exe",
    ".exe",
    "$env:",
]


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def _text(path: Path) -> str:
    assert path.is_file(), f"缺少文件：{path.relative_to(ROOT)}"
    return path.read_text("utf-8")


def _code_only(text: str) -> str:
    """去掉整行注释，只留可执行代码。

    注释里写「这里把 Compress-Archive 换成 tar」「不要写 desktop\\package.json」是好事，
    但别让这些说明文字把"禁止 Windows-only 写法"的断言绊倒，所以检查前先剥掉注释行。
    """

    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _heredoc(text: str, opener: str) -> str:
    """取出 shell 脚本里某段 heredoc 的内容（用于检查生成的 .desktop / 启动器）。"""

    start = text.find(opener)
    assert start >= 0, f"脚本里找不到 heredoc 起点：{opener}"
    body_start = text.find("\n", start) + 1
    marker = opener.rsplit("<<", 1)[1].strip()
    body_end = text.find(f"\n{marker}\n", body_start)
    assert body_end > body_start, f"heredoc 没有正常结束：{marker}"
    return text[body_start:body_end]


# --------------------------------------------------------------------------- #
# 1) 所有 shell 脚本：shebang / 严格模式 / 只能 LF
# --------------------------------------------------------------------------- #
def test_shell_scripts_are_bash_with_strict_mode():
    for script in SHELL_SCRIPTS:
        raw = script.read_bytes()
        assert raw, f"{script.name} 是空文件"
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{script.name} 带 BOM，bash 会把 shebang 认错行"
        # CRLF 是 .sh 的头号杀手：第一行 "#!/usr/bin/env bash\r" → 直接 command not found
        assert b"\r\n" not in raw, f"{script.name} 含 CRLF，请保持 LF"
        text = raw.decode("utf-8")
        first = text.splitlines()[0]
        assert first.startswith("#!"), f"{script.name} 第一行必须是 shebang"
        assert "bash" in first, f"{script.name} 必须用 bash 解释（别用 sh，下面用到 pipefail）"
        assert "set -euo pipefail" in text, f"{script.name} 必须 set -euo pipefail（fail fast，别静默继续）"


def test_shell_scripts_are_not_empty_skeletons():
    """每个脚本都得有实质内容，防止为了过测试写个空壳。"""

    assert len(_text(PACKAGING_SH).splitlines()) > 80
    assert len(_text(DESKTOP_SH).splitlines()) > 80
    assert len(_text(LAUNCHER_SH).splitlines()) > 40


# --------------------------------------------------------------------------- #
# 2) 一键启动：.bat 必须留着（Windows 用户还在用），.sh 是它的 Linux 等价物
# --------------------------------------------------------------------------- #
def test_launcher_bat_is_preserved():
    text = LAUNCHER_BAT.read_text("utf-8", errors="replace")
    assert text.lower().startswith("@echo off")
    assert "chcp 65001" in text.lower()
    assert "python -m slab serve" in text
    assert "requirements.txt" in text
    assert ".venv" in text
    assert "pause" in text.lower()


def test_launcher_sh_mirrors_bat_behaviour():
    text = _text(LAUNCHER_SH)
    assert "python3" in text, "必须用 python3 而不是 python（多数发行版没有 python）"
    assert ".venv" in text
    assert "requirements.txt" in text
    assert "-m slab serve" in text
    assert "uv venv" in text and "python3 -m venv" in text, "应同时兼容 uv 与内置 venv"
    assert "127.0.0.1:8790" in text
    assert "python3-venv" in text, "Debian/Ubuntu 没装 python3-venv 时 venv 会失败，提示必须写清楚"
    # 前台 exec：信号（Ctrl+C）要直达 python，否则 bash 吞掉信号 → 后端变孤儿
    assert re.search(r"^exec ", text, re.MULTILINE), "启动服务必须 exec，不能包在子 shell 里"
    # 不许把开发机的绝对路径写死
    assert "E:\\AGENT" not in text and "C:\\Users" not in text


# --------------------------------------------------------------------------- #
# 3) console.spec：平台条件化
# --------------------------------------------------------------------------- #
def test_console_spec_is_platform_conditional():
    text = _text(SPEC)
    assert "sys.platform" in text and "IS_WINDOWS" in text, "spec 必须有平台分支"
    # Linux 明确不要 icon / version（PE 专有资源）
    assert re.search(r"^icon = None", text, re.MULTILINE), "Linux 分支 icon 必须是 None"
    assert re.search(r"^version_file = None", text, re.MULTILINE), "Linux 分支 version 必须是 None"
    # Windows 仍然吃 .ico + VERSIONINFO
    assert "app.ico" in text
    assert "version_info.txt" in text
    block = text[text.find("if IS_WINDOWS:"):]
    assert "app.ico" in block.split("EXE_COMMON")[0], "Windows 分支必须继续用 app.ico"


def test_console_spec_keeps_onedir_and_bundle_data():
    text = _text(SPEC)
    assert "slab/web" in text and "collect_submodules" in text
    assert "COLLECT" in text, "onedir 分支（COLLECT）不能丢"
    # onedir 是默认：SLAB_EXE_ONEFILE 只作为可选项
    assert 'os.environ.get("SLAB_EXE_ONEFILE")' in text


# --------------------------------------------------------------------------- #
# 4) desktop/main.js：进程组（本次最致命的 bug）
# --------------------------------------------------------------------------- #
def test_main_js_spawns_backend_in_its_own_process_group():
    text = _text(MAIN_JS)
    # detached 必须与平台绑定：POSIX 开、Windows 关（Windows 开 detached 会多一个黑框）
    assert "detached: !IS_WIN" in text, "spawn 必须带 detached: !IS_WIN，否则 kill(-pid) 一定失败"
    # 负 pid = 整组，这是 Linux 上"连 SnowLuma/AstrBot 一起收掉"的唯一办法
    assert re.search(r"process\.kill\(-pid", text), "必须按进程组发信号（process.kill(-pid, ...)）"
    # 只 SIGTERM 会留僵尸：必须有宽限期 + SIGKILL 兜底
    assert "'SIGTERM'" in text and "'SIGKILL'" in text
    assert "groupAlive" in text, "需要探测整个进程组是否还活着"
    grace = re.search(r"async function killTree\(pid, graceMs = (\d+)\)", text)
    assert grace, "killTree 必须支持宽限期参数"
    assert 2000 <= int(grace.group(1)) <= 3000, "SIGTERM→SIGKILL 的宽限应在 2-3 秒（够后端优雅退出）"
    # Windows 的 taskkill /T 行为不能被顺手删掉
    assert "taskkill" in text and "'/T'" in text


def test_main_js_resolves_linux_backend_name():
    text = _text(MAIN_JS)
    assert re.search(
        r"const BACKEND_BIN = IS_WIN \? 'SnowLuma-AstrBot-Console\.exe' : 'SnowLuma-AstrBot-Console';",
        text,
    ), "后端可执行名必须按平台取（Linux 没有扩展名）"
    # 候选路径里不许再出现硬编码的 exe 名字
    assert "'backend', 'SnowLuma-AstrBot-Console.exe'" not in text
    assert "chmodSync" in text and "X_OK" in text, "Linux 上要补可执行位（解压后常常是 644 → EACCES）"


def test_main_js_picks_linux_storage_root():
    text = _text(MAIN_JS)
    assert "/proc/mounts" in text, "Linux 分支必须读 /proc/mounts 找挂载点"
    assert "XDG_DATA_HOME" in text, "默认应回到 XDG（与后端 plat.default_storage_base 一致）"
    assert "SLAB_STORAGE_ROOT" in text, "环境变量优先级最高，不能被 Linux 分支吃掉"
    assert "statfsSync" in text
    # 挂载点解析要处理 /proc/mounts 的八进制转义（否则带空格的挂载点全对不上）
    assert "0o" in text or r"\\([0-7]{3})" in text or "parseInt(oct, 8)" in text


def test_main_js_uses_png_icon_on_linux():
    text = _text(MAIN_JS)
    assert "icon.png" in text, "Linux 窗口管理器只认 png"
    assert "windowIconPath" in text
    assert re.search(r"icon:\s*windowIconPath\(\)", text), "窗口 icon 必须走平台判断"


def test_main_js_has_no_windows_only_path_literals():
    """关键提示文案里不许再写死 Windows 路径（Linux 用户看不懂 \\ 反斜杠）。"""

    text = _text(MAIN_JS)
    assert "resources\\\\backend\\\\" not in text
    assert "data\\\\logs\\\\console.log" not in text


# --------------------------------------------------------------------------- #
# 5) 打包脚本内容
# --------------------------------------------------------------------------- #
def test_packaging_build_script_builds_onedir_tarball():
    text = _text(PACKAGING_SH)
    assert "unset SLAB_EXE_ONEFILE" in text, "Linux 必须强制 onedir（onefile 在 noexec /tmp 上起不来）"
    assert "tar -czf" in text, "产物是 tar.gz"
    assert "VERSION" in text and "README.md" in text
    assert "docs/LINUX.md" in text, "docs/LINUX.md 存在时要打进包里"
    assert "start.sh" in text, "包里要有启动脚本"
    # 冒烟：真起服务 → 健康接口 → SIGTERM → 查残留
    assert "/api/health" in text
    assert re.search(r"kill -TERM|SIGTERM|-s TERM", text), "冒烟要用 SIGTERM 验证优雅退出"
    assert re.search(r"kill -KILL|SIGKILL|-s KILL", text), "SIGTERM 不退时要有 SIGKILL 兜底"
    assert "pgrep" in text, "冒烟必须检查有没有残留进程"
    assert "mktemp -d" in text, "冒烟数据必须进临时目录，不能污染仓库的 data/"


def test_desktop_build_script_assembles_electron_and_desktop_entry():
    text = _text(DESKTOP_SH)
    assert "node_modules/electron/dist" in text, "Electron 运行时从 node_modules 里拷（不用 electron-builder）"
    assert "resources/app" in text and "resources/backend" in text
    assert "tar -czf" in text
    assert "node --check" in text, "手写 JS 先做语法校验"
    assert "chrome-sandbox" in text, "tar.gz 解压后 SUID 沙箱失效，启动器必须处理否则 Electron 起不来"
    # Electron 的 dist 里有个叫 version 的文件（内容是 33.x），会被升级检查当成"新版本号"
    assert "electron-version" in text, "必须把 Electron 的 version 文件改名，并放自己的 VERSION"
    assert 'cp "$ROOT/VERSION" "$APP_DIR/VERSION"' in text


def test_desktop_entry_has_required_fields():
    block = _heredoc(_text(DESKTOP_SH), 'cat > "$APP_DIR/${PROJECT_NAME}.desktop" <<DESKTOP')
    for field in ("[Desktop Entry]", "Type=Application", "Name=", "Exec=", "Icon=",
                  "Terminal=false", "Categories=", "Comment="):
        assert field in block, f".desktop 缺少字段：{field}"
    assert ".sh" in block, "Exec 应该指向启动器（它负责 --no-sandbox 兜底）"
    assert ".png" in block, "Icon 必须是 png（GNOME/KDE 不认 ico）"


def test_linux_scripts_contain_no_windows_only_commands():
    for script in (PACKAGING_SH, DESKTOP_SH, LAUNCHER_SH):
        text = _code_only(_text(script))
        for token in WINDOWS_ONLY_TOKENS:
            assert token not in text, (
                f"{script.name} 里出现了 Windows-only 写法 {token!r}，在 Linux 上会直接失败"
            )


def test_linux_build_scripts_also_emit_zip_for_inapp_upgrade():
    """Linux 发布必须**同时**给一个 .zip。

    原因：控制台的「升级」走 ``slab/upgrade.py::inspect_zip``，它只接受 .zip
    （``suffix != ".zip"`` 直接拒绝）。只发 tar.gz 的话，Linux 用户在界面里选升级包
    会被判成"下错包"。用 python 的 zipfile 而不是 zip 命令：构建机不一定装了 zip。
    """

    for script in (PACKAGING_SH, DESKTOP_SH):
        text = _text(script)
        assert "zipfile" in text, f"{script.name} 应该用 python zipfile 打 .zip（不依赖 zip 命令）"
        assert re.search(r'\.zip"?\s*$', text, re.MULTILINE), f"{script.name} 里要真的生成 *.zip 产物"
        assert "external_attr" in text, (
            f"{script.name}: zip 要写 st_mode 到 external_attr，解压后入口才仍有可执行位"
        )


# --------------------------------------------------------------------------- #
# 6) 图标资源
# --------------------------------------------------------------------------- #
def test_desktop_assets_have_png_icon():
    raw = ICON_PNG.read_bytes()
    assert raw.startswith(b"\x89PNG\r\n\x1a\n"), "icon.png 不是合法 PNG"
    # IHDR 紧跟签名：宽高各 4 字节大端，色深 1 字节，颜色类型 1 字节
    width = int.from_bytes(raw[16:20], "big")
    height = int.from_bytes(raw[20:24], "big")
    bit_depth, color_type = raw[24], raw[25]
    assert width == height >= 256, f"图标至少 256px 才够桌面环境缩放到 128/64/48（当前 {width}px）"
    assert (bit_depth, color_type) == (8, 6), "必须是 8bit RGBA（带透明圆角）"
    # Windows 那份还留着，别被顺手删掉
    assert ICON_ICO.is_file()


# --------------------------------------------------------------------------- #
# 7) scripts/bump：跨平台
# --------------------------------------------------------------------------- #
def test_bump_ps1_uses_cross_platform_paths():
    text = _code_only(_text(BUMP_PS1))
    assert "desktop\\package.json" not in text, (
        'bump.ps1 不许写 "desktop\\package.json"：Linux/macOS 的 pwsh 7 把反斜杠当普通字符'
    )
    assert 'Join-Path (Join-Path $Root "desktop") "package.json"' in text
    full = _text(BUMP_PS1)
    assert "VERSION" in full and "CHANGELOG" in full and "package.json" in full


def test_bump_sh_exists_as_linux_equivalent():
    text = _text(BUMP_SH)
    assert "VERSION" in text and "CHANGELOG.md" in text and "desktop" in text
    for kind in ("patch", "minor", "major"):
        assert kind in text
    assert "package.json" in text


# --------------------------------------------------------------------------- #
# 8) 语法自检：bash -n（只在类 Unix 上跑）
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows 上的 bash.exe 是 WSL 壳，路径不通；语法在 WSL 里单独验证",
)
def test_shell_syntax_with_bash_n():
    bash = shutil.which("bash")
    assert bash, "找不到 bash"
    for script in SHELL_SCRIPTS:
        proc = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
        assert proc.returncode == 0, f"{script.name} 语法错误：{proc.stderr}"


@pytest.mark.skipif(
    shutil.which("shellcheck") is None or os.name == "nt",
    reason="没装 shellcheck（可选工具），跳过",
)
def test_shellcheck_if_available():
    shellcheck = shutil.which("shellcheck")
    for script in SHELL_SCRIPTS:
        proc = subprocess.run(
            [shellcheck, "-S", "warning", "-e", "SC1091", str(script)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"{script.name} shellcheck 告警：\n{proc.stdout}{proc.stderr}"


# --------------------------------------------------------------------------- #
# 9) 桌面壳的 Linux 依赖别混进 Windows 现有测试的运行路径
# --------------------------------------------------------------------------- #
def test_linux_only_tooling_is_not_imported_by_runtime_code():
    """打包脚本用到的 Python 模块不允许进入运行时包（console.spec 的 excludes 兜底）。"""

    text = _text(SPEC)
    if (ROOT / "packaging" / "make_icon.py").is_file():
        assert '"PIL"' in text, "PIL 只在打包机上用，运行时必须 exclude"


def test_version_files_untouched_by_packaging_scripts():
    """打包脚本不许自己写 VERSION / CHANGELOG（版本统一由 scripts/bump.* 提）。"""

    for script in (PACKAGING_SH, DESKTOP_SH):
        text = _text(script)
        assert "> \"$ROOT/VERSION\"" not in text
        assert "CHANGELOG" not in text
    assert sys.version_info >= (3, 8)  # 这些测试本身用到的语法/路径 API 的前提
