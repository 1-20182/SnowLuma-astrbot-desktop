"""一次性联调探针：模拟 SnowLuma 的 OneBot WS 客户端连入 AstrBot(aiocqhttp 反向 WS)。

用法：
    python tools/probe_astrbot_onebot.py --url ws://127.0.0.1:6199/ws --token test-token-123 \
        --self-id 3985934176 --user-id 10001 --text "你好"

用途：在不启动 QQ 的情况下验证「AstrBot 收到事件 → 回发 action」这条链路，
同时也是我们最终真机联调时的诊断工具。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time


async def probe(args: argparse.Namespace) -> int:
    import websockets

    headers = {
        "User-Agent": "OneBot/11",
        "X-Self-ID": str(args.self_id),
        "X-Client-Role": "Universal",
    }
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    url = args.url
    if args.token_in_query:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}access_token={args.token}"

    print(f"[probe] 连接 {url} …")
    try:
        async with websockets.connect(url, additional_headers=headers, open_timeout=10) as ws:
            print("[probe] 已连接")
            await asyncio.sleep(0.5)

            if args.command == "login-info":
                await ws.send(json.dumps({"action": "get_login_info", "params": {}, "echo": "e1"}))
            else:
                event = {
                    "post_type": "message",
                    "message_type": "private" if not args.group_id else "group",
                    "sub_type": "friend" if not args.group_id else "normal",
                    "time": int(time.time()),
                    "self_id": int(args.self_id),
                    "message_id": int(time.time() * 1000) % 10**9,
                    "message": [{"type": "text", "data": {"text": args.text}}],
                    "raw_message": args.text,
                    "font": 0,
                    "sender": {
                        "user_id": int(args.user_id),
                        "nickname": args.nickname,
                        "card": "",
                    },
                }
                if args.group_id:
                    event["group_id"] = int(args.group_id)
                print(f"[probe] 发送事件: {json.dumps(event, ensure_ascii=False)[:200]}")
                await ws.send(json.dumps(event))

            deadline = time.time() + args.timeout
            got = 0
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.time()))
                except asyncio.TimeoutError:
                    break
                got += 1
                try:
                    payload = json.loads(raw)
                except ValueError:
                    print(f"[probe] 非 JSON: {raw[:200]}")
                    continue
                if "action" in payload:
                    print(f"[probe] ← action: {payload.get('action')} params="
                          f"{json.dumps(payload.get('params', {}), ensure_ascii=False)[:400]}")
                else:
                    print(f"[probe] ← 其他: {json.dumps(payload, ensure_ascii=False)[:400]}")
            print(f"[probe] 结束，共收到 {got} 条消息")
            return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[probe] 连接失败: {type(exc).__name__}: {exc}")
        return 2


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="AstrBot OneBot 反向 WS 探针")
    p.add_argument("--url", default="ws://127.0.0.1:6199/ws")
    p.add_argument("--token", default="")
    p.add_argument("--token-in-query", action="store_true")
    p.add_argument("--self-id", default="10000")
    p.add_argument("--user-id", default="10001")
    p.add_argument("--group-id", default="")
    p.add_argument("--nickname", default="联调测试")
    p.add_argument("--text", default="你好")
    p.add_argument("--command", default="message", choices=["message", "login-info"])
    p.add_argument("--timeout", type=float, default=20.0)
    args = p.parse_args(argv)
    return asyncio.run(probe(args))


if __name__ == "__main__":
    sys.exit(main())
