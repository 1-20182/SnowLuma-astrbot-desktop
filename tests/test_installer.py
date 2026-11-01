"""部署引擎单测（T1）。

原则：**绝不真的下 40MB、绝不真的 clone AstrBot**
* 下载：用 ``httpx.MockTransport`` 造本地假服务器（含断点续传/重试/大小校验）；
* git / uv / pip：monkeypatch ``installer._run``；
* 目录：全部落在 pytest 的 ``tmp_path``（通过 monkeypatch ``slab.paths`` 的目录常量）。
"""

from __future__ import annotations

import io
import json
import socket
import sys
import tarfile
import zipfile
from pathlib import Path

import httpx
import pytest

from slab import installer, paths
from slab.config import AppConfig
from slab.supervisor import supervisor


# --------------------------------------------------------------------------- #
# 测试基建
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _current_target() -> str:
    try:
        return installer.detect_snowluma_target()
    except installer.DeployError:  # pragma: no cover - 非 win/linux 平台直接跳过
        pytest.skip("当前平台没有 SnowLuma 官方发行包")


def build_snowluma_zip(
    version: str = "2.2.1",
    target: str | None = None,
    with_node: bool = True,
) -> bytes:
    """在内存里造一个「和官方发行包结构一致」的小 zip。"""

    buffer = io.BytesIO()
    files: dict[str, bytes] = {
        "index.mjs": b"// fake snowluma entry\n",
        "launcher.bat": b"node ./index.mjs\n",
        "check-node-version.cjs": b"'use strict';\n",
        "EULA.md": b"# EULA\n",
        "PRIVACY.md": b"# Privacy\n",
        "client/index.html": b"<html>snowluma</html>",
        "native/snowluma.node": b"\x00\x01",
        "package.json": json.dumps({"name": "@snowluma/runtime", "version": version}).encode(),
        "release.json": json.dumps(
            {"version": version, "target": target or _current_target(), "entries": []}
        ).encode(),
    }
    if with_node:
        files["node.exe"] = b"MZ-fake-node-binary"
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def build_astrbot_zip(prefix: str = "AstrBot-master") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{prefix}/main.py", "print('astrbot')\n")
        zf.writestr(f"{prefix}/requirements.txt", "aiohttp>=3.11\n")
        zf.writestr(f"{prefix}/pyproject.toml", 'version = "4.29.3"\n')
    return buffer.getvalue()


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """把 paths 的目录常量指向 tmp_path，并清空版本缓存。"""

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
    cfg.astrbot.webui_port = _free_port()
    cfg.snowluma.webui_port = _free_port()
    cfg.normalise()  # 生成 link token / 两个 WebUI 初始密码，便于断言 env
    return {"tmp": tmp_path, "data": data, "runtime": runtime, "cfg": cfg}


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# 资产选择矩阵
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "platform,machine,edition,expected",
    [
        ("win32", "x86_64", "full", "SnowLuma-v2.2.1-win-x64.zip"),
        ("win32", "AMD64", "full", "SnowLuma-v2.2.1-win-x64.zip"),
        ("win32", "x86_64", "lite", "SnowLuma-v2.2.1-win-x64-lite.zip"),
        ("linux", "x86_64", "full", "SnowLuma-v2.2.1-linux-x64.tar.gz"),
        ("linux", "x86_64", "lite", "SnowLuma-v2.2.1-linux-x64-lite.tar.gz"),
        ("linux", "aarch64", "full", "SnowLuma-v2.2.1-linux-arm64.tar.gz"),
        ("linux", "arm64", "lite", "SnowLuma-v2.2.1-linux-arm64-lite.tar.gz"),
    ],
)
def test_asset_matrix(platform, machine, edition, expected):
    assert installer.snowluma_asset_name(
        "v2.2.1", edition, sys_platform=platform, machine=machine
    ) == expected
    # 不带 v 的版本号也能工作
    assert installer.snowluma_asset_name(
        "2.2.1", edition, sys_platform=platform, machine=machine
    ) == expected


def test_asset_macos_rejected_with_chinese_hint():
    with pytest.raises(installer.DeployError) as exc:
        installer.snowluma_asset_name("v2.2.1", "full", sys_platform="darwin", machine="arm64")
    message = str(exc.value)
    assert "macOS" in message and "下一步" in message


def test_asset_windows_arm64_rejected():
    with pytest.raises(installer.DeployError) as exc:
        installer.snowluma_asset_name("v2.2.1", "full", sys_platform="win32", machine="ARM64")
    assert "ARM64" in str(exc.value)


def test_asset_requires_version():
    with pytest.raises(installer.DeployError):
        installer.snowluma_asset_name("latest", "full", sys_platform="win32", machine="x86_64")


def test_asset_url_without_api():
    url = installer.snowluma_asset_url("v2.2.1", "SnowLuma-v2.2.1-win-x64.zip")
    assert url == (
        "https://github.com/SnowLuma/SnowLuma/releases/download/"
        "v2.2.1/SnowLuma-v2.2.1-win-x64.zip"
    )
    assert installer.check_download_url(url) == url


def test_pick_asset_is_case_insensitive():
    assets = [{"name": "SnowLuma-v2.2.1-win-x64.zip", "url": "u", "size": 1}]
    assert installer.pick_asset(assets, "snowluma-v2.2.1-win-x64.zip") is assets[0]
    assert installer.pick_asset(assets, "nope.zip") is None


# --------------------------------------------------------------------------- #
# 版本比较 / Node 版本判定
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("v2.2.1", (2, 2, 1, "")),
        ("2.2.1", (2, 2, 1, "")),
        ("2.2", (2, 2, 0, "")),
        ("v2.2.1-beta.1", (2, 2, 1, "beta.1")),
        ("", None),
        ("abc", None),
    ],
)
def test_parse_version(raw, expected):
    assert installer.parse_version(raw) == expected


def test_compare_versions():
    assert installer.compare_versions("v2.2.1", "v2.2.0") == 1
    assert installer.compare_versions("v2.2.0", "v2.2.1") == -1
    assert installer.compare_versions("v2.2.1", "2.2.1") == 0
    assert installer.compare_versions("v2.10.0", "v2.9.9") == 1
    assert installer.compare_versions("v2.2.1-beta.1", "v2.2.1") == -1
    assert installer.same_version("2.2.1", "v2.2.1") is True
    assert installer.same_version("2.2.1", "v2.2.2") is False
    assert installer.tag_for("2.2.1") == "v2.2.1"
    assert installer.tag_for("v2.2.1") == "v2.2.1"
    assert installer.tag_for("latest") == ""


@pytest.mark.parametrize(
    "version,supported",
    [
        ("24.13.0", False),  # 本机版本：lite 不可用
        ("v24.13.0", False),
        ("24.22.0", False),
        ("24.22.1", True),
        ("24.30.0", True),
        ("26.11.9", False),
        ("26.12.1", True),
        ("27.0.0", True),
        ("22.5.0", False),
        ("", False),
        ("garbage", False),
    ],
)
def test_node_version_supported(version, supported):
    assert installer.node_version_supported(version) is supported


# --------------------------------------------------------------------------- #
# 下载：白名单 / 镜像 / 续传 / 重试 / 校验
# --------------------------------------------------------------------------- #
def test_download_url_whitelist():
    good = [
        "https://github.com/SnowLuma/SnowLuma/releases/download/v2.2.1/a.zip",
        "https://objects.githubusercontent.com/x/y.zip",
        "https://codeload.github.com/AstrBotDevs/AstrBot/zip/refs/heads/master",
        "https://api.github.com/repos/SnowLuma/SnowLuma/releases/latest",
    ]
    for url in good:
        assert installer.check_download_url(url) == url
    bad = [
        "http://github.com/x.zip",
        "https://evil.example.com/x.zip",
        "https://github.com.evil.example.com/x.zip",
        "ftp://github.com/x.zip",
        "",
    ]
    for url in bad:
        with pytest.raises(installer.DeployError):
            installer.check_download_url(url)


def test_mirror_prefix_applied():
    url = "https://github.com/a/b.zip"
    assert installer.with_mirror(url, "https://ghfast.top/") == f"https://ghfast.top/{url}"
    assert installer.with_mirror(url, "") == url
    with pytest.raises(installer.DeployError):
        installer.with_mirror(url, "http://mirror.example")


def test_download_streams_reports_progress_and_cleans_part(tmp_path):
    payload = b"x" * 4000
    progress: list[tuple[int, int]] = []
    out = tmp_path / "pkg.zip"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"].startswith("slab-console/")
        return httpx.Response(200, content=payload, headers={"content-length": str(len(payload))})

    installer.http_download("https://github.com/x/y.zip", out,
                            on_progress=lambda done, total: progress.append((done, total)),
                            client=_mock_client(handler))
    assert out.read_bytes() == payload
    assert not (tmp_path / "pkg.zip.part").exists()
    assert progress and progress[-1][0] == len(payload)
    assert progress[-1][1] == len(payload)


def test_download_resumes_with_range_header(tmp_path):
    out = tmp_path / "pkg.zip"
    (tmp_path / "pkg.zip.part").write_bytes(b"0123456789")
    requested: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requested["range"] = request.headers.get("range", "")
        return httpx.Response(
            206,
            content=b"abcdefghij",
            headers={"content-range": "bytes 10-19/20", "content-length": "10"},
        )

    installer.http_download("https://github.com/x/y.zip", out, expect_size=20,
                            client=_mock_client(handler))
    assert requested["range"] == "bytes=10-"
    assert out.read_bytes() == b"0123456789abcdefghij"


def test_download_retries_after_server_error(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.time, "sleep", lambda *_: None)
    calls = {"n": 0}
    out = tmp_path / "pkg.bin"

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, content=b"ok")

    installer.http_download("https://github.com/x/y.bin", out, client=_mock_client(handler))
    assert calls["n"] == 2
    assert out.read_bytes() == b"ok"


def test_download_404_is_not_retried(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, content=b"nope")

    with pytest.raises(installer.DeployError) as exc:
        installer.http_download("https://github.com/x/y.zip", tmp_path / "y.zip",
                                client=_mock_client(handler))
    assert calls["n"] == 1
    assert "404" in str(exc.value) and "下一步" in str(exc.value)


def test_download_size_mismatch_raises(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"0123")

    with pytest.raises(installer.DeployError) as exc:
        installer.http_download("https://github.com/x/y.zip", tmp_path / "y.zip",
                                expect_size=100, client=_mock_client(handler))
    assert "大小不符" in str(exc.value)
    assert not (tmp_path / "y.zip").exists()  # 不能把坏文件当成完成品


# --------------------------------------------------------------------------- #
# 网络错误分类（证书 vs 超时 vs 其它）
# --------------------------------------------------------------------------- #
def test_net_error_hint_distinguishes_certificate_problem():
    import ssl

    try:
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed: unable to get local issuer")
        except ssl.SSLError as inner:
            raise httpx.ConnectError("connection failed") from inner
    except httpx.ConnectError as wrapped:
        assert installer.is_ssl_error(wrapped) is True
        hint = installer.net_error_hint(wrapped, "下载 SnowLuma 发行包")
        assert "证书" in hint and "系统证书库" in hint
        assert "离线安装" in hint or "镜像" in hint


def test_net_error_hint_distinguishes_timeout():
    try:
        raise httpx.ConnectTimeout("timed out")
    except httpx.ConnectTimeout as exc:
        hint = installer.net_error_hint(exc, "下载")
        assert "超时" in hint
        assert "断点续传" in hint or "镜像" in hint


def test_net_error_hint_generic_connection_error():
    exc = httpx.ConnectError("getaddrinfo failed")
    hint = installer.net_error_hint(exc, "获取版本信息")
    assert "无法连接" in hint and "下一步" in hint


def test_download_failure_uses_classified_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.time, "sleep", lambda *_: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("certificate verify failed")

    with pytest.raises(installer.DeployError) as exc:
        installer.http_download("https://github.com/x/y.zip", tmp_path / "y.zip",
                                client=_mock_client(handler), retries=2)
    message = str(exc.value)
    assert "证书" in message
    assert "已重试 2 次" in message


def test_check_github_reports_reason(monkeypatch):
    class _Boom:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def head(self, *a, **k):
            raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(installer.httpx, "Client", _Boom)
    probe = installer.check_github()
    assert probe["ok"] is False
    assert "超时" in probe["reason"]
    assert installer.github_reachable() is False


# --------------------------------------------------------------------------- #
# 压缩包：校验 / 解压 / 路径穿越
# --------------------------------------------------------------------------- #
def test_verify_and_extract_zip(tmp_path):
    archive = tmp_path / "sl.zip"
    archive.write_bytes(build_snowluma_zip())
    assert installer.verify_archive(archive) == "zip"
    root = installer.extract_archive(archive, tmp_path / "out")
    assert (root / "index.mjs").is_file()
    assert (root / "client" / "index.html").is_file()
    assert (root / "node.exe").is_file()


def test_extract_tar_gz(tmp_path):
    archive = tmp_path / "sl.tar.gz"
    payload = build_snowluma_zip()
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        with tarfile.open(archive, "w:gz") as tf:
            for info in zf.infolist():
                data = zf.read(info.filename)
                ti = tarfile.TarInfo(info.filename)
                ti.size = len(data)
                ti.mode = 0o644
                tf.addfile(ti, io.BytesIO(data))
    assert installer.verify_archive(archive) == "tar"
    root = installer.extract_archive(archive, tmp_path / "out")
    assert (root / "index.mjs").is_file()


def test_extract_rejects_zip_slip(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../evil.txt", "pwned")
    with pytest.raises(installer.DeployError) as exc:
        installer.extract_archive(archive, tmp_path / "out")
    assert "路径穿越" in str(exc.value)
    assert not (tmp_path / "evil.txt").exists()


def test_extract_rejects_absolute_member(tmp_path):
    archive = tmp_path / "evil2.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("C:/Windows/System32/evil.dll", "pwned")
    with pytest.raises(installer.DeployError) as exc:
        installer.extract_archive(archive, tmp_path / "out")
    assert "绝对路径" in str(exc.value)


def test_verify_archive_detects_corruption(tmp_path):
    payload = b"PAYLOAD-" * 200
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("index.mjs", payload)
    raw = bytearray(archive.read_bytes())
    offset = raw.find(b"PAYLOAD-")
    raw[offset] = ord("X")
    archive.write_bytes(bytes(raw))
    with pytest.raises(installer.DeployError) as exc:
        installer.verify_archive(archive)
    assert "校验失败" in str(exc.value) or "损坏" in str(exc.value)


def test_verify_archive_rejects_garbage(tmp_path):
    archive = tmp_path / "not-a-package.zip"
    archive.write_bytes(b"this is not a zip")
    with pytest.raises(installer.DeployError) as exc:
        installer.verify_archive(archive)
    assert "无法识别压缩包格式" in str(exc.value)


def test_archive_release_info_reads_metadata(tmp_path):
    archive = tmp_path / "sl.zip"
    archive.write_bytes(build_snowluma_zip(version="2.2.1"))
    info = installer.archive_release_info(archive)
    assert info["version"] == "2.2.1"
    assert info["target"] == _current_target()


# --------------------------------------------------------------------------- #
# runtime.json
# --------------------------------------------------------------------------- #
def test_write_runtime_config_merges_and_overrides_port(tmp_path):
    install = tmp_path / "sl"
    (install / "config").mkdir(parents=True)
    (install / "config" / "runtime.json").write_text(
        json.dumps({"webuiPort": 5199, "logRetainDays": 30, "customKey": 1}), "utf-8"
    )
    path = installer.write_runtime_config(install, 5099)
    data = json.loads(path.read_text("utf-8"))
    assert data["webuiPort"] == 5099
    assert data["webuiHost"] == "127.0.0.1"
    assert data["logRetainDays"] == 30  # 用户设置保留
    assert data["customKey"] == 1
    assert data["hookAutoLoad"] is False  # 官方默认
    assert data["webuiTls"] == {"enabled": False}


def test_write_runtime_config_rejects_bad_port(tmp_path):
    with pytest.raises(installer.DeployError):
        installer.write_runtime_config(tmp_path / "sl", 70000)


@pytest.mark.parametrize(
    "line,expected",
    [
        ("17:51:30 INFO    [WebUI] listening http://127.0.0.1:5199", 5199),
        ("[WebUI] listening http://0.0.0.0:5099", 5099),
        ("WARN  [WebUI] port 5299 is in use, using 5300 instead", None),
        ("[OneBot] session started: UIN=3985934176", None),
    ],
)
def test_parse_listening_port(line, expected):
    assert installer.parse_listening_port(line) == expected


def test_detect_snowluma_port_from_logs(sandbox, monkeypatch):
    from slab.events import bus

    home = sandbox["runtime"] / "snowluma"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "snowluma-2026-10-29.log").write_text(
        "17:49:08 INFO  [WebUI] listening http://127.0.0.1:5199\n", "utf-8"
    )
    assert installer.detect_snowluma_port(sandbox["cfg"]) == 5199


# --------------------------------------------------------------------------- #
# 部署 SnowLuma
# --------------------------------------------------------------------------- #
def test_deploy_snowluma_offline_is_idempotent(sandbox):
    cfg: AppConfig = sandbox["cfg"]
    cfg.snowluma.version = "2.2.1"
    archive = sandbox["tmp"] / "SnowLuma-v2.2.1-win-x64.zip"
    archive.write_bytes(build_snowluma_zip())
    events: list[dict] = []

    first = installer.deploy_snowluma(cfg, progress=events.append, offline_zip=str(archive))
    home = sandbox["runtime"] / "snowluma"
    assert first["installed"] is True and first["skipped"] is False
    assert first["version"] == "2.2.1" and first["edition"] == "full"
    assert first["dir"] == str(home)
    assert (home / "index.mjs").is_file() and (home / "node.exe").is_file()
    runtime_json = json.loads((home / "config" / "runtime.json").read_text("utf-8"))
    assert runtime_json["webuiPort"] == cfg.snowluma.webui_port
    assert runtime_json["webuiHost"] == "127.0.0.1"
    # 进度事件单调不减，最后一步是 done/1.0
    fractions = [e["progress"] for e in events]
    assert fractions == sorted(fractions), fractions
    assert events[-1]["step"] == "done" and events[-1]["progress"] == 1.0
    assert all(e["target"] == "snowluma" for e in events)

    second = installer.deploy_snowluma(cfg, offline_zip=str(archive))
    assert second["skipped"] is True
    assert second["version"] == "2.2.1"


def test_deploy_snowluma_online_uses_release_assets(sandbox, monkeypatch):
    cfg: AppConfig = sandbox["cfg"]
    cfg.snowluma.version = "latest"
    archive_bytes = build_snowluma_zip()
    asset_name = installer.snowluma_asset_name("v2.2.1", "full")
    index = {
        "tags": ["v2.2.1"],
        "latest": "v2.2.1",
        "releases": {
            "v2.2.1": {
                "tag": "v2.2.1",
                "prerelease": False,
                "published_at": "2026-10-01T00:00:00Z",
                "assets": [
                    {
                        "name": asset_name,
                        "url": f"https://github.com/SnowLuma/SnowLuma/releases/download/v2.2.1/{asset_name}",
                        "size": len(archive_bytes),
                    }
                ],
            }
        },
        "fetched_at": 1.0,
        "stale": False,
        "error": "",
    }
    monkeypatch.setattr(installer, "_release_index", lambda force=False: index)
    captured: dict = {}

    def fake_download(url, dest, *, on_progress=None, mirror="", expect_size=None, **kwargs):
        captured.update(url=url, dest=Path(dest), expect_size=expect_size, mirror=mirror)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(archive_bytes)
        if on_progress:
            on_progress(len(archive_bytes) // 2, len(archive_bytes))
            on_progress(len(archive_bytes), len(archive_bytes))
        return Path(dest)

    monkeypatch.setattr(installer, "http_download", fake_download)
    result = installer.deploy_snowluma(cfg, mirror="https://ghfast.top")
    assert captured["url"].endswith(asset_name)
    assert captured["expect_size"] == len(archive_bytes)
    assert captured["mirror"] == "https://ghfast.top"
    assert result["installed"] is True and result["source"] == "github"


def test_deploy_snowluma_latest_degrades_when_github_unreachable(sandbox, monkeypatch):
    cfg: AppConfig = sandbox["cfg"]
    cfg.snowluma.version = "2.2.1"
    archive = sandbox["tmp"] / "SnowLuma-v2.2.1-win-x64.zip"
    archive.write_bytes(build_snowluma_zip())
    installer.deploy_snowluma(cfg, offline_zip=str(archive))

    cfg.snowluma.version = "latest"

    def boom(*args, **kwargs):
        raise installer.DeployError("无法访问 GitHub（网络受限）。下一步：填写镜像前缀或用离线安装。")

    monkeypatch.setattr(installer, "resolve_release", boom)
    result = installer.deploy_snowluma(cfg)
    assert result["skipped"] is True and result["installed"] is True
    assert "GitHub" in result["warning"]


def test_deploy_snowluma_without_install_and_no_network_raises(sandbox, monkeypatch):
    cfg: AppConfig = sandbox["cfg"]
    cfg.snowluma.version = "latest"

    def boom(*args, **kwargs):
        raise installer.DeployError("无法获取 SnowLuma 最新版本号（GitHub 不可达）。")

    monkeypatch.setattr(installer, "resolve_release", boom)
    with pytest.raises(installer.DeployError) as exc:
        installer.deploy_snowluma(cfg)
    assert "GitHub" in str(exc.value)


def test_deploy_snowluma_offline_zip_from_env(sandbox, monkeypatch):
    """无界面部署（python -m slab install snowluma）可用环境变量指定离线包。"""

    cfg: AppConfig = sandbox["cfg"]
    cfg.snowluma.version = "2.2.1"
    archive = sandbox["tmp"] / "SnowLuma-v2.2.1-win-x64.zip"
    archive.write_bytes(build_snowluma_zip())
    monkeypatch.setenv("SLAB_SNOWLUMA_OFFLINE_ZIP", str(archive))
    result = installer.deploy_snowluma(cfg)
    assert result["installed"] is True
    assert result["source"] == "offline"


def test_deploy_snowluma_rejects_corrupt_offline_zip(sandbox):
    cfg: AppConfig = sandbox["cfg"]
    cfg.snowluma.version = "2.2.1"
    bad = sandbox["tmp"] / "broken.zip"
    bad.write_bytes(b"definitely not a zip")
    with pytest.raises(installer.DeployError) as exc:
        installer.deploy_snowluma(cfg, offline_zip=str(bad))
    assert "压缩包" in str(exc.value)


def test_deploy_snowluma_rejects_wrong_platform_offline_zip(sandbox):
    cfg: AppConfig = sandbox["cfg"]
    cfg.snowluma.version = "2.2.1"
    other = "linux-x64" if _current_target().startswith("win32") else "win32-x64"
    archive = sandbox["tmp"] / "wrong.zip"
    archive.write_bytes(build_snowluma_zip(target=other))
    with pytest.raises(installer.DeployError) as exc:
        installer.deploy_snowluma(cfg, offline_zip=str(archive))
    assert "不适用" in str(exc.value)


def test_deploy_snowluma_preserves_old_config_on_upgrade(sandbox):
    cfg: AppConfig = sandbox["cfg"]
    cfg.snowluma.version = "2.2.1"
    archive = sandbox["tmp"] / "SnowLuma-v2.2.1-win-x64.zip"
    archive.write_bytes(build_snowluma_zip())
    installer.deploy_snowluma(cfg, offline_zip=str(archive))
    home = sandbox["runtime"] / "snowluma"
    (home / "config" / "webui.json").write_text('{"passwordHash":"keep-me"}', "utf-8")
    (home / "config" / "onebot_123456.json").write_text('{"wsClients":[]}', "utf-8")

    newer = sandbox["tmp"] / "SnowLuma-v2.2.2-win-x64.zip"
    newer.write_bytes(build_snowluma_zip(version="2.2.2"))
    result = installer.deploy_snowluma(cfg, offline_zip=str(newer), force=True)
    assert result["version"] == "2.2.2"
    assert (home / "config" / "webui.json").read_text("utf-8") == '{"passwordHash":"keep-me"}'
    assert (home / "config" / "onebot_123456.json").is_file()
    # 旧目录已备份
    assert result["backup"] and Path(result["backup"]).is_dir()


def test_install_dir_traversal_rejected(sandbox):
    cfg: AppConfig = sandbox["cfg"]
    archive = sandbox["tmp"] / "sl.zip"
    archive.write_bytes(build_snowluma_zip())
    for bad in ("../evil", "..\\evil", "runtime/../../evil"):
        cfg.snowluma.install_dir = bad
        with pytest.raises(installer.DeployError) as exc:
            installer.deploy_snowluma(cfg, offline_zip=str(archive))
        assert ".." in str(exc.value)


def test_install_dir_relative_rejected(sandbox):
    cfg: AppConfig = sandbox["cfg"]
    cfg.snowluma.install_dir = "my-snowluma"
    with pytest.raises(installer.DeployError) as exc:
        installer.snowluma_dir(cfg)
    assert "绝对路径" in str(exc.value)


def test_install_dir_system_folder_rejected(sandbox):
    cfg: AppConfig = sandbox["cfg"]
    cfg.astrbot.install_dir = "C:/Windows/System32/astrbot"
    with pytest.raises(installer.DeployError) as exc:
        installer.astrbot_dir(cfg)
    assert "系统目录" in str(exc.value)


def test_ensure_within_blocks_escape(sandbox):
    base = sandbox["runtime"]
    installer.ensure_within(base, base / "snowluma" / "config")  # ok
    with pytest.raises(installer.DeployError):
        installer.ensure_within(base, sandbox["tmp"] / "outside")


def test_install_status_and_uninstall(sandbox):
    cfg: AppConfig = sandbox["cfg"]
    cfg.snowluma.version = "2.2.1"
    archive = sandbox["tmp"] / "SnowLuma-v2.2.1-win-x64.zip"
    archive.write_bytes(build_snowluma_zip())
    installer.deploy_snowluma(cfg, offline_zip=str(archive))

    status = installer.install_status(cfg)
    assert status["snowluma"]["installed"] is True
    assert status["snowluma"]["version"] == "2.2.1"
    assert status["astrbot"]["installed"] is False

    removed = installer.uninstall("snowluma", cfg)
    assert removed["removed"] is True
    assert not (sandbox["runtime"] / "snowluma").exists()
    assert Path(removed["backup"]).is_dir()
    assert "备份" in removed["message"]

    again = installer.uninstall("snowluma", cfg)
    assert again["removed"] is False
    with pytest.raises(installer.DeployError):
        installer.uninstall("both", cfg)


# --------------------------------------------------------------------------- #
# 部署 AstrBot（全部用假的 _run / 假的下载器）
# --------------------------------------------------------------------------- #
def _fake_runner(calls: list[list[str]], *, create_venv: bool = True, fail: str = ""):
    def fake_run(argv, *, cwd=None, timeout=0, env=None, on_line=None):
        args = [str(a) for a in argv]
        calls.append(args)
        joined = " ".join(args)
        if fail and fail in joined:
            return installer.CmdResult(False, 1, f"模拟失败: {joined}", args)
        if "clone" in args:
            dest = Path(args[-1])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "main.py").write_text("print('astrbot')\n", "utf-8")
            (dest / "requirements.txt").write_text("aiohttp>=3.11\n", "utf-8")
            (dest / "pyproject.toml").write_text('version = "4.29.3"\n', "utf-8")
            (dest / ".git").mkdir(exist_ok=True)
        if "venv" in args and create_venv and args[0] != "uv":
            venv = Path(args[-1])
            target = venv / ("Scripts" if sys.platform == "win32" else "bin") / (
                "python.exe" if sys.platform == "win32" else "python"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", "utf-8")
        if "rev-parse" in args:
            return installer.CmdResult(True, 0, "abc1234", args)
        return installer.CmdResult(True, 0, "", args)

    return fake_run


def test_deploy_astrbot_git_path(sandbox, monkeypatch):
    cfg: AppConfig = sandbox["cfg"]
    cfg.astrbot.version = "main"  # 历史默认值，应映射到 master
    calls: list[list[str]] = []
    monkeypatch.setattr(installer, "_run", _fake_runner(calls))
    monkeypatch.setattr(installer, "_which", lambda name: "C:/fake/git.exe" if name == "git" else "")
    monkeypatch.setattr(
        installer,
        "python_report",
        lambda: {"ok": True, "uv": "", "base": "C:/Python312/python.exe", "version": "3.12",
                 "min_version": "3.10", "preferred": "3.12", "download_url": ""},
    )
    monkeypatch.setattr(installer, "_py_version", lambda exe: (3, 12))

    result = installer.deploy_astrbot(cfg)
    home = sandbox["runtime"] / "astrbot"
    assert result["installed"] is True and result["source"] == "git"
    assert result["commit"] == "abc1234"
    assert result["version"] == "4.29.3"
    assert (home / "main.py").is_file()
    assert calls[0][:4] == ["C:/fake/git.exe", "clone", "--depth", "1"]
    assert calls[0][4:6] == ["--branch", "master"]
    assert any("venv" in c for c in calls)
    assert any("pip" in c and "install" in c for c in calls)


def test_deploy_astrbot_updates_existing_clone(sandbox, monkeypatch):
    cfg: AppConfig = sandbox["cfg"]
    home = sandbox["runtime"] / "astrbot"
    home.mkdir(parents=True)
    (home / ".git").mkdir()
    (home / "main.py").write_text("print(1)\n", "utf-8")
    (home / "requirements.txt").write_text("aiohttp\n", "utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(installer, "_run", _fake_runner(calls))
    monkeypatch.setattr(installer, "_which", lambda name: "C:/fake/git.exe" if name == "git" else "")
    monkeypatch.setattr(
        installer, "python_report",
        lambda: {"ok": True, "uv": "", "base": "python", "version": "3.12",
                 "min_version": "3.10", "preferred": "3.12", "download_url": ""},
    )
    monkeypatch.setattr(installer, "_py_version", lambda exe: (3, 12))
    # 已有 venv，直接复用
    py = installer.astrbot_python(cfg)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("", "utf-8")

    installer.deploy_astrbot(cfg)
    flat = [" ".join(c) for c in calls]
    assert any(" fetch " in f" {f} " and "--depth" in f for f in flat)
    assert any(" reset --hard FETCH_HEAD" in f for f in flat)
    assert not any(" clone " in f" {f} " for f in flat)


def test_deploy_astrbot_without_git_uses_zipball(sandbox, monkeypatch):
    cfg: AppConfig = sandbox["cfg"]
    zip_bytes = build_astrbot_zip()
    captured: dict = {}

    def fake_download(url, dest, *, on_progress=None, mirror="", expect_size=None, **kwargs):
        captured["url"] = url
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(zip_bytes)
        return Path(dest)

    calls: list[list[str]] = []
    monkeypatch.setattr(installer, "http_download", fake_download)
    monkeypatch.setattr(installer, "_run", _fake_runner(calls))
    monkeypatch.setattr(installer, "_which", lambda name: "")  # 既没有 git 也没有 uv
    monkeypatch.setattr(
        installer, "python_report",
        lambda: {"ok": True, "uv": "", "base": "python", "version": "3.12",
                 "min_version": "3.10", "preferred": "3.12", "download_url": ""},
    )
    monkeypatch.setattr(installer, "_py_version", lambda exe: (3, 12))

    result = installer.deploy_astrbot(cfg)
    assert captured["url"] == (
        "https://codeload.github.com/AstrBotDevs/AstrBot/zip/refs/heads/master"
    )
    assert result["source"] == "zip"
    home = sandbox["runtime"] / "astrbot"
    assert (home / "main.py").is_file()
    assert any("venv" in c for c in calls)


def test_deploy_astrbot_zipball_url_for_tag():
    assert installer._astrbot_zipball_url("master") == (
        "https://codeload.github.com/AstrBotDevs/AstrBot/zip/refs/heads/master"
    )
    assert installer._astrbot_zipball_url("v4.29.3") == (
        "https://codeload.github.com/AstrBotDevs/AstrBot/zip/refs/tags/v4.29.3"
    )
    assert installer.resolve_astrbot_ref("main") == "master"
    assert installer.resolve_astrbot_ref("dev") == "dev"


def test_deploy_astrbot_blocks_when_port_busy(sandbox):
    cfg: AppConfig = sandbox["cfg"]
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        cfg.astrbot.webui_port = int(srv.getsockname()[1])
        with pytest.raises(installer.DeployError) as exc:
            installer.deploy_astrbot(cfg)
    assert "已被占用" in str(exc.value) and "下一步" in str(exc.value)


def test_python_report_flags_missing_python(sandbox, monkeypatch):
    monkeypatch.setattr(installer, "_which", lambda name: "")
    monkeypatch.setattr(installer, "_base_python", lambda: "C:/Python27/python.exe")
    monkeypatch.setattr(installer, "_py_version", lambda exe: (2, 7))
    report = installer.python_report()
    assert report["ok"] is False
    assert "python.org" in report["message"]


def test_deploy_both_reports_partial_failure(sandbox, monkeypatch):
    cfg: AppConfig = sandbox["cfg"]

    def boom(cfg=None, progress=None, **kwargs):
        raise installer.DeployError("磁盘空间不足")

    monkeypatch.setattr(installer, "deploy_snowluma", boom)
    monkeypatch.setattr(installer, "deploy_astrbot",
                        lambda cfg=None, progress=None, **kwargs: {"installed": True, "dir": "x"})
    result = installer.deploy_both(cfg)
    assert result["ok"] is False
    assert "磁盘空间不足" in result["errors"]["snowluma"]
    assert result["results"]["astrbot"]["installed"] is True


def test_deploy_snowluma_refuses_when_running(sandbox, monkeypatch):
    from slab.supervisor import ServiceState

    cfg: AppConfig = sandbox["cfg"]
    archive = sandbox["tmp"] / "sl.zip"
    archive.write_bytes(build_snowluma_zip())
    monkeypatch.setattr(
        installer.supervisor, "state",
        lambda name: ServiceState(name=name, status="running", pid=1234),
    )
    with pytest.raises(installer.DeployError) as exc:
        installer.deploy_snowluma(cfg, offline_zip=str(archive))
    assert "正在运行" in str(exc.value)


# --------------------------------------------------------------------------- #
# ServiceDef：argv / cwd / env / 就绪条件
# --------------------------------------------------------------------------- #
def _fake_snowluma_install(sandbox, with_node: bool = True) -> Path:
    home = sandbox["runtime"] / "snowluma"
    home.mkdir(parents=True, exist_ok=True)
    (home / "index.mjs").write_text("// entry\n", "utf-8")
    if with_node:
        (home / "node.exe").write_text("MZ", "utf-8")
    return home


def test_snowluma_service_def_full(sandbox):
    home = _fake_snowluma_install(sandbox)
    cfg: AppConfig = sandbox["cfg"]
    cfg.snowluma.edition = "full"
    definition = installer.snowluma_service_def()
    assert definition.name == "snowluma"
    assert definition.argv(cfg) == [str(home / "node.exe"), "index.mjs"]
    assert definition.cwd(cfg) == home
    assert "listening" in definition.ready_regex
    # 端口可能被自动换掉，所以不绑定 ready_port
    assert definition.ready_port is None
    env = definition.env(cfg)
    assert env["SNOWLUMA_WEBUI_PORT"] == str(cfg.snowluma.webui_port)
    assert env["SNOWLUMA_WEBUI_HOST"] == "127.0.0.1"
    assert env["SNOWLUMA_ACCEPT_EULA"] == "1"
    assert env["SNOWLUMA_ACCEPT_PRIVACY"] == "1"
    # 默认 manual：不自动注入，只注入控制台选中的 QQ 进程（避免接管用户自己的 QQ）
    assert env["SNOWLUMA_HOOK_AUTOLOAD"] == "0"
    cfg.snowluma.hook_mode = "auto"
    assert definition.env(cfg)["SNOWLUMA_HOOK_AUTOLOAD"] == "1"
    cfg.snowluma.hook_mode = "manual"
    assert env["SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD"] == cfg.snowluma.webui_password


def test_snowluma_service_def_full_missing_node_raises(sandbox):
    _fake_snowluma_install(sandbox, with_node=False)
    cfg: AppConfig = sandbox["cfg"]
    cfg.snowluma.edition = "full"
    with pytest.raises(installer.DeployError) as exc:
        installer.snowluma_service_def().argv(cfg)
    assert "node.exe" in str(exc.value) and "下一步" in str(exc.value)


def test_snowluma_service_def_not_installed_raises(sandbox):
    cfg: AppConfig = sandbox["cfg"]
    with pytest.raises(installer.DeployError) as exc:
        installer.snowluma_service_def().argv(cfg)
    assert "尚未安装" in str(exc.value)


def test_snowluma_service_def_lite_uses_path_node(sandbox, monkeypatch):
    _fake_snowluma_install(sandbox, with_node=False)
    cfg: AppConfig = sandbox["cfg"]
    cfg.snowluma.edition = "lite"
    node_path = "C:/Program Files/nodejs/node.exe"
    monkeypatch.setattr(installer, "_which", lambda name: node_path if name == "node" else "")
    monkeypatch.setattr(installer, "node_version", lambda exe=None: "24.22.1")
    assert installer.snowluma_service_def().argv(cfg) == [node_path, "index.mjs"]


def test_snowluma_service_def_lite_rejects_old_node(sandbox, monkeypatch):
    home = _fake_snowluma_install(sandbox, with_node=False)
    cfg: AppConfig = sandbox["cfg"]
    cfg.snowluma.edition = "lite"
    monkeypatch.setattr(installer, "_which", lambda name: "C:/node/node.exe" if name == "node" else "")
    monkeypatch.setattr(installer, "node_version", lambda exe=None: "24.13.0")  # 本机版本
    with pytest.raises(installer.DeployError) as exc:
        installer.snowluma_service_def().argv(cfg)
    message = str(exc.value)
    assert "24.22.1" in message and "full" in message
    assert home.exists()


def test_snowluma_service_def_lite_without_node_raises(sandbox, monkeypatch):
    _fake_snowluma_install(sandbox, with_node=False)
    cfg: AppConfig = sandbox["cfg"]
    cfg.snowluma.edition = "lite"
    monkeypatch.setattr(installer, "_which", lambda name: "")
    with pytest.raises(installer.DeployError) as exc:
        installer.snowluma_service_def().argv(cfg)
    assert "Node.js" in str(exc.value)


def test_snowluma_lite_check_prefers_bundled_cjs(sandbox, monkeypatch):
    """lite 预检优先调用包内 check-node-version.cjs（口径与运行时完全一致）。"""

    home = sandbox["runtime"] / "snowluma"
    home.mkdir(parents=True, exist_ok=True)
    (home / "check-node-version.cjs").write_text("'use strict';\n", "utf-8")
    calls: list[list[str]] = []

    def fake_run(argv, *, cwd=None, timeout=0, env=None, on_line=None):
        args = [str(a) for a in argv]
        calls.append(args)
        if "--version" in args:
            return installer.CmdResult(True, 0, "v24.13.0", args)
        return installer.CmdResult(False, 1, "error: SnowLuma requires Node.js ^24.22.1", args)

    monkeypatch.setattr(installer, "_run", fake_run)
    with pytest.raises(installer.DeployError) as exc:
        installer.check_node_version(home, "C:/node/node.exe")
    assert "自检脚本" in str(exc.value)
    assert any(str(home / "check-node-version.cjs") in c for c in calls)


def test_astrbot_service_def(sandbox):
    home = sandbox["runtime"] / "astrbot"
    home.mkdir(parents=True)
    (home / "main.py").write_text("print(1)\n", "utf-8")
    cfg: AppConfig = sandbox["cfg"]
    py = installer.astrbot_python(cfg)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("", "utf-8")

    definition = installer.astrbot_service_def()
    assert definition.name == "astrbot"
    assert definition.argv(cfg) == [str(py), "main.py"]
    assert definition.cwd(cfg) == home
    assert definition.ready_regex and "Running on http://" in definition.ready_regex
    assert definition.ready_port(cfg) == cfg.astrbot.webui_port
    assert definition.env(cfg)["PYTHONUNBUFFERED"] == "1"


def test_astrbot_service_def_missing_venv_raises(sandbox):
    home = sandbox["runtime"] / "astrbot"
    home.mkdir(parents=True)
    (home / "main.py").write_text("print(1)\n", "utf-8")
    with pytest.raises(installer.DeployError) as exc:
        installer.astrbot_service_def().argv(sandbox["cfg"])
    assert "虚拟环境" in str(exc.value)


def test_register_services_registers_both():
    installer.register_services()
    definitions = supervisor.definitions()
    assert "snowluma" in definitions and "astrbot" in definitions
    assert definitions["snowluma"].display == "SnowLuma"
    assert definitions["astrbot"].display == "AstrBot"
    assert definitions["snowluma"].kind == "process"


# --------------------------------------------------------------------------- #
# 版本列表降级
# --------------------------------------------------------------------------- #
def test_list_versions_degrades_without_network(sandbox, monkeypatch):
    def boom(url, timeout=15.0):
        raise RuntimeError("certificate verify failed")

    monkeypatch.setattr(installer, "_http_get_json", boom)
    data = installer.list_versions()
    assert data["snowluma"] == installer.FALLBACK_SNOWLUMA_TAGS
    assert data["stale"] is True
    assert "证书" in data["error"] or "GitHub" in data["error"]
    assert data["astrbot"] == "master"
    assert data["astrbot_branches"] == ["master"]


def test_list_versions_uses_api_data(sandbox, monkeypatch):
    release_payload = [
        {
            "tag_name": "v2.2.1",
            "prerelease": False,
            "draft": False,
            "published_at": "2026-10-01T00:00:00Z",
            "assets": [
                {"name": "SnowLuma-v2.2.1-win-x64.zip", "browser_download_url": "https://github.com/a", "size": 41},
                {"name": "SnowLuma-v2.2.1-win-x64-lite.zip", "browser_download_url": "https://github.com/b", "size": 12},
            ],
        },
        {
            "tag_name": "v2.2.0",
            "prerelease": False,
            "draft": False,
            "assets": [{"name": "SnowLuma-v2.2.0-win-x64.zip", "browser_download_url": "https://github.com/c", "size": 40}],
        },
        {"tag_name": "v2.3.0-rc.1", "prerelease": True, "draft": False, "assets": []},
    ]

    def fake_get(url, timeout=15.0):
        if "releases" in url:
            return release_payload
        return [{"name": "master"}, {"name": "dev"}]

    monkeypatch.setattr(installer, "_http_get_json", fake_get)
    data = installer.list_versions(force=True)
    assert data["snowluma"][0] == "v2.2.1"
    assert data["snowluma_latest"] == "v2.2.1"  # 预发布不算 latest
    assert "v2.2.0" in data["snowluma"]
    assert data["stale"] is False and data["error"] == ""
    assert data["astrbot_branches"][0] == "master"
    assert "dev" in data["astrbot_branches"]


def test_versions_cache_avoids_refetch(sandbox, monkeypatch):
    calls = {"n": 0}

    def fake_get(url, timeout=15.0):
        calls["n"] += 1
        return [{"tag_name": "v2.2.1", "prerelease": False, "assets": [], "published_at": ""}]

    monkeypatch.setattr(installer, "_http_get_json", fake_get)
    installer.list_versions(force=True)
    first = calls["n"]
    installer.list_versions()
    assert calls["n"] == first  # 命中缓存



