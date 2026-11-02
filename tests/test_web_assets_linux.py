"""Linux 版前端文案 / 文档 / 平台判定的静态检查。

对应 1.8.0（Linux 版）。这些测试不启动浏览器、不依赖操作系统，只钉住三件事：

1. 前端在 Linux 上说对话：Windows 专有说法（C 盘、黑框、双击 exe、python.exe…）
   在 Linux 上会被换成挂载点 / 启动脚本 / python3；
2. 平台判定**只读后端字段**（`envcheck.env_report()` 的 env.is_linux / tag / os / platform），
   前端不许自己嗅探系统；
3. `docs/LINUX.md` 存在且关键小节齐全，README / DEPLOY / MANUAL / TROUBLESHOOTING / ARCHITECTURE
   都补了 Linux 分支，CHANGELOG 有 [1.8.0] 段。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "slab" / "web"
APP_JS = WEB / "app.js"
INDEX = WEB / "index.html"
LINUX_DOC = ROOT / "docs" / "LINUX.md"

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
# 单引号字典条目：'中文键': '英文值'（值可能缩进到下一行）
PAIR_RE = re.compile(r"""'((?:[^'\\]|\\.)*)'\s*:\s*'((?:[^'\\]|\\.)*)'""", re.S)


def _read(path: Path) -> str:
    return path.read_text("utf-8")


def _i18n_source() -> str:
    return _read(WEB / "i18n.js") + "\n" + _read(WEB / "i18n-extra.js")


def _i18n_pairs() -> dict[str, str]:
    pairs: dict[str, str] = {}
    for name in ("i18n.js", "i18n-extra.js"):
        for key, value in PAIR_RE.findall(_read(WEB / name)):
            pairs.setdefault(key, value)
    return pairs


def _function_body(source: str, name: str) -> str:
    """截出 ``function name(...) { ... }`` 的函数体（够用即可，不做完整解析）。"""

    start = source.index(f"function {name}(")
    nxt = source.find("\n  function ", start + 1)
    return source[start: nxt if nxt > 0 else len(source)]


# --------------------------------------------------------------------------- #
# 1. docs/LINUX.md
# --------------------------------------------------------------------------- #
def test_linux_doc_exists_and_is_substantial():
    assert LINUX_DOC.is_file(), "缺少 docs/LINUX.md"
    text = _read(LINUX_DOC)
    assert text.count("\n## ") >= 8, "docs/LINUX.md 小节太少"
    assert "SnowLuma" in text and "AstrBot" in text


def test_linux_doc_support_matrix():
    text = _read(LINUX_DOC)
    for key in ("支持矩阵", "Ubuntu 22.04", "Ubuntu 24.04", "Debian 12", "arm64", "未实测"):
        assert key in text, f"LINUX.md 支持矩阵缺少「{key}」"
    assert "Windows" in text, "支持矩阵应同时说明 Windows"


def test_linux_doc_dependencies_and_runtimes():
    text = _read(LINUX_DOC)
    # apt 依赖一行命令
    assert "apt install" in text and "apt update" in text
    for pkg in ("git", "python3-venv", "libcap2-bin", "xdg-utils"):
        assert pkg in text, f"LINUX.md 未提到系统依赖 {pkg}"
    # Python ≥ 3.12（AstrBot 硬门槛）与 Ubuntu 22.04 的补法
    assert "3.12" in text and "python3.12" in text
    assert "uv python install" in text, "22.04 上补 3.12 的办法应写出来"


def test_linux_doc_node_version_gate():
    text = _read(LINUX_DOC)
    assert "^24.22.1" in text and "26.12.1" in text, "必须写清 Node 版本门槛"
    assert "发行版" in text and "自带" in text, "要说明发行版包不满足、用 full 包自带 node"
    assert "full" in text


def test_linux_doc_setcap_and_ptrace_scope():
    text = _read(LINUX_DOC)
    assert "setcap cap_sys_ptrace=ep" in text
    assert "/proc/sys/kernel/yama/ptrace_scope" in text
    assert "ptrace_scope=3" in text, "要说明 Yama=3 直接禁止"
    assert "同一个 uid" in text or "同 uid" in text, "必须强调与 QQ 同 uid"
    assert "readlink -f" in text, "setcap 要打在真实路径上"


def test_linux_doc_qq_and_multi_instance():
    text = _read(LINUX_DOC)
    assert "/opt/QQ" in text
    # 官方包是 QQNT，文件名形如 QQ_<版本>_<日期>_<arch>_<序号>.deb，落点 /opt/QQ；
    # <arch> 用 Debian 命名（amd64 / arm64），版本日期段会变 → 文档用 glob，不写死完整文件名。
    # 老教程里的 linuxqq_*.deb 是停更的 QQ for Linux 2.0（装到 /opt/tencent-qq），注入不了。
    assert "QQNT" in text, "要说明装的是 QQNT，而不是已停更的 QQ for Linux 2.0"
    assert "QQ_*_" in text, "要给出官方 QQNT deb 的安装命令（glob 形态）"
    assert "amd64" in text and "arm64" in text, "要把两种 Debian 架构 token 都写出来"
    assert "linuxqq" in text, "要点名已停更的 linuxqq_*.deb，避免用户装错"
    assert "sudo apt install -y ./linuxqq" not in text, "别再把停更的 linuxqq_*.deb 当安装命令"
    assert "扫码" in text, "要说明扫码登录"
    assert "独立 HOME" in text and "SingletonLock" in text, "多开要讲独立 HOME 与单实例锁"
    assert "深度分身" in text or "DLL" in text, "要说明 Linux 没有 DLL 注入"


def test_linux_doc_covers_in_app_upgrade():
    """应用内升级：zip 才是升级包（inspect_zip 只认 .zip），tar.gz 是分发；需说明可执行位兜底。"""

    text = _read(LINUX_DOC)
    assert "8.5 应用内升级" in text, "缺少「应用内升级」小节"
    assert ".tar.gz" in text and ".zip" in text
    assert "inspect_zip" in text, "要说明后端只接受 zip 升级包"
    assert "chmod +x" in text, "要给出 Permission denied 的兜底命令"
    assert "electron-version" in text, "要说明桌面版 zip 里 version 改名、避免版本号被判成 33.x"


def test_linux_doc_headless_and_systemd():
    text = _read(LINUX_DOC)
    for tool in ("Xvfb", "fluxbox", "x11vnc", "novnc", "websockify"):
        assert tool.lower() in text.lower(), f"无头服务器指引缺少 {tool}"
    assert "systemd --user" in text
    assert "loginctl enable-linger" in text
    assert "journalctl --user" in text


def test_linux_doc_logs_and_troubleshooting():
    text = _read(LINUX_DOC)
    assert "data/logs" in text, "要写清日志位置"
    assert "常见故障" in text
    for key in ("EPERM", "DISPLAY", "ptrace_scope", "setcap", "/opt/QQ"):
        assert key in text, f"故障表缺少「{key}」相关条目"


# --------------------------------------------------------------------------- #
# 2. 平台判定：只读后端字段
# --------------------------------------------------------------------------- #
def test_platform_detection_reads_backend_fields_only():
    js = _read(APP_JS)
    assert "function platformOf(" in js and "function currentPlatform(" in js
    # 后端 envcheck.env_report() 会带的字段
    for field in ("is_linux", "is_windows", "platform_tag", "e.os", "e.platform"):
        assert field in js, f"平台判定应读取后端字段 {field}"
    # 不许自己嗅探系统
    for bad in ("navigator.userAgent", "navigator.platform", "userAgentData", "process.platform"):
        assert bad not in js, f"前端不应自己猜系统（发现 {bad}）"


def test_platform_copy_keeps_windows_untouched():
    """Linux 分支只在确定是 Linux 时改写；Windows / 未知平台保持 HTML 原文。"""

    js = _read(APP_JS)
    body = _function_body(js, "applyPlatformCopy")
    assert "if (plat !== PLAT.LINUX) return plat;" in body, "非 Linux 必须原样返回，不改写 Windows 文案"


def test_env_page_renders_linux_specific_checks():
    js = _read(APP_JS)
    # 后端「整包」优先，其次自己按 env.linux 推
    assert "env.checks" in js
    assert "linuxEnvFallback" in js
    for key in ("ptrace_scope", "node_cap_ptrace", "python3-venv", "DISPLAY", "venv_ok"):
        assert key in js, f"环境自检缺少 Linux 项 {key}"
    assert "setcap" in js and "sudo apt install" in js, "修复指引要给出可直接复制的命令"
    assert "envFixRow" in js and "修复：" in js


def test_env_row_labels_cover_backend_check_keys():
    """envcheck.env_report() 的 checks key 都要有前端行标题（否则英文界面会显示后端中文 label）。"""

    js = _read(APP_JS)
    start = js.index("const ENV_ROW_LABELS = {")
    block = js[start: js.index("};", start)]
    for key in ("python", "git", "disk", "node", "ptrace_scope", "node_cap_ptrace",
                "display", "venv", "uv", "pnpm"):
        assert f"    {key}:" in block, f"ENV_ROW_LABELS 缺少 checks key「{key}」"


def test_env_check_levels_are_respected():
    """后端约定：ok=null 表示未知（不是失败）；ok=false 且 level=warn 的项（例如 node）不能标红。"""

    js = _read(APP_JS)
    body = _function_body(js, "appendEnvCheck")
    assert "row.level === 'warn'" in body, "要读后端 level 字段"
    assert "soft ? 'warn' : false" in body, "level=warn 的失败项应显示为警告而不是错误"
    assert "state !== true" in body, "只有不是通过的项才追加修复指引"


# --------------------------------------------------------------------------- #
# 3. 文案：Linux 说法存在，且中英两套都有
# --------------------------------------------------------------------------- #
LINUX_COPY = (
    "独立 HOME 多开",
    "Python（≥ 3.12，控制台与 AstrBot 都要）",
    "可用磁盘空间（建议 ≥ 3 GB）",
    "python3-venv（创建虚拟环境需要）",
    "Yama ptrace_scope（= 3 时禁止注入）",
    "node 的 cap_sys_ptrace（注入权限）",
    "图形会话（DISPLAY，扫码登录需要）",
    "修复：",
    "清空备份（data/backups）",
    "在文件管理器里打开",
    "想放到别的挂载点就改这里；留空用默认位置",
    "默认：程序目录/runtime/snowluma",
    "留空 = 程序目录/runtime/astrbot",
    "路径已复制，可粘贴到文件管理器地址栏",
    "已用独立 HOME 启动多开 QQ",
    "独立 HOME 多开失败",
    "大约 3–10 分钟；输出都显示在下方日志里",
    "未知（缺 libcap2-bin 时无法确认）",
    "不会额外弹出终端窗口",
    "用独立 HOME 启动一个新的 QQ 实例（Linux 没有 DLL 注入这回事）",
    "选择要多开的 QQ 安装",
)


def test_linux_copy_present_in_frontend():
    js = _read(APP_JS)
    html = _read(INDEX)
    missing = [text for text in LINUX_COPY if text not in js and text not in html]
    assert not missing, f"前端缺少 Linux 文案: {missing}"


def test_linux_copy_has_english_translations():
    """文案走 i18n：每条 Linux 中文都要有英文词条，且英文里不能再夹中文。"""

    pairs = _i18n_pairs()
    missing = [text for text in LINUX_COPY if text not in pairs]
    assert not missing, f"i18n 缺少词条: {missing}"
    untranslated = [text for text in LINUX_COPY if CJK_RE.search(pairs[text])]
    assert not untranslated, f"这些词条的英文里还夹着中文: {untranslated}"


def test_linux_copy_avoids_windows_only_wording():
    """applyPlatformCopy / linuxEnvFallback 里的 Linux 文案不该再提 C 盘、黑框、python.exe。"""

    js = _read(APP_JS)
    for func in ("applyPlatformCopy", "linuxEnvFallback"):
        body = _function_body(js, func)
        for bad in ("C 盘", "C盘", "盘符", "黑框", "python.exe", "Scripts"):
            assert bad not in body, f"{func} 的 Linux 文案里出现了 Windows 专有说法「{bad}」"


def test_frontend_has_no_bare_drive_letter_paths():
    """前端不再把 Windows 绝对路径写死在用户可见位置（平台示例走 platCopy 例外）。"""

    offenders = []
    for name in ("app.js", "i18n.js", "i18n-extra.js", "index.html",
                 "term.js", "wizard.js", "app.css", "theme.css"):
        path = WEB / name
        if not path.is_file():
            continue
        for lineno, line in enumerate(_read(path).splitlines(), 1):
            if not re.search(r"[A-Za-z]:\\\\", line):
                continue
            if "platCopy(" in line:
                continue  # 平台示例：Linux 上走另一份文案，不会显示出来
            offenders.append(f"{name}:{lineno}: {line.strip()}")
    assert not offenders, "前端出现裸的盘符路径:\n" + "\n".join(offenders)


# --------------------------------------------------------------------------- #
# 4. QQ 页：Linux 用独立 HOME，没有 DLL 分身
# --------------------------------------------------------------------------- #
def test_qq_linux_branch_uses_launch_isolated_only():
    js = _read(APP_JS)
    body = _function_body(js, "launchIsolatedQq")
    linux_part, sep, win_part = body.partition("// Windows：优先用")
    assert sep, "launchIsolatedQq 应保留 Windows 的深度分身分支（带注释）"
    assert "/api/qq/launch-isolated" in linux_part, "Linux 分支应走「独立 HOME」的隔离启动接口"
    assert "/api/qq/clone" not in linux_part, "Linux 分支不应调用 DLL 分身接口"
    assert "/api/qq/clone/status" in win_part, "Windows 分支保持原样（深度分身）"


def test_qq_page_gives_linux_install_hint():
    html = _read(INDEX)
    js = _read(APP_JS)
    assert 'id="qq-linux-hint"' in html and 'id="qq-linux-install"' in html
    # 官方 Linux QQ 是 QQNT 的 deb，落点 /opt/QQ（SnowLuma 只认这个）。
    # 文件名版本/日期段会变 → 前端也用 glob（./QQ_*.deb），并说明 amd64 / arm64 两种架构。
    # 旧版 linuxqq_*.deb 是已停更的 QQ 2.0（装到 /opt/tencent-qq），注入不到，不能引导用户去装。
    assert "QQ_*.deb" in js, "未安装时要给出官方 QQNT deb 安装命令（glob 形态）"
    assert "amd64" in js and "arm64" in js, "要提示两种 Debian 架构包"
    assert "tencent-qq" not in js, "不能引导用户装已停更的 QQ 2.0"
    assert "/opt/QQ" in js


def test_qq_multi_instance_mode_comes_from_backend():
    """多开形态按后端 /api/qq/summary 的 ``clone_mode`` 切，而不是只看平台自己猜。"""

    js = _read(APP_JS)
    assert "clone_mode" in _function_body(js, "renderQq"), "renderQq 要读后端 clone_mode 字段"
    mode_body = _function_body(js, "qqHomeMode")
    assert "qqCloneMode === 'home'" in mode_body and "qqCloneMode === 'inject'" in mode_body
    assert "isLinux()" in mode_body, "后端没给 clone_mode 时才退回平台判定"
    assert "qqHomeMode()" in _function_body(js, "launchIsolatedQq"), "启动入口要按 clone_mode 分流"


# --------------------------------------------------------------------------- #
# 5. 其它文档 / CHANGELOG 的 Linux 补丁
# --------------------------------------------------------------------------- #
def test_readme_documents_linux():
    readme = _read(ROOT / "README.md")
    for key in (
        "Linux",
        "Ubuntu 22.04",
        "docs/LINUX.md",
        "一键启动.sh",
        "build-linux.sh",
        "slab/plat.py",
    ):
        assert key in readme, f"README 缺少「{key}」"
    assert "setcap" in readme, "README 应指向 Linux 注入前置条件"
    assert ".tar.gz" in readme and ".zip" in readme, "README 打包段要说明 Linux 同时产 tar.gz 与 zip（升级只认 zip）"
    assert "8.5" in readme, "README 应指向 LINUX.md §8.5（应用内升级）"


def test_deploy_manual_architecture_have_linux_notes():
    deploy = _read(ROOT / "docs" / "DEPLOY.md")
    assert "Linux" in deploy and "linux-x64" in deploy
    assert "setcap" in deploy or "ptrace" in deploy

    manual = _read(ROOT / "docs" / "MANUAL.md")
    assert "Linux" in manual and "独立 HOME" in manual
    assert "/opt/QQ" in manual

    arch = _read(ROOT / "docs" / "ARCHITECTURE.md")
    assert "plat.py" in arch, "架构文档应说明平台层"
    assert "进程组" in arch, "要说明 POSIX 用进程组收尾"
    assert "/proc/net/tcp" in arch, "要说明 Linux 端口→PID 的实现"


def test_troubleshooting_has_linux_entries():
    trouble = _read(ROOT / "docs" / "TROUBLESHOOTING.md")
    sections = re.findall(r"^## \d+\.", trouble, flags=re.MULTILINE)
    assert len(sections) >= 15, f"排错文档条目不足，只有 {len(sections)} 条"
    # setcap / ptrace_scope / DISPLAY 三类
    assert "cap_sys_ptrace" in trouble and "setcap" in trouble
    assert "ptrace_scope" in trouble and "yama" in trouble.lower()
    assert "DISPLAY" in trouble
    assert "LINUX.md" in trouble


def test_changelog_has_linux_release_section():
    text = _read(ROOT / "CHANGELOG.md")
    assert "## [1.8.0]" in text, "CHANGELOG 缺少 1.8.0（Linux 版）条目"
    section = text.split("## [1.8.0]", 1)[1].split("\n## [", 1)[0]
    for key in ("Linux", "plat.py", "setcap", "docs/LINUX.md", "一键启动.sh", "build-linux.sh"):
        assert key in section, f"CHANGELOG 1.8.0 段缺少「{key}」"
    # Linux 版不能自称「仅支持 Windows」
    assert "仅支持 Windows" not in section
