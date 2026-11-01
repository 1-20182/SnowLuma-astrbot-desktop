"""下载源（加速镜像）与测速。

国内直连 GitHub 经常很慢，安装包动辄 40 MB。这里维护一份镜像清单，
提供一个"测速 → 选最快"的能力；部署时按配置的 `app.download_mirror` 决定走哪条链路。

约定：镜像都是"前缀代理"形式，即 ``https://ghfast.top/`` + 原始 GitHub URL。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from . import installer, paths

# id → (显示名, 前缀, 说明)
MIRRORS: List[Dict[str, str]] = [
    {"id": "official", "label": "官方 GitHub（直连）", "prefix": "",
     "note": "最稳，但国内速度取决于网络环境"},
    {"id": "ghfast", "label": "ghfast.top", "prefix": "https://ghfast.top/",
     "note": "常用加速，支持 release 与 codeload"},
    {"id": "ghproxy_net", "label": "ghproxy.net", "prefix": "https://ghproxy.net/",
     "note": "老牌代理，偶有波动"},
    {"id": "gh_proxy", "label": "gh-proxy.com", "prefix": "https://gh-proxy.com/",
     "note": "支持大文件"},
    {"id": "moeyy", "label": "github.moeyy.xyz", "prefix": "https://github.moeyy.xyz/",
     "note": "备用线路"},
    {"id": "llkk", "label": "gh.llkk.cc", "prefix": "https://gh.llkk.cc/",
     "note": "备用线路"},
    {"id": "ghproxy_cc", "label": "ghproxy.cc", "prefix": "https://ghproxy.cc/",
     "note": "备用线路"},
]

MIRROR_IDS = [m["id"] for m in MIRRORS]
_AUTO_CACHE: Dict[str, Any] = {"ts": 0.0, "mirror": "official", "result": None}
AUTO_TTL = 30 * 60  # 30 分钟内复用上次测速结果
SAMPLE_BYTES = 512 * 1024
PER_SOURCE_TIMEOUT = 10.0
TOTAL_TIMEOUT = 8.0


def mirror_prefix(mirror_id: str) -> str:
    for item in MIRRORS:
        if item["id"] == mirror_id:
            return item["prefix"]
    return ""


def mirror_label(mirror_id: str) -> str:
    for item in MIRRORS:
        if item["id"] == mirror_id:
            return item["label"]
    return mirror_id or "official"


def rewrite(url: str, mirror_id: str) -> str:
    """把 GitHub 原始 URL 改写成镜像 URL（official 原样返回）。"""

    prefix = mirror_prefix(mirror_id)
    if not prefix:
        return url
    return prefix + url


def catalog() -> List[Dict[str, Any]]:
    return [dict(item) for item in MIRRORS]


def test_asset_url(cfg) -> str:
    """用 SnowLuma 的 lite 包当测速样本（体积小、与真实安装同一台机器）。"""

    tag = str(getattr(cfg.snowluma, "version", "") or "latest").strip()
    if tag in ("", "latest"):
        try:
            data = installer.list_versions()
            tags = data.get("snowluma") or []
            tag = str(tags[0]) if tags else ""
        except Exception:  # noqa: BLE001 - 拿不到就退回已知稳定版本
            tag = ""
    if not tag:
        tag = "v2.2.1"
    if not tag.startswith("v"):
        tag = f"v{tag}"
    return (f"https://github.com/SnowLuma/SnowLuma/releases/download/{tag}/"
            f"SnowLuma-{tag}-win-x64-lite.zip")


async def _measure(mirror: Dict[str, str], asset_url: str) -> Dict[str, Any]:
    """对单个源量一量：首个字节延迟 + 3MB 样本的平均速率。"""

    import httpx

    url = rewrite(asset_url, mirror["id"])
    start = time.perf_counter()
    first_byte: Optional[float] = None
    total = 0
    error = ""
    try:
        timeout = httpx.Timeout(PER_SOURCE_TIMEOUT, connect=6.0)
        transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")  # 强制 IPv4，避免 IPv6 先试导致的超时
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, transport=transport) as client:
            async with client.stream("GET", url, headers={"Range": f"bytes=0-{SAMPLE_BYTES - 1}"}) as resp:
                if resp.status_code not in (200, 206):
                    return {**mirror, "ok": False, "mbps": 0.0, "latency_ms": 0,
                            "bytes": 0, "error": f"HTTP {resp.status_code}", "url": url}
                async for chunk in resp.aiter_bytes(65536):
                    if first_byte is None:
                        first_byte = time.perf_counter() - start
                    total += len(chunk)
                    if total >= SAMPLE_BYTES:
                        break
    except Exception as exc:  # noqa: BLE001 - 网络异常都算"这个源不可用"
        error = f"{type(exc).__name__}: {exc}"[:160]
    elapsed = max(1e-6, time.perf_counter() - start)
    mbps = round((total / (1024 * 1024)) / elapsed, 2) if total else 0.0
    ok = bool(total) and not error
    return {
        **mirror,
        "url": url,
        "ok": ok,
        "mbps": mbps if ok else 0.0,
        "latency_ms": int(first_byte * 1000) if first_byte else 0,
        "bytes": total,
        "error": "" if ok else (error or "没有收到数据"),
    }


async def speedtest(cfg, *, sample_mb: int = 3) -> Dict[str, Any]:
    """并发测所有源，返回按速率排序的结果。"""

    asset = test_asset_url(cfg)
    started = time.perf_counter()
    # 只并发 2 个：家用带宽下同时拉 7 个样本会互相抢带宽，测出来全都不准
    sem = asyncio.Semaphore(2)

    async def _one(mirror: Dict[str, str]) -> Dict[str, Any]:
        async with sem:
            try:
                return await asyncio.wait_for(_measure(mirror, asset), timeout=TOTAL_TIMEOUT)
            except asyncio.TimeoutError:
                return {**mirror, "ok": False, "mbps": 0.0, "latency_ms": 0, "bytes": 0,
                        "error": f"超时（>{int(TOTAL_TIMEOUT)}s）", "url": rewrite(asset, mirror["id"])}

    results = await asyncio.gather(*[_one(m) for m in MIRRORS])
    elapsed = round(time.perf_counter() - started, 1)
    ordered = sorted(results, key=lambda r: (0 if r["ok"] else 1, -float(r["mbps"] or 0)))
    best = next((r for r in ordered if r["ok"]), None)
    return {
        "ok": True,
        "asset": asset,
        "sample_mb": sample_mb,
        "elapsed": elapsed,
        "recommended": best["id"] if best else "official",
        "results": ordered,
        "tested_at": time.time(),
    }


async def pick_fastest(cfg, *, force: bool = False) -> Dict[str, Any]:
    """给"自动"模式用：挑当前最快的源，结果缓存 30 分钟。"""

    cached = _AUTO_CACHE.get("result")
    if not force and cached and time.time() - _AUTO_CACHE["ts"] < AUTO_TTL:
        return {"mirror": _AUTO_CACHE["mirror"], "cached": True, "result": cached}
    data = await speedtest(cfg)
    best = data["recommended"]
    # 直连要是够快就不折腾镜像（镜像本身也有单点风险）
    top = next((r for r in data["results"] if r["id"] == best), None)
    if top and best != "official" and top["mbps"] < 1.5:
        best = "official"
    _AUTO_CACHE.update({"ts": time.time(), "mirror": best, "result": data})
    return {"mirror": best, "cached": False, "result": data}


def cached_fastest() -> str:
    """返回 30 分钟内测出来的最快源 id（没有就返回空串）。"""

    cached = _AUTO_CACHE.get("result")
    if cached and time.time() - _AUTO_CACHE["ts"] < AUTO_TTL:
        return str(_AUTO_CACHE.get("mirror") or "")
    return ""

def effective_mirror(cfg) -> str:
    """配置里的 mirror 值 → 实际要用的 id（auto 的情况由 async 的 pick_fastest 决定）。"""

    raw = str(getattr(cfg.app, "download_mirror", "") or "auto").strip()
    return raw if raw in MIRROR_IDS or raw == "auto" else "auto"
