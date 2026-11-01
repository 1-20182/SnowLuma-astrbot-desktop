"""Lead 维护：终端页（实时输出 + 进程监视）。"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from slab.server.app import create_app

ROOT = Path(__file__).resolve().parents[1]
TERM_JS = ROOT / "slab" / "web" / "term.js"
INDEX = ROOT / "slab" / "web" / "index.html"


def test_procs_endpoint_reports_process_tree():
    with TestClient(create_app()) as c:
        body = c.get("/api/procs").json()
    assert body["ok"] is True
    assert body["root_pid"] == os.getpid()
    rows = body["processes"]
    assert rows, "进程树里至少要包含控制台自己"
    root = next(r for r in rows if r["pid"] == os.getpid())
    for key in ("pid", "ppid", "name", "cpu", "mem_mb", "created", "cmdline", "service"):
        assert key in root, f"进程信息缺少字段 {key}"
    assert root["service"] == "console"
    assert isinstance(root["mem_mb"], (int, float)) and root["mem_mb"] > 0


def test_term_page_markup():
    html = INDEX.read_text("utf-8")
    for element in ('id="page-term"', 'id="term-view"', 'id="proc-body"', 'id="term-filter"'):
        assert element in html, f"终端页缺少 {element}"
    assert 'href="#term"' in html, "部署页应该能一键跳到终端"
    assert 'theme.css' in html and '{{V}}' in html, '静态资源应带版本号防缓存'


def test_term_js_streams_sse_and_procs():
    src = TERM_JS.read_text("utf-8")
    assert "new EventSource('/api/events')" in src
    assert "/api/procs" in src
    assert "/api/logs" in src
    # 输出必须用 textContent（防止日志里的 <script> 被当成 HTML 执行）
    assert "textContent" in src
    assert "innerHTML" not in src
    # 断线重连 + 行数上限，避免刷爆页面
    assert "setTimeout(connect" in src
    assert "MAX_LINES" in src
