"""进程内事件总线 + 日志环形缓冲。

用途：
* 后台线程（部署进度、子进程日志）把事件推进总线；
* Web SSE 接口 /api/events 把事件推给浏览器；
* /api/logs 从环形缓冲取最近 N 行，页面刷新后不丢历史。

线程安全：``publish`` 可以在任意线程调用，内部用 ``call_soon_threadsafe``
把事件投递到事件循环。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Set

MAX_LOG_LINES = 2000
MAX_RECENT_EVENTS = 200


class EventBus:
    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop
        self._subscribers: Set[asyncio.Queue] = set()
        self._lock = threading.RLock()
        self._recent: Deque[dict] = deque(maxlen=MAX_RECENT_EVENTS)
        self._logs: Dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=MAX_LOG_LINES))
        self._seq = 0

    # ------------------------------------------------------------------ loop
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # --------------------------------------------------------------- publish
    def publish(self, type_: str, **payload: Any) -> dict:
        """发布一个事件；返回落库的事件 dict。"""

        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "ts": time.time(), "type": type_, **payload}
            self._recent.append(event)
            if type_ == "log":
                svc = str(payload.get("service", "console"))
                self._logs[svc].append(event)
            subs = list(self._subscribers)

        for q in subs:
            loop = self._loop
            if loop is not None and loop.is_running():
                try:
                    loop.call_soon_threadsafe(self._offer, q, event)
                except RuntimeError:  # 事件循环已关闭
                    pass
            else:  # 事件循环未启动（例如单元测试/同步脚本）
                self._offer(q, event)
        return event

    def log(self, service: str, line: str, level: str = "info") -> dict:
        return self.publish("log", service=service, line=line.rstrip("\r\n"), level=level)

    # ------------------------------------------------------------- subscribe
    def _offer(self, q: asyncio.Queue, event: dict) -> None:
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def subscribe(self, maxsize: int = 1000) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # ------------------------------------------------------------------ read
    def recent(self, limit: int = 50) -> List[dict]:
        with self._lock:
            items = list(self._recent)
        return items[-limit:]

    def logs(self, service: str | None = None, limit: int = 200) -> List[dict]:
        with self._lock:
            if service:
                items = list(self._logs.get(service, ()))
            else:
                items = sorted(
                    (e for d in self._logs.values() for e in d), key=lambda e: e["seq"]
                )
        return items[-limit:]

    def log_services(self) -> List[str]:
        with self._lock:
            return sorted(self._logs)


bus = EventBus()
