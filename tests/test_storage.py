"""Lead 维护：存储规划（默认避开 C 盘 / 单盘才用 C / 老用户不搬家）。"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from slab import paths, storage
from slab.server.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def _drive(letter: str, free: float, system: bool = False) -> dict:
    return {"letter": letter, "root": f"{letter}:\\", "is_system": system,
            "free_gb": free, "total_gb": free * 2}


def test_only_system_drive_falls_back_to_c(monkeypatch, tmp_path):
    monkeypatch.delenv("SLAB_STORAGE_ROOT", raising=False)
    monkeypatch.setattr(storage, "list_drives", lambda: [_drive("C", 80, system=True)])
    plan = storage.plan(tmp_path)
    assert plan["base"].upper().startswith("C:")
    assert "只有一块盘" in plan["reason"]


def test_prefers_non_system_drive_with_most_space(monkeypatch, tmp_path):
    monkeypatch.delenv("SLAB_STORAGE_ROOT", raising=False)
    monkeypatch.setattr(storage, "list_drives", lambda: [
        _drive("C", 200, system=True), _drive("D", 120), _drive("E", 400),
    ])
    plan = storage.plan(tmp_path)
    assert plan["base"].upper().startswith("E:")
    assert "非系统盘" in plan["reason"]


def test_env_root_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("SLAB_STORAGE_ROOT", str(tmp_path / "custom"))
    monkeypatch.setattr(storage, "list_drives", lambda: [_drive("D", 100)])
    plan = storage.plan(tmp_path)
    assert plan["base"] == str(tmp_path / "custom")
    assert plan["source"] == "env"


def test_legacy_install_is_not_moved(monkeypatch, tmp_path):
    """程序旁边已经装了东西：不要因为"新默认在 D 盘"就换位置。"""

    monkeypatch.delenv("SLAB_STORAGE_ROOT", raising=False)
    monkeypatch.setattr(storage, "list_drives", lambda: [
        _drive("C", 100, system=True), _drive("D", 500),
    ])
    (tmp_path / "runtime" / "snowluma").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.json").write_text("{}", "utf-8")

    plan = storage.plan(tmp_path)
    assert Path(plan["runtime_dir"]) == tmp_path / "runtime"
    assert Path(plan["data_dir"]) == tmp_path / "data"
    assert plan["legacy_runtime"] and plan["legacy_data"]
    # 下载缓存仍然去非系统盘
    assert plan["downloads_dir"].upper().startswith("D:")


def test_fresh_install_puts_everything_off_system_drive(monkeypatch, tmp_path):
    monkeypatch.delenv("SLAB_STORAGE_ROOT", raising=False)
    monkeypatch.setattr(storage, "list_drives", lambda: [
        _drive("C", 100, system=True), _drive("D", 500),
    ])
    plan = storage.plan(tmp_path)
    assert plan["runtime_dir"].upper().startswith("D:")
    assert plan["data_dir"].upper().startswith("D:")
    assert plan["downloads_dir"].upper().startswith("D:")


def test_paths_are_not_on_system_drive_by_default():
    """当前机器如果有非 C 盘，默认数据/安装/下载都不该落在 C 盘。"""

    non_system = [d for d in storage.list_drives() if not d.get("is_system")]
    if not non_system or os.environ.get("SLAB_DATA_DIR"):
        return  # 只有一块盘 / 测试环境已显式指定，跳过
    if paths.STORAGE.get("legacy_data") or paths.STORAGE.get("legacy_runtime"):
        return  # 老目录沿用，属预期
    for value in (str(paths.DATA_DIR), str(paths.RUNTIME_DIR), str(paths.DOWNLOAD_DIR)):
        assert not value.upper().startswith("C:"), value


def test_state_exposes_storage_plan():
    with TestClient(create_app()) as c:
        body = c.get("/api/state").json()
    st = body["storage"]
    assert st["runtime_dir"] and st["data_dir"] and st["downloads_dir"]
    assert isinstance(st["drives"], list)
    assert body["paths"]["downloads"] == st["downloads_dir"]
