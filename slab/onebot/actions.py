"""OneBot v11 常用动作（action）便捷封装。

只做「参数拼装 + 调用 + 结果整形」，不关心连接生命周期；
任何 :class:`OneBotClient` 实例（真连 SnowLuma、或单测里的假实现）都能直接套用。

返回约定
--------
* ``send_*`` 返回 OneBot 的 ``data`` 字典（通常含 ``message_id``），至少是 ``{}``；
* ``get_*_list`` 返回列表（对端返回奇怪结构时给 ``[]``，不抛异常）；
* 网络/协议错误照原样上抛（:class:`slab.onebot.client.OneBotError` 家族），由上层翻译成中文。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Union

from .client import OneBotClient
from .model import Message, normalise_message, to_cq_string

log = logging.getLogger("slab.onebot")

#: 只发送 CQ 码字符串（老实现）而不是消息段数组
TARGET_TYPES = ("group", "private", "group_temp")


def target_params(target_type: str, target_id: Union[str, int]) -> Dict[str, Any]:
    """把 ``(group|private, id)`` 翻译成动作参数。

    Args:
        target_type: ``group`` / ``private``。
        target_id: 群号或 QQ 号。

    Returns:
        形如 ``{"group_id": "123"}`` 或 ``{"user_id": "123"}`` 的参数字典。

    Raises:
        ValueError: ``target_type`` 非法或 ``target_id`` 为空。
    """

    kind = str(target_type or "").strip().lower()
    ident = str(target_id if target_id is not None else "").strip()
    if kind not in TARGET_TYPES:
        raise ValueError(f"未知的发送目标类型: {target_type}（只支持 group / private）")
    if not ident:
        raise ValueError("发送目标 ID 不能为空")
    if kind == "group":
        return {"group_id": ident}
    return {"user_id": ident}


def message_payload(message: Message, *, as_string: bool = False) -> Union[List[Dict[str, Any]], str]:
    """把消息规整成 OneBot 可发送的载荷（默认消息段数组）。"""

    segments = normalise_message(message)
    if as_string:
        return to_cq_string(segments)
    return segments


class OneBotActions:
    """一组绑定在固定客户端上的动作封装。"""

    def __init__(self, client: OneBotClient, *, as_string: bool = False) -> None:
        self.client = client
        self.as_string = as_string

    # ------------------------------------------------------------- 发消息
    async def send_group_msg(
        self,
        group_id: Union[str, int],
        message: Message,
        *,
        auto_escape: bool = False,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """发送群消息。"""

        data = await self.client.call(
            "send_group_msg",
            {
                "group_id": str(group_id),
                "message": message_payload(message, as_string=self.as_string),
                "auto_escape": bool(auto_escape),
            },
            timeout=timeout,
        )
        return data if isinstance(data, dict) else {}

    async def send_private_msg(
        self,
        user_id: Union[str, int],
        message: Message,
        *,
        auto_escape: bool = False,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """发送私聊消息。"""

        data = await self.client.call(
            "send_private_msg",
            {
                "user_id": str(user_id),
                "message": message_payload(message, as_string=self.as_string),
                "auto_escape": bool(auto_escape),
            },
            timeout=timeout,
        )
        return data if isinstance(data, dict) else {}

    async def send_text(
        self,
        target_type: str,
        target_id: Union[str, int],
        text: str,
        *,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """按目标类型发送纯文本（``/api/link/test-send`` 走这里）。"""

        if not str(text or "").strip():
            raise ValueError("发送内容不能为空")
        if str(target_type).strip().lower() == "group":
            return await self.send_group_msg(target_id, str(text), timeout=timeout)
        return await self.send_private_msg(target_id, str(text), timeout=timeout)

    async def send_msg(
        self,
        *,
        message_type: str,
        message: Message,
        group_id: Union[str, int] = "",
        user_id: Union[str, int] = "",
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """通用发送入口（自动选择群/私聊动作）。"""

        if str(message_type).lower() == "group":
            return await self.send_group_msg(group_id or user_id, message, timeout=timeout)
        return await self.send_private_msg(user_id, message, timeout=timeout)

    # ------------------------------------------------------------- 查询
    async def get_login_info(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """获取登录号信息（``user_id`` / ``nickname``）。"""

        data = await self.client.call("get_login_info", {}, timeout=timeout)
        return data if isinstance(data, dict) else {}

    async def get_group_list(self, timeout: Optional[float] = None) -> List[Dict[str, Any]]:
        """获取群列表。"""

        data = await self.client.call("get_group_list", {}, timeout=timeout)
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    async def get_friend_list(self, timeout: Optional[float] = None) -> List[Dict[str, Any]]:
        """获取好友列表。"""

        data = await self.client.call("get_friend_list", {}, timeout=timeout)
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    async def get_status(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """获取运行状态。"""

        data = await self.client.call("get_status", {}, timeout=timeout)
        return data if isinstance(data, dict) else {}

    async def get_version_info(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """获取实现版本信息（探活用：能通通常说明 action 通道正常）。"""

        data = await self.client.call("get_version_info", {}, timeout=timeout)
        return data if isinstance(data, dict) else {}

    async def whoami(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """组合查询：登录号 + 好友数 + 群数（``/api/link/whoami`` 用）。"""

        info = await self.get_login_info(timeout=timeout)
        friends = await self.get_friend_list(timeout=timeout)
        groups = await self.get_group_list(timeout=timeout)
        return {
            "user_id": str(info.get("user_id", "")),
            "nickname": str(info.get("nickname", "")),
            "friends": len(friends),
            "groups": len(groups),
            "group_list": [
                {"group_id": str(g.get("group_id", "")), "group_name": str(g.get("group_name", ""))}
                for g in groups
            ],
            "friend_list": [
                {"user_id": str(f.get("user_id", "")), "nickname": str(f.get("nickname", ""))}
                for f in friends
            ],
        }


async def send_text(
    client: OneBotClient,
    target_type: str,
    target_id: Union[str, int],
    text: str,
    *,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """模块级便捷函数：``await send_text(client, "private", 123, "你好")``。"""

    return await OneBotActions(client).send_text(target_type, target_id, text, timeout=timeout)
