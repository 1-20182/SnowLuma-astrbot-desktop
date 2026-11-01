"""OneBot 协议层测试（T2）：模型 + 应用侧客户端 + 实现侧（反向）客户端 + 握手探针。

全部用本机临时端口上的假 WS 服务端，不触碰真实 SnowLuma / AstrBot / QQ。
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any, Dict, List, Optional

import pytest
from websockets.asyncio.server import serve

from slab.onebot import model
from slab.onebot.client import (
    OneBotActionFailed,
    OneBotClient,
    OneBotNotConnected,
    OneBotTimeout,
    probe_ws_handshake,
)
from slab.onebot.model import OneBotEvent
from slab.onebot.reverse import ReverseOneBotClient


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def start_server(handler) -> tuple[Any, int]:
    server = await serve(handler, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    return server, port


# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #
def test_cq_roundtrip_and_escape() -> None:
    cq = "你好[CQ:at,qq=123][CQ:image,file=a.png]&[x]"
    segments = model.parse_cq_string(cq)
    assert segments[0] == {"type": "text", "data": {"text": "你好"}}
    assert segments[1] == {"type": "at", "data": {"qq": "123"}}
    assert segments[2] == {"type": "image", "data": {"file": "a.png"}}
    assert segments[3]["data"]["text"] == "&[x]"
    # 转回 CQ 码：段本身用 CQ 码表示，文本里的特殊字符必须被转义
    rebuilt = model.to_cq_string(segments)
    assert "[CQ:at,qq=123]" in rebuilt
    assert "&amp;&#91;x&#93;" in rebuilt
    # 再解析一次仍然等价
    assert model.parse_cq_string(rebuilt)[1] == {"type": "at", "data": {"qq": "123"}}


def test_build_message_and_render() -> None:
    message = model.build_message(model.reply_segment(99), model.at_segment(123), " 你好")
    assert message[0] == {"type": "reply", "data": {"id": "99"}}
    assert message[1] == {"type": "at", "data": {"qq": "123"}}
    assert model.message_to_text(message) == "[回复:99]@123 你好"
    assert model.plain_text(message) == " 你好"
    assert model.at_qq_list(message) == ["123"]


def test_event_fields_and_at_detection() -> None:
    raw = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 123456,
        "user_id": 20002,
        "self_id": 3985934176,
        "message_id": -1234567890,  # 有符号 32 位，负数合法
        "raw_message": "[CQ:at,qq=3985934176] 你好",
        "message": [
            {"type": "at", "data": {"qq": "3985934176"}},
            {"type": "text", "data": {"text": " 你好"}},
        ],
        "sender": {"user_id": 20002, "nickname": "小明", "card": "卡片名"},
        "time": 1700000000,
    }
    event = model.parse_event(raw)
    assert event.is_message and event.is_group
    assert event.self_id == "3985934176"
    assert event.user_id == "20002"
    assert event.group_id == "123456"
    assert event.message_id == "-1234567890"
    assert event.nickname == "卡片名"
    assert event.text == "@3985934176 你好"
    assert event.plain == " 你好"
    assert event.is_at() is True
    assert event.is_at("999") is False
    assert event.at_ids() == ["3985934176"]
    assert event.matches_trigger("all") is True
    assert event.matches_trigger("at_or_prefix", ["/"]) is True
    # 原样转发时 ID 类型不被改写
    assert event.to_dict()["user_id"] == 20002
    assert event.to_dict()["message_id"] == -1234567890


def test_event_trigger_rules() -> None:
    plain_group = OneBotEvent.message_event("group", 20002, "你好", self_id="10000", group_id=1)
    assert plain_group.matches_trigger("all") is True
    assert plain_group.matches_trigger("at_or_prefix", ["/"]) is False

    at_group = OneBotEvent.message_event(
        "group", 20002, model.build_message(model.at_segment(10000), "你好"),
        self_id="10000", group_id=1,
    )
    assert at_group.matches_trigger("at_or_prefix", ["/"]) is True

    prefixed = OneBotEvent.message_event("group", 20002, "/help", self_id="10000", group_id=1)
    assert prefixed.matches_trigger("at_or_prefix", ["/", "#"]) is True
    assert prefixed.starts_with(["#"]) is False

    private = OneBotEvent.message_event("private", 20002, "随便聊聊", self_id="10000")
    assert private.is_private
    assert private.matches_trigger("at_or_prefix", ["/"]) is True  # 私聊不需要 @/前缀

    self_sent = OneBotEvent.message_event("private", 10000, "hi", self_id="10000")
    assert self_sent.is_self_sent is True


def test_action_payload_helpers() -> None:
    assert model.is_action_payload({"action": "send_msg", "params": {}, "echo": "1"}) is True
    assert model.is_action_payload({"post_type": "message"}) is False
    assert model.is_event_payload({"post_type": "message"}) is True
    assert model.action_ok("e1", {"message_id": 1}) == {
        "status": "ok", "retcode": 0, "data": {"message_id": 1}, "echo": "e1"
    }
    failed = model.action_failed("e2", "boom", 1404)
    assert failed["status"] == "failed" and failed["retcode"] == 1404 and failed["echo"] == "e2"


# --------------------------------------------------------------------------- #
# 应用侧客户端
# --------------------------------------------------------------------------- #
async def test_client_call_echo_correlation() -> None:
    seen: List[Dict[str, Any]] = []

    async def handler(ws):
        async for raw in ws:
            payload = json.loads(raw)
            seen.append(payload)
            await ws.send(json.dumps({
                "status": "ok",
                "retcode": 0,
                "data": {"action": payload["action"], "params": payload.get("params")},
                "echo": payload["echo"],
            }))

    server, port = await start_server(handler)
    client = OneBotClient(f"ws://127.0.0.1:{port}/", token="tok", self_id="10000")
    try:
        await client.start()
        assert await client.wait_connected(5) is True

        async def one(action: str, params: dict) -> Any:
            return await client.call(action, params, timeout=3)

        results = await asyncio.gather(
            one("send_private_msg", {"user_id": "1", "message": "a"}),
            one("get_login_info", {}),
            one("get_group_list", {}),
        )
        assert [item["action"] for item in results] == [
            "send_private_msg", "get_login_info", "get_group_list",
        ]
        # 每次调用都有唯一的 echo
        echoes = {item["echo"] for item in seen}
        assert len(echoes) == 3
        # 握手头符合 OneBot v11
        assert client.headers()["X-Client-Role"] == "Universal"
        assert client.headers()["X-Self-ID"] == "10000"
        assert client.headers()["Authorization"] == "Bearer tok"
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()


async def test_client_timeout_and_action_failed() -> None:
    async def handler(ws):
        async for raw in ws:
            payload = json.loads(raw)
            if payload["action"] == "never_answer":
                continue
            await ws.send(json.dumps({
                "status": "failed", "retcode": 100, "data": None,
                "message": "参数错误", "echo": payload["echo"],
            }))

    server, port = await start_server(handler)
    client = OneBotClient(f"ws://127.0.0.1:{port}/", token="", name="t")
    try:
        await client.start()
        assert await client.wait_connected(5)
        with pytest.raises(OneBotTimeout):
            await client.call("never_answer", {}, timeout=0.2)
        with pytest.raises(OneBotActionFailed) as excinfo:
            await client.call("bad_action", {}, timeout=3)
        assert excinfo.value.retcode == 100
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()


async def test_client_not_connected_raises() -> None:
    client = OneBotClient(f"ws://127.0.0.1:{free_port()}/", token="")
    with pytest.raises(OneBotNotConnected):
        await client.call("get_login_info", {}, timeout=0.2)
    assert client.connected is False
    assert client.status()["state"] == "idle"


async def test_client_dispatches_events() -> None:
    events: List[OneBotEvent] = []

    async def handler(ws):
        await ws.send(json.dumps({
            "post_type": "message",
            "message_type": "private",
            "user_id": 20002,
            "self_id": 10000,
            "message": [{"type": "text", "data": {"text": "你好"}}],
            "raw_message": "你好",
            "message_id": 1,
            "time": 1700000000,
            "sender": {"user_id": 20002, "nickname": "小明"},
        }))
        await asyncio.sleep(5)

    server, port = await start_server(handler)
    client = OneBotClient(f"ws://127.0.0.1:{port}/", token="", name="t")
    client.on_event(events.append)
    try:
        await client.start()
        assert await wait_until(lambda: len(events) == 1)
        event = events[0]
        assert event.is_private
        assert event.text == "你好"
        assert event.user_id == "20002"
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()


async def test_client_reconnects_with_backoff() -> None:
    connections: List[int] = []

    async def handler(ws):
        connections.append(1)
        await ws.close()  # 立刻断开，逼客户端重连

    server, port = await start_server(handler)
    client = OneBotClient(
        f"ws://127.0.0.1:{port}/", token="", name="t",
        reconnect_base=0.05, reconnect_max=0.1,
    )
    try:
        await client.start()
        assert await wait_until(lambda: len(connections) >= 2, timeout=6)
        assert client.status()["reconnects"] >= 1
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()


# --------------------------------------------------------------------------- #
# 实现侧（反向）客户端
# --------------------------------------------------------------------------- #
async def test_reverse_client_sends_events_and_answers_actions() -> None:
    received_events: List[Dict[str, Any]] = []
    responses: List[Dict[str, Any]] = []

    async def handler(ws):
        headers = ws.request.headers
        assert headers["authorization"] == "Bearer secret"
        assert headers["x-self-id"] == "3985934176"
        assert headers["x-client-role"].lower() == "universal"
        received_events.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({
            "action": "send_private_msg",
            "params": {"user_id": 20002, "message": [{"type": "text", "data": {"text": "hi"}}],
                       "self_id": 3985934176},
            "echo": "e-1",
        }))
        responses.append(json.loads(await ws.recv()))

    async def handle_action(action: str, params: Dict[str, Any]) -> Any:
        assert action == "send_private_msg"
        assert params["user_id"] == 20002
        return {"message_id": 42}

    server, port = await start_server(handler)
    client = ReverseOneBotClient(
        f"ws://127.0.0.1:{port}/ws",
        token="secret",
        self_id="3985934176",
        name="rev",
        handler=handle_action,
    )
    try:
        await client.start()
        assert await client.wait_connected(5)
        event = OneBotEvent.message_event("private", 20002, "你好", self_id="3985934176")
        assert await client.send_event(event) is True
        assert await wait_until(lambda: len(responses) == 1)

        assert received_events[0]["post_type"] == "message"
        assert received_events[0]["self_id"] == "3985934176"
        assert responses[0] == {"status": "ok", "retcode": 0, "data": {"message_id": 42}, "echo": "e-1"}
        assert client.status()["actions_ok"] == 1
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()


async def test_reverse_client_returns_error_response() -> None:
    responses: List[Dict[str, Any]] = []

    async def handler(ws):
        await ws.send(json.dumps({"action": "do_fail", "params": {}, "echo": "e-2"}))
        responses.append(json.loads(await ws.recv()))
        await asyncio.sleep(3)  # 保持连接，避免客户端在断言前就重连

    def boom(_action: str, _params: Dict[str, Any]) -> Any:
        raise RuntimeError("下游炸了")

    server, port = await start_server(handler)
    client = ReverseOneBotClient(f"ws://127.0.0.1:{port}/ws", token="", name="rev", handler=boom)
    try:
        await client.start()
        assert await client.wait_connected(5)
        assert await wait_until(lambda: len(responses) == 1)
        payload = responses[0]
        assert payload["status"] == "failed"
        assert payload["retcode"] == 1400
        assert payload["echo"] == "e-2"
        assert "下游炸了" in payload["message"]
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()


async def test_reverse_client_unsupported_action() -> None:
    responses: List[Dict[str, Any]] = []

    async def handler(ws):
        await ws.send(json.dumps({"action": "unknown_action", "params": {}, "echo": "e-3"}))
        responses.append(json.loads(await ws.recv()))
        await asyncio.sleep(3)

    server, port = await start_server(handler)
    client = ReverseOneBotClient(f"ws://127.0.0.1:{port}/ws", token="", name="rev")
    try:
        await client.start()
        assert await client.wait_connected(5)
        assert await wait_until(lambda: len(responses) == 1)
        assert responses[0]["retcode"] == 1404
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()


# --------------------------------------------------------------------------- #
# 握手探针
# --------------------------------------------------------------------------- #
async def test_probe_ws_handshake_success() -> None:
    async def handler(ws):
        await asyncio.sleep(0.5)

    server, port = await start_server(handler)
    try:
        result = await probe_ws_handshake(
            f"ws://127.0.0.1:{port}/ws",
            {"Authorization": "Bearer tok", "X-Self-ID": "1", "X-Client-Role": "universal"},
            timeout=3.0,
        )
        assert result["ok"] is True
        assert result["status_code"] == 101
    finally:
        server.close()
        await server.wait_closed()


async def test_probe_ws_handshake_connection_refused() -> None:
    result = await probe_ws_handshake(
        f"ws://127.0.0.1:{free_port()}/ws", {"X-Self-ID": "1"}, timeout=1.0
    )
    assert result["ok"] is False
    assert result["status_code"] == 0
    assert "连不上" in result["detail"]


async def test_probe_ws_handshake_token_rejected() -> None:
    """模仿 aiocqhttp：token 不对直接 403。探针要能把「token 错」与「没启动」区分开。"""

    def process_request(connection, request):  # noqa: ANN001
        if request.headers.get("authorization") != "Bearer right-token":
            return connection.respond(403, "Forbidden\n")
        return None

    async def handler(ws):
        await asyncio.sleep(0.5)

    server = await serve(handler, "127.0.0.1", 0, process_request=process_request)
    port = int(server.sockets[0].getsockname()[1])
    try:
        bad = await probe_ws_handshake(
            f"ws://127.0.0.1:{port}/ws",
            {"Authorization": "Bearer wrong-token", "X-Self-ID": "1"},
            timeout=2.0,
        )
        assert bad["ok"] is False
        assert bad["status_code"] == 403
        assert "token" in bad["detail"]

        good = await probe_ws_handshake(
            f"ws://127.0.0.1:{port}/ws",
            {"Authorization": "Bearer right-token", "X-Self-ID": "1"},
            timeout=2.0,
        )
        assert good["ok"] is True
        assert good["status_code"] == 101
    finally:
        server.close()
        await server.wait_closed()
