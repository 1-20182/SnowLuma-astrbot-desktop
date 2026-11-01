"""OneBot v11 协议层（T2 维护）。

子模块
------
* ``model``   ：消息段拼装/解析、事件字段提取（纯函数，无 IO）
* ``client``  ：面向 OneBot 实现端（SnowLuma）的 WS 客户端（应用侧，发 action 收 response）
* ``reverse`` ：面向 OneBot 应用端（AstrBot aiocqhttp 反向 WS）的 WS 客户端（收 action 发 response）
* ``actions`` ：``send_group_msg`` / ``send_private_msg`` 等便捷封装

设计原则：全部 async；网络层可注入 connector 便于单测；不在导入期创建事件循环对象。
"""

from __future__ import annotations

from .actions import OneBotActions, send_text
from .client import (
    OneBotActionFailed,
    OneBotClient,
    OneBotDisconnected,
    OneBotError,
    OneBotTimeout,
)
from .model import (
    OneBotEvent,
    build_message,
    message_to_text,
    normalise_message,
    parse_cq_string,
    parse_event,
    plain_text,
    to_cq_string,
)
from .reverse import ReverseOneBotClient

__all__ = [
    "OneBotActions",
    "send_text",
    "OneBotClient",
    "ReverseOneBotClient",
    "OneBotError",
    "OneBotTimeout",
    "OneBotDisconnected",
    "OneBotActionFailed",
    "OneBotEvent",
    "build_message",
    "normalise_message",
    "message_to_text",
    "plain_text",
    "to_cq_string",
    "parse_cq_string",
    "parse_event",
]
