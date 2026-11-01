"""实现侧 WS 客户端：连入 AstrBot 的 aiocqhttp 反向 WebSocket。

链路角色与 native 模式里的 SnowLuma 完全一致：

* 握手头 ``Authorization: Bearer <token>`` + ``X-Self-ID`` + ``X-Client-Role``；
* 上来之后**发事件**（``post_type`` 帧），**收 action**（``{"action","params","echo"}``），
  按 ``echo`` 回 ``{"status","retcode","data","echo"}``。

AstrBot（aiocqhttp）的鉴权只读 ``Authorization`` 头、且要求 ``X-Client-Role``
（源码见 ``.venv/Lib/site-packages/aiocqhttp/__init__.py`` 的 ``_handle_wsr``），
所以这里既不依赖 query token，也不主动发 ``meta_event``（AstrBot 不需要）。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from typing import Any, Callable, Dict, Optional, Set, Union

from .client import BaseWsClient, OneBotActionFailed, OneBotNotConnected
from .model import OneBotEvent, action_failed, action_ok, is_action_payload

log = logging.getLogger("slab.onebot")

#: 动作处理函数：``(action, params) -> data``，同步或 async 均可
ActionHandler = Callable[[str, Dict[str, Any]], Any]


class ReverseOneBotClient(BaseWsClient):
    """以「OneBot 实现端」身份连入应用端的反向 WS 客户端。

    Args:
        url: 应用端的反向 WS 地址，例如 ``ws://127.0.0.1:6199/ws``。
        token: 与 AstrBot ``ws_reverse_token`` 一致。
        self_id: 机器人 QQ 号，需与事件里的 ``self_id`` 一致，否则应用端回发的 action 会路由错账号。
        handler: 动作处理器，也可稍后用 :meth:`on_action` 注册。
    """

    #: 未实现动作的返回码（OneBot v11: 不支持的 action）
    RETCODE_UNSUPPORTED = 1404
    #: 动作执行出错
    RETCODE_FAILED = 1400
    #: 下游连接不可用（动作送不出去）
    RETCODE_NOT_CONNECTED = 1401

    def __init__(
        self,
        *args: Any,
        handler: Optional[ActionHandler] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._action_handler: Optional[ActionHandler] = handler
        self._action_tasks: Set[asyncio.Task] = set()
        self._actions_in = 0
        self._actions_ok = 0
        self._actions_failed = 0
        self._events_out = 0
        self._last_action = ""

    # ------------------------------------------------------------- 注册
    def on_action(self, handler: ActionHandler) -> ActionHandler:
        """注册动作处理器（后注册的覆盖先注册的）。"""

        self._action_handler = handler
        return handler

    # ------------------------------------------------------------- 发送事件
    async def send_event(self, event: Union[OneBotEvent, Dict[str, Any]]) -> bool:
        """把一个事件发给应用端；未连接时返回 ``False``（由调用方决定丢弃/计数）。"""

        payload = event.to_dict() if isinstance(event, OneBotEvent) else dict(event)
        try:
            await self.send_payload(payload)
        except Exception as exc:  # noqa: BLE001 - 断线时静默丢弃，等重连
            log.debug("[%s] 事件发送失败: %s", self.name, exc)
            return False
        self._events_out += 1
        return True

    # ------------------------------------------------------------- 收发
    async def _serve(self, ws: Any) -> None:
        while True:
            raw = await ws.recv()
            text = self._decode(raw)
            if not text:
                continue
            payload = self._loads(text)
            if payload is None:
                log.debug("[%s] 收到非 JSON 帧: %s", self.name, text[:120])
                continue
            if is_action_payload(payload):
                task = asyncio.create_task(self._handle_action(payload), name=f"action-{self.name}")
                self._action_tasks.add(task)
                task.add_done_callback(self._action_tasks.discard)
                continue
            log.debug("[%s] 忽略非动作帧: %s", self.name, str(payload)[:120])

    async def _handle_action(self, payload: Dict[str, Any]) -> None:
        action = str(payload.get("action") or "")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        echo = payload.get("echo")
        self._actions_in += 1
        self._last_action = action
        if self._action_handler is None:
            self._actions_failed += 1
            await self._safe_send(action_failed(echo, f"中继未实现动作: {action}", self.RETCODE_UNSUPPORTED))
            return
        try:
            result = self._action_handler(action, dict(params or {}))
            if inspect.isawaitable(result):
                result = await result
            self._actions_ok += 1
            await self._safe_send(action_ok(echo, result))
        except asyncio.CancelledError:
            raise
        except OneBotActionFailed as exc:
            # 下游给了明确的错误码就原样透传，别让应用端只看到笼统的 1400
            self._actions_failed += 1
            log.warning("[%s] 动作 %s 失败: %s", self.name, action, exc)
            await self._safe_send(action_failed(echo, str(exc), exc.retcode or self.RETCODE_FAILED))
        except OneBotNotConnected as exc:
            self._actions_failed += 1
            log.warning("[%s] 动作 %s 无法送达: %s", self.name, action, exc)
            await self._safe_send(action_failed(echo, str(exc), self.RETCODE_NOT_CONNECTED))
        except Exception as exc:  # noqa: BLE001 - 任何异常都要回包，否则对端会一直等
            self._actions_failed += 1
            log.warning("[%s] 动作 %s 执行失败: %s: %s", self.name, action, type(exc).__name__, exc)
            await self._safe_send(action_failed(echo, f"{type(exc).__name__}: {exc}", self.RETCODE_FAILED))

    async def _safe_send(self, payload: Dict[str, Any]) -> None:
        try:
            await self.send_payload(payload)
        except Exception as exc:  # noqa: BLE001
            log.debug("[%s] 响应发送失败: %s", self.name, exc)

    # ------------------------------------------------------------- 状态
    def status(self) -> Dict[str, Any]:
        info = super().status()
        info.update(
            {
                "actions_in": self._actions_in,
                "actions_ok": self._actions_ok,
                "actions_failed": self._actions_failed,
                "events_out": self._events_out,
                "last_action": self._last_action,
            }
        )
        return info

    def _on_stopped(self) -> None:
        for task in list(self._action_tasks):
            task.cancel()

    async def stop(self) -> None:
        await super().stop()
        for task in list(self._action_tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._action_tasks.clear()
