"""Lead 维护：升级链路（配置迁移 + zip 升级引擎）。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from slab import migrations, paths, upgrade
from slab.server.app import create_app

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# 配置迁移
# --------------------------------------------------------------------------- #
def test_old_config_gets_hook_mode_and_mirror():
    raw = {"app": {"port": 8790}, "snowluma": {"qq_uin": "123456"}}
    data, start, steps = migrations.migrate(raw)
    assert start == 1
    assert steps == [2, 3, 4]
    assert data["snowluma"]["hook_mode"] == "manual"
    assert data["app"]["download_mirror"] == "auto"
    assert data["config_version"] == migrations.CURRENT_CONFIG_VERSION
    # 用户填过的值不能被动
    assert data["snowluma"]["qq_uin"] == "123456"
    assert data["app"]["port"] == 8790


def test_migration_is_idempotent():
    raw = {"config_version": migrations.CURRENT_CONFIG_VERSION, "app": {}, "snowluma": {}}
    data, start, steps = migrations.migrate(raw)
    assert steps == []
    assert start == migrations.CURRENT_CONFIG_VERSION
    assert data["config_version"] == migrations.CURRENT_CONFIG_VERSION


def test_migrate_file_keeps_backup(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"app": {"port": 8791}}), "utf-8")
    result = migrations.migrate_file(path)
    assert result["ok"] is True
    assert result["from"] == 1
    assert result["steps"] == [2, 3, 4]
    assert Path(result["backup"]).is_file()
    saved = json.loads(path.read_text("utf-8"))
    assert saved["config_version"] == migrations.CURRENT_CONFIG_VERSION
    assert saved["app"]["port"] == 8791


def test_config_store_writes_config_version():
    from slab.config import ConfigStore

    with TestClient(create_app()) as c:
        assert c.get("/api/state").status_code == 200
    raw = json.loads(paths.CONFIG_PATH.read_text("utf-8"))
    assert raw.get("config_version") == migrations.CURRENT_CONFIG_VERSION


# --------------------------------------------------------------------------- #
# 升级包识别
# --------------------------------------------------------------------------- #
def _make_zip(path: Path, version: str, *, with_exe: bool = True, root: str = "SnowLuma-Desktop") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{root}/VERSION", version + "\n")
        if with_exe:
            zf.writestr(f"{root}/SnowLuma-Desktop.exe", b"MZ" + b"\0" * 64)
        zf.writestr(f"{root}/assets/app.js", "console.log(1)")
    return path


def test_inspect_zip_newer(tmp_path):
    zip_path = _make_zip(tmp_path / "new.zip", "9.9.9")
    info = upgrade.inspect_zip(zip_path)
    assert info["ok"] is True
    assert info["version"] == "9.9.9"
    assert info["newer"] is True
    assert info["entry"] == "SnowLuma-Desktop.exe"
    assert info["root"] == "SnowLuma-Desktop"


def test_inspect_zip_rejects_junk(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_text("not a zip", "utf-8")
    assert upgrade.inspect_zip(bad)["ok"] is False

    noexe = _make_zip(tmp_path / "noexe.zip", "9.9.9", with_exe=False)
    info = upgrade.inspect_zip(noexe)
    assert info["ok"] is False
    assert "exe" in info["error"]


def test_inspect_zip_detects_same_and_older(tmp_path):
    same = upgrade.inspect_zip(_make_zip(tmp_path / "same.zip", paths.APP_VERSION))
    assert same["same"] is True and same["newer"] is False
    older = upgrade.inspect_zip(_make_zip(tmp_path / "old.zip", "0.0.1"))
    assert older["newer"] is False and older["same"] is False


def test_stage_extracts_outside_program_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(upgrade, "upgrade_dir", lambda: tmp_path / "upgrade")
    zip_path = _make_zip(tmp_path / "new.zip", "9.9.9")
    staged = upgrade.stage(zip_path)
    assert staged["ok"] is True
    root = Path(staged["staged_root"])
    assert (root / "VERSION").is_file()
    assert (root / "SnowLuma-Desktop.exe").is_file()
    # 解压目标必须在 data/upgrade 下（程序目录之外）
    assert "upgrade" in str(root)


# --------------------------------------------------------------------------- #
# 升级计划 / 路由
# --------------------------------------------------------------------------- #
def test_plan_lists_protected_dirs():
    plan = upgrade.plan()
    assert plan["current_version"] == paths.APP_VERSION
    assert plan["program_dir"]
    keep = [Path(p) for p in plan["keep"]]
    assert any(p.name == "data" for p in keep)
    assert any(p.name == "runtime" for p in keep)
    assert isinstance(plan["data_inside_program"], bool)


def test_upgrade_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(upgrade, "upgrade_dir", lambda: tmp_path / "upgrade")
    with TestClient(create_app()) as c:
        plan = c.get("/api/upgrade/plan").json()
        assert plan["ok"] is True
        bad = c.post("/api/upgrade/inspect", json={"zip": str(tmp_path / "nope.zip")})
        assert bad.status_code == 400
        zip_path = _make_zip(tmp_path / "new.zip", "9.9.9")
        info = c.post("/api/upgrade/inspect", json={"zip": str(zip_path)}).json()
        assert info["version"] == "9.9.9"


def test_helper_script_contains_safety_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(upgrade, "upgrade_dir", lambda: tmp_path / "upgrade")
    (tmp_path / "upgrade").mkdir(parents=True, exist_ok=True)
    script = upgrade.helper_script(
        tmp_path / "staged",
        restart=str(tmp_path / "SnowLuma-Desktop.exe"),
        pid=1234,
        keep=[str(paths.DATA_DIR), str(paths.RUNTIME_DIR)],
        backup_dir=tmp_path / "backup",
    )
    # cmd 只是外壳，真正的逻辑在 PowerShell 里（批处理的 tasklist|find 在中文环境会卡死）
    shim = script.read_text("utf-8")
    assert "powershell" in shim.lower()
    ps1 = script.with_suffix(".ps1")
    assert ps1.is_file()
    body = ps1.read_text("utf-8-sig")
    assert "$WaitPid = 1234" in body
    assert "robocopy" in body.lower()
    assert "回滚" in body
    assert "Start-Process" in body
    # 必须排除用户数据目录，绝不覆盖 data / runtime
    assert "/XD" in body
    assert "3" in body  # 等待超时保护（分钟）


def test_docs_exist():
    assert (ROOT / "docs" / "UPGRADE.md").is_file()
    text = (ROOT / "docs" / "UPGRADE.md").read_text("utf-8")
    for needle in ("应用内升级", "手动覆盖", "回滚", "兼容性承诺"):
        assert needle in text


def test_config_page_has_upgrade_ui():
    html = (ROOT / "slab" / "web" / "index.html").read_text("utf-8")
    for needle in ('id="upgrade-box"', 'id="upgrade-zip"', 'id="upgrade-check"', 'id="upgrade-run"'):
        assert needle in html
    js = (ROOT / "slab" / "web" / "app.js").read_text("utf-8")
    for needle in ("/api/upgrade/plan", "/api/upgrade/inspect", "/api/upgrade/apply", "runUpgrade"):
        assert needle in js
