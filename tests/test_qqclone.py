"""Lead 维护：QQ 深度分身（复用 AppClone sandbox DLL）。"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

from slab import qqclone
from slab.server import routes_qq
from slab.server.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_sandbox_components_bundled():
    status = qqclone.status()
    assert Path(status["sandbox_dir"]) == ROOT / "vendor" / "sandbox"
    assert status["sandbox_ready"] is True
    assert (ROOT / "vendor" / "sandbox" / "inject64.dll").stat().st_size > 100_000


def test_clone_env_has_isolation_markers():
    env = qqclone.build_clone_env("bot")
    assert env["FENSHEN_MUTEX_TAG"] == "bot"
    assert env["APPDATA"] == env["FENSHEN_ROAMING"]
    assert env["LOCALAPPDATA"] == env["FENSHEN_LOCAL"]
    assert "TEMP" in env
    # 关键：不能重定向 USERPROFILE（实测会让 QQ 启动器直接卡死）
    assert "USERPROFILE" not in env
    assert "HOMEDRIVE" not in env and "HOMEPATH" not in env
    # 我们不启用注册表重定向（没有 HKCU 快照，启用会让 QQ 读到空注册表）
    assert "FENSHEN_REG_SUBKEY" not in env


def test_env_block_is_unicode_and_double_nul():
    env = qqclone.build_clone_env("bot")
    blob = qqclone.build_env_block(env)
    assert blob.endswith(b"\x00\x00\x00\x00")
    text = blob.decode("utf-16le")
    assert "FENSHEN_MUTEX_TAG=bot" in text
    # 环境块必须排序（Windows 约定），检查前两个键有序
    entries = [line for line in text.split("\x00") if "=" in line]
    keys = [e.split("=", 1)[0].upper() for e in entries]
    assert keys == sorted(keys)


def test_clone_dirs_under_storage_base():
    dirs = qqclone.clone_dirs("bot")
    assert dirs["home"].name == "home"
    assert dirs["home"].parent.parent.name == "clones"
    assert "qq" in dirs["home"].parts


def test_arch_detection():
    assert qqclone._is_64bit_exe(Path(sys.executable)) is True


def test_clone_status_route():
    with TestClient(create_app()) as c:
        body = c.get("/api/qq/clone/status").json()
    assert body["ok"] is True
    assert body["sandbox_ready"] is True
    assert body["clone_id"] == "bot"


def test_clone_launch_route(monkeypatch):
    calls = {}

    def fake_launch(exe, clone_id="bot"):
        calls["exe"] = str(exe)
        calls["clone_id"] = clone_id
        return {"ok": True, "pid": 4321, "clone_id": clone_id, "arch": "x64"}

    monkeypatch.setattr(qqclone, "launch_clone", fake_launch)
    monkeypatch.setattr(routes_qq.qq, "list_installs", lambda: [{"exe": r"C:\Program Files\Tencent\QQNT\QQ.exe"}])
    with TestClient(create_app()) as c:
        body = c.post("/api/qq/clone/launch", json={}).json()
    assert body["pid"] == 4321
    assert calls["exe"].endswith("QQ.exe")


def test_clone_launch_without_qq(monkeypatch):
    monkeypatch.setattr(routes_qq.qq, "list_installs", lambda: [])
    with TestClient(create_app()) as c:
        res = c.post("/api/qq/clone/launch", json={})
    assert res.status_code == 404
