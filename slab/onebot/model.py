"""OneBot v11 轻量模型：消息段拼装/解析 + 事件字段提取。

本模块是**纯数据翻译层**：不做网络请求、不读写文件、不碰事件循环，
因此可以被中继（bridge）、REST 路由与单元测试任意复用。

约定
----
* 「消息」有两种 OneBot 合法形态：CQ 码字符串（``"你好[CQ:at,qq=1]"``）或消息段数组
  （``[{"type": "text", "data": {"text": "你好"}}]``）。内部统一用**消息段数组**表示，
  必要时才降级成 CQ 码字符串。
* ID 类字段（``self_id`` / ``user_id`` / ``group_id`` / ``message_id``）在模型里一律是
  ``str``（避免 JS 侧 64 位整数与 Python int 的表述差异），但 ``raw`` 里保留原始值，
  转发时原样发出，不会被改写。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

MessageSegment = Dict[str, Any]
Message = Union[str, List[MessageSegment]]

#: CQ 码里需要反转义的四类字符（顺序敏感：``&`` 必须最先处理）
_CQ_ESCAPES = (("&", "&amp;"), ("[", "&#91;"), ("]", "&#93;"), (",", "&#44;"))
_CQ_PATTERN = re.compile(r"\[CQ:([A-Za-z0-9_.\-]+)((?:,[^\]]*)?)\]")

#: 纯文字消息段类型
TEXT = "text"
IMAGE = "image"
AT = "at"
REPLY = "reply"
FACE = "face"

#: 各段类型在「人类可读文本」里的占位符
_SEGMENT_PLACEHOLDER = {
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "face": "[表情]",
    "mface": "[表情]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "forward": "[合并转发]",
    "markdown": "[Markdown]",
}


# --------------------------------------------------------------------------- #
# CQ 码编解码
# --------------------------------------------------------------------------- #
def cq_escape(text: str) -> str:
    """把普通文本转义成 CQ 码里的安全文本。"""

    out = str(text)
    for raw, escaped in _CQ_ESCAPES:
        out = out.replace(raw, escaped)
    return out


def cq_unescape(text: str) -> str:
    """把 CQ 码里的文本还原成普通文本。"""

    out = str(text)
    for raw, escaped in reversed(_CQ_ESCAPES):
        out = out.replace(escaped, raw)
    return out


def parse_cq_string(text: str) -> List[MessageSegment]:
    """解析 CQ 码字符串为消息段数组。

    Args:
        text: 形如 ``"你好[CQ:at,qq=123][CQ:image,file=a.png]"`` 的字符串。

    Returns:
        消息段数组；无法识别的 ``[CQ:...]`` 片段会原样作为文本保留（不丢内容）。
    """

    if not text:
        return []
    segments: List[MessageSegment] = []
    pos = 0
    for match in _CQ_PATTERN.finditer(text):
        if match.start() > pos:
            chunk = cq_unescape(text[pos : match.start()])
            if chunk:
                segments.append({"type": TEXT, "data": {"text": chunk}})
        seg_type = match.group(1).lower()
        data: Dict[str, str] = {}
        body = match.group(2)
        if body:
            for pair in body.split(",")[1:]:
                if not pair:
                    continue
                key, _, value = pair.partition("=")
                data[cq_unescape(key)] = cq_unescape(value)
        segments.append({"type": seg_type, "data": data})
        pos = match.end()
    if pos < len(text):
        chunk = cq_unescape(text[pos:])
        if chunk:
            segments.append({"type": TEXT, "data": {"text": chunk}})
    return segments


def to_cq_string(message: Message) -> str:
    """把消息段数组降级成 CQ 码字符串（用于 ``messageFormat: "string"`` 的对端）。"""

    parts: List[str] = []
    for seg in normalise_message(message):
        seg_type = str(seg.get("type", ""))
        raw_data = seg.get("data")
        data: Dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        if seg_type == TEXT:
            parts.append(cq_escape(str(data.get("text", ""))))
            continue
        args = "".join(
            f",{key}={cq_escape(str(value))}"
            for key, value in data.items()
            if value is not None
        )
        parts.append(f"[CQ:{seg_type}{args}]")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# 消息段拼装
# --------------------------------------------------------------------------- #
def _clean(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None}


def text_segment(text: str) -> MessageSegment:
    """纯文本消息段。"""

    return {"type": TEXT, "data": {"text": str(text)}}


def image_segment(file: str, **extra: Any) -> MessageSegment:
    """图片消息段。``file`` 支持 URL / 本地路径 / base64:// 。"""

    return {"type": IMAGE, "data": _clean({"file": str(file), **extra})}


def at_segment(qq: Union[str, int]) -> MessageSegment:
    """@ 某人消息段；``qq="all"`` 表示 @全体成员。"""

    return {"type": AT, "data": {"qq": str(qq)}}


def reply_segment(message_id: Union[str, int]) -> MessageSegment:
    """引用回复消息段。"""

    return {"type": REPLY, "data": {"id": str(message_id)}}


def face_segment(face_id: Union[str, int]) -> MessageSegment:
    """QQ 系统表情消息段。"""

    return {"type": FACE, "data": {"id": str(face_id)}}


def normalise_message(message: Optional[Message]) -> List[MessageSegment]:
    """把任意 OneBot 消息形态规整成消息段数组（深拷贝，不修改入参）。"""

    if message is None:
        return []
    if isinstance(message, str):
        return parse_cq_string(message)
    segments: List[MessageSegment] = []
    for item in message:
        if isinstance(item, str):  # 容错：数组里混了裸字符串
            segments.append(text_segment(item))
            continue
        if not isinstance(item, dict):
            continue
        seg_type = str(item.get("type", TEXT)) or TEXT
        raw_data = item.get("data")
        data = copy.deepcopy(raw_data) if isinstance(raw_data, dict) else {}
        segments.append({"type": seg_type, "data": data})
    return segments


def build_message(*parts: Union[str, MessageSegment, Iterable[Union[str, MessageSegment]]]) -> List[MessageSegment]:
    """拼装消息：字符串自动转 text 段，其余按段追加。

    Examples:
        >>> build_message(at_segment(123), " 你好")
        [{'type': 'at', 'data': {'qq': '123'}}, {'type': 'text', 'data': {'text': ' 你好'}}]
    """

    segments: List[MessageSegment] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, str):
            if part:
                segments.append(text_segment(part))
            continue
        if isinstance(part, dict):
            segments.extend(normalise_message([part]))  # type: ignore[list-item]
            continue
        segments.extend(normalise_message(list(part)))  # type: ignore[arg-type]
    return segments


def message_to_text(message: Message) -> str:
    """把消息渲染成可读文本（@ 显示为 ``@qq``，非文本段显示占位符）。"""

    out: List[str] = []
    for seg in normalise_message(message):
        seg_type = str(seg.get("type", ""))
        raw_data = seg.get("data")
        data: Dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        if seg_type == TEXT:
            out.append(str(data.get("text", "")))
        elif seg_type == AT:
            out.append(f"@{data.get('qq', '')}")
        elif seg_type == REPLY:
            out.append(f"[回复:{data.get('id', '')}]")
        else:
            out.append(_SEGMENT_PLACEHOLDER.get(seg_type, f"[{seg_type}]"))
    return "".join(out)


def plain_text(message: Message) -> str:
    """只取文本段（用于前缀匹配、日志摘要）。"""

    return "".join(
        str((seg.get("data") or {}).get("text", ""))
        for seg in normalise_message(message)
        if seg.get("type") == TEXT
    )


def at_qq_list(message: Message) -> List[str]:
    """列出消息里所有被 @ 的 QQ 号。"""

    return [
        str((seg.get("data") or {}).get("qq", ""))
        for seg in normalise_message(message)
        if seg.get("type") == AT
    ]


# --------------------------------------------------------------------------- #
# 事件
# --------------------------------------------------------------------------- #
def _as_id(value: Any) -> str:
    """把 ID 类字段统一成字符串（``None`` → ``""``，int/float → 无小数点的字符串）。"""

    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


@dataclass
class OneBotEvent:
    """一个 OneBot v11 事件（消息/通知/请求/元事件）的只读视图。

    只覆盖联动真正用到的字段，其余原样保存在 :attr:`raw` 里。
    """

    raw: Dict[str, Any] = field(default_factory=dict)
    post_type: str = ""
    message_type: str = ""
    sub_type: str = ""
    notice_type: str = ""
    request_type: str = ""
    meta_event_type: str = ""
    self_id: str = ""
    user_id: str = ""
    group_id: str = ""
    target_id: str = ""
    operator_id: str = ""
    message_id: str = ""
    raw_message: str = ""
    message: List[MessageSegment] = field(default_factory=list)
    sender: Dict[str, Any] = field(default_factory=dict)
    time: int = 0

    # ------------------------------------------------------------- 基础属性
    @property
    def is_message(self) -> bool:
        return self.post_type in ("message", "message_sent")

    @property
    def is_group(self) -> bool:
        return self.is_message and self.message_type == "group"

    @property
    def is_private(self) -> bool:
        return self.is_message and self.message_type == "private"

    @property
    def is_self_sent(self) -> bool:
        """是否是「自己发的」消息（AstrBot 侧配置的机器人被报告自身消息时会出现）。"""

        if self.post_type == "message_sent":
            return True
        return bool(self.user_id) and self.user_id == self.self_id

    @property
    def nickname(self) -> str:
        sender = self.sender if isinstance(self.sender, dict) else {}
        return str(sender.get("card") or sender.get("nickname") or "")

    @property
    def text(self) -> str:
        """整条消息的可读文本（含 ``@qq`` 与 ``[图片]`` 占位符）。"""

        return message_to_text(self.message)

    @property
    def plain(self) -> str:
        """仅文本段的拼接结果（前缀匹配用这个）。"""

        return plain_text(self.message)

    # ------------------------------------------------------------- 运行判断
    def at_ids(self) -> List[str]:
        return at_qq_list(self.message)

    def is_at(self, qq: Optional[Union[str, int]] = None) -> bool:
        """消息里是否 @ 了 ``qq``（默认 @ 自己）。``"all"`` 不算。"""

        target = _as_id(qq if qq is not None else self.self_id)
        if not target:
            return False
        return target in self.at_ids()

    def starts_with(self, prefixes: Sequence[str]) -> bool:
        """文本（跳过前导空白）是否以任一前缀开头。"""

        body = self.plain.lstrip()
        return any(prefix and body.startswith(prefix) for prefix in prefixes)

    def matches_trigger(self, mode: str = "all", prefixes: Sequence[str] = ("/",)) -> bool:
        """是否命中触发条件。

        * ``all``：命中
        * ``at_or_prefix``：群消息需要 @ 自己或以前缀开头；私聊直接命中
          （私聊里没有「@」的语义，用前缀强制反而会把日常聊天挡掉）。
        """

        if mode == "all":
            return True
        if self.is_group:
            return self.is_at() or self.starts_with(prefixes)
        return True

    # ------------------------------------------------------------- 变换/输出
    def to_dict(self) -> Dict[str, Any]:
        """返回可直接发出去的原始事件（深拷贝，转发时不做任何改写）。"""

        return copy.deepcopy(self.raw)

    def summary(self) -> str:
        """一行日志摘要。"""

        where = f"group {self.group_id}" if self.is_group else "private"
        return f"[{self.post_type}/{self.message_type or '-'}] {where} {self.user_id}: {self.text}"[:300]

    # ------------------------------------------------------------- 构造器
    @classmethod
    def parse(cls, raw: Dict[str, Any]) -> "OneBotEvent":
        """从原始事件 dict 提取字段；缺字段一律给空值，不抛异常。"""

        if not isinstance(raw, dict):
            return cls(raw={})
        message_field = raw.get("message")
        if message_field is None:
            message_field = raw.get("raw_message") or []
        sender = raw.get("sender")
        return cls(
            raw=copy.deepcopy(raw),
            post_type=str(raw.get("post_type") or ""),
            message_type=str(raw.get("message_type") or ""),
            sub_type=str(raw.get("sub_type") or ""),
            notice_type=str(raw.get("notice_type") or ""),
            request_type=str(raw.get("request_type") or ""),
            meta_event_type=str(raw.get("meta_event_type") or ""),
            self_id=_as_id(raw.get("self_id")),
            user_id=_as_id(raw.get("user_id")),
            group_id=_as_id(raw.get("group_id")),
            target_id=_as_id(raw.get("target_id")),
            operator_id=_as_id(raw.get("operator_id")),
            message_id=_as_id(raw.get("message_id")),
            raw_message=str(raw.get("raw_message") or ""),
            message=normalise_message(message_field),  # type: ignore[arg-type]
            sender=copy.deepcopy(sender) if isinstance(sender, dict) else {},
            time=int(raw.get("time") or 0),
        )

    @classmethod
    def message_event(
        cls,
        message_type: str,
        user_id: Union[str, int],
        message: Message,
        *,
        self_id: Union[str, int] = "",
        group_id: Union[str, int] = "",
        message_id: Union[str, int] = "",
        sender: Optional[Dict[str, Any]] = None,
        sub_type: str = "",
        timestamp: Optional[int] = None,
    ) -> "OneBotEvent":
        """构造一条消息事件（中继/测试用，字段与 OneBot v11 对齐）。"""

        import time as _time

        segments = normalise_message(message)
        sender_info: Dict[str, Any] = dict(sender or {})
        sender_info.setdefault("user_id", _as_id(user_id))
        raw: Dict[str, Any] = {
            "post_type": "message",
            "message_type": message_type,
            "sub_type": sub_type or ("normal" if message_type == "group" else "friend"),
            "time": int(timestamp if timestamp is not None else _time.time()),
            "self_id": _as_id(self_id),
            "message_id": _as_id(message_id),
            "user_id": _as_id(user_id),
            "message": segments,
            "raw_message": to_cq_string(segments),
            "font": 0,
            "sender": sender_info,
        }
        if message_type == "group":
            raw["group_id"] = _as_id(group_id)
        return cls.parse(raw)


def parse_event(raw: Dict[str, Any]) -> OneBotEvent:
    """模块级便捷函数，等价于 :meth:`OneBotEvent.parse`。"""

    return OneBotEvent.parse(raw)


# --------------------------------------------------------------------------- #
# action / response 载荷
# --------------------------------------------------------------------------- #
def is_action_payload(payload: Any) -> bool:
    """判断一帧 JSON 是否是「动作请求」（AstrBot → OneBot 端）。"""

    return isinstance(payload, dict) and "action" in payload and "post_type" not in payload


def is_event_payload(payload: Any) -> bool:
    """判断一帧 JSON 是否是「事件」（OneBot 端 → AstrBot）。"""

    return isinstance(payload, dict) and "post_type" in payload


def action_ok(echo: Any, data: Any = None) -> Dict[str, Any]:
    """构造成功的动作响应（OneBot v11 标准载荷）。"""

    return {"status": "ok", "retcode": 0, "data": data, "echo": echo}


def action_failed(echo: Any, message: str, retcode: int = 1400) -> Dict[str, Any]:
    """构造失败的动作响应；``retcode`` 取 OneBot 常见的 1400/1403/1404。"""

    return {
        "status": "failed",
        "retcode": int(retcode),
        "data": None,
        "echo": echo,
        "message": str(message)[:500],
    }
