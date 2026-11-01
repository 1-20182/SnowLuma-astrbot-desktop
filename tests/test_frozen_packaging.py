"""Lead 维护：打包（PyInstaller）相关的路径与兜底行为。"""

from __future__ import annotations

import importlib
import sys


def test_frozen_paths_use_exe_dir_and_bundle(monkeypatch, tmp_path):
    """打包模式下 APP_ROOT 取 exe 目录，WEB_DIR 取 _MEIPASS 里的只读资源。"""

    from slab import paths as p

    bundle = tmp_path / "meipass"
    (bundle / "slab" / "web").mkdir(parents=True)
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "SnowLuma-AstrBot-Console.exe"), raising=False)
    try:
        frozen = importlib.reload(p)
        assert frozen.FROZEN is True
        assert frozen.APP_ROOT == exe_dir.resolve()
        assert frozen.WEB_DIR == (bundle / "slab" / "web").resolve()
        assert frozen.DATA_DIR.name == "data"
    finally:
        monkeypatch.undo()
        importlib.reload(p)  # 还原成源码模式，避免污染其它用例


def test_source_mode_paths_point_to_repo(monkeypatch):
    from slab import paths as p

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    try:
        src = importlib.reload(p)
        assert src.FROZEN is False
        assert (src.APP_ROOT / "slab" / "web").exists()
        assert src.WEB_DIR.parent.name == "slab"
    finally:
        monkeypatch.undo()
        importlib.reload(p)


def test_ensure_stdio_creates_log(tmp_path):
    from slab import _frozen

    logfile = _frozen.ensure_stdio(tmp_path / "logs")
    assert logfile.exists() or logfile.parent.exists()
    assert logfile.name == "console.log"
    assert logfile.parent.is_dir()


def test_report_fatal_writes_crash_log(tmp_path):
    from slab import _frozen

    try:
        raise RuntimeError("端口 8790 被占用（测试）")
    except RuntimeError as exc:
        path = _frozen.report_fatal(exc, tmp_path / "logs", box=False)
    assert path.exists()
    text = path.read_text("utf-8")
    assert "RuntimeError" in text and "8790" in text


def test_entry_script_imports_package_main():
    """packaging/entry.py 必须能 import 到 slab.__main__（打包入口的薄包装）。"""

    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    entry = (root / "packaging" / "entry.py").read_text("utf-8")
    assert "from slab.__main__ import main" in entry
    spec = (root / "packaging" / "console.spec").read_text("utf-8")
    assert "slab/web" in spec and "collect_submodules" in spec
