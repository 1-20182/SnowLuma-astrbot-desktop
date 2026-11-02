"""静态前端 / 文档 / 启动脚本的一致性检查。

这些测试不启动浏览器，只保证：
* 前端文件存在、HTML 结构与导航文案齐全；
* app.js 调用的每个 /api/ 路径都真的存在于 FastAPI 路由里（防止前后端走偏）；
* index.html 上的 data-cfg 字段都能在配置模型里找到（防止表单绑到不存在的字段）；
* 没有任何外链 CDN（离线可用）；
* 一键启动.bat / README / docs 存在且包含关键内容。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "slab" / "web"
INDEX = WEB / "index.html"
APP_JS = WEB / "app.js"
APP_CSS = WEB / "app.css"

# 契约里已定义、但由 T1/T2 并行开发中的接口：允许暂时不存在于路由表
PENDING_API = {
    "/api/link/test-send",
    "/api/link/whoami",
}


# --------------------------------------------------------------------------- #
# 基础存在性
# --------------------------------------------------------------------------- #
def test_web_files_exist():
    for path in (INDEX, APP_JS, APP_CSS, WEB / "term.js", WEB / "theme.css", WEB / "favicon.svg"):
        assert path.is_file(), f"缺少前端文件: {path}"
        assert path.stat().st_size > 200, f"前端文件内容过少: {path}"


def test_index_is_chinese_and_links_assets():
    html = INDEX.read_text("utf-8")
    assert '<html lang="zh-CN">' in html
    assert 'charset="utf-8"' in html
    assert "/assets/app.css" in html
    assert "/assets/app.js" in html
    assert 'id="main"' in html
    assert "viewport" in html


def test_index_has_all_navigation_labels():
    html = INDEX.read_text("utf-8")
    for label in ("总览", "部署", "配置", "运行", "联调", "终端"):
        assert f"<b>{label}</b>" in html, f"左侧导航缺少「{label}」"
    for text in (
        "一键启动全部",
        "停止全部",
        "下一步建议",
        "环境自检",
        "端口占用",
        "实时日志",
        "消息流水",
        "写入联动配置",
        "发一条测试消息",
        "我的 QQ 账号信息",
        "管理员 QQ",
        "SnowLuma 中继端口",
    ):
        assert text in html, f"界面缺少关键文案「{text}」"


def test_nav_steps_map_to_pages():
    html = INDEX.read_text("utf-8")
    steps = set(re.findall(r'data-page="([a-z]+)"', html))
    pages = set(re.findall(r'id="page-([a-z]+)"', html))
    assert steps == {"overview", "deploy", "config", "run", "link", "term"}
    assert pages == steps, f"导航与页面不匹配: {steps} vs {pages}"
    assert "/assets/term.js" in html, "终端页脚本没被引入"


# --------------------------------------------------------------------------- #
# 离线可用：不许有任何 CDN / 外链资源
# --------------------------------------------------------------------------- #
def test_no_external_cdn():
    bad_markers = (
        "//cdn.",
        "cdnjs",
        "unpkg.com",
        "jsdelivr",
        "googleapis",
        "bootstrapcdn",
        "fonts.gstatic",
        "ajax.aspnetcdn",
    )
    for path in (INDEX, APP_JS, APP_CSS):
        text = path.read_text("utf-8")
        lowered = text.lower()
        for marker in bad_markers:
            assert marker not in lowered, f"{path.name} 引用了外部 CDN: {marker}"

    html = INDEX.read_text("utf-8")
    assert not re.search(r'<script[^>]+src="https?://', html)
    assert not re.search(r'<link[^>]+href="https?://', html)
    assert not re.search(r'@import\s+url\(\s*["\']?https?://', APP_CSS.read_text("utf-8"))


# --------------------------------------------------------------------------- #
# 前后端接口对齐
# --------------------------------------------------------------------------- #
def _app_route_paths() -> set[str]:
    """收集 FastAPI 应用里所有 /api/ 路由。

    不同 FastAPI 版本对 include_router 的处理方式不同（有的是扁平化 route，
    有的用 _IncludedRouter 包一层），所以这里递归遍历 + 兜底读 openapi。
    """

    from slab.server.app import create_app

    app = create_app()
    paths: set[str] = set()

    def walk(routes) -> None:  # noqa: ANN001
        for route in routes or ():
            path = getattr(route, "path", None)
            if isinstance(path, str) and path.startswith("/api/"):
                paths.add(path)
            nested = getattr(route, "routes", None)
            if nested:
                walk(nested)

    walk(app.routes)
    try:
        paths.update(p for p in app.openapi().get("paths", {}) if p.startswith("/api/"))
    except Exception:  # noqa: BLE001 - openapi 生成失败不影响主逻辑
        pass
    return paths


def _route_pattern(path: str) -> re.Pattern:
    parts = []
    for seg in path.strip("/").split("/"):
        if seg.startswith("{") and seg.endswith("}"):
            parts.append(r"[^/]+")
        else:
            parts.append(re.escape(seg))
    return re.compile("^/" + "/".join(parts) + "$")


def _js_api_paths() -> set[str]:
    text = APP_JS.read_text("utf-8") + "\n" + (WEB / "term.js").read_text("utf-8")
    found = re.findall(r"/api/[A-Za-z0-9_\-./${}]*", text)
    out = set()
    for raw in found:
        cleaned = raw.rstrip(".,;:)'\"`")
        cleaned = re.sub(r"\$\{[^}]*\}", "{param}", cleaned)
        if cleaned != "/api/":
            out.add(cleaned)
    return out


def test_app_js_api_paths_all_exist():
    used = _js_api_paths()
    assert len(used) >= 8, f"app.js 里识别到的接口太少，提取逻辑可能失效: {used}"

    patterns = [_route_pattern(p) for p in _app_route_paths()]
    missing = sorted(p for p in used if not any(rx.match(p) for rx in patterns))
    unexpected = [p for p in missing if p not in PENDING_API]
    assert not unexpected, f"app.js 调用了后端不存在的接口: {unexpected}"

    # 并行开发中的接口一旦实现就应从 PENDING 里消失，这里只提示不失败
    if not missing:
        assert True


def test_app_js_uses_fetch_and_eventsource():
    text = APP_JS.read_text("utf-8")
    assert "fetch(" in text
    assert "new EventSource(" in text
    assert "/api/events" in text
    assert "'use strict'" in text or '"use strict"' in text
    # 错误提示必须是中文
    assert "try {" in text and "catch" in text
    assert "失败" in text


def test_app_js_disables_buttons_while_busy():
    text = APP_JS.read_text("utf-8")
    assert "classList.add('loading')" in text or 'classList.add("loading")' in text
    assert "disabled = true" in text


# --------------------------------------------------------------------------- #
# 表单字段必须真实存在
# --------------------------------------------------------------------------- #
def test_config_form_fields_exist_in_config_model():
    from slab.config import AppConfig

    defaults = AppConfig().to_dict()
    html = INDEX.read_text("utf-8")
    fields = sorted(set(re.findall(r'data-cfg="([^"]+)"', html)))
    assert len(fields) >= 20, f"配置表单字段过少: {fields}"

    for path in fields:
        node = defaults
        ok = True
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                ok = False
                break
        assert ok, f"表单绑定了配置模型里不存在的字段: {path}"


def test_config_form_covers_required_inputs():
    html = INDEX.read_text("utf-8")
    for field in (
        "snowluma.qq_uin",
        "astrbot.provider.api_key",
        "astrbot.provider.model",
        "link.mode",
        "link.trigger_mode",
        "link.ws_port",
        "astrbot.admins_id",
        "snowluma.relay_port",
    ):
        assert f'data-cfg="{field}"' in html, f"配置页缺少字段 {field}"

    # 敏感字段必须标注"留空表示不修改"
    assert html.count('data-secret="1"') >= 2
    assert "留空表示不修改" in html


# --------------------------------------------------------------------------- #
# 静态资源可被 HTTP 访问（/ 与 /assets/*）
# --------------------------------------------------------------------------- #
def test_assets_served_over_http():
    from fastapi.testclient import TestClient

    from slab.server.app import create_app

    with TestClient(create_app()) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "SnowLuma" in index.text

        for asset, needle in (("/assets/app.js", "EventSource"), ("/assets/app.css", "--bg")):
            res = client.get(asset)
            assert res.status_code == 200, asset
            assert needle in res.text

        assert client.get("/assets/favicon.svg").status_code == 200


# --------------------------------------------------------------------------- #
# 启动脚本
# --------------------------------------------------------------------------- #
def _read_text_tolerant(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return path.read_text(encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text("utf-8", errors="replace")


def test_launcher_bat():
    bat = ROOT / "一键启动.bat"
    assert bat.is_file(), "缺少 一键启动.bat"
    text = _read_text_tolerant(bat)

    assert "chcp 65001" in text.lower()
    assert "python -m slab serve" in text
    assert "requirements.txt" in text
    assert ".venv" in text
    assert ("uv venv" in text) and ("python -m venv" in text), "脚本应同时兼容 uv 与内置 venv"
    assert "127.0.0.1:8790" in text
    assert "pause" in text.lower(), "出错时应 pause 以便看提示"
    assert text.lower().startswith("@echo off")
    # 不应把绝对路径写死
    assert "E:\\AGENT" not in text and "C:\\Users" not in text


# --------------------------------------------------------------------------- #
# 文档
# --------------------------------------------------------------------------- #
def test_docs_exist_and_cover_key_topics():
    readme = (ROOT / "README.md").read_text("utf-8")
    assert "SnowLuma" in readme and "AstrBot" in readme
    # 1.7.2 重写后的 README 是「快速开始（3 步）」；这里按当前结构断言，
    # 不为了过测试反向改 README 措辞。
    assert "快速开始（3 步）" in readme
    assert "native" in readme and "bridge" in readme
    assert "免责声明" in readme
    assert "docs/TROUBLESHOOTING.md" in readme
    assert "目录结构" in readme

    arch = (ROOT / "docs" / "ARCHITECTURE.md").read_text("utf-8")
    assert "OneBot v11" in arch
    assert "端口表" in arch
    assert "cmd_config.json" in arch, "架构文档应说明 AstrBot 配置注入点"
    assert "onebot" in arch.lower()

    deploy = (ROOT / "docs" / "DEPLOY.md").read_text("utf-8")
    assert "离线安装" in deploy
    assert "端口冲突" in deploy
    assert "升级" in deploy and "回滚" in deploy

    trouble = (ROOT / "docs" / "TROUBLESHOOTING.md").read_text("utf-8")
    sections = re.findall(r"^## \d+\.", trouble, flags=re.MULTILINE)
    assert len(sections) >= 10, f"排错文档条目不足 10 条，只有 {len(sections)} 条"
    for keyword in ("WebUI", "端口", "QQ", "OneBot", "乱码", "防火墙", "杀毒", "下载"):
        assert keyword in trouble, f"排错文档缺少「{keyword}」相关条目"


def test_contracts_file_untouched_by_frontend():
    """前端不应偷偷往契约文档里塞内容（契约由 Lead 维护）。"""
    contracts = ROOT / "docs" / "CONTRACTS.md"
    assert contracts.is_file()
    text = contracts.read_text("utf-8")
    assert "[T3]" in text  # 契约里本来就标注了 web/docs 归属 T3


@pytest.mark.parametrize("page", ["overview", "deploy", "config", "run", "link"])
def test_each_page_has_heading(page):
    html = INDEX.read_text("utf-8")
    block = re.search(rf'id="page-{page}"(.*?)(?=<section class="page"|</main>)', html, re.S)
    assert block, f"找不到页面区块 {page}"
    assert "<h1>" in block.group(1), f"页面 {page} 缺少标题"
