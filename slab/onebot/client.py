"""OneBot v11 WebSocket 客户端（应用侧 / 中继侧通用底座）。

两种方向
--------
* :class:`OneBotClient`（本文件）——**应用侧**：连到 OneBot 实现端（SnowLuma 的 WS 服务端），
  收事件、发 ``action`` 并按 ``echo`` 关联 ``response``。
* :class:`slab.onebot.reverse.ReverseOneBotClient`——**实现侧**：连到应用端（AstrBot 的
  aiocqhttp 反向 WS），发事件、收 ``action`` 并回 ``response``。

共同能力（:class:`BaseWsClient`）：Bearer/query 两种 token 传递方式、
指数退避自动重连、连接状态属性、事件循环内无阻塞、可注入 ``connector`` 以便单测。

安全说明：``status()`` 返回的 URL 里 token 会被掩码，不会泄露到前端/日志。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence

from .model import OneBotEvent, is_event_payload

log = logging.getLogger("slab.onebot")

try:  # websockets >= 13 的新 asyncio 实现（本项目锁定 17.x）
    from websockets.asyncio.client import connect as _ws_connect
except ImportError:  # pragma: no cover - 兼容旧版本
    from websockets.client import connect as _ws_connect  # type: ignore[no-redef]


def _header_kwarg(func: Any) -> str:
    """老版本 websockets 用 ``extra_headers``，新版本用 ``additional_headers``。"""

    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - 内置/装饰过的可调用对象
        return "additional_headers"
    if "additional_headers" in params:
        return "additional_headers"
    if "extra_headers" in params:  # pragma: no cover
        return "extra_headers"
    return "additional_headers"


_HEADER_KWARG = _header_kwarg(_ws_connect)

EventHandler = Callable[[OneBotEvent], Any]
StateHandler = Callable[["BaseWsClient"], Any]


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class OneBotError(RuntimeError):
    """OneBot 层通用异常。"""


class OneBotNotConnected(OneBotError):
    """当前没有可用连接（动作无法送达）。"""


class OneBotTimeout(OneBotError):
    """等待 action 响应超时。"""


class OneBotDisconnected(OneBotError):
    """等待 action 响应期间连接断开。"""


class OneBotActionFailed(OneBotError):
    """对端返回了失败响应（``status != ok`` 或 ``retcode`` 非 0/1）。"""

    def __init__(self, action: str, message: str, *, retcode: int = -1, data: Any = None) -> None:
        super().__init__(f"{action} 失败(retcode={retcode}): {message}")
        self.action = action
        self.retcode = retcode
        self.data = data


# --------------------------------------------------------------------------- #
# 探针
# --------------------------------------------------------------------------- #
async def probe_ws_handshake(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    *,
    timeout: float = 3.0,
) -> Dict[str, Any]:
    """只做一次 WS 握手就断开，用于判断"对端在不在 / token 对不对"。

    这条探针独立于 SnowLuma 与 QQ，适合做 ``/api/link/status`` 里 OneBot 服务端的指示灯：

    * ``status_code == 101``：服务在、token 正确（握手成功）；
    * ``401`` / ``403``：服务在，但没带 token / token 不匹配（aiocqhttp 的语义）；
    * 连不上（``status_code == 0``）：端口没监听或适配器没起来。

    Returns:
        ``{"ok": bool, "status_code": int, "url": str, "detail": str, "error": str}``
    """

    kwargs: Dict[str, Any] = {_HEADER_KWARG: dict(headers or {}), "open_timeout": timeout,
                              "ping_interval": None}
    try:
        async with _ws_connect(url, **kwargs) as ws:
            await ws.close()
        return {"ok": True, "status_code": 101, "url": url,
                "detail": "WebSocket 握手成功（服务在线且 token 正确）", "error": ""}
    except Exception as exc:  # noqa: BLE001 - 探针不抛异常，只翻译
        response = getattr(exc, "response", None)
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code in (401, 403):
            detail = f"对端在线但拒绝握手（HTTP {status_code}：token 不匹配或未携带）"
        elif status_code:
            detail = f"对端在线但握手失败（HTTP {status_code}）"
        else:
            detail = f"连不上 {url}（服务未启动或端口不对）"
        return {"ok": False, "status_code": status_code, "url": url, "detail": detail,
                "error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- #
# 底座
# --------------------------------------------------------------------------- #
class BaseWsClient:
    """带自动重连的 OneBot WS 连接底座。

    Args:
        url: ``ws://host:port/path``；query 里的 token 由 :attr:`token_in_query` 决定是否追加。
        token: 访问令牌；以 ``Authorization: Bearer`` 头发送。
        self_id: 机器人 QQ 号，写进 ``X-Self-ID``。
        role: ``universal`` / ``api`` / ``event``，写进 ``X-Client-Role``。
        token_in_query: 是否同时把 token 放进查询参数（兼容只读 query 的实现）。
        connector: 可注入的连接工厂，签名为 ``(url, headers, **kwargs) -> AsyncContextManager``；
            默认使用 :func:`websockets.asyncio.client.connect`。单测可注入假连接。
        reconnect_base / reconnect_max: 指数退避的起始/上限秒数。
    """

    #: 重连退避上限（秒）
    DEFAULT_RECONNECT_MAX = 30.0

    def __init__(
        self,
        url: str,
        *,
        token: str = "",
        self_id: str = "",
        role: str = "universal",
        token_in_query: bool = False,
        query_token_param: str = "access_token",
        connector: Optional[Callable[..., Any]] = None,
        name: str = "onebot",
        ping_interval: float = 30.0,
        ping_timeout: float = 10.0,
        open_timeout: float = 10.0,
        reconnect_base: float = 1.0,
        reconnect_max: float = DEFAULT_RECONNECT_MAX,
        max_size: Optional[int] = None,
    ) -> None:
        self.url = str(url or "")
        self.token = str(token or "")
        self.self_id = str(self_id or "")
        self.role = (str(role or "universal")).lower()
        self.token_in_query = bool(token_in_query)
        self.query_token_param = query_token_param
        self.name = name
        self._connector = connector or self._default_connector
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._open_timeout = open_timeout
        self._reconnect_base = max(0.05, float(reconnect_base))
        self._reconnect_max = max(self._reconnect_base, float(reconnect_max))
        self._max_size = max_size

        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._stopping = False
        self._ws: Any = None

        self._state = "idle"  # idle|connecting|connected|disconnected|reconnecting|stopped
        self._last_error = ""
        self._connected_at: Optional[float] = None
        self._connects = 0
        self._reconnects = 0
        self._dropped = 0

        self._event_handlers: List[EventHandler] = []
        self._connect_handlers: List[StateHandler] = []
        self._disconnect_handlers: List[StateHandler] = []

    # ------------------------------------------------------------- 连接参数
    @property
    def url_with_query(self) -> str:
        """实际连接用的 URL（必要时追加 ``access_token`` 查询参数）。"""

        if not (self.token and self.token_in_query):
            return self.url
        sep = "&" if "?" in self.url else "?"
        return f"{self.url}{sep}{self.query_token_param}={self.token}"

    @property
    def masked_url(self) -> str:
        """掩码后的 URL（展示/日志用，绝不带 token）。"""

        return self.url

    def headers(self) -> Dict[str, str]:
        """OneBot v11 握手头（与 SnowLuma wsClient 完全一致）。"""

        headers = {
            "User-Agent": "OneBot/11",
            "X-Client-Role": self.role.capitalize(),  # universal -> Universal
        }
        if self.self_id:
            headers["X-Self-ID"] = self.self_id
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _default_connector(self, url: str, headers: Dict[str, str], **kwargs: Any) -> Any:
        opts: Dict[str, Any] = {
            _HEADER_KWARG: headers,
            "open_timeout": kwargs.get("open_timeout", self._open_timeout),
            "ping_interval": kwargs.get("ping_interval", self._ping_interval),
            "ping_timeout": kwargs.get("ping_timeout", self._ping_timeout),
        }
        if self._max_size is not None:
            opts["max_size"] = self._max_size
        return _ws_connect(url, **opts)

    # ------------------------------------------------------------- 状态
    @property
    def connected(self) -> bool:
        return self._state == "connected" and self._ws is not None

    @property
    def state(self) -> str:
        return self._state

    @property
    def last_error(self) -> str:
        return self._last_error

    def status(self) -> Dict[str, Any]:
        """连接状态快照（给 ``/api/link/status`` 与日志用）。"""

        return {
            "name": self.name,
            "url": self.masked_url,
            "role": self.role,
            "self_id": self.self_id,
            "state": self._state,
            "connected": self.connected,
            "connected_at": self._connected_at,
            "uptime": round(time.time() - self._connected_at, 1) if self._connected_at else 0.0,
            "connects": self._connects,
            "reconnects": self._reconnects,
            "dropped": self._dropped,
            "last_error": self._last_error,
        }

    # ------------------------------------------------------------- 回调注册
    def on_event(self, handler: EventHandler) -> EventHandler:
        """注册事件回调（同步或 ``async def`` 均可）。"""

        self._event_handlers.append(handler)
        return handler

    def on_connect(self, handler: StateHandler) -> StateHandler:
        self._connect_handlers.append(handler)
        return handler

    def on_disconnect(self, handler: StateHandler) -> StateHandler:
        self._disconnect_handlers.append(handler)
        return handler

    async def _notify(self, handlers: Sequence[Callable[[Any], Any]], payload: Any, label: str) -> None:
        for handler in list(handlers):
            try:
                result = handler(payload)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - 回调异常不能打断连接
                log.warning("[%s] %s 回调失败: %s: %s", self.name, label, type(exc).__name__, exc)

    # ------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        """启动后台连接任务（幂等）。"""

        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._stop_event = asyncio.Event()
        self._state = "connecting"
        self._task = asyncio.create_task(self._run(), name=f"onebot-{self.name}")

    async def stop(self) -> None:
        """停止连接任务并关闭底层连接（幂等）。"""

        self._stopping = True
        self._stop_event.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._close_connection()
        self._state = "stopped"
        self._on_stopped()

    async def wait_connected(self, timeout: Optional[float] = 10.0) -> bool:
        """等待连接就绪；超时返回 ``False``（不抛异常，便于状态页使用）。"""

        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.connected:
            if deadline is not None and time.monotonic() > deadline:
                return False
            if self._task is not None and self._task.done() and not self.connected:
                return False
            await asyncio.sleep(0.02)
        return True

    async def _close_connection(self) -> None:
        ws, self._ws = self._ws, None
        if ws is None:
            return
        with contextlib.suppress(Exception):
            await ws.close()

    # ------------------------------------------------------------- 主循环
    def _backoff(self, attempt: int) -> float:
        """指数退避：base * 2^(attempt-1)，带 ±20% 抖动，封顶 reconnect_max。"""

        raw = self._reconnect_base * (2 ** max(0, attempt - 1))
        jitter = raw * 0.2 * (uuid.uuid4().int % 100 / 100.0)
        return min(self._reconnect_max, raw + jitter)

    async def _sleep_or_stop(self, delay: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)

    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            self._state = "connecting"
            stack = contextlib.AsyncExitStack()
            try:
                ws = await stack.enter_async_context(
                    self._connector(
                        self.url_with_query,
                        self.headers(),
                        open_timeout=self._open_timeout,
                        ping_interval=self._ping_interval,
                        ping_timeout=self._ping_timeout,
                    )
                )
            except asyncio.CancelledError:
                await stack.aclose()
                raise
            except Exception as exc:  # noqa: BLE001 - 连不上就退避重试
                await stack.aclose()
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._state = "reconnecting"
                attempt += 1
                delay = self._backoff(attempt)
                log.warning("[%s] 连接 %s 失败(%s)，%.1fs 后重试", self.name, self.masked_url,
                            self._last_error, delay)
                await self._sleep_or_stop(delay)
                continue

            attempt = 0
            self._ws = ws
            self._state = "connected"
            self._connected_at = time.time()
            self._connects += 1
            self._last_error = ""
            log.info("[%s] 已连接 %s", self.name, self.masked_url)
            await self._notify(self._connect_handlers, self, "connect")
            try:
                await self._serve(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.warning("[%s] 连接异常: %s", self.name, self._last_error)
            finally:
                self._ws = None
                self._connected_at = None
                self._state = "disconnected"
                await stack.aclose()
                self._on_disconnected()
                await self._notify(self._disconnect_handlers, self, "disconnect")
            if self._stopping:
                return
            attempt += 1
            self._reconnects += 1
            delay = self._backoff(attempt)
            log.info("[%s] 连接已断开，%.1fs 后重连", self.name, delay)
            await self._sleep_or_stop(delay)

    # ------------------------------------------------------ 子类需实现
    async def _serve(self, ws: Any) -> None:
        """在一条已建立的连接上跑收发循环，直到连接断开。"""

        raise NotImplementedError

    def _on_disconnected(self) -> None:
        """连接断开时的清理钩子（子类覆盖，用于失败挂起的请求）。"""

    def _on_stopped(self) -> None:
        """``stop()`` 完成后的清理钩子。"""

    # ------------------------------------------------------------- 发送
    async def send_payload(self, payload: Dict[str, Any]) -> None:
        """发送一帧 JSON；未连接时抛 :class:`OneBotNotConnected`。"""

        ws = self._ws
        if ws is None or self._state != "connected":
            raise OneBotNotConnected(f"{self.name} 尚未连接")
        await ws.send(json.dumps(payload, ensure_ascii=False))

    @staticmethod
    def _decode(raw: Any) -> str:
        if isinstance(raw, (bytes, bytearray)):
            return raw.decode("utf-8", "replace")
        return str(raw)

    @staticmethod
    def _loads(text: str) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None


# --------------------------------------------------------------------------- #
# 应用侧客户端
# --------------------------------------------------------------------------- #
class OneBotClient(BaseWsClient):
    """面向 OneBot 实现端（例如 SnowLuma ``ws-default``）的应用侧客户端。

    用法::

        client = OneBotClient("ws://127.0.0.1:3001/", token="...", self_id="10000")
        client.on_event(lambda evt: print(evt.summary()))
        await client.start()
        await client.wait_connected(5)
        await client.call("send_private_msg", {"user_id": 1, "message": "hi"})
    """

    def __init__(self, *args: Any, action_timeout: float = 15.0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.action_timeout = float(action_timeout)
        self._pending: Dict[str, asyncio.Future] = {}
        self._echo_seq = 0

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
            await self._handle_payload(payload)

    async def _handle_payload(self, payload: Dict[str, Any]) -> None:
        echo = payload.get("echo")
        if echo is not None and echo in self._pending:
            future = self._pending.pop(echo)
            if not future.done():
                future.set_result(payload)
            return
        if is_event_payload(payload):
            event = OneBotEvent.parse(payload)
            await self._notify(self._event_handlers, event, "event")
            return
        log.debug("[%s] 忽略未知帧: %s", self.name, str(payload)[:120])

    # ------------------------------------------------------------- 调用
    async def call_raw(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """发送 action 并返回**完整响应载荷**（``{status,retcode,data,echo}``）。"""

        if not self.connected:
            raise OneBotNotConnected(f"{self.name} 尚未连接，无法调用 {action}")
        self._echo_seq += 1
        echo = f"slab-{self.name}-{self._echo_seq}-{uuid.uuid4().hex[:6]}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            await self.send_payload({"action": action, "params": params or {}, "echo": echo})
            return await asyncio.wait_for(future, timeout=timeout or self.action_timeout)
        except asyncio.TimeoutError as exc:
            raise OneBotTimeout(f"{action} 超时（{timeout or self.action_timeout:.0f}s 无响应）") from exc
        finally:
            self._pending.pop(echo, None)

    async def call(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """发送 action 并返回 ``data``；失败抛 :class:`OneBotActionFailed`。"""

        payload = await self.call_raw(action, params, timeout=timeout)
        retcode = payload.get("retcode")
        status = str(payload.get("status") or "").lower()
        failed = status in ("failed", "error") or (retcode is not None and int(retcode) not in (0, 1))
        if failed:
            message = str(payload.get("message") or payload.get("wording") or status or "未知错误")
            raise OneBotActionFailed(action, message, retcode=int(retcode or -1), data=payload.get("data"))
        return payload.get("data")

    # ------------------------------------------------------------- 清理
    def _on_disconnected(self) -> None:
        self._fail_pending()

    def _on_stopped(self) -> None:
        self._fail_pending()

    def _fail_pending(self) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(OneBotDisconnected(f"{self.name} 连接已断开"))
        self._dropped += len(pending) if pending else 0
