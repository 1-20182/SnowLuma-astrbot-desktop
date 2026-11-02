"""中继（bridge）与联动路由的测试（T2）。

包含真正跑通的双向端到端：
* 假 AstrBot 反向 WS 服务端（扮演 aiocqhttp：收事件、发 action、收回包）；
* 假 SnowLuma WS 服务端（扮演 OneBot 实现端：发事件、执行 action、按 echo 回 response）；
* 真 :class:`slab.bridge.Bridge` 把两边接起来，验证「QQ 消息 → AstrBot → 回复 → QQ」。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from websockets.asyncio.server import serve

from slab import astrbot_admin, bridge, snowluma_admin
from slab.bridge import Bridge, BridgeFilters
from slab.config import AppConfig
from slab.events import bus
from slab.onebot.model import OneBotEvent
from slab.server import routes_link


# --------------------------------------------------------------------------- #
# 假服务端
# --------------------------------------------------------------------------- #
class FakeAstrBot:
    """扮演 AstrBot 的 aiocqhttp 反向 WS 服务端（只读事件，主动发 action）。"""

    def __init__(self) -> None:
        self.headers: Dict[str, str] = {}
        self.events: List[Dict[str, Any]] = []
        self.responses: List[Dict[str, Any]] = []
        self.connected = asyncio.Event()
        self._conn: Any = None

    async def handler(self, ws) -> None:
        self.headers = dict(ws.request.headers)
        self._conn = ws
        self.connected.set()
        async for raw in ws:
            payload = json.loads(raw)
            if payload.get("post_type"):
                self.events.append(payload)
            else:
                self.responses.append(payload)

    async def send_action(self, action: str, params: Dict[str, Any], echo: str) -> None:
        assert self._conn is not None
        await self._conn.send(json.dumps({"action": action, "params": params, "echo": echo}))


class FakeSnowLuma:
    """扮演 SnowLuma 的 OneBot WS 服务端（正向）。"""

    def __init__(self) -> None:
        self.headers: Dict[str, str] = {}
        self.actions: List[Dict[str, Any]] = []
        self.connected = asyncio.Event()
        self._conn: Any = None

    async def handler(self, ws) -> None:
        self.headers = dict(ws.request.headers)
        self._conn = ws
        self.connected.set()
        async for raw in ws:
            payload = json.loads(raw)
            if "action" not in payload:
                continue
            self.actions.append(payload)
            params = payload.get("params") or {}
            if payload["action"] == "send_private_msg" and str(params.get("user_id")) == "999":
                await ws.send(json.dumps({
                    "status": "failed", "retcode": 100, "data": None,
                    "message": "qq_hook client closed", "echo": payload.get("echo"),
                }))
                continue
            data: Dict[str, Any] = {"message_id": 987} if payload["action"].startswith("send_") else {}
            if payload["action"] == "get_login_info":
                data = {"user_id": 3985934176, "nickname": "测试机器人"}
            elif payload["action"] == "get_friend_list":
                data = [{"user_id": 20002, "nickname": "小明"}]
            elif payload["action"] == "get_group_list":
                data = [{"group_id": 123456, "group_name": "测试群"}]
            await ws.send(json.dumps({
                "status": "ok", "retcode": 0, "data": data, "echo": payload.get("echo"),
            }))

    async def push(self, event: Dict[str, Any]) -> None:
        assert self._conn is not None
        await self._conn.send(json.dumps(event, ensure_ascii=False))


async def start(handler) -> tuple[Any, int]:
    server = await serve(handler, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1])


async def shutdown(*servers: Any) -> None:
    """关闭假服务端并带超时等待，避免测试挂死。"""

    for server in servers:
        server.close()
    for server in servers:
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(server.wait_closed(), timeout=5)


async def wait_until(predicate, timeout: float = 6.0, interval: float = 0.02) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


@pytest.fixture()
def cfg() -> AppConfig:
    config = AppConfig()
    config.normalise()
    config.link.token = "e2e-token"
    config.link.ws_host = "127.0.0.1"
    config.link.mode = "bridge"
    config.link.trigger_mode = "all"
    config.link.allow_group = True
    config.link.allow_private = True
    config.snowluma.qq_uin = "3985934176"
    return config


@pytest.fixture(autouse=True)
def _clear_status_cache():
    """状态缓存是进程级单例，测试之间必须清掉，避免串味。"""

    bridge.reset_status_cache()
    yield
    bridge.reset_status_cache()


# --------------------------------------------------------------------------- #
# 端到端
# --------------------------------------------------------------------------- #
async def test_end_to_end_message_and_reply(cfg: AppConfig) -> None:
    astrbot = FakeAstrBot()
    snowluma = FakeSnowLuma()
    astrbot_server, astrbot_port = await start(astrbot.handler)
    snowluma_server, snowluma_port = await start(snowluma.handler)
    cfg.link.ws_port = astrbot_port
    cfg.snowluma.relay_port = snowluma_port

    link = Bridge(cfg)
    try:
        await link.start()
        assert await wait_until(lambda: astrbot.connected.is_set() and snowluma.connected.is_set())

        # 握手头符合 SnowLuma 的语义（AstrBot 侧按 X-Self-ID 路由 action）
        assert astrbot.headers["authorization"] == "Bearer e2e-token"
        assert astrbot.headers["x-self-id"] == "3985934176"
        assert astrbot.headers["x-client-role"].lower() == "universal"
        assert snowluma.headers["authorization"] == "Bearer e2e-token"

        # ---- QQ 消息 → AstrBot
        await snowluma.push({
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "time": 1700000000,
            "self_id": 3985934176,
            "user_id": 20002,
            "message_id": 555,
            "message": [{"type": "text", "data": {"text": "你好 AstrBot"}}],
            "raw_message": "你好 AstrBot",
            "sender": {"user_id": 20002, "nickname": "小明"},
        })
        assert await wait_until(lambda: len(astrbot.events) == 1)
        forwarded = astrbot.events[0]
        assert forwarded["self_id"] == 3985934176
        assert forwarded["user_id"] == 20002
        assert forwarded["message"][0]["data"]["text"] == "你好 AstrBot"

        # ---- AstrBot 回复 → QQ（action 走中继回到 SnowLuma）
        await astrbot.send_action(
            "send_private_msg",
            {"user_id": 20002, "message": [{"type": "text", "data": {"text": "你好呀"}}],
             "self_id": 3985934176},
            "reply-echo-1",
        )
        assert await wait_until(lambda: len(astrbot.responses) == 1)
        response = astrbot.responses[0]
        assert response["echo"] == "reply-echo-1"
        assert response["status"] == "ok"
        assert response["data"] == {"message_id": 987}

        # 下游真的执行了 send_private_msg，且 self_id 被剔除（其它参数原样透传）
        assert await wait_until(lambda: any(a["action"] == "send_private_msg" for a in snowluma.actions))
        sent = next(a for a in snowluma.actions if a["action"] == "send_private_msg")
        assert str(sent["params"]["user_id"]) == "20002"
        assert sent["params"]["message"][0]["data"]["text"] == "你好呀"
        assert "self_id" not in sent["params"]

        # 统计与审计
        status = link.status()
        assert status["stats"]["events_in"] == 1
        assert status["stats"]["events_forwarded"] == 1
        assert status["stats"]["actions_ok"] == 1
        assert status["mode"] == "bridge"
        messages = [event for event in bus.recent(200) if event.get("type") == "message"]
        assert any(item["direction"] == "in" and item["text"] == "你好 AstrBot" for item in messages)
        assert any(item["direction"] == "out" and "你好呀" in item["text"] for item in messages)
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(link.stop(), timeout=5)
        await shutdown(astrbot_server, snowluma_server)


async def test_duplicate_delivery_is_forwarded_once(cfg: AppConfig) -> None:
    """同一条消息被投递两次（SnowLuma 直连 + 中继双通道）时只转发一次。

    否则 AstrBot 处理两遍、回两遍 —— 用户看到的就是「发一条，机器人回两条」。
    """

    astrbot = FakeAstrBot()
    snowluma = FakeSnowLuma()
    astrbot_server, astrbot_port = await start(astrbot.handler)
    snowluma_server, snowluma_port = await start(snowluma.handler)
    cfg.link.ws_port = astrbot_port
    cfg.snowluma.relay_port = snowluma_port

    link = Bridge(cfg)
    try:
        await link.start()
        assert await wait_until(lambda: astrbot.connected.is_set() and snowluma.connected.is_set())

        event = {
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "time": 1700000000,
            "self_id": 3985934176,
            "user_id": 20002,
            "message_id": 888,
            "message": [{"type": "text", "data": {"text": "你好"}}],
            "raw_message": "你好",
            "sender": {"user_id": 20002, "nickname": "小明"},
        }
        await snowluma.push(event)
        assert await wait_until(lambda: len(astrbot.events) == 1)

        await snowluma.push(event)  # 同一条消息第二次抵达
        assert await wait_until(lambda: link.stats["events_duplicate"] == 1)
        await asyncio.sleep(0.2)
        assert len(astrbot.events) == 1  # 只转发了一次
        status = link.status()
        assert status["stats"]["events_forwarded"] == 1
        assert status["stats"]["events_in"] == 2
        assert status["dedupe_window"] > 0

        # 不同 message_id 的消息照常转发（去重不能误伤正常消息）
        await snowluma.push({**event, "message_id": 889, "raw_message": "再来一条"})
        assert await wait_until(lambda: len(astrbot.events) == 2)
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(link.stop(), timeout=5)
        await shutdown(astrbot_server, snowluma_server)


async def test_group_message_requires_at_or_prefix(cfg: AppConfig) -> None:
    astrbot = FakeAstrBot()
    snowluma = FakeSnowLuma()
    astrbot_server, astrbot_port = await start(astrbot.handler)
    snowluma_server, snowluma_port = await start(snowluma.handler)
    cfg.link.ws_port = astrbot_port
    cfg.snowluma.relay_port = snowluma_port
    cfg.link.trigger_mode = "at_or_prefix"
    cfg.link.trigger_prefixes = ["/"]

    link = Bridge(cfg)
    try:
        await link.start()
        assert await wait_until(lambda: astrbot.connected.is_set() and snowluma.connected.is_set())

        base = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 123456,
            "time": 1700000000,
            "self_id": 3985934176,
            "user_id": 20002,
            "message_id": 1,
            "sender": {"user_id": 20002, "nickname": "小明"},
        }
        # 1) 普通群消息：没有 @ 也没有前缀 → 丢弃
        await snowluma.push({**base, "message": [{"type": "text", "data": {"text": "闲聊"}}],
                             "raw_message": "闲聊"})
        assert await wait_until(lambda: link.stats["events_in"] == 1)
        assert link.stats["events_dropped"] == 1
        assert link.stats["last_drop_reason"]
        assert astrbot.events == []

        # 2) @ 机器人 → 转发
        await snowluma.push({
            **base,
            "message_id": 2,
            "message": [{"type": "at", "data": {"qq": "3985934176"}},
                        {"type": "text", "data": {"text": " 你好"}}],
            "raw_message": "[CQ:at,qq=3985934176] 你好",
        })
        assert await wait_until(lambda: len(astrbot.events) == 1)

        # 3) 前缀触发 → 转发
        await snowluma.push({**base, "message_id": 3,
                             "message": [{"type": "text", "data": {"text": "/help"}}],
                             "raw_message": "/help"})
        assert await wait_until(lambda: len(astrbot.events) == 2)
        assert astrbot.events[1]["group_id"] == 123456
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(link.stop(), timeout=5)
        await shutdown(astrbot_server, snowluma_server)


async def test_ignore_self_and_disabled_kinds(cfg: AppConfig) -> None:
    astrbot = FakeAstrBot()
    snowluma = FakeSnowLuma()
    astrbot_server, astrbot_port = await start(astrbot.handler)
    snowluma_server, snowluma_port = await start(snowluma.handler)
    cfg.link.ws_port = astrbot_port
    cfg.snowluma.relay_port = snowluma_port
    cfg.link.allow_group = False
    cfg.link.ignore_self = True

    link = Bridge(cfg)
    try:
        await link.start()
        assert await wait_until(lambda: astrbot.connected.is_set() and snowluma.connected.is_set())
        base = {"post_type": "message", "time": 1, "self_id": 3985934176, "message_id": 1,
                "message": [{"type": "text", "data": {"text": "hi"}}], "raw_message": "hi",
                "sender": {"user_id": 3985934176}}
        # 自己发的（user_id == self_id）
        await snowluma.push({**base, "message_type": "private", "user_id": 3985934176})
        # 群消息被禁用
        await snowluma.push({**base, "message_type": "group", "group_id": 1, "user_id": 20002})
        assert await wait_until(lambda: link.stats["events_in"] == 2)
        assert link.stats["events_forwarded"] == 0
        assert link.stats["events_dropped"] == 2
        assert astrbot.events == []
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(link.stop(), timeout=5)
        await shutdown(astrbot_server, snowluma_server)


async def test_failed_action_is_audited(cfg: AppConfig) -> None:
    """发送失败也要进审计/事件总线（否则用户在 UI 里只看到"没回复"）。"""

    astrbot = FakeAstrBot()
    snowluma = FakeSnowLuma()
    astrbot_server, astrbot_port = await start(astrbot.handler)
    snowluma_server, snowluma_port = await start(snowluma.handler)
    cfg.link.ws_port = astrbot_port
    cfg.snowluma.relay_port = snowluma_port

    link = Bridge(cfg)
    try:
        await link.start()
        assert await wait_until(lambda: astrbot.connected.is_set() and snowluma.connected.is_set())
        await astrbot.send_action(
            "send_private_msg",
            {"user_id": "999", "message": [{"type": "text", "data": {"text": "发不出去"}}],
             "self_id": 3985934176},
            "echo-fail",
        )
        assert await wait_until(lambda: len(astrbot.responses) == 1)
        response = astrbot.responses[0]
        assert response["status"] == "failed"
        assert response["retcode"] == 100
        assert link.stats["actions_failed"] == 1

        failed_events = [event for event in bus.recent(200)
                         if event.get("type") == "message" and event.get("ok") is False]
        assert failed_events
        assert "qq_hook" in failed_events[-1]["error"]
        assert failed_events[-1]["direction"] == "out"
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(link.stop(), timeout=5)
        await shutdown(astrbot_server, snowluma_server)


async def test_service_task_idles_in_native_mode(monkeypatch) -> None:
    """native 模式不需要中继：task 应当待机（不建连接、不占端口）。"""

    cfg = AppConfig()
    cfg.normalise()
    cfg.link.mode = "native"
    monkeypatch.setattr(bridge, "get_config", lambda: cfg)

    task = asyncio.create_task(bridge._service_task())
    try:
        await asyncio.sleep(0.2)
        assert bridge.current() is None
        assert task.done() is False
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert bridge.current() is None


# --------------------------------------------------------------------------- #
# 过滤规则单测
# --------------------------------------------------------------------------- #
def test_filter_rules_unit() -> None:
    filters = BridgeFilters(allow_private=True, allow_group=True, trigger_mode="at_or_prefix",
                            prefixes=["/", "#"], ignore_self=True, forward_non_text=False)
    private = OneBotEvent.message_event("private", 20002, "随便聊", self_id="10000")
    assert filters.decide(private) == (True, "")

    self_msg = OneBotEvent.message_event("private", 10000, "hi", self_id="10000")
    allowed, reason = filters.decide(self_msg)
    assert allowed is False and "自己" in reason

    group_plain = OneBotEvent.message_event("group", 20002, "闲聊", self_id="10000", group_id=1)
    allowed, reason = filters.decide(group_plain)
    assert allowed is False and "前缀" in reason

    group_at = OneBotEvent.message_event(
        "group", 20002,
        [{"type": "at", "data": {"qq": "10000"}}, {"type": "text", "data": {"text": "在"}}],
        self_id="10000", group_id=1,
    )
    assert filters.decide(group_at)[0] is True

    group_at_all = OneBotEvent.message_event(
        "group", 20002,
        [{"type": "at", "data": {"qq": "all"}}, {"type": "text", "data": {"text": "在"}}],
        self_id="10000", group_id=1,
    )
    assert filters.decide(group_at_all)[0] is False  # @全体不算 @机器人

    group_prefix = OneBotEvent.message_event("group", 20002, "#help", self_id="10000", group_id=1)
    assert filters.decide(group_prefix)[0] is True

    image_only = OneBotEvent.message_event(
        "group", 20002, [{"type": "image", "data": {"file": "a.png"}}],
        self_id="10000", group_id=1,
    )
    allowed, reason = filters.decide(image_only)
    assert allowed is False and "非文本" in reason

    no_group = BridgeFilters(allow_group=False)
    assert no_group.decide(group_at)[0] is False

    no_private = BridgeFilters(allow_private=False)
    assert no_private.decide(private)[0] is False

    trigger_all = BridgeFilters(trigger_mode="all")
    assert trigger_all.decide(group_plain)[0] is True

    notice = OneBotEvent.parse({"post_type": "notice", "notice_type": "group_increase"})
    assert filters.decide(notice)[0] is True  # 通知类直接放行


# --------------------------------------------------------------------------- #
# 服务注册 / 状态
# --------------------------------------------------------------------------- #
def test_register_service_defines_task() -> None:
    from slab.supervisor import supervisor

    bridge.register_service()
    definition = supervisor.definitions()[bridge.SERVICE_NAME]
    assert definition.kind == "task"
    assert definition.task_factory is not None


async def test_link_status_merges_probes(cfg: AppConfig, monkeypatch) -> None:
    async def fake_snowluma_probe(_cfg, **_kwargs):
        return {
            "ok": True, "state": "connected", "connected": True,
            "clients": [{"uin": "3985934176", "name": "astrbot", "status": "ok",
                         "detail": "已连接", "connected": True}],
            "suggestions": [], "accounts": ["3985934176"],
        }

    async def fake_astrbot_probe(_cfg, **_kwargs):
        return {
            "ok": True, "state": "ready", "provider_ok": True,
            "onebot_ws": {"ok": True, "status_code": 101},
            "suggestions": [], "config": {},
        }

    monkeypatch.setattr(snowluma_admin, "probe", fake_snowluma_probe)
    monkeypatch.setattr(astrbot_admin, "probe", fake_astrbot_probe)
    cfg.link.mode = "native"

    result = await bridge.link_status(cfg)
    assert result["ok"] is True
    assert result["ready"] is True
    assert result["mode"] == "native"
    assert result["onebot"]["ready"] is True
    assert result["onebot"]["clients_connected"] == 1
    assert set(result) >= {"snowluma", "astrbot", "onebot", "bridge", "recent_messages", "suggestions"}
    assert result["suggestions"]


async def test_link_status_cache_and_force(cfg: AppConfig, monkeypatch) -> None:
    """status 会被前端高频轮询：必须命中缓存，force 时才真正重新探测。"""

    calls = {"snowluma": 0, "astrbot": 0}

    async def fake_snowluma_probe(_cfg, **_kwargs):
        calls["snowluma"] += 1
        return {"ok": True, "connected": True, "clients": [], "suggestions": [], "accounts": []}

    async def fake_astrbot_probe(_cfg, **_kwargs):
        calls["astrbot"] += 1
        return {"ok": True, "state": "ready", "onebot_ws": {"ok": True, "status_code": 101},
                "suggestions": [], "config": {}}

    monkeypatch.setattr(snowluma_admin, "probe", fake_snowluma_probe)
    monkeypatch.setattr(astrbot_admin, "probe", fake_astrbot_probe)

    first = await bridge.link_status(cfg)
    second = await bridge.link_status(cfg)
    assert first["cached"] is False
    assert second["cached"] is True
    assert calls == {"snowluma": 1, "astrbot": 1}

    forced = await bridge.link_status(cfg, force=True)
    assert forced["cached"] is False
    assert calls == {"snowluma": 2, "astrbot": 2}


async def test_test_send_falls_back_to_snowluma_api(cfg: AppConfig, monkeypatch) -> None:
    """native 模式没有 wsServers 时，test-send 应回退 SnowLuma WebUI 直调。"""

    async def no_client(_cfg, timeout: float = 3.0):  # noqa: ARG001
        from slab.onebot.client import OneBotNotConnected

        raise OneBotNotConnected("连不上 SnowLuma 的 OneBot WS 服务端")

    async def fake_invoke(_cfg, action, params=None, **_kwargs):  # noqa: ANN001
        assert action == "send_private_msg"
        assert params["user_id"] == "20002"
        return {"ok": True, "uin": "3985934176", "action": action,
                "data": {"message_id": 11}, "payload": {}}

    monkeypatch.setattr(bridge, "_ephemeral_client", no_client)
    monkeypatch.setattr(snowluma_admin, "invoke_action", fake_invoke)

    result = await bridge.test_send(cfg, "private", "20002", "测试消息")
    assert result["ok"] is True
    assert result["via"] == "snowluma-api"
    assert result["data"] == {"message_id": 11}


async def test_apply_link_offline_writes_both_sides(cfg: AppConfig, tmp_path, monkeypatch) -> None:
    snowluma_home = tmp_path / "snowluma"
    (snowluma_home / "config").mkdir(parents=True)
    astrbot_home = tmp_path / "astrbot"
    (astrbot_home / "data").mkdir(parents=True)
    cfg.snowluma.install_dir = str(snowluma_home)
    cfg.astrbot.install_dir = str(astrbot_home)
    cfg.astrbot.provider["api_key"] = "sk-test"
    cfg.astrbot.admins_id = ["3985934176"]
    cfg.snowluma.qq_uin = "3985934176"
    cfg.link.mode = "bridge"
    cfg.snowluma.relay_port = 3001

    async def fake_snowluma_probe(_cfg, **_kwargs):
        return {"ok": True, "connected": True, "state": "connected", "clients": [],
                "suggestions": [], "accounts": ["3985934176"]}

    async def fake_astrbot_probe(_cfg, **_kwargs):
        return {"ok": True, "state": "ready", "onebot_ws": {"ok": True, "status_code": 101},
                "suggestions": [], "provider_ok": True, "config": {}}

    monkeypatch.setattr(snowluma_admin, "probe", fake_snowluma_probe)
    monkeypatch.setattr(astrbot_admin, "probe", fake_astrbot_probe)

    result = await bridge.apply_link(cfg, restart=False)
    steps = {step["step"]: step for step in result["steps"]}

    assert steps["apply_astrbot"]["ok"] is True
    assert steps["apply_snowluma_bridge"]["ok"] is True
    assert steps["restart_snowluma"]["skipped"] is True

    astrbot_cfg = json.loads((astrbot_home / "data" / "cmd_config.json").read_text("utf-8"))
    aiocqhttp = next(item for item in astrbot_cfg["platform"] if item["type"] == "aiocqhttp")
    assert aiocqhttp["ws_reverse_port"] == cfg.link.ws_port
    assert aiocqhttp["ws_reverse_token"] == cfg.link.token
    assert "3985934176" in astrbot_cfg["admins_id"]

    snowluma_cfg = json.loads((snowluma_home / "config" / "onebot.json").read_text("utf-8"))
    servers = snowluma_cfg["networks"]["wsServers"]
    assert servers and servers[0]["port"] == 3001


# --------------------------------------------------------------------------- #
# 路由契约
# --------------------------------------------------------------------------- #
def _link_client(monkeypatch, **overrides) -> TestClient:
    app = FastAPI()
    app.include_router(routes_link.router)

    async def default_status(_cfg=None, **_kwargs):
        return {"ok": True, "mode": "native", "ready": True, "snowluma": {}, "astrbot": {},
                "onebot": {"ready": True}, "bridge": {}, "recent_messages": [], "suggestions": []}

    async def default_apply(_cfg=None, **_kwargs):
        return {"ok": True, "mode": "native", "steps": [{"step": "apply_astrbot", "ok": True}],
                "verification": {"ready": True}, "applied": {}}

    async def default_send(_cfg, target_type, target_id, text):
        return {"ok": True, "via": "onebot", "data": {"message_id": 1},
                "echo": f"{target_type}:{target_id}:{text}"}

    async def default_whoami(_cfg):
        return {"ok": True, "via": "onebot", "user_id": "3985934176", "friends": 1, "groups": 1}

    monkeypatch.setattr(bridge, "link_status", overrides.get("status", default_status))
    monkeypatch.setattr(bridge, "apply_link", overrides.get("apply", default_apply))
    monkeypatch.setattr(bridge, "test_send", overrides.get("send", default_send))
    monkeypatch.setattr(bridge, "whoami", overrides.get("whoami", default_whoami))
    return TestClient(app)


def test_route_status_ok(monkeypatch) -> None:
    with _link_client(monkeypatch) as client:
        body = client.get("/api/link/status").json()
    assert body["ok"] is True and body["ready"] is True


def test_route_apply_ok(monkeypatch) -> None:
    with _link_client(monkeypatch) as client:
        body = client.post("/api/link/apply", json={"restart": False}).json()
    assert body["ok"] is True
    assert body["steps"][0]["step"] == "apply_astrbot"


def test_route_apply_reports_step_failure(monkeypatch) -> None:
    async def failing_apply(_cfg=None, **_kwargs):
        return {"ok": False, "steps": [{"step": "apply_astrbot", "ok": False,
                                        "detail": "请先在配置页填写大模型 API Key"}],
                "verification": {}, "applied": {}}

    with _link_client(monkeypatch, apply=failing_apply) as client:
        response = client.post("/api/link/apply", json={})
    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert "API Key" in body["error"] or "部分步骤失败" in body["error"]


def test_route_test_send_validation(monkeypatch) -> None:
    with _link_client(monkeypatch) as client:
        response = client.post("/api/link/test-send",
                               json={"target_type": "bad", "target_id": "1", "text": "x"})
        assert response.status_code == 400
        assert response.json()["ok"] is False

        response = client.post("/api/link/test-send",
                               json={"target_type": "private", "target_id": "", "text": "x"})
        assert response.status_code == 400

        response = client.post("/api/link/test-send",
                               json={"target_type": "private", "target_id": "1", "text": ""})
        assert response.status_code == 400

        ok = client.post("/api/link/test-send",
                         json={"target_type": "group", "target_id": "123", "text": "hi"})
        assert ok.status_code == 200
        assert ok.json()["via"] == "onebot"


def test_route_test_send_failure(monkeypatch) -> None:
    async def failing_send(_cfg, target_type, target_id, text):  # noqa: ARG001
        return {"ok": False, "error": "连不上 SnowLuma 的 OneBot WS 服务端"}

    with _link_client(monkeypatch, send=failing_send) as client:
        response = client.post("/api/link/test-send",
                               json={"target_type": "private", "target_id": "1", "text": "hi"})
    assert response.status_code == 400
    assert "SnowLuma" in response.json()["error"]


def test_route_whoami(monkeypatch) -> None:
    with _link_client(monkeypatch) as client:
        body = client.get("/api/link/whoami").json()
    assert body["ok"] is True
    assert body["user_id"] == "3985934176"

    async def failing_whoami(_cfg):
        return {"ok": False, "error": "没有可用的查询通道"}

    with _link_client(monkeypatch, whoami=failing_whoami) as client:
        response = client.get("/api/link/whoami")
    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_route_probe_and_accounts(monkeypatch, tmp_path) -> None:
    cfg = AppConfig()
    cfg.normalise()
    cfg.link.mode = "native"
    monkeypatch.setattr("slab.server.routes_link.get_config", lambda: cfg)

    async def fake_snowluma_probe(_cfg, **_kwargs):
        return {"ok": True, "connected": True, "clients": [], "suggestions": ["a"],
                "accounts": ["3985934176"]}

    async def fake_astrbot_probe(_cfg, **_kwargs):
        return {"ok": True, "onebot_ws": {"ok": True, "status_code": 101}, "suggestions": []}

    monkeypatch.setattr(snowluma_admin, "probe", fake_snowluma_probe)
    monkeypatch.setattr(astrbot_admin, "probe", fake_astrbot_probe)

    app = FastAPI()
    app.include_router(routes_link.router)
    with TestClient(app) as client:
        probe_body = client.post("/api/link/probe").json()
        accounts_body = client.get("/api/link/accounts").json()
    assert probe_body["ok"] is True
    assert probe_body["onebot"]["relay_url"].endswith("/")
    assert accounts_body["ok"] is True
    assert isinstance(accounts_body["accounts"], list)


# --------------------------------------------------------------------------- #
# 「能收不能发」：aiocqhttp 的 bot 登记被别的同账号连接删掉
# 现状：登记只在**重新建连**时恢复；接入方在 bridge 模式是中继、native 模式是 SnowLuma
# --------------------------------------------------------------------------- #
def test_bridge_status_counts_bridge_as_the_onebot_client(monkeypatch) -> None:
    """bridge 模式下 SnowLuma 的 wsClients 为空是正常的，不能报「未连接」。"""

    cfg = AppConfig()
    cfg.normalise()
    cfg.link.mode = "bridge"

    async def fake_snowluma_probe(_cfg, **_kwargs):
        return {"ok": True, "connected": False, "clients": [], "suggestions": [],
                "accounts": ["3985934176"]}

    async def fake_astrbot_probe(_cfg, **_kwargs):
        return {"ok": True, "onebot_ws": {"ok": True, "status_code": 101}, "suggestions": []}

    class StubBridge:
        self_id = "3985934176"
        running = True

        def status(self):
            return {
                "running": True,
                "self_id": "3985934176",
                "upstream": {"connected": True, "state": "connected"},
                "downstream": {"connected": True, "state": "connected"},
                "stats": {},
                "recent": [],
            }

    monkeypatch.setattr(snowluma_admin, "probe", fake_snowluma_probe)
    monkeypatch.setattr(astrbot_admin, "probe", fake_astrbot_probe)
    monkeypatch.setattr(bridge, "_bridge", StubBridge())

    status = asyncio.run(bridge._collect_status(cfg))  # noqa: SLF001
    assert status["onebot"]["clients_connected"] == 1
    assert status["onebot"]["clients_source"] == "bridge"
    assert status["onebot"]["ready"] is True


def test_route_repair_bridge_restarts_bridge_and_retries(monkeypatch) -> None:
    """bridge 模式下修复要走「重启中继重新登记」，且 ApiNotAvailable 后要重试一次。"""

    cfg = AppConfig()
    cfg.normalise()
    cfg.link.mode = "bridge"
    cfg.astrbot.admins_id = ["3317812824"]
    cfg.astrbot.webui_username = "astrbot"
    cfg.astrbot.webui_password = "pw"
    monkeypatch.setattr("slab.server.routes_link.get_config", lambda: cfg)

    reregistered: List[int] = []

    async def fake_reregister(_cfg=None, **_kwargs):
        reregistered.append(1)
        return {"ok": True, "detail": "中继已重新接入 AstrBot"}

    async def fake_single_channel(_cfg, **_kwargs):
        return {"ok": True, "changed": False}

    async def fake_stop(_name, *_a, **_k):  # SnowLuma 不该被重启
        raise AssertionError("bridge 模式不应该重启 SnowLuma")

    texts: List[str] = []

    def fake_send(_base, _key, _umo, text, **_kwargs):
        texts.append(text)
        if len(texts) == 1:
            # AstrBot 的 ApiNotAvailable 是空消息异常，OpenAPI 包成这句
            return {"ok": False, "status": 400, "message": "Failed to send message: "}
        return {"ok": True, "status": 200, "message": "已通过 AstrBot 发出"}

    monkeypatch.setattr(bridge, "reregister", fake_reregister)
    monkeypatch.setattr(snowluma_admin, "ensure_single_channel", fake_single_channel)
    monkeypatch.setattr(astrbot_admin, "_dashboard_token", lambda *_a, **_k: "jwt")
    monkeypatch.setattr(astrbot_admin, "ensure_api_key", lambda *_a, **_k: "key")
    monkeypatch.setattr(astrbot_admin, "send_im_message", fake_send)
    from slab import supervisor as supervisor_mod

    monkeypatch.setattr(supervisor_mod.supervisor, "stop", fake_stop)

    app = FastAPI()
    app.include_router(routes_link.router)
    with TestClient(app) as client:
        body = client.post("/api/link/repair").json()

    assert body["ok"] is True
    assert len(reregistered) == 2, "首次登记 + 自愈重试各一次"
    assert texts and "自愈重试" in texts[-1]
    assert any("重启中继" in step["name"] for step in body["steps"])


def test_looks_like_no_bot_matches_empty_api_not_available() -> None:
    assert routes_link._looks_like_no_bot("Failed to send message: ") is True  # noqa: SLF001
    assert routes_link._looks_like_no_bot("ApiNotAvailable") is True  # noqa: SLF001
    assert routes_link._looks_like_no_bot("Failed to send message: not friend") is False  # noqa: SLF001


def test_snowluma_probe_bridge_mode_does_not_ask_for_a_direct_client(monkeypatch, tmp_path) -> None:
    """bridge 模式下「SnowLuma 没有 wsClient」是正常状态，不能引着用户去写直连。"""

    cfg = AppConfig()
    cfg.normalise()
    cfg.link.mode = "bridge"
    cfg.snowluma.install_dir = str(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "onebot.json").write_text("{}", encoding="utf-8")

    async def fake_port_open(*_a, **_k):
        return True

    class FailingWebUI:
        def __init__(self, *_a, **_k):
            pass

        async def login(self):
            raise snowluma_admin.SnowLumaApiError("未授权")

        async def close(self):
            return None

    monkeypatch.setattr(snowluma_admin, "_port_open_async", fake_port_open)
    monkeypatch.setattr(snowluma_admin, "SnowLumaWebUI", FailingWebUI)
    monkeypatch.setattr(snowluma_admin, "list_accounts", lambda *_a, **_k: ["3985934176"])
    monkeypatch.setattr(snowluma_admin, "_process_state",
                        lambda _name: {"status": "running", "pid": 1})
    monkeypatch.setattr(snowluma_admin, "log_client_states",
                        lambda *_a, **_k: {"available": False, "source_file": "", "age": 0.0,
                                           "clients": []})
    monkeypatch.setattr(snowluma_admin, "webui_credentials",
                        lambda *_a, **_k: {"password": "pw", "username": "admin"})
    monkeypatch.setattr(snowluma_admin, "account_sources",
                        lambda *_a, **_k: {"files": [], "data_dirs": [], "logs": []})

    result = asyncio.run(snowluma_admin.probe(cfg))
    assert result["state"] == "relay"
    assert not any("写入配置" in s for s in result["suggestions"])



def test_snowluma_probe_warmup_does_not_blame_the_password(monkeypatch, tmp_path) -> None:
    """刚点完「一键启动全部」时面板还没就绪，不能提示用户去查 WebUI 密码。"""

    import time as _time

    cfg = AppConfig()
    cfg.normalise()
    cfg.link.mode = "bridge"
    cfg.snowluma.install_dir = str(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "onebot.json").write_text("{}", encoding="utf-8")

    async def fake_port_open(*_a, **_k):
        return True

    class FailingWebUI:
        def __init__(self, *_a, **_k):
            pass

        async def login(self):
            raise snowluma_admin.SnowLumaApiError("未授权")

        async def close(self):
            return None

    monkeypatch.setattr(snowluma_admin, "_port_open_async", fake_port_open)
    monkeypatch.setattr(snowluma_admin, "SnowLumaWebUI", FailingWebUI)
    monkeypatch.setattr(snowluma_admin, "list_accounts", lambda *_a, **_k: [])
    monkeypatch.setattr(snowluma_admin, "_process_state",
                        lambda _name: {"status": "running", "pid": 1,
                                       "started_at": _time.time() - 3.0})
    monkeypatch.setattr(snowluma_admin, "log_client_states",
                        lambda *_a, **_k: {"available": False, "source_file": "", "age": 0.0,
                                           "clients": []})
    monkeypatch.setattr(snowluma_admin, "webui_credentials",
                        lambda *_a, **_k: {"password": "pw", "username": "admin"})
    monkeypatch.setattr(snowluma_admin, "account_sources",
                        lambda *_a, **_k: {"files": [], "data_dirs": [], "logs": []})

    result = asyncio.run(snowluma_admin.probe(cfg))
    tips = " | ".join(result["suggestions"])
    assert "刚启动" in tips
    assert "检查 WebUI 密码" not in tips
