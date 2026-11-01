"""Lead 维护：版本号单一来源与更新日志一致性。

规则（用户要求）：语义化版本，任何代码改动都要 bump，并同步 CHANGELOG。
"""

from __future__ import annotations

import json
from pathlib import Path

from slab import paths

ROOT = Path(__file__).resolve().parents[1]


def test_version_file_matches_app_version():
    version = (ROOT / "VERSION").read_text("utf-8").strip()
    assert version == paths.APP_VERSION
    parts = version.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), f"不是语义化版本: {version}"


def test_desktop_package_version_matches():
    pkg = json.loads((ROOT / "desktop" / "package.json").read_text("utf-8"))
    assert pkg["version"] == paths.APP_VERSION


def test_changelog_has_current_version():
    text = (ROOT / "CHANGELOG.md").read_text("utf-8")
    assert f"## [{paths.APP_VERSION}]" in text, f"CHANGELOG 缺少 {paths.APP_VERSION} 的条目"
    # 至少有一条 1.0.0 的历史记录
    assert "## [1.0.0]" in text


def test_bump_script_exists():
    script = ROOT / "scripts" / "bump.ps1"
    assert script.is_file()
    body = script.read_text("utf-8-sig")
    assert "VERSION" in body and "CHANGELOG" in body and "package.json" in body


def test_health_and_state_report_same_version():
    from fastapi.testclient import TestClient

    from slab.server.app import create_app

    with TestClient(create_app()) as c:
        health = c.get("/api/health").json()
        state = c.get("/api/state").json()
    assert health["version"] == paths.APP_VERSION
    assert state["version"] == paths.APP_VERSION
