"""部署路由单测（T1）：/api/deploy/env、/versions、/{target}、/status、DELETE。

要点：
* 绝不真的下载/克隆：``installer.deploy_*`` 与 ``installer.uninstall`` 全部 monkeypatch；
* POST 必须**立刻返回** ``{"ok": true, "started": true}``，进度通过 SSE 事件推送；
* 版本接口在 GitHub 不可达时必须降级而不是报错。
"""

from __future__ import annotations

import inspect
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from slab import installer
from slab.events import bus
from slab.server import routes_deploy
from slab.server.app import create_app


def _client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def _reset_jobs():
    """每个用例前后清空任务表，避免上一个用例的后台线程干扰。"""

    with routes_deploy._jobs_lock:
        routes_deploy._jobs.clear()
    yield
    deadline = time.time() + 5
    while time.time() < deadline:
        with routes_deploy._jobs_lock:
            running = [n for n, j in routes_deploy._jobs.items() if j.get("running")]
        if not running:
            break
        time.sleep(0.05)
    with routes_deploy._jobs_lock:
        routes_deploy._jobs.clear()


def _wait_job(client: TestClient, name: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = client.get("/api/deploy/status").json()["jobs"]
        job = jobs.get(name) or {}
        if job and not job.get("running") and job.get("step") != "idle":
            return job
        time.sleep(0.05)
    raise AssertionError(f"部署任务 {name} 未在 {timeout}s 内结束")


# --------------------------------------------------------------------------- #
# GET /api/deploy/env
# --------------------------------------------------------------------------- #
def test_env_report_shape():
    with _client() as client:
        response = client.get("/api/deploy/env")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    # 兼容 Lead 的 envcheck.env_report 结构
    assert "tools" in body and "python" in body["tools"]
    assert isinstance(body["installed"]["snowluma"], bool)
    assert "free_space_gb" in body
    # T1 增补的结构
    assert set(body["deploy"]) == {"snowluma", "astrbot"}
    assert "dir" in body["deploy"]["snowluma"]
    assert "python" in body["deploy"]["astrbot"]
    assert "astrbot_webui" in body["ports"]
    recommend = body["recommend"]
    assert recommend["snowluma_edition"] in ("full", "lite")
    assert recommend["node_required"] == installer.NODE_REQUIRED
    assert isinstance(recommend["lite_supported"], bool)
    assert isinstance(recommend["astrbot_installable"], bool)
    assert recommend["python"]["min_version"] == "3.12"
    assert isinstance(body["warnings"], list)


def test_env_report_recommends_full_when_node_too_old(monkeypatch):
    from slab import envcheck

    original = envcheck.env_report

    def fake_report():
        report = original()
        report["tools"]["node"] = {"name": "node", "path": "C:/node.exe", "ok": True, "version": "v24.13.0"}
        return report

    monkeypatch.setattr(envcheck, "env_report", fake_report)
    envcheck.invalidate_cache()
    with _client() as client:
        body = client.get("/api/deploy/env").json()
    assert body["recommend"]["lite_supported"] is False
    assert body["recommend"]["snowluma_edition"] == "full"
    assert any("24.22.1" in w for w in body["warnings"])


def test_env_reports_config_vs_runtime_port(monkeypatch):
    from slab import envcheck, installer, paths

    envcheck.invalidate_cache()
    home = Path(paths.RUNTIME_DIR) / "snowluma"
    (home / "logs").mkdir(parents=True, exist_ok=True)
    (home / "release.json").write_text('{"version": "2.2.1", "target": "win32-x64"}', "utf-8")
    (home / "index.mjs").write_text("// entry", "utf-8")
    (home / "logs" / "snowluma-2026-10-29.log").write_text(
        "[WebUI] listening http://127.0.0.1:5312\n", "utf-8"
    )
    try:
        with _client() as client:
            body = client.get("/api/deploy/env").json()
        assert body["deploy"]["snowluma"]["installed"] is True
        assert body["deploy"]["snowluma"]["runtime_port"] == 5312
        assert body["snowluma_port"]["runtime"] == 5312
        assert body["snowluma_port"]["mismatch"] is True
        assert any("5312" in w for w in body["warnings"])
    finally:
        import shutil

        shutil.rmtree(home, ignore_errors=True)
        envcheck.invalidate_cache()


def test_env_github_probe_reports_reason(monkeypatch):
    from slab import envcheck

    envcheck.invalidate_cache()
    monkeypatch.setattr(
        installer, "check_github",
        lambda *a, **k: {"ok": False, "reason": "证书校验失败：…", "status": 0, "error": "x"},
    )
    with _client() as client:
        body = client.get("/api/deploy/env?net=1").json()
    assert body["recommend"]["github"]["checked"] is True
    assert body["recommend"]["github"]["reachable"] is False
    assert "证书" in body["recommend"]["github"]["reason"]
    envcheck.invalidate_cache()


# --------------------------------------------------------------------------- #
# GET /api/deploy/versions
# --------------------------------------------------------------------------- #
def test_versions_falls_back_when_github_unreachable(monkeypatch):
    def boom(url, timeout=15.0):
        raise RuntimeError("CERTIFICATE_VERIFY_FAILED")

    monkeypatch.setattr(installer, "_http_get_json", boom)
    monkeypatch.setattr(installer, "_versions_state",
                        {"data": None, "ts": 0.0, "error": "", "fail_ts": 0.0})
    monkeypatch.setattr(installer, "_branches_state",
                        {"data": None, "ts": 0.0, "error": "", "fail_ts": 0.0})
    with _client() as client:
        body = client.get("/api/deploy/versions").json()
    assert body["ok"] is True
    assert body["snowluma"]  # 降级到内置 tag 列表
    assert body["stale"] is True
    assert ("证书" in body["error"]) or ("GitHub" in body["error"])
    assert body["astrbot"] == "master"


def test_versions_returns_tags_and_branches(monkeypatch):
    index = {
        "tags": ["v2.2.1", "v2.2.0"],
        "latest": "v2.2.1",
        "releases": {},
        "fetched_at": time.time(),
        "stale": False,
        "error": "",
    }
    monkeypatch.setattr(installer, "_release_index", lambda force=False: index)
    monkeypatch.setattr(installer, "_astrbot_branches",
                        lambda force=False: {"default": "master", "branches": ["master", "dev"], "error": ""})
    with _client() as client:
        body = client.get("/api/deploy/versions").json()
    assert body["snowluma"] == ["v2.2.1", "v2.2.0"]
    assert body["snowluma_latest"] == "v2.2.1"
    assert body["astrbot"] == "master"
    assert body["astrbot_branches"] == ["master", "dev"]


# --------------------------------------------------------------------------- #
# POST /api/deploy/{target}
# --------------------------------------------------------------------------- #
def test_post_returns_immediately_and_reports_progress(monkeypatch):
    gate = threading.Event()
    entered = threading.Event()

    def fake_deploy(cfg=None, progress=None, **kwargs):
        entered.set()
        # 真实实现用 installer.DeployProgress 上报（推 SSE + 更新任务状态）
        report = installer.DeployProgress("snowluma", progress)
        report("download", 0.42, "已下载 18/41 MB")
        gate.wait(5)
        report("done", 1.0, "安装完成")
        return {"installed": True, "skipped": False, "version": "2.2.1", "dir": "X:/runtime/snowluma"}

    monkeypatch.setattr(installer, "deploy_snowluma", fake_deploy)
    with _client() as client:
        started_at = time.time()
        response = client.post("/api/deploy/snowluma")
        elapsed = time.time() - started_at
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True and body["started"] is True
        assert body["target"] == "snowluma"
        assert elapsed < 3.0, f"POST 没有立即返回（{elapsed:.2f}s）"

        assert entered.wait(5), "后台线程没有开始执行部署"

        # 同一目标并发提交会被拒绝
        conflict = client.post("/api/deploy/snowluma")
        assert conflict.status_code == 409
        assert conflict.json()["ok"] is False

        gate.set()
        job = _wait_job(client, "snowluma")
        assert job["ok"] is True
        assert job["result"]["version"] == "2.2.1"
        assert job["progress"] == 1.0

        status = client.get("/api/deploy/status").json()
        assert status["ok"] is True and status["running"] == []

    events = [e for e in bus.recent(300) if e.get("type") == "deploy"]
    assert any(
        e.get("target") == "snowluma" and e.get("step") == "download" and e.get("progress") == 0.42
        for e in events
    ), "缺少下载进度事件"
    assert any(e.get("step") == "finished" and e.get("ok") is True for e in events)
    assert any(e.get("step") == "start" for e in events)


def test_post_failure_sets_error_state(monkeypatch):
    def fake_deploy(cfg=None, progress=None, **kwargs):
        raise installer.DeployError("下载失败（已重试 3 次）：HTTP 403\n下一步：填写镜像前缀。")

    monkeypatch.setattr(installer, "deploy_astrbot", fake_deploy)
    with _client() as client:
        assert client.post("/api/deploy/astrbot").json()["started"] is True
        job = _wait_job(client, "astrbot")
    assert job["ok"] is False
    assert "镜像前缀" in job["error"]
    assert job["step"] == "error"
    events = [e for e in bus.recent(300) if e.get("type") == "deploy" and e.get("target") == "astrbot"]
    assert any(e.get("step") == "error" and e.get("ok") is False for e in events)


def test_post_both_runs_both_services(monkeypatch):
    calls = []

    def fake_snow(cfg=None, progress=None, **kwargs):
        calls.append("snowluma")
        return {"installed": True, "skipped": True, "version": "2.2.1"}

    def fake_astr(cfg=None, progress=None, **kwargs):
        calls.append("astrbot")
        return {"installed": True, "commit": "abc1234"}

    monkeypatch.setattr(installer, "deploy_snowluma", fake_snow)
    monkeypatch.setattr(installer, "deploy_astrbot", fake_astr)
    with _client() as client:
        body = client.post("/api/deploy/both").json()
        assert body["services"] == ["snowluma", "astrbot"]
        _wait_job(client, "astrbot")
    assert calls == ["snowluma", "astrbot"]


def test_post_passes_options_through(monkeypatch, tmp_path):
    seen: dict = {}
    offline = tmp_path / "SnowLuma-v2.2.1-win-x64.zip"
    offline.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    def fake_deploy(cfg=None, progress=None, **kwargs):
        seen.update(kwargs)
        return {"installed": True}

    monkeypatch.setattr(installer, "deploy_snowluma", fake_deploy)
    with _client() as client:
        response = client.post(
            "/api/deploy/snowluma",
            json={
                "edition": "lite",
                "version": "v2.2.1",
                "mirror": "https://ghfast.top",
                "offline_zip": str(offline),
                "force": True,
            },
        )
        assert response.status_code == 200
        _wait_job(client, "snowluma")
    assert seen["edition"] == "lite"
    assert seen["version"] == "v2.2.1"
    assert seen["mirror"] == "https://ghfast.top"
    assert seen["offline_zip"] == str(offline)
    assert seen["force"] is True


@pytest.mark.parametrize(
    "payload,expect",
    [
        ({"edition": "ultra"}, "full 或 lite"),
        ({"mirror": "http://insecure.example"}, "https"),
        ({"version": "../etc/passwd"}, "不合法"),
        ({"offline_zip": "Z:/definitely/missing.zip"}, "不存在"),
    ],
)
def test_post_rejects_bad_options(payload, expect):
    with _client() as client:
        response = client.post("/api/deploy/snowluma", json=payload)
    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert expect in body["error"]


def test_post_rejects_unknown_target():
    with _client() as client:
        response = client.post("/api/deploy/notexist")
    assert response.status_code == 400
    assert "未知部署目标" in response.json()["error"]


# --------------------------------------------------------------------------- #
# DELETE /api/deploy/{target}
# --------------------------------------------------------------------------- #
def test_delete_uninstalls(monkeypatch):
    called: dict = {}

    def fake_uninstall(target, cfg=None):
        called["target"] = target
        return {"ok": True, "removed": True, "dir": "X:/runtime/snowluma",
                "backup": "X:/data/backups/snowluma-1", "message": "已卸载并备份"}

    monkeypatch.setattr(installer, "uninstall", fake_uninstall)
    with _client() as client:
        response = client.delete("/api/deploy/snowluma")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["removed"] is True
    assert called["target"] == "snowluma"
    assert "备份" in body["message"]
    events = [e for e in bus.recent(300) if e.get("type") == "deploy"]
    assert any(e.get("step") == "uninstall" for e in events)


def test_delete_reports_deploy_error(monkeypatch):
    def fake_uninstall(target, cfg=None):
        raise installer.DeployError("目录无法移动（被占用）。下一步：关闭占用文件的程序后重试。")

    monkeypatch.setattr(installer, "uninstall", fake_uninstall)
    with _client() as client:
        response = client.delete("/api/deploy/astrbot")
    assert response.status_code == 400
    assert "被占用" in response.json()["error"]


def test_delete_rejects_both():
    with _client() as client:
        response = client.delete("/api/deploy/both")
    assert response.status_code == 400
    assert response.json()["ok"] is False


# --------------------------------------------------------------------------- #
# POST /api/deploy/open-dir
# --------------------------------------------------------------------------- #
@pytest.fixture()
def installed_cfg(tmp_path, monkeypatch):
    """让 snowluma 安装目录指向 tmp_path，并把它写进配置。"""

    from slab.config import AppConfig

    home = tmp_path / "snowluma"
    home.mkdir()
    cfg = AppConfig()
    cfg.snowluma.install_dir = str(home)
    cfg.astrbot.install_dir = str(tmp_path / "astrbot-missing")
    cfg.normalise()
    monkeypatch.setattr(routes_deploy, "get_config", lambda: cfg)
    return cfg, home


def _patch_opener(monkeypatch, opened: list[str]) -> None:
    """把「打开目录」换成一个只记录调用的假实现。

    平台差异（explorer / xdg-open）由 ``slab.plat.open_path`` 负责，单独用
    ``test_open_dir_uses_platform_command`` 按平台断言，路由层只管调用与错误映射。
    """

    def fake_open(path):
        opened.append(str(path))
        return True

    monkeypatch.setattr(routes_deploy, "_open_path", fake_open)


def test_open_dir_launches_platform_opener(installed_cfg, monkeypatch):
    cfg, home = installed_cfg
    opened: list[str] = []
    _patch_opener(monkeypatch, opened)
    with _client() as client:
        response = client.post("/api/deploy/open-dir", json={"target": "snowluma"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["path"] == str(home)
    assert opened == [str(home)]


def test_open_dir_uses_platform_command(monkeypatch):
    """真正去开目录的那条命令要按平台切换：Windows=explorer，Linux=xdg-open。

    两个平台都是 fire-and-forget 的 ``Popen``（资源管理器卡在"恢复会话"时不能连带挂住
    控制台的请求线程），所以只 patch ``Popen``；再用 ``inspect.getsource`` 钉住
    "Windows 分支不许改成 run 等结果"这个契约，免得以后又被重构掉。
    """

    from slab import plat

    opened: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        opened.append([str(a) for a in argv])
        return object()

    if plat.IS_WINDOWS:
        monkeypatch.setattr(plat.subprocess, "Popen", fake_popen)
        assert plat.open_path(Path(r"C:\bots")) is True
        assert opened and opened[0][0] == "explorer" and opened[0][1] == r"C:\bots"
        source = inspect.getsource(plat.open_path)
        assert 'Popen(["explorer"' in source, "Windows 分支必须是 fire-and-forget 的 Popen"
    else:
        monkeypatch.setattr(plat.shutil, "which",
                            lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else "")
        monkeypatch.setattr(plat.subprocess, "Popen", fake_popen)
        assert plat.open_path(Path("/opt/bots")) is True
        # POSIX 上 **不能** 是 explorer，必须是文件管理器程序（xdg-open）
        assert opened and opened[0][0].endswith("xdg-open") and opened[0][1] == "/opt/bots"


def test_open_dir_without_file_manager_is_501(installed_cfg, monkeypatch):
    """没有文件管理器（无头 Linux）时要给 501 + 可复制的路径，而不是 500。"""

    monkeypatch.setattr(routes_deploy, "_open_path", lambda path: False)
    with _client() as client:
        response = client.post("/api/deploy/open-dir", json={"target": "snowluma"})
    assert response.status_code == 501
    assert "手动打开这个路径" in response.json()["error"]


def test_open_dir_missing_directory_is_chinese_400(installed_cfg, monkeypatch):
    monkeypatch.setattr(routes_deploy.subprocess, "Popen", lambda *a, **k: object())
    with _client() as client:
        response = client.post("/api/deploy/open-dir", json={"target": "astrbot"})
    assert response.status_code == 400
    assert "请先安装" in response.json()["error"]


def test_open_dir_unknown_target(installed_cfg):
    with _client() as client:
        response = client.post("/api/deploy/open-dir", json={"target": "system32"})
    assert response.status_code == 400
    assert "未知目标" in response.json()["error"]


def test_open_dir_rejects_path_outside_sandbox(installed_cfg, monkeypatch):
    """配置里写的目录被篡改到沙箱外时必须拒绝。"""

    cfg, _ = installed_cfg
    monkeypatch.setattr(installer, "snowluma_dir", lambda _cfg=None: Path("C:/Windows"))
    cfg.snowluma.install_dir = ""
    with _client() as client:
        response = client.post("/api/deploy/open-dir", json={"target": "snowluma"})
    assert response.status_code == 400
    assert "未授权" in response.json()["error"] or "系统目录" in response.json()["error"]


def test_open_dir_data_target(tmp_path, monkeypatch):
    from slab import paths

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(routes_deploy.paths, "DATA_DIR", data_dir)
    opened: list[str] = []
    _patch_opener(monkeypatch, opened)
    with _client() as client:
        response = client.post("/api/deploy/open-dir", json={"target": "data"})
    assert response.status_code == 200
    assert response.json()["path"] == str(Path(paths.DATA_DIR))
    assert opened == [str(data_dir)]


# --------------------------------------------------------------------------- #
# 自检缓存
# --------------------------------------------------------------------------- #
def test_env_uses_cached_report(monkeypatch):
    from slab import envcheck

    calls = {"n": 0}
    original = envcheck.env_report

    def counting_report():
        calls["n"] += 1
        return original()

    monkeypatch.setattr(envcheck, "env_report", counting_report)
    # 新增了磁盘缓存 + 后台刷新：先把缓存清干净，避免首屏命中磁盘里的旧结果
    monkeypatch.setattr(envcheck, "_read_disk_cache", lambda: None)
    monkeypatch.setattr(envcheck, "_write_disk_cache", lambda data: None)
    envcheck.invalidate_cache()
    with _client() as client:
        assert client.get("/api/deploy/env").status_code == 200
        assert client.get("/api/deploy/env").status_code == 200
    assert calls["n"] <= 2, "两次请求不应重复做完整自检（应命中缓存/后台刷新）"
    envcheck.invalidate_cache()
    with _client() as client:
        assert client.get("/api/deploy/env").status_code == 200
    assert calls["n"] == 2


def test_deploy_invalidates_env_cache(monkeypatch):
    from slab import envcheck

    monkeypatch.setattr(installer, "deploy_snowluma",
                        lambda cfg=None, progress=None, **kw: {"installed": True, "skipped": True})
    envcheck.env_report_cached()  # 预热缓存
    assert envcheck._CACHE["data"] is not None
    with _client() as client:
        assert client.post("/api/deploy/snowluma").json()["started"] is True
        _wait_job(client, "snowluma")
    assert envcheck._CACHE["data"] is None

