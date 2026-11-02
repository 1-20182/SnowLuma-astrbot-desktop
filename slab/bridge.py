"""内置中继（bridge 模式）：把 SnowLuma 的 QQ 事件转给 AstrBot，把 AstrBot 的回复转回 QQ。

链路
----
::

    QQ ⇄ SnowLuma(OneBot 实现端)
             │  ① bridge 作为「OneBot 应用端」连 SnowLuma 的 wsServers（默认 3001）
             │     收事件、发 action
             ▼
          中继 Bridge
             │  ② bridge 作为「OneBot 实现端」连 AstrBot 的 aiocqhttp 反向 WS（默认 6199）
             │     发事件、收 action 并按 echo 回 response
             ▼
        AstrBot(LLM)

与 native 模式的差别：native 让 SnowLuma 直接连 AstrBot（少一跳、延迟更低），
bridge 多一层但能在中间做过滤、审计与"无 QQ 也能联调"。两者共用同一套 token/端口。

本模块同时提供：
* :func:`register_service` —— 把中继注册成 ``kind="task"`` 的服务（随控制台进程生命周期）；
* :func:`run_forever` —— ``python -m slab bridge`` 前台调试；
* :func:`link_status` / :func:`apply_link` / :func:`test_send` / :func:`whoami` —— 供 REST 路由复用。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import astrbot_admin, snowluma_admin
from .config import AppConfig, get_config
from .events import bus
from .onebot.actions import OneBotActions
from .onebot.client import OneBotClient, OneBotError, OneBotNotConnected
from .onebot.model import OneBotEvent, message_to_text
from .onebot.reverse import ReverseOneBotClient
from .supervisor import ServiceDef, port_open, supervisor

log = logging.getLogger("slab.bridge")

SERVICE_NAME = "bridge"
#: 中继等待 action 响应的默认超时（秒）
DEFAULT_ACTION_TIMEOUT = 20.0
#: 事件审计保留条数
MAX_AUDIT = 50
#: 同一条消息（同 self_id + message_id）的去重窗口（秒）
DEDUPE_WINDOW = 5.0
#: 去重表最多记多少条 message_id
MAX_DEDUPE = 512


# --------------------------------------------------------------------------- #
# 过滤规则
# --------------------------------------------------------------------------- #
@dataclass
class BridgeFilters:
    """事件过滤规则（对应 ``config.link.*``）。"""

    allow_private: bool = True
    allow_group: bool = True
    trigger_mode: str = "at_or_prefix"  # all | at_or_prefix
    prefixes: List[str] = field(default_factory=lambda: ["/"])
    ignore_self: bool = True
    forward_non_text: bool = True

    @classmethod
    def from_cfg(cls, cfg: AppConfig) -> "BridgeFilters":
        link = cfg.link
        return cls(
            allow_private=bool(link.allow_private),
            allow_group=bool(link.allow_group),
            trigger_mode=str(link.trigger_mode or "at_or_prefix"),
            prefixes=[str(p) for p in (link.trigger_prefixes or []) if str(p)],
            ignore_self=bool(link.ignore_self),
            forward_non_text=bool(link.forward_non_text),
        )

    def decide(self, event: OneBotEvent) -> Tuple[bool, str]:
        """返回 ``(是否转发, 原因)``；原因用于日志/审计（便于用户自查为什么没回）。"""

        if not event.is_message:
            return True, "非消息事件（通知/请求/元事件）直接转发"
        if self.ignore_self and event.is_self_sent:
            return False, "机器人自己发出的消息"
        if event.is_group and not self.allow_group:
            return False, "已在配置里关闭群消息"
        if event.is_private and not self.allow_private:
            return False, "已在配置里关闭私聊消息"
        if not self.forward_non_text and not event.plain.strip() and not event.is_at():
            return False, "纯非文本消息（图片/表情等）且已关闭非文本转发"
        if not event.matches_trigger(self.trigger_mode, self.prefixes):
            prefixes = "、".join(self.prefixes) or "（未设置）"
            return False, f"群消息既没有 @机器人 也不以 {prefixes} 开头（触发方式：@ 或前缀）"
        return True, ""


# --------------------------------------------------------------------------- #
# 中继本体
# --------------------------------------------------------------------------- #
class Bridge:
    """双向中继主体。

    Args:
        cfg: 控制台配置。
        upstream: 注入用的「面向 AstrBot」客户端（默认按配置创建）。
        downstream: 注入用的「面向 SnowLuma」客户端（默认按配置创建）。
        event_bus: 注入用事件总线（默认全局 ``bus``）。
    """

    def __init__(
        self,
        cfg: AppConfig,
        *,
        upstream: Optional[ReverseOneBotClient] = None,
        downstream: Optional[OneBotClient] = None,
        event_bus: Any = None,
        action_timeout: float = DEFAULT_ACTION_TIMEOUT,
    ) -> None:
        self.cfg = cfg
        self.filters = BridgeFilters.from_cfg(cfg)
        self.bus = event_bus if event_bus is not None else bus
        self.action_timeout = float(action_timeout)
        self.self_id = self._resolve_self_id(cfg)
        self.started_at: Optional[float] = None
        self.stats: Dict[str, Any] = {
            "events_in": 0,
            "events_forwarded": 0,
            "events_dropped": 0,
            "events_duplicate": 0,
            "actions_in": 0,
            "actions_ok": 0,
            "actions_failed": 0,
            "last_drop_reason": "",
            "last_event_at": 0.0,
            "last_action_at": 0.0,
        }
        self.recent: List[Dict[str, Any]] = []
        #: 同一条消息只转发一次。SnowLuma 里若同时存在 native 直连 wsClient 与中继
        #: wsServer（或中继被启动了两份），同一个 message_id 会到两次；转发两次
        #: AstrBot 就处理两遍、回两遍 —— 用户看到的是「发一条，机器人回两条」。
        self.dedupe_window = DEDUPE_WINDOW
        self._seen_messages: Dict[str, float] = {}
        self._stop_event = asyncio.Event()
        self._upstream_started = False
        self._stopping = False

        link = cfg.link
        self.upstream = upstream or ReverseOneBotClient(
            f"ws://{link.ws_host}:{int(link.ws_port)}/ws",
            token=str(link.token or ""),
            self_id=self.self_id,
            role="universal",
            name="bridge→astrbot",
            handler=self.handle_action,
        )
        self.downstream = downstream or OneBotClient(
            snowluma_admin.relay_ws_url(cfg),
            token=snowluma_admin.relay_token(cfg),
            self_id=self.self_id,
            role="universal",
            name="bridge→snowluma",
            action_timeout=self.action_timeout,
        )
        self.downstream.on_event(self._on_downstream_event)
        self.actions = OneBotActions(self.downstream)

    # ------------------------------------------------------------- 初始化
    @staticmethod
    def _resolve_self_id(cfg: AppConfig) -> str:
        """确定中继"代表哪个 QQ 号"：配置里手填的优先，其次账号发现，最后留空待事件里取。"""

        configured = str(cfg.snowluma.qq_uin or "").strip()
        if configured:
            return configured
        try:
            accounts = snowluma_admin.list_accounts(cfg)
        except Exception:  # noqa: BLE001 - 账号发现失败不影响启动
            accounts = []
        return accounts[0] if accounts else ""

    # ------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        """启动两条连接；失败不抛（各自内部会退避重连）。"""

        if self.started_at:
            return
        self.started_at = time.time()
        self._stop_event = asyncio.Event()
        self._log(
            f"中继启动：SnowLuma={snowluma_admin.relay_ws_url(self.cfg)} ↔ "
            f"AstrBot=ws://{self.cfg.link.ws_host}:{int(self.cfg.link.ws_port)}/ws "
            f"(self_id={self.self_id or '待定'})"
        )
        await self.downstream.start()
        if self.self_id:
            await self.upstream.start()
            self._upstream_started = True
        else:
            # AstrBot/aiocqhttp 用握手头里的 X-Self-ID 路由 action，没有账号就先不连，
            # 等第一条事件把 self_id "认领"下来再连（否则对端会 500/路由失败）。
            self._log("尚未确定 QQ 号，等第一条事件后再连接 AstrBot", level="warning")
        self.bus.publish("link", action="bridge_start", self_id=self.self_id,
                         relay_url=snowluma_admin.relay_ws_url(self.cfg))

    async def _ensure_upstream_started(self) -> None:
        if self._upstream_started or self._stopping:
            return
        self._upstream_started = True
        await self.upstream.start()

    async def stop(self) -> None:
        """停止两条连接。"""

        if not self.started_at:
            return
        self._stopping = True
        await self.upstream.stop()
        await self.downstream.stop()
        self.started_at = None
        self._stop_event.set()
        self._log("中继已停止")
        self.bus.publish("link", action="bridge_stop")

    async def wait_closed(self) -> None:
        """阻塞直到 :meth:`stop`（供 ``task_factory`` 使用）。"""

        await self._stop_event.wait()

    @property
    def running(self) -> bool:
        return bool(self.started_at)

    # ------------------------------------------------------------- 事件方向
    def _on_downstream_event(self, event: OneBotEvent) -> None:
        """SnowLuma → 中继 → AstrBot（同步回调，转发任务异步调度）。"""

        self.stats["events_in"] += 1
        self.stats["last_event_at"] = time.time()

        if not self.self_id and event.self_id:
            # 没配置账号时，按第一条事件"认领"账号（AstrBot 按 X-Self-ID 路由 action）
            self.self_id = event.self_id
            self.upstream.self_id = event.self_id
            self.downstream.self_id = event.self_id
            self._log(f"自动采用账号 {event.self_id}（如需固定请填写 QQ 号）")
            task = asyncio.create_task(self._ensure_upstream_started(), name="bridge-upstream")
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
        elif self.self_id and event.self_id and event.self_id != self.self_id:
            self.stats["events_dropped"] += 1
            self.stats["last_drop_reason"] = f"其它账号 {event.self_id} 的事件（中继只服务 {self.self_id}）"
            return

        allowed, reason = self.filters.decide(event)
        if not allowed:
            self.stats["events_dropped"] += 1
            self.stats["last_drop_reason"] = reason
            if event.is_message:
                self._record(direction="in", event=event, forwarded=False, reason=reason)
            return

        duplicate, seen_reason = self._is_duplicate(event)
        if duplicate:
            self.stats["events_dropped"] += 1
            self.stats["events_duplicate"] += 1
            self.stats["last_drop_reason"] = seen_reason
            self._record(direction="in", event=event, forwarded=False, reason=seen_reason)
            return

        task = asyncio.create_task(self._forward_event(event), name="bridge-forward")
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    def _is_duplicate(self, event: OneBotEvent) -> Tuple[bool, str]:
        """同一条消息（同 self_id + message_id）在窗口内只放行一次。

        SnowLuma 只要同时存在 native 直连 wsClient 与中继 wsServer（或中继被
        启动了两份），同一个 ``message_id`` 就会到我们两次；放行两次 AstrBot 就
        回两遍。这里做最后一道防线，正常情况下由「中继模式清理残留直连」根治。
        """

        if not event.is_message or not event.message_id or self.dedupe_window <= 0:
            return False, ""
        key = f"{event.self_id or self.self_id}:{event.message_id}"
        now = time.time()
        last = self._seen_messages.get(key)
        if last is not None and (now - last) < self.dedupe_window:
            return True, f"重复事件（message_id={event.message_id} 已转发过，疑似存在直连+中继双通道）"
        self._seen_messages[key] = now
        if len(self._seen_messages) > MAX_DEDUPE:
            cutoff = now - self.dedupe_window
            self._seen_messages = {
                item_key: ts for item_key, ts in self._seen_messages.items() if ts >= cutoff
            }
            while len(self._seen_messages) > MAX_DEDUPE:
                self._seen_messages.pop(next(iter(self._seen_messages)))
        return False, ""

    async def _forward_event(self, event: OneBotEvent) -> None:
        ok = await self.upstream.send_event(event)
        if not ok and not self._stopping:
            # 可能是刚认领账号、连接还在建立：给它 2 秒再试一次，避免首条消息丢失
            await self._ensure_upstream_started()
            if await self.upstream.wait_connected(2.0):
                ok = await self.upstream.send_event(event)
        if ok:
            self.stats["events_forwarded"] += 1
            self._record(direction="in", event=event, forwarded=True)
            if event.is_message:
                self.bus.publish(
                    "message",
                    direction="in",
                    platform="qq",
                    user=event.user_id,
                    group=event.group_id,
                    text=event.text,
                    message_type=event.message_type,
                    nickname=event.nickname,
                )
        else:
            self.stats["events_dropped"] += 1
            self.stats["last_drop_reason"] = "AstrBot 连接未就绪，事件被丢弃"
            self._record(direction="in", event=event, forwarded=False,
                         reason="AstrBot 连接未就绪")

    # ------------------------------------------------------------- 动作方向
    async def handle_action(self, action: str, params: Dict[str, Any]) -> Any:
        """AstrBot → 中继 → SnowLuma：执行 action 并把 ``data`` 回给 AstrBot。

        注意 AstrBot（aiocqhttp）会在 ``params`` 里带 ``self_id``，OneBot 规范里没有这个参数，
        部分实现会因此校验失败，所以这里取出用于核对后**剔除**。
        """

        self.stats["actions_in"] += 1
        self.stats["last_action_at"] = time.time()
        payload = dict(params or {})
        event_self_id = str(payload.pop("self_id", "") or "")
        if event_self_id and self.self_id and event_self_id != self.self_id:
            self.stats["actions_failed"] += 1
            raise OneBotError(
                f"动作 {action} 的 self_id={event_self_id} 与中继账号 {self.self_id} 不一致"
            )
        try:
            data = await self.downstream.call(action, payload, timeout=self.action_timeout)
        except OneBotError as exc:
            self.stats["actions_failed"] += 1
            self._audit_failure(action, payload, str(exc))
            raise
        except Exception as exc:  # noqa: BLE001
            self.stats["actions_failed"] += 1
            self._audit_failure(action, payload, f"{type(exc).__name__}: {exc}")
            raise OneBotError(f"{type(exc).__name__}: {exc}") from exc
        self.stats["actions_ok"] += 1
        if action.startswith("send_"):
            self._record_action(action, payload, ok=True)
            self.bus.publish(
                "message",
                direction="out",
                platform="qq",
                ok=True,
                user=str(payload.get("user_id") or ""),
                group=str(payload.get("group_id") or ""),
                text=message_to_text(payload.get("message") or ""),
                action=action,
            )
        return data

    def _audit_failure(self, action: str, params: Dict[str, Any], error: str) -> None:
        """把失败的 action 也写进审计与事件总线（用户在 UI 能看到"发送失败"）。"""

        info = {
            "direction": "out",
            "forwarded": False,
            "ok": False,
            "action": action,
            "error": str(error)[:300],
            "user": str(params.get("user_id") or ""),
            "group": str(params.get("group_id") or ""),
            "text": message_to_text(params.get("message") or "")[:200],
        }
        item = {"ts": time.time(), **info}
        self.recent.append(item)
        del self.recent[:-MAX_AUDIT]
        with contextlib.suppress(Exception):
            self.bus.publish("message", platform="qq", **info)

    # ------------------------------------------------------------- 审计
    def _record(self, *, direction: str, event: OneBotEvent, forwarded: bool, reason: str = "") -> None:
        item = {
            "ts": time.time(),
            "direction": direction,
            "forwarded": forwarded,
            "reason": reason,
            "post_type": event.post_type,
            "message_type": event.message_type,
            "user": event.user_id,
            "group": event.group_id,
            "nickname": event.nickname,
            "text": event.text[:200],
        }
        self.recent.append(item)
        del self.recent[:-MAX_AUDIT]

    def _record_action(self, action: str, params: Dict[str, Any], *, ok: bool) -> None:
        self.recent.append(
            {
                "ts": time.time(),
                "direction": "out",
                "forwarded": ok,
                "action": action,
                "user": str(params.get("user_id") or ""),
                "group": str(params.get("group_id") or ""),
                "text": message_to_text(params.get("message") or "")[:200],
            }
        )
        del self.recent[:-MAX_AUDIT]

    def _log(self, message: str, level: str = "info") -> None:
        log.info(message) if level == "info" else log.warning(message)
        with contextlib.suppress(Exception):
            self.bus.log(SERVICE_NAME, message, level=level)

    # ------------------------------------------------------------- 状态
    def status(self) -> Dict[str, Any]:
        """中继自身状态快照。"""

        return {
            "running": self.running,
            "started_at": self.started_at,
            "uptime": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "self_id": self.self_id,
            "upstream_started": self._upstream_started,
            "mode": self.cfg.link.mode,
            "filters": {
                "allow_private": self.filters.allow_private,
                "allow_group": self.filters.allow_group,
                "trigger_mode": self.filters.trigger_mode,
                "prefixes": list(self.filters.prefixes),
                "ignore_self": self.filters.ignore_self,
                "forward_non_text": self.filters.forward_non_text,
            },
            "stats": dict(self.stats),
            "dedupe_window": self.dedupe_window,
            "upstream": self.upstream.status(),
            "downstream": self.downstream.status(),
            "recent": list(self.recent[-MAX_AUDIT:]),
        }


# --------------------------------------------------------------------------- #
# 进程内单例 / 服务注册
# --------------------------------------------------------------------------- #
_bridge: Optional[Bridge] = None


def current() -> Optional[Bridge]:
    """返回正在运行的中继实例（没运行时为 ``None``）。"""

    return _bridge


async def _service_task() -> None:
    """``kind="task"`` 服务的任务体：随控制台进程生命周期运行。

    只有 ``link.mode == "bridge"`` 时才真正建立连接；``native`` 模式（SnowLuma 直连
    AstrBot）下**待机**：不建连接、不重连、不刷日志，只是每 2 秒看一眼模式是否变化。
    """

    global _bridge
    logged = False
    healed = False
    while True:
        cfg = get_config()
        if cfg.link.mode == "bridge":
            if not healed:
                # 起中继前先把 SnowLuma 里 native 时代残留的直连 wsClient 清掉：
                # 它和目标中继会把同一条消息各投递一次，AstrBot 就会回两遍。
                healed = True
                try:
                    fixed = await snowluma_admin.ensure_single_channel(cfg)
                except Exception as exc:  # noqa: BLE001 - 自愈失败不影响中继启动
                    log.warning("清理残留直连适配器失败（忽略）：%s", exc)
                else:
                    if fixed.get("changed"):
                        bus.log(SERVICE_NAME, f"⚠ {fixed.get('detail')}", level="warning")
                        # 等 SnowLuma 把那条直连彻底断开、AstrBot 清完旧登记再连上来：
                        # 否则可能"连上了但没登记成可发送的 bot"（表现为能收不能发）。
                        await asyncio.sleep(2.0)
                    elif fixed.get("ok"):
                        log.debug("没有残留的 native 直连，事件只会投递一次")
            bridge = Bridge(cfg)
            _bridge = bridge
            try:
                await bridge.start()
                await bridge.wait_closed()
            finally:
                with contextlib.suppress(Exception):
                    await bridge.stop()
                _bridge = None
            return  # 由 supervisor.stop() 取消；重新启动会再进入本循环
        if not logged:
            bus.log(SERVICE_NAME, "当前为 native 模式，中继待机（不建立连接）")
            logged = True
        await asyncio.sleep(2.0)


async def reregister(cfg: Optional[AppConfig] = None, *, timeout: float = 20.0) -> Dict[str, Any]:
    """让中继**重新接入** AstrBot —— 也就是重新登记「可发送的 bot」。

    为什么需要重连才能修：aiocqhttp 的 ``_wsr_api_clients`` 是按 ``X-Self-ID``
    存的一张表，任意一条同 ``self_id`` 的连接断开时，它都直接 ``del`` 这条登记
    （``_remove_wsr_api_client`` 不校验是不是自己）。所以只要有一条「别的」同账号
    连接短暂连进 AstrBot 又断开（例如旧版本控制台的 WS 握手探针），中继这条真实
    连接就变成「连着的黑户」：事件照收，AstrBot 却认为没有 bot 可用，所有回复都
    ``ApiNotAvailable`` —— 用户看到的就是「能收不能发」，而且不重连永远好不了。

    做法：停中继 → 等 AstrBot 把旧连接注销干净 → 起中继 → 等到上游 WS 真的连上。
    只适用于 bridge 模式；native 模式下「可发送的 bot」是 SnowLuma 自己那条连接。
    """

    cfg = cfg or get_config()
    if SERVICE_NAME not in supervisor.definitions():
        return {"ok": False, "detail": "中继服务未注册（可能尚未部署）"}
    if str(cfg.link.mode) != "bridge":
        return {"ok": False, "skipped": True, "detail": "当前是 native 模式，不需要重启中继"}
    try:
        await supervisor.stop(SERVICE_NAME)
        # 给 AstrBot 一点时间把旧连接注销：新旧连接重叠时它会按 self_id 误删新登记
        await asyncio.sleep(1.5)
        await supervisor.start(SERVICE_NAME, cfg)
    except Exception as exc:  # noqa: BLE001 - 交由调用方展示
        return {"ok": False, "detail": f"重启中继失败：{exc}"}
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        bridge = _bridge
        if bridge is not None and bridge.upstream.connected:
            return {
                "ok": True,
                "detail": "中继已重新接入 AstrBot（bot 登记已刷新）",
                "self_id": bridge.self_id,
            }
        await asyncio.sleep(0.4)
    return {"ok": False, "detail": f"{int(timeout)} 秒内中继没重新连上 AstrBot（看「运行」页日志）"}


def register_service() -> None:
    """注册 ``bridge`` 服务定义（kind='task'）。"""

    supervisor.register(
        ServiceDef(
            name=SERVICE_NAME,
            display="OneBot 中继",
            kind="task",
            task_factory=_service_task,
        )
    )


async def run_forever(cfg: Optional[AppConfig] = None) -> None:
    """``python -m slab bridge`` 用：前台跑中继，Ctrl+C 退出。"""

    bridge = Bridge(cfg or get_config())
    await bridge.start()
    try:
        await asyncio.sleep(float("inf"))
    except asyncio.CancelledError:
        pass
    finally:
        await bridge.stop()


# --------------------------------------------------------------------------- #
# 供 REST 复用：状态 / 应用 / 发消息 / whoami
# --------------------------------------------------------------------------- #
def _recent_messages(limit: int = 10) -> List[Dict[str, Any]]:
    items = [event for event in bus.recent(200) if event.get("type") == "message"]
    return items[-limit:]


async def _collect_status(cfg: AppConfig) -> Dict[str, Any]:
    """真正执行探测并组装四块状态（不带缓存）。"""

    snowluma = await snowluma_admin.probe(cfg)
    astrbot = await astrbot_admin.probe(cfg)
    bridge = _bridge
    bridge_status: Dict[str, Any]
    if bridge is not None:
        bridge_status = bridge.status()
    else:
        state = supervisor.state(SERVICE_NAME).as_dict() if SERVICE_NAME in supervisor.definitions() else {}
        idle = str(state.get("status") or "") in ("running", "starting")
        bridge_status = {
            "running": False,
            "idle": idle,
            "service": state,
            "hint": (
                "中继待机中（native 模式不需要中继，切到 bridge 模式会自动连接）"
                if idle
                else "中继未运行（native 模式不需要；bridge 模式下请启动「OneBot 中继」）"
            ),
        }

    # SnowLuma 侧的 wsClient 连接（native 模式的核心证据）
    sl_clients = snowluma.get("clients") or []
    sl_connected = [item for item in sl_clients if item.get("connected")]
    onebot = {
        "mode": cfg.link.mode,
        "server": astrbot.get("onebot_ws", {}),
        "server_ready": bool(astrbot.get("onebot_ws", {}).get("ok")),
        "clients": sl_clients,
        "clients_connected": len(sl_connected),
        "self_id": (bridge.self_id if bridge else "") or cfg.snowluma.qq_uin or "",
        "relay_url": snowluma_admin.relay_ws_url(cfg),
    }
    if cfg.link.mode == "native":
        onebot["ready"] = onebot["server_ready"] and bool(sl_connected)
        onebot["hint"] = (
            "SnowLuma 已作为 OneBot 客户端连上 AstrBot"
            if onebot["ready"]
            else "等待 SnowLuma 连上 AstrBot（需 QQ 已登录并已写入配置）"
        )
    else:
        up = (bridge_status.get("upstream") or {}).get("connected") if bridge else False
        down = (bridge_status.get("downstream") or {}).get("connected") if bridge else False
        onebot["ready"] = bool(up and down)
        onebot["bridge_upstream"] = bool(up)
        onebot["bridge_downstream"] = bool(down)
        # 中继模式下「接进 AstrBot 的那条 OneBot 连接」是中继自己提供的，SnowLuma
        # 的 wsClients 天生为空。这里按中继上游算，否则界面/自检会永远显示「未连接」，
        # 还会顺着这条错提示去「写入配置」——那正好会写出第二条直连、把 bot 登记冲掉。
        onebot["clients_connected"] = 1 if up else 0
        onebot["clients_source"] = "bridge"
        onebot["clients_note"] = (
            "中继模式下这条连接由内置中继提供，SnowLuma 侧不需要（也不应该有）wsClient"
        )
        onebot["hint"] = "中继双向已连" if onebot["ready"] else "中继连接未就绪"

    suggestions = list(snowluma.get("suggestions") or []) + list(astrbot.get("suggestions") or [])
    if cfg.link.mode == "bridge":
        if not bridge_status.get("running"):
            suggestions.insert(0, "当前是「中继」模式，请在服务页启动「OneBot 中继」")
        elif not onebot["ready"]:
            suggestions.insert(0, "中继未双向连通：确认 SnowLuma 已在 3001 端口提供 WS 服务且 token 一致")
    elif bridge_status.get("running"):
        suggestions.insert(
            0,
            "当前是 native 模式，但中继仍在运行（会不断重连 3001 并刷日志），建议停止「OneBot 中继」",
        )
    if not suggestions:
        suggestions.append("链路正常，可以直接在 QQ 里发消息测试")

    ready = bool(
        (snowluma.get("connected") if cfg.link.mode == "native" else onebot["ready"])
        and onebot["server_ready"]
    )
    return {
        "ok": True,
        "mode": cfg.link.mode,
        "ready": ready,
        "snowluma": snowluma,
        "astrbot": astrbot,
        "onebot": onebot,
        "bridge": bridge_status,
        "recent_messages": _recent_messages(),
        "suggestions": suggestions,
        "checked_at": time.time(),
    }


#: 状态缓存时长（秒）：前端可能每 3-5 秒轮询一次，避免每次都做 WS 握手 + HTTP 登录
STATUS_TTL = 3.0
_status_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_status_lock: Optional[asyncio.Lock] = None


def _status_lock_obj() -> asyncio.Lock:
    global _status_lock
    if _status_lock is None:
        _status_lock = asyncio.Lock()
    return _status_lock


def reset_status_cache() -> None:
    """清空状态缓存（测试 / 用户点「刷新」时可用）。"""

    _status_cache["ts"] = 0.0
    _status_cache["data"] = None


async def link_status(
    cfg: Optional[AppConfig] = None,
    *,
    force: bool = False,
    ttl: float = STATUS_TTL,
) -> Dict[str, Any]:
    """``GET /api/link/status`` 的主体：四块状态 + 最近消息 + 建议。

    四块分别是 ``snowluma``（配置/进程/适配器连接）、``astrbot``（配置/进程/提供商）、
    ``onebot``（反向 WS 握手 + 每条连接的 up/down）、``bridge``（中继自身）。

    为保护两侧服务（AstrBot 登录有限流、WS 握手有开销），结果会缓存 ``ttl`` 秒，
    并用锁做并发去重；``force=True`` 时强制刷新（``/api/link/probe`` 用它）。
    """

    cfg = cfg or get_config()
    now = time.time()
    cached = _status_cache["data"]
    if not force and cached is not None and (now - float(_status_cache["ts"])) < ttl:
        data = copy.deepcopy(cached)
        data["cached"] = True
        data["cache_age"] = round(now - float(_status_cache["ts"]), 2)
        return data

    async with _status_lock_obj():
        cached = _status_cache["data"]
        if not force and cached is not None and (time.time() - float(_status_cache["ts"])) < ttl:
            data = copy.deepcopy(cached)
            data["cached"] = True
            data["cache_age"] = round(time.time() - float(_status_cache["ts"]), 2)
            return data
        data = await _collect_status(cfg)
        _status_cache["data"] = copy.deepcopy(data)
        _status_cache["ts"] = time.time()
        result = copy.deepcopy(data)
        result["cached"] = False
        result["cache_age"] = 0.0
        return result


async def _restart_service(name: str, *, was_running: bool) -> Dict[str, Any]:
    """按需要重启一个子服务（未注册/原本未运行则跳过）。"""

    if name not in supervisor.definitions():
        return {"ok": False, "skipped": True, "detail": f"服务 {name} 未注册（可能尚未部署）"}
    if not was_running:
        return {"ok": True, "skipped": True, "detail": f"服务 {name} 原本未运行，未重启"}
    try:
        await supervisor.restart(name, get_config())
        return {"ok": True, "skipped": False, "detail": f"已重启 {name}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "skipped": False, "detail": f"重启 {name} 失败：{exc}"}


async def _wait_port(port: int, timeout: float = 30.0) -> bool:
    """轮询等待端口就绪（AstrBot 启动较慢，默认给 30 秒）。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await asyncio.to_thread(port_open, int(port)):
            return True
        await asyncio.sleep(0.3)
    return False


async def apply_link(
    cfg: Optional[AppConfig] = None,
    *,
    restart: bool = True,
    require_provider: bool = True,
) -> Dict[str, Any]:
    """``POST /api/link/apply``：按模式把配置写到两侧，并按需重启受影响的服务。

    顺序（AstrBot 运行中手改配置会被内存快照覆盖，所以必须先停）：

    1. 停 AstrBot（若在运行）；
    2. 写 ``data/cmd_config.json``（备份 + 深合并 + 原子写）；
    3. 起 AstrBot 并等待面板端口就绪；
    4. 写 SnowLuma ``runtime.json`` / OneBot 配置（native 写 wsClient，bridge 写 wsServer）；
    5. SnowLuma 热重载未生效时重启它；
    6. 重启中继（bridge 模式）；
    7. 探测两侧，返回逐步结果。

    Args:
        cfg: 控制台配置；默认取全局配置。
        restart: 是否真的重启子服务（测试/演示可传 False，只写文件）。
        require_provider: AstrBot 侧缺少 API Key 时是否报错。
    """

    cfg = cfg or get_config()
    steps: List[Dict[str, Any]] = []
    astrbot_was_running = supervisor.is_active("astrbot") if "astrbot" in supervisor.definitions() else False
    snowluma_was_running = supervisor.is_active("snowluma") if "snowluma" in supervisor.definitions() else False
    bridge_was_running = supervisor.is_active(SERVICE_NAME) if SERVICE_NAME in supervisor.definitions() else False

    # ---- 1. 停 AstrBot
    if restart and astrbot_was_running:
        try:
            await supervisor.stop("astrbot")
            steps.append({"step": "stop_astrbot", "ok": True, "detail": "已停止 AstrBot（避免内存配置覆盖手写文件）"})
        except Exception as exc:  # noqa: BLE001
            steps.append({"step": "stop_astrbot", "ok": False, "detail": f"停止 AstrBot 失败：{exc}"})
    else:
        steps.append({"step": "stop_astrbot", "ok": True, "skipped": True,
                      "detail": "AstrBot 未运行，无需停止"})

    # ---- 2. 写 AstrBot 配置
    try:
        astrbot_result = await astrbot_admin.apply(cfg, require_provider=require_provider)
        steps.append({"step": "apply_astrbot", "ok": True,
                      "detail": f"已写入 {astrbot_result['path']}", "result": astrbot_result})
    except Exception as exc:  # noqa: BLE001
        steps.append({"step": "apply_astrbot", "ok": False, "detail": str(exc)})
        astrbot_result = {}

    # ---- 3. 起 AstrBot
    if restart and astrbot_was_running:
        try:
            await supervisor.start("astrbot", cfg)
            ready = await _wait_port(int(cfg.astrbot.webui_port), timeout=40.0)
            steps.append({"step": "start_astrbot", "ok": True,
                          "detail": "AstrBot 已重启" + ("，面板端口已就绪" if ready else "，面板端口尚未就绪（可能还在启动）")})
        except Exception as exc:  # noqa: BLE001
            steps.append({"step": "start_astrbot", "ok": False, "detail": f"启动 AstrBot 失败：{exc}"})
    else:
        steps.append({"step": "start_astrbot", "ok": True, "skipped": True,
                      "detail": "AstrBot 原本未运行，未自动启动"})

    # ---- 4. SnowLuma 配置
    try:
        webui_result = await snowluma_admin.apply_webui(cfg)
        steps.append({"step": "apply_snowluma_webui", "ok": bool(webui_result.get("ok")),
                      "detail": webui_result.get("detail") or webui_result.get("error", ""),
                      "result": webui_result})
    except Exception as exc:  # noqa: BLE001
        steps.append({"step": "apply_snowluma_webui", "ok": False, "detail": str(exc)})

    snowluma_result: Dict[str, Any] = {}
    try:
        if cfg.link.mode == "native":
            snowluma_result = await snowluma_admin.apply_native(cfg, hot=restart)
        else:
            snowluma_result = await snowluma_admin.apply_relay(cfg, hot=restart)
        steps.append(
            {
                "step": f"apply_snowluma_{cfg.link.mode}",
                "ok": bool(snowluma_result.get("ok")),
                "detail": snowluma_result.get("error")
                or (
                    f"已写入 {len(snowluma_result.get('files') or [])} 个配置文件"
                    + (
                        f"，并清理了 {len(snowluma_result.get('removed_direct_clients') or [])} 条"
                        " native 直连（否则同一条消息会被投递两次、机器人回两遍）"
                        if snowluma_result.get("removed_direct_clients")
                        else ""
                    )
                ),
                "result": snowluma_result,
            }
        )
    except Exception as exc:  # noqa: BLE001
        steps.append({"step": f"apply_snowluma_{cfg.link.mode}", "ok": False, "detail": str(exc)})

    # ---- 5. 需要时重启 SnowLuma（热重载没生效）
    hot_applied = bool(((snowluma_result.get("hot") or {}).get("applied")))
    if restart and snowluma_was_running and not hot_applied:
        try:
            await supervisor.restart("snowluma", cfg)
            steps.append({"step": "restart_snowluma", "ok": True, "detail": "已重启 SnowLuma 使配置生效"})
        except Exception as exc:  # noqa: BLE001
            steps.append({"step": "restart_snowluma", "ok": False, "detail": f"重启 SnowLuma 失败：{exc}"})
    else:
        steps.append({"step": "restart_snowluma", "ok": True, "skipped": True,
                      "detail": "热重载已生效，无需重启" if hot_applied else "SnowLuma 原本未运行"})

    # ---- 6. 中继（native 模式下必须停下，否则它会一直重连 3001 刷日志）
    if cfg.link.mode == "bridge":
        if restart and bridge_was_running:
            try:
                await supervisor.restart(SERVICE_NAME, cfg)
                steps.append({"step": "restart_bridge", "ok": True, "detail": "已重启中继"})
            except Exception as exc:  # noqa: BLE001
                steps.append({"step": "restart_bridge", "ok": False, "detail": f"重启中继失败：{exc}"})
        elif restart and SERVICE_NAME in supervisor.definitions():
            try:
                await supervisor.start(SERVICE_NAME, cfg)
                steps.append({"step": "restart_bridge", "ok": True, "detail": "已启动中继"})
            except Exception as exc:  # noqa: BLE001
                steps.append({"step": "restart_bridge", "ok": False, "detail": f"启动中继失败：{exc}"})
        else:
            steps.append({"step": "restart_bridge", "ok": True, "skipped": True,
                          "detail": "中继未运行，需要时请手动启动"})
    else:
        if restart and bridge_was_running and SERVICE_NAME in supervisor.definitions():
            try:
                await supervisor.stop(SERVICE_NAME)
                steps.append({"step": "stop_bridge", "ok": True, "detail": "native 模式：已停止中继"})
            except Exception as exc:  # noqa: BLE001
                steps.append({"step": "stop_bridge", "ok": False, "detail": f"停止中继失败：{exc}"})
        else:
            steps.append({"step": "stop_bridge", "ok": True, "skipped": True,
                          "detail": "native 模式不需要中继（已确认中继未运行）"})

    # ---- 7. 复核
    try:
        status = await link_status(cfg, force=True)
        steps.append({"step": "verify", "ok": bool(status.get("ready")),
                      "detail": "链路已就绪" if status.get("ready") else "配置已写入，但链路尚未就绪（见 suggestions）"})
        verification = {"ready": status.get("ready"), "onebot": status.get("onebot"),
                        "suggestions": status.get("suggestions")}
    except Exception as exc:  # noqa: BLE001
        steps.append({"step": "verify", "ok": False, "detail": f"复核失败：{exc}"})
        verification = {"ready": False, "error": str(exc)}

    return {
        "ok": all(step.get("ok") for step in steps),
        "mode": cfg.link.mode,
        "steps": steps,
        "verification": verification,
        "applied": {"astrbot": astrbot_result, "snowluma": snowluma_result},
    }


# --------------------------------------------------------------------------- #
# 测试发消息 / whoami
# --------------------------------------------------------------------------- #
async def _ephemeral_client(cfg: AppConfig, timeout: float = 3.0) -> OneBotClient:
    """建一条临时 OneBot 连接到 SnowLuma 的 WS 服务端（用于发消息/查询）。"""

    client = OneBotClient(
        snowluma_admin.relay_ws_url(cfg),
        token=snowluma_admin.relay_token(cfg),
        self_id=str(cfg.snowluma.qq_uin or ""),
        role="universal",
        name="slab-temp",
        action_timeout=timeout,
        reconnect_base=0.2,
        reconnect_max=0.5,
    )
    await client.start()
    connected = await client.wait_connected(timeout)
    if not connected:
        await client.stop()
        raise OneBotNotConnected(
            "连不上 SnowLuma 的 OneBot WS 服务端（"
            f"{snowluma_admin.relay_ws_url(cfg)}）。请确认 SnowLuma 已登录 QQ，且已在配置里开启 wsServer"
        )
    return client


async def test_send(cfg: AppConfig, target_type: str, target_id: str, text: str) -> Dict[str, Any]:
    """``POST /api/link/test-send``：优先走 OneBot 直发，失败回退 AstrBot 控制面 API。

    Returns:
        ``{"ok", "via": "onebot|bridge|astrbot-api", "data"/"detail"}``
    """

    errors: List[str] = []
    body = str(text or "").strip()
    if not body:
        raise ValueError("发送内容不能为空")
    ident = str(target_id or "").strip()
    if not ident:
        raise ValueError("目标 ID 不能为空")

    # 1) 中继正在运行 → 直接用它的下游连接
    bridge = _bridge
    if bridge is not None and bridge.downstream.connected:
        try:
            data = await bridge.actions.send_text(target_type, ident, body)
            return {"ok": True, "via": "bridge", "data": data}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"中继发送失败：{exc}")

    # 2) 临时 OneBot 连接直发
    try:
        client = await _ephemeral_client(cfg)
    except OneBotNotConnected as exc:
        errors.append(str(exc))
    else:
        try:
            actions = OneBotActions(client)
            data = await actions.send_text(target_type, ident, body)
            return {"ok": True, "via": "onebot", "data": data}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"OneBot 直发失败：{exc}")
        finally:
            await client.stop()

    # 3) 回退 SnowLuma WebUI 的 /api/debug/invoke（native 模式下没有 wsServers 时可用）
    action = "send_group_msg" if str(target_type).strip().lower() == "group" else "send_private_msg"
    try:
        invoke_params: Dict[str, Any] = {"message": [{"type": "text", "data": {"text": body}}]}
        if action == "send_group_msg":
            invoke_params["group_id"] = ident
        else:
            invoke_params["user_id"] = ident
        result = await snowluma_admin.invoke_action(cfg, action, invoke_params)
        return {"ok": True, "via": "snowluma-api", "uin": result.get("uin"), "data": result.get("data")}
    except Exception as exc:  # noqa: BLE001
        errors.append(f"SnowLuma WebUI 直调失败：{exc}")

    # 4) 回退 AstrBot 控制面 API（走已连接平台，不消耗 LLM）
    creds = astrbot_admin.read_dashboard_password(cfg)
    if creds.get("password"):
        umo = astrbot_admin.im_umo(cfg, target_type, ident)
        api = astrbot_admin.AstrBotApi(
            astrbot_admin.dashboard_url(cfg),
            username=creds.get("username", cfg.astrbot.webui_username),
            password=creds["password"],
        )
        try:
            payload = await api.send_message(umo, body)
            return {"ok": True, "via": "astrbot-api", "umo": umo, "data": payload}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"AstrBot API 发送失败：{exc}")
        finally:
            await api.close()
    else:
        errors.append("无法调用 AstrBot 控制面 API（未取到面板密码）")

    return {"ok": False, "error": "；".join(errors) or "没有可用的发送通道"}


async def whoami(cfg: AppConfig) -> Dict[str, Any]:
    """``GET /api/link/whoami``：优先 OneBot ``get_login_info``，失败回退 AstrBot ``bots/stats``。"""

    errors: List[str] = []
    bridge = _bridge
    if bridge is not None and bridge.downstream.connected:
        try:
            info = await bridge.actions.whoami()
            return {"ok": True, "via": "bridge", **info}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"中继查询失败：{exc}")
    try:
        client = await _ephemeral_client(cfg)
    except OneBotNotConnected as exc:
        errors.append(str(exc))
    else:
        try:
            info = await OneBotActions(client).whoami()
            return {"ok": True, "via": "onebot", **info}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"OneBot 查询失败：{exc}")
        finally:
            await client.stop()

    # SnowLuma WebUI 直调（native 模式没有 wsServers 时的主通道）
    try:
        info = await snowluma_admin.invoke_action(cfg, "get_login_info", {})
        data = info.get("data") if isinstance(info.get("data"), dict) else {}
        friends: Any = []
        groups: Any = []
        with contextlib.suppress(Exception):
            friends = (await snowluma_admin.invoke_action(cfg, "get_friend_list", {})).get("data")
        with contextlib.suppress(Exception):
            groups = (await snowluma_admin.invoke_action(cfg, "get_group_list", {})).get("data")
        return {
            "ok": True,
            "via": "snowluma-api",
            "user_id": str(data.get("user_id", "")),
            "nickname": str(data.get("nickname", "")),
            "friends": len(friends) if isinstance(friends, list) else 0,
            "groups": len(groups) if isinstance(groups, list) else 0,
            "errors": errors,
        }
    except Exception as exc:  # noqa: BLE001
        errors.append(f"SnowLuma WebUI 直调失败：{exc}")

    creds = astrbot_admin.read_dashboard_password(cfg)
    if creds.get("password"):
        api = astrbot_admin.AstrBotApi(
            astrbot_admin.dashboard_url(cfg),
            username=creds.get("username", cfg.astrbot.webui_username),
            password=creds["password"],
        )
        try:
            stats = await api.bots_stats()
            return {"ok": True, "via": "astrbot-api", "stats": stats, "errors": errors}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"AstrBot API 查询失败：{exc}")
        finally:
            await api.close()

    return {"ok": False, "error": "；".join(errors) or "没有可用的查询通道"}
