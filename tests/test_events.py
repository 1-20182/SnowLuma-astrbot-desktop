import asyncio

import pytest

from slab.events import EventBus


def test_publish_and_logs():
    bus = EventBus()
    bus.log("astrbot", "hello")
    bus.log("snowluma", "world", level="warn")
    bus.publish("service", name="astrbot", status="running")

    logs = bus.logs()
    assert len(logs) == 2
    assert logs[0]["service"] == "astrbot"
    assert bus.logs("snowluma")[0]["level"] == "warn"
    assert bus.log_services() == ["astrbot", "snowluma"]
    assert bus.recent(5)[-1]["type"] == "service"


def test_ring_buffer_caps():
    from slab.events import MAX_LOG_LINES

    bus = EventBus()
    for i in range(MAX_LOG_LINES + 500):
        bus.log("x", f"line-{i}")
    logs = bus.logs("x", MAX_LOG_LINES * 2)
    assert len(logs) == MAX_LOG_LINES, "环形缓冲必须按上限截断"
    assert logs[-1]["line"] == f"line-{MAX_LOG_LINES + 499}"
    assert logs[0]["line"] == f"line-500"


@pytest.mark.asyncio
async def test_subscribe_receives_event():
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())
    q = bus.subscribe()
    bus.publish("deploy", target="astrbot", progress=0.5)
    ev = await asyncio.wait_for(q.get(), timeout=2)
    assert ev["type"] == "deploy"
    assert ev["progress"] == 0.5
    bus.unsubscribe(q)
    assert bus.subscriber_count == 0


def test_publish_from_thread():
    import threading

    bus = EventBus()
    t = threading.Thread(target=lambda: bus.log("t", "from-thread"))
    t.start()
    t.join()
    assert bus.logs("t")[0]["line"] == "from-thread"
