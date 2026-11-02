"""Linux / 平台分叉单测：安装编排（installer）、QQ 接入（qq/qqclone）、升级助手、路由。

设计原则：
* **Windows 上也要能跑**：纯逻辑（资产命名、venv 路径、文案、返回结构）两边都断言；
* 真的只能在一个平台上做的（可执行位、bash 脚本语法、zenity/xdg-open 命令）用
  ``pytest.mark.skipif`` 标出来，而不是靠"只在 Linux 跑"糊过去；
* 平台差异的**唯一来源**是 :mod:`slab.plat`，所以断言一律拿 ``plat.*`` 做参照，
  不写死 ``node.exe`` / ``Scripts`` 这类字面量。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import stat
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from slab import installer, paths, plat, qq, qqclone, upgrade
from slab.config import AppConfig
from slab.server import routes_deploy
from slab.server.app import create_app

POSIX_ONLY = pytest.mark.skipif(plat.IS_WINDOWS, reason="POSIX 专有行为（可执行位 / bash / xdg）")
WINDOWS_ONLY = pytest.mark.skipif(not plat.IS_WINDOWS, reason="Windows 专有行为（PowerShell / explorer）")


# --------------------------------------------------------------------------- #
# 测试基建
# --------------------------------------------------------------------------- #
@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """把 paths 的目录常量指向 tmp_path（与 test_installer.py 的 fixture 同构）。"""

    data = tmp_path / "data"
    runtime = tmp_path / "runtime"
    for name, value in (
        ("DATA_DIR", data),
        ("RUNTIME_DIR", runtime),
        ("DOWNLOAD_DIR", data / "downloads"),
        ("LOG_DIR", data / "logs"),
        ("BACKUP_DIR", data / "backups"),
    ):
        monkeypatch.setattr(paths, name, value)
    paths.ensure_dirs()
    monkeypatch.setattr(installer, "_versions_state",
                        {"data": None, "ts": 0.0, "error": "", "fail_ts": 0.0})
    monkeypatch.setattr(installer, "_branches_state",
                        {"data": None, "ts": 0.0, "error": "", "fail_ts": 0.0})
    monkeypatch.setattr(installer, "_node_version_cache", {})
    cfg = AppConfig()
    cfg.astrbot.webui_port = 0  # 0 = 让系统分配，避免撞端口
    cfg.snowluma.webui_port = 0
    cfg.normalise()
    return {"tmp": tmp_path, "data": data, "runtime": runtime, "cfg": cfg}


def _zip_with(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:
        for name, payload in files.items():
            zf.writestr(name, payload)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 1. 资产选择：Linux 只有 tar.gz，Windows 只有 zip
# --------------------------------------------------------------------------- #
def test_asset_extension_follows_target():
    assert installer.snowluma_asset_ext("linux-x64") == ".tar.gz"
    assert installer.snowluma_asset_ext("linux-arm64") == ".tar.gz"
    assert installer.snowluma_asset_ext("win32-x64") == ".zip"


def test_detect_target_matches_platform_layer():
    """本机探测结果必须与 platform_tag() 一致（平台差异只认 plat）。"""

    tag = plat.platform_tag()
    target = installer.detect_snowluma_target()
    if tag == "win-x64":
        assert target == "win32-x64"
    elif tag in ("linux-x64", "linux-arm64"):
        assert target == tag
    else:  # pragma: no cover - 只在奇怪平台
        pytest.skip(f"平台 {tag} 不在支持范围")


def test_linux_assets_are_tar_gz_and_never_zip():
    for machine, suffix in (("x86_64", "linux-x64"), ("aarch64", "linux-arm64")):
        for edition, lite in (("full", ""), ("lite", "-lite")):
            name = installer.snowluma_asset_name(
                "v2.2.1", edition, sys_platform="linux", machine=machine
            )
            assert name == f"SnowLuma-v2.2.1-{suffix}{lite}.tar.gz"
            assert not name.endswith(".zip")


def test_windows_assets_are_zip_and_never_tarball():
    name = installer.snowluma_asset_name("v2.2.1", "full", sys_platform="win32", machine="AMD64")
    assert name == "SnowLuma-v2.2.1-win-x64.zip"
    assert not name.endswith(".tar.gz")


def test_macos_is_rejected_with_actionable_chinese():
    with pytest.raises(installer.DeployError) as exc:
        installer.detect_snowluma_target("darwin", "arm64")
    message = str(exc.value)
    assert "macOS" in message and "下一步" in message


# --------------------------------------------------------------------------- #
# 2. 平台相关的文件名 / 路径一律取自 plat
# --------------------------------------------------------------------------- #
def test_exe_suffix_and_node_name_come_from_platform_layer():
    assert installer.EXE_SUFFIX == plat.exe_suffix()
    assert installer.INSTALL_DIR_EXAMPLE == (r"D:\SnowLuma" if plat.IS_WINDOWS else "/opt/SnowLuma")


def test_astrbot_venv_python_uses_platform_layout(sandbox):
    cfg: AppConfig = sandbox["cfg"]
    expected = plat.venv_python(sandbox["runtime"] / "astrbot")
    assert installer.astrbot_python(cfg) == expected
    # POSIX 是 .venv/bin/python，Windows 是 .venv\Scripts\python.exe
    assert expected.parent.name == ("Scripts" if plat.IS_WINDOWS else "bin")


def test_installed_snowluma_looks_for_platform_node(sandbox):
    home = sandbox["runtime"] / "snowluma"
    home.mkdir(parents=True)
    (home / "index.mjs").write_text("// entry\n", "utf-8")
    (home / "release.json").write_text(json.dumps({"version": "2.2.1"}), "utf-8")

    # 只有本平台的 node 才算 full 版
    assert installer.installed_snowluma(sandbox["cfg"])["has_node"] is False
    (home / plat.node_bin_name()).write_text("MZ", "utf-8")
    status = installer.installed_snowluma(sandbox["cfg"])
    assert status["has_node"] is True and status["edition"] == "full"
    assert status["node"] == str(home / plat.node_bin_name())


# --------------------------------------------------------------------------- #
# 3. 解压：tar.gz / zip + 可执行位兜底
# --------------------------------------------------------------------------- #
def _zip_with_node() -> bytes:
    return _zip_with({
        "index.mjs": b"// entry\n",
        "launcher.sh": b"#!/bin/sh\nexec ./node ./index.mjs\n",
        plat.node_bin_name(): b"MZ-fake-node",
    })


def test_extract_zip_keeps_entry_and_node(tmp_path):
    archive = tmp_path / "sl.zip"
    archive.write_bytes(_zip_with_node())
    root = installer.extract_archive(archive, tmp_path / "out")
    assert (root / "index.mjs").is_file()
    assert (root / plat.node_bin_name()).is_file()


def test_extract_tar_gz_is_accepted(tmp_path):
    import tarfile

    archive = tmp_path / "sl.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        for name, payload in {"index.mjs": b"// entry\n", plat.node_bin_name(): b"MZ"}.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644  # 故意不给可执行位，验证 chmod 兜底
            tf.addfile(info, io.BytesIO(payload))
    assert installer.verify_archive(archive) == "tar"
    root = installer.extract_archive(archive, tmp_path / "out")
    assert (root / plat.node_bin_name()).is_file()


@POSIX_ONLY
def test_extract_marks_binaries_executable_on_posix(tmp_path):
    """离线包（zip）会丢可执行位——解压后必须兜底 chmod +x，否则启动报 Permission denied。"""

    archive = tmp_path / "sl.zip"
    archive.write_bytes(_zip_with_node())
    root = installer.extract_archive(archive, tmp_path / "out")
    node = root / plat.node_bin_name()
    launcher = root / "launcher.sh"
    assert os.access(node, os.X_OK)
    assert os.access(launcher, os.X_OK)


def test_chmod_binaries_is_noop_on_windows(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / plat.node_bin_name()).write_text("MZ", "utf-8")
    if plat.IS_WINDOWS:
        assert installer._chmod_binaries(root) == []
    else:  # pragma: no cover - 平台相关
        assert plat.node_bin_name() in installer._chmod_binaries(root)


# --------------------------------------------------------------------------- #
# 4. AstrBot：Python≥3.12 的引导（uv 优先，其次 python3.12 -m venv）
# --------------------------------------------------------------------------- #
def _progress() -> "installer.DeployProgress":
    return installer.DeployProgress("astrbot", None)


def test_create_venv_prefers_uv_and_installs_python312(sandbox, monkeypatch):
    home = sandbox["runtime"] / "astrbot"
    home.mkdir(parents=True)
    py = plat.venv_python(home)
    calls: list[list[str]] = []

    def fake_run(argv, *, cwd=None, timeout=0, env=None, on_line=None):
        args = [str(a) for a in argv]
        calls.append(args)
        # uv venv 负责把 python 放到位（命令名是绝对路径，按 basename 判）
        if len(args) >= 2 and Path(args[0]).name == "uv" and args[1] == "venv":
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_text("", "utf-8")
        return installer.CmdResult(True, 0, "", args)

    monkeypatch.setattr(installer, "_run", fake_run)
    monkeypatch.setattr(installer, "python_report", lambda: {
        "ok": True, "uv": "/usr/local/bin/uv", "base": "/usr/bin/python3.10", "version": "3.10",
        "min_version": "3.12", "preferred": "3.12", "download_url": "",
    })
    monkeypatch.setattr(installer, "_py_version", lambda exe: (3, 12))

    info = installer._create_venv(home, py, _progress())
    flat = [" ".join([Path(c[0]).name, *c[1:]]) for c in calls]
    # 1) 先让 uv 装 Python 3.12；2) 再用 uv venv --python 3.12 建虚拟环境
    assert f"uv python install {installer.ASTRBOT_PREFERRED_PYTHON}" in flat
    assert any(c[0].endswith("uv") and c[1] == "venv" and "--python" in c for c in calls)
    assert info["python"] == str(py) and info["version"] == "3.12"
    assert info["uv"] == "/usr/local/bin/uv"


def test_create_venv_without_uv_uses_system_python(sandbox, monkeypatch):
    home = sandbox["runtime"] / "astrbot"
    home.mkdir(parents=True)
    py = plat.venv_python(home)
    base = "C:/Python312/python.exe" if plat.IS_WINDOWS else "/usr/bin/python3.12"
    calls: list[list[str]] = []

    def fake_run(argv, *, cwd=None, timeout=0, env=None, on_line=None):
        args = [str(a) for a in argv]
        calls.append(args)
        if "venv" in args:
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_text("", "utf-8")
        return installer.CmdResult(True, 0, "", args)

    monkeypatch.setattr(installer, "_run", fake_run)
    monkeypatch.setattr(installer, "python_report", lambda: {
        "ok": True, "uv": "", "base": base, "version": "3.12",
        "min_version": "3.12", "preferred": "3.12", "download_url": "",
    })
    monkeypatch.setattr(installer, "_py_version", lambda exe: (3, 12))

    installer._create_venv(home, py, _progress())
    assert calls[0] == [base, "-m", "venv", str(home / ".venv")]


def test_python_report_hint_is_actionable_per_platform(monkeypatch):
    monkeypatch.setattr(installer, "_which", lambda name: "")
    monkeypatch.setattr(installer, "_base_python", lambda: "old-python")
    monkeypatch.setattr(installer, "_py_version", lambda exe: (3, 7))
    report = installer.python_report()
    assert report["ok"] is False
    message = str(report["message"])
    assert "下一步" in message
    if plat.IS_WINDOWS:
        assert "python.org" in message
    else:
        assert "apt" in message and "python3.12" in message


def test_base_python_prefers_312_on_posix(monkeypatch):
    """Ubuntu 22.04 的 python3 是 3.10，所以 POSIX 上要先按版本号找解释器。"""

    seen: list[str] = []

    def fake_which(name: str) -> str:
        seen.append(name)
        return f"/usr/bin/{name}"

    monkeypatch.setattr(installer, "_which", fake_which)
    chosen = installer._base_python()
    if plat.IS_WINDOWS:
        # Windows 保持老顺序：先 python，再 python3，不折腾带版本号的名字
        assert chosen == "/usr/bin/python"
        assert seen[0] == "python"
    else:
        assert chosen == "/usr/bin/python3.12"
        assert seen[0] == "python3.12"


def test_astrbot_env_sets_root(sandbox):
    cfg: AppConfig = sandbox["cfg"]
    env = installer.astrbot_env(cfg)
    assert env["ASTRBOT_ROOT"] == str(installer.astrbot_dir(cfg))
    assert env["ASTRBOT_DASHBOARD_PORT"] == str(int(cfg.astrbot.webui_port))


def test_deps_failure_hint_mentions_build_tools_on_linux():
    hint = installer._deps_failure_hint("error: command 'gcc' failed: No such file or directory")
    assert "下一步" not in hint  # 调用方统一加「下一步：」
    if plat.IS_WINDOWS:
        assert "PyPI" in hint and "build-essential" not in hint
    else:
        assert "build-essential" in hint and "apt install" in hint


def test_install_deps_raises_with_executable_hint(sandbox, monkeypatch):
    home = sandbox["runtime"] / "astrbot"
    home.mkdir(parents=True)
    (home / "requirements.txt").write_text("aiohttp\n", "utf-8")
    py = plat.venv_python(home)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("", "utf-8")

    monkeypatch.setattr(installer, "_run", lambda argv, **kw: installer.CmdResult(
        False, 1, "error: command 'gcc' failed", [str(a) for a in argv]))
    with pytest.raises(installer.DeployError) as exc:
        installer._install_astrbot_deps(home, py, _progress())
    message = str(exc.value)
    assert "下一步" in message
    if not plat.IS_WINDOWS:
        assert "build-essential" in message


# --------------------------------------------------------------------------- #
# 5. QQ：Linux 在 /opt/QQ，多开靠独立 HOME
# --------------------------------------------------------------------------- #
def test_qq_names_come_from_platform_layer():
    assert qq.QQ_EXE_NAMES == plat.qq_exe_names()
    assert qq._PROC_EXE_NAMES == plat.qq_process_names()
    if plat.IS_WINDOWS:
        assert "QQ.exe" in qq.QQ_EXE_NAMES
    else:
        assert "qq" in qq.QQ_EXE_NAMES and "/opt/QQ" in [str(p) for p in plat.qq_install_dirs()]


def test_posix_isolated_env_switches_home_and_xdg(tmp_path):
    home = tmp_path / "profile"
    env = qq.posix_isolated_env(home)
    assert env["HOME"] == str(home)
    assert env["XDG_CONFIG_HOME"] == str(home / ".config")
    assert env["XDG_DATA_HOME"] == str(home / ".local" / "share")
    assert env["XDG_CACHE_HOME"] == str(home / ".cache")
    assert env["TMPDIR"] == str(home / "tmp")
    assert env["SLAB_ISOLATED_QQ"] == "1"
    # 目录要真的建出来（QQ 启动前就得存在）
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "TMPDIR"):
        assert Path(env[key]).is_dir()


def test_launch_isolated_without_qq_raises_chinese_hint(monkeypatch):
    monkeypatch.setattr(qq, "list_installs", lambda: [])
    with pytest.raises(FileNotFoundError) as exc:
        qq.launch_isolated("")
    message = str(exc.value)
    if plat.IS_WINDOWS:
        assert "QQ.exe" in message  # Windows 文案保持原样
    else:
        assert "下一步" in message and "/opt/QQ" in message


def test_qq_summary_reports_clone_mode():
    body = qq.summary()
    assert body["ok"] is True
    assert body["clone_mode"] == ("inject" if plat.IS_WINDOWS else "home")
    assert body["platform"] == plat.platform_tag()
    assert isinstance(body["installs"], list) and isinstance(body["processes"], list)


@POSIX_ONLY
def test_spawn_posix_isolated_passes_no_sandbox_and_home(tmp_path, monkeypatch):
    exe = tmp_path / "qq"
    exe.write_text("#!/bin/sh\nexit 0\n", "utf-8")
    exe.chmod(0o755)
    captured: dict = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        captured["session"] = kwargs.get("start_new_session")

        class _P:
            pid = 4242

        return _P()

    monkeypatch.setattr(qq.subprocess, "Popen", fake_popen)
    proc = qq.spawn_posix_isolated(exe, tmp_path / "home")
    assert proc.pid == 4242
    assert captured["args"][0] == str(exe)
    assert "--no-sandbox" in captured["args"]      # Linux QQ 不加会崩
    assert captured["env"]["HOME"] == str(tmp_path / "home")
    assert captured["session"] is True              # 独立会话，控制台退出不连带


def test_linux_install_hint_uses_platform_deb_arch():
    """QQ 安装指引里的 deb 架构 token 要按平台：x64=amd64、arm64=arm64（不是 x86_64）。"""

    expected = "arm64" if plat.platform_tag().endswith("arm64") else "amd64"
    assert qq.DEB_ARCH == expected
    hint = qq.LINUX_INSTALL_HINT
    assert f"QQ_*_{expected}.deb" in hint
    assert "/opt/QQ" in hint
    assert "x86_64" not in hint
    # 与 /api/qq/summary 里给前端的字段保持一致（Windows 上该字段为空，前端用自己那行）
    assert qq.summary()["install_hint"] == ("" if plat.IS_WINDOWS else hint)


# --------------------------------------------------------------------------- #
# 6. qqclone：Windows 注入保留，Linux 换 HOME 分身
# --------------------------------------------------------------------------- #
def test_clone_status_is_platform_aware():
    body = qqclone.status("bot")
    assert body["ok"] is True and body["clone_id"] == "bot"
    assert body["mode"] == ("inject" if plat.IS_WINDOWS else "home")
    assert body["platform"] == plat.platform_tag()
    # 结构字段保持不变（前端与既有测试都依赖）
    for key in ("sandbox_dir", "sandbox_ready", "home", "exists"):
        assert key in body


def test_clone_dirs_isolated_per_clone_id():
    a = qqclone.clone_dirs("bot")["home"]
    b = qqclone.clone_dirs("bot2")["home"]
    assert a != b and a.parent.parent.name == "clones"


@POSIX_ONLY
def test_launch_clone_on_posix_uses_isolated_home(tmp_path, monkeypatch):
    exe = tmp_path / "qq"
    exe.write_text("#!/bin/sh\nexit 0\n", "utf-8")
    exe.chmod(0o755)
    captured: dict = {}

    def fake_spawn(exe_arg, home_arg, extra_args=None):
        captured["exe"] = str(exe_arg)
        captured["home"] = str(home_arg)

        class _P:
            pid = 777

        return _P()

    monkeypatch.setattr(qq, "spawn_posix_isolated", fake_spawn)
    result = qqclone.launch_clone(exe, "bot")
    assert result["ok"] is True and result["pid"] == 777
    assert result["mode"] == "home"
    assert captured["home"] == str(qqclone.clone_dirs("bot")["home"])
    # Linux 上没有注入这一步
    assert result["injected"] == ""


def test_qqclone_import_does_not_require_wintypes():
    """顶层 import 不能因为 ctypes.wintypes 在 Linux 缺失而炸（回归测试）。"""

    code = "import slab.qqclone, slab.qq, slab.upgrade, slab.server.routes_qq; print('ok')"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            cwd=str(Path(__file__).resolve().parents[1]))
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


# --------------------------------------------------------------------------- #
# 7. 路由：目录选择框 / 打开目录 / 文案
# --------------------------------------------------------------------------- #
def test_pick_dir_dispatch_uses_posix_picker_on_linux(monkeypatch):
    """POSIX 分支要真的去调 zenity；Windows 分支走 PowerShell。"""

    calls: dict = {}

    def fake_posix(title, initial):
        calls["posix"] = (title, initial)
        return {"ok": True, "path": "/opt/SnowLuma"}

    def fake_windows(title, initial):
        calls["windows"] = (title, initial)
        return {"ok": True, "path": r"D:\SnowLuma"}

    monkeypatch.setattr(routes_deploy, "_pick_dir_posix", fake_posix)
    monkeypatch.setattr(routes_deploy, "_pick_dir_windows", fake_windows)
    body = routes_deploy._pick_dir_native("选择目录", "/opt")
    assert body["ok"] is True
    if plat.IS_WINDOWS:
        assert "windows" in calls and "posix" not in calls
    else:
        assert "posix" in calls and "windows" not in calls


def test_pick_dir_posix_uses_zenity(monkeypatch):
    monkeypatch.setattr(routes_deploy.shutil, "which",
                        lambda name: "/usr/bin/zenity" if name == "zenity" else "")
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = [str(a) for a in argv]

        class _R:
            returncode = 0
            stdout = "/opt/SnowLuma\n"
            stderr = ""

        return _R()

    monkeypatch.setattr(routes_deploy.subprocess, "run", fake_run)
    body = routes_deploy._pick_dir_posix("选择目录", "/opt")
    assert body == {"ok": True, "path": "/opt/SnowLuma"}
    assert captured["argv"][0].endswith("zenity")
    assert "--directory" in captured["argv"]


def test_pick_dir_posix_without_dialog_raises_unavailable(monkeypatch):
    monkeypatch.setattr(routes_deploy.shutil, "which", lambda name: "")
    with pytest.raises(routes_deploy._PickerUnavailable) as exc:
        routes_deploy._pick_dir_posix("选择目录", "")
    message = str(exc.value)
    assert "手动输入" in message and "zenity" in message


def test_pick_dir_route_maps_unavailable_to_501(monkeypatch):
    def boom(title, initial):
        raise routes_deploy._PickerUnavailable("没有目录选择框。下一步：手动输入路径")

    monkeypatch.setattr(routes_deploy, "_pick_dir_native", boom)
    with TestClient(create_app()) as client:
        response = client.post("/api/deploy/pick-dir", json={})
    assert response.status_code == 501
    assert "手动输入路径" in response.json()["error"]


def test_pick_dir_route_returns_path_when_available(monkeypatch):
    monkeypatch.setattr(routes_deploy, "_pick_dir_native",
                        lambda title, initial: {"ok": True, "path": "/opt/SnowLuma"})
    with TestClient(create_app()) as client:
        body = client.post("/api/deploy/pick-dir", json={}).json()
    assert body["ok"] is True and body["path"] == "/opt/SnowLuma"


def test_qq_summary_route_exposes_platform():
    with TestClient(create_app()) as client:
        body = client.get("/api/qq/summary").json()
    assert body["platform"] == plat.platform_tag()
    assert body["clone_mode"] == ("inject" if plat.IS_WINDOWS else "home")


# --------------------------------------------------------------------------- #
# 8. 升级助手：Windows 用 PowerShell，POSIX 用 bash
# --------------------------------------------------------------------------- #
def test_entry_hints_cover_linux_launcher_names():
    if plat.IS_WINDOWS:
        assert "SnowLuma-Desktop.exe" in upgrade.ENTRY_HINTS
    else:
        assert "snowluma-astrbot-console" in upgrade.ENTRY_HINTS


#: 桌面版产物里的两个入口名（packaging/build-linux.sh 与 desktop/build-linux.sh 的产物）
SHELL_ENTRY = "SnowLuma-Desktop.exe" if plat.IS_WINDOWS else "SnowLuma-Desktop"
BACKEND_ENTRY = "SnowLuma-AstrBot-Console.exe" if plat.IS_WINDOWS else "SnowLuma-AstrBot-Console"


def _desktop_layout(root: Path) -> tuple[Path, Path]:
    """造一个桌面版安装目录：``<root>/<壳>`` + ``<root>/resources/backend/<后端>``。

    真实布局（packaging-shell 的产物）就是这样：Electron 壳在最外层，后端被拷进
    ``resources/backend``，而控制台进程的 ``APP_ROOT`` 指向的是**后端**目录。
    """

    backend = root / "resources" / "backend"
    backend.mkdir(parents=True)
    (backend / BACKEND_ENTRY).write_text("MZ" if plat.IS_WINDOWS else "#!/bin/sh\n", "utf-8")
    shell = root / SHELL_ENTRY
    shell.write_text("MZ" if plat.IS_WINDOWS else "#!/bin/sh\n", "utf-8")
    return backend, shell


def test_upgrade_plan_desktop_restart_prefers_desktop_shell(tmp_path, monkeypatch):
    """桌面版升级后要重启**桌面壳**，不是内嵌后端。

    后端是 Electron 的子进程：重启后端会被 main.js 判定成「后端意外退出」→ 弹窗 +
    app.quit()，用户升级完窗口直接消失（新后端还可能变孤儿）。APP_ROOT 在桌面版里是
    ``<root>/resources/backend``，所以壳在 ``APP_ROOT.parent.parent``。
    """

    from slab import upgrade as upgrade_mod

    backend, shell = _desktop_layout(tmp_path / "SnowLuma-Desktop")
    monkeypatch.setattr(paths, "APP_ROOT", backend)
    monkeypatch.setattr(upgrade_mod, "upgrade_dir", lambda: tmp_path / "upgrade")
    monkeypatch.setenv("SLAB_DESKTOP", "1")

    info = upgrade_mod.plan()
    assert info["desktop"] is True
    assert Path(info["restart"]) == shell


def test_inspect_zip_prefers_shallow_entry(tmp_path):
    """桌面版 zip 里两个 basename 都命中 ENTRY_HINTS 时要取最浅的（壳，不是内嵌后端）。

    zip 的 namelist 顺序 = 写入顺序，不可控，所以必须按深度排序，否则展示/重启目标会飘。
    """

    zip_path = tmp_path / "upgrade.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        # 故意先写内嵌后端，模拟"打包顺序不同"的情况
        zf.writestr(f"SnowLuma-Desktop/resources/backend/{BACKEND_ENTRY}", b"MZ")
        zf.writestr(f"SnowLuma-Desktop/{SHELL_ENTRY}", b"MZ")
        zf.writestr("SnowLuma-Desktop/VERSION", "9.9.9\n")
    info = upgrade.inspect_zip(zip_path)
    assert info["ok"] is True
    assert info["entry"] == SHELL_ENTRY
    assert info["root"] == "SnowLuma-Desktop"


def _add_member(zf: zipfile.ZipFile, name: str, data: bytes, mode: int) -> None:
    """像 packaging/build-linux.sh 那样：把 st_mode 写进 external_attr（create_system=Unix）。"""

    info = zipfile.ZipInfo(name)
    info.date_time = (2026, 1, 1, 0, 0, 0)
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.create_system = 3
    zf.writestr(info, data)


def _add_symlink_member(zf: zipfile.ZipFile, name: str, target: str) -> None:
    info = zipfile.ZipInfo(name)
    info.date_time = (2026, 1, 1, 0, 0, 0)
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    info.create_system = 3
    zf.writestr(info, target.encode())


def test_read_zip_version_prefers_exact_version_file(tmp_path):
    """Electron 自带的浅层小写 ``version``（33.4.11）不能冒充我们的 ``VERSION``。"""

    zip_path = tmp_path / "upgrade.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("SnowLuma-Desktop/version", "33.4.11\n")           # Electron 的（浅 1 层）
        zf.writestr("SnowLuma-Desktop/VERSION", "1.8.0\n")
        zf.writestr(f"SnowLuma-Desktop/{SHELL_ENTRY}", b"MZ")
    info = upgrade.inspect_zip(zip_path)
    # 关键：取的是我们的 VERSION，不是 Electron 的浅层 version
    assert info["version"] == "1.8.0"
    assert info["version"] != "33.4.11"


def test_read_zip_version_falls_back_to_case_insensitive(tmp_path):
    """只有小写 ``version`` 时才退回大小写不敏感（老包/手工包不能直接判"未知"）。"""

    zip_path = tmp_path / "upgrade.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("SnowLuma-Desktop/version", "1.8.0\n")
        zf.writestr(f"SnowLuma-Desktop/{SHELL_ENTRY}", b"MZ")
    assert upgrade.inspect_zip(zip_path)["version"] == "1.8.0"


@POSIX_ONLY
def test_stage_restores_exec_bits_and_symlinks(tmp_path, monkeypatch):
    """解压升级包必须还原 external_attr 里的权限位与软链（extractall 两样都丢）。

    不还原的后果：升级后 ``start.sh`` 变 0644 → 用户按 README 跑启动器直接
    ``Permission denied``；软链变"内容=目标路径"的普通文件 → dlopen 失败。
    """

    from slab import upgrade as upgrade_mod

    monkeypatch.setattr(upgrade_mod, "upgrade_dir", lambda: tmp_path / "upgrade")
    (tmp_path / "upgrade").mkdir(parents=True, exist_ok=True)
    zip_path = tmp_path / "upgrade.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("SnowLuma-AstrBot-Console/VERSION", "1.8.0\n")
        _add_member(zf, "SnowLuma-AstrBot-Console/SnowLuma-AstrBot-Console", b"#!ELF\n", 0o755)
        _add_member(zf, "SnowLuma-AstrBot-Console/start.sh", b"#!/bin/sh\nexec ./SnowLuma-AstrBot-Console\n", 0o755)
        _add_member(zf, "SnowLuma-AstrBot-Console/_internal/libfoo.so", b"\x7fELF", 0o644)
        _add_member(zf, "SnowLuma-AstrBot-Console/_internal/libboost.so", b"\x7fELF", 0o755)
        _add_symlink_member(zf, "SnowLuma-AstrBot-Console/_internal/liblink.so", "libfoo.so")

    staged = upgrade_mod.stage(zip_path)
    assert staged["ok"] is True
    root = Path(staged["staged_root"])
    entry = root / "SnowLuma-AstrBot-Console"
    launcher = root / "start.sh"
    assert os.access(entry, os.X_OK), oct(entry.stat().st_mode)
    assert os.access(launcher, os.X_OK), oct(launcher.stat().st_mode)
    # 普通数据文件不该被误加 +x
    assert not os.access(root / "_internal" / "libfoo.so", os.X_OK)
    # 与 packaging-shell 的验收口径一致：**所有**带 +x 的条目都必须恢复，不能只照顾入口
    assert os.access(root / "_internal" / "libboost.so", os.X_OK)
    link = root / "_internal" / "liblink.so"
    assert link.is_symlink() and os.readlink(link) == "libfoo.so"


def test_stage_survives_symlink_member_on_windows(tmp_path, monkeypatch):
    """Windows 上不还原软链（需要额外权限），但**不能崩**、文件该在的还在。"""

    from slab import upgrade as upgrade_mod

    monkeypatch.setattr(upgrade_mod, "upgrade_dir", lambda: tmp_path / "upgrade")
    (tmp_path / "upgrade").mkdir(parents=True, exist_ok=True)
    zip_path = tmp_path / "upgrade.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("SnowLuma-AstrBot-Console/VERSION", "1.8.0\n")
        _add_member(zf, f"SnowLuma-AstrBot-Console/{BACKEND_ENTRY}", b"MZ", 0o755)
        _add_symlink_member(zf, "SnowLuma-AstrBot-Console/resources/liblink.so", "libfoo.so")
    staged = upgrade_mod.stage(zip_path)
    assert staged["ok"] is True
    assert (Path(staged["staged_root"]) / BACKEND_ENTRY).is_file()


def test_helper_script_sh_has_safety_steps(tmp_path, monkeypatch):
    from slab import upgrade as upgrade_mod

    monkeypatch.setattr(upgrade_mod, "upgrade_dir", lambda: tmp_path / "upgrade")
    (tmp_path / "upgrade").mkdir(parents=True, exist_ok=True)
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "snowluma-astrbot-console").write_text("#!/bin/sh\n", "utf-8")
    script = upgrade_mod.helper_script_sh(
        staged,
        restart=str(tmp_path / "prog" / "snowluma-astrbot-console"),
        pid=1234,
        keep=[str(tmp_path / "prog" / "data")],
        backup_dir=tmp_path / "backup",
    )
    body = script.read_text("utf-8")
    assert body.startswith("#!/usr/bin/env bash")
    assert "WAIT_PID=1234" in body
    assert "kill -0 \"$WAIT_PID\"" in body          # 等旧进程退出
    assert "tar -C \"$SRC\"" in body                 # 解包到新目录
    assert "mv \"$DST\" \"$BAK\"" in body            # 备份（rename）
    assert "mv \"$NEW\" \"$DST\"" in body            # 原子替换
    assert "回滚" in body
    assert "setsid" in body or "nohup" in body      # 重启要脱离会话
    if not plat.IS_WINDOWS:
        assert os.access(script, os.X_OK)


@POSIX_ONLY
def test_helper_script_sh_is_valid_bash(tmp_path, monkeypatch):
    from slab import upgrade as upgrade_mod

    monkeypatch.setattr(upgrade_mod, "upgrade_dir", lambda: tmp_path / "upgrade")
    (tmp_path / "upgrade").mkdir(parents=True, exist_ok=True)
    script = upgrade_mod.helper_script_sh(
        tmp_path / "staged",
        restart=str(tmp_path / "prog" / "snowluma-astrbot-console"),
        pid=4321,
        keep=[str(tmp_path / "prog" / "data"), str(tmp_path / "prog" / "runtime")],
        backup_dir=tmp_path / "backup",
    )
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@WINDOWS_ONLY
def test_windows_helper_script_still_powershell(tmp_path, monkeypatch):
    from slab import upgrade as upgrade_mod

    monkeypatch.setattr(upgrade_mod, "upgrade_dir", lambda: tmp_path / "upgrade")
    (tmp_path / "upgrade").mkdir(parents=True, exist_ok=True)
    script = upgrade_mod.helper_script(
        tmp_path / "staged",
        restart=str(tmp_path / "SnowLuma-Desktop.exe"),
        pid=99,
        keep=[str(tmp_path / "prog" / "data")],
        backup_dir=tmp_path / "backup",
    )
    assert script.suffix == ".cmd"
    assert "powershell" in script.read_text("utf-8").lower()
