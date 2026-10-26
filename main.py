"""空 @ 接话：保留唤醒词，让空 @ 走正常上下文。

框架的唤醒检查阶段会剪掉消息开头的唤醒词；内置的空 @ 处理又会把
“只 @”换成一段固定提示词，并且不把正常对话上下文交给模型。

这个插件在唤醒检查之后、内置空 @ 处理之前运行：

- 带内容的唤醒词：把被剪掉的唤醒词补回消息，避免“为什么”这类词被吃掉；
- 只 @ / 只唤醒：把这条消息重新摆成“@ + 一句话”的普通形状，让正常
  流程带着完整上下文去回复；不会塞固定提示词；
- 带 @ 的正常内容：照旧去掉 @，不额外处理；
- 命令：完全不碰，避免指令被补回的唤醒词影响。

空 @ / 只唤醒回复完后，仍然保留原来的等待时间；等待只认刚才那个人。
"""

from __future__ import annotations

import asyncio
import copy
from sys import maxsize

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.utils.session_waiter import (
    USER_SESSIONS,
    SessionController,
    SessionFilter,
    session_waiter,
)


class _SenderSessionFilter(SessionFilter):
    """同一个群里的等待只认刚才 @ 的这个人，不抢别人的消息。"""

    def filter(self, event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"


@register(
    "astrbot_plugin_empty_mention_plus",
    "大肥鱼、coalblock666710",
    "空 @ 或只唤醒时保留完整上下文，并补回被剪掉的唤醒词",
    "1.3.0",
)
class EmptyMentionPlus(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        self.conf = config if config is not None else {}

    # ------------------------------------------------------------------ 配置

    def _cfg(self, key: str, default):
        try:
            value = self.conf.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _int_cfg(
        self,
        key: str,
        default: int,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        try:
            value = int(self._cfg(key, default))
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    # -------------------------------------------------------------- 事件判断

    @staticmethod
    def _has_command_handler(event: AstrMessageEvent) -> bool:
        """当前消息是否已经被某个指令处理器接管。"""
        try:
            handlers = event.get_extra("activated_handlers") or []
        except Exception:
            return False
        for handler in handlers:
            for event_filter in getattr(handler, "event_filters", []) or []:
                if isinstance(event_filter, (CommandFilter, CommandGroupFilter)):
                    return True
        return False

    @staticmethod
    def _plain_text(messages: list) -> str:
        parts: list[str] = []
        for message in messages:
            if isinstance(message, Comp.Plain):
                text = getattr(message, "text", "") or ""
                if text:
                    parts.append(text)
        return "".join(parts).strip()

    @staticmethod
    def _is_wordlike_prefix(prefix: str) -> bool:
        """只补回有语义的唤醒词，不补斜杠或纯符号。"""
        if not prefix or prefix.startswith("@"):
            return False
        for char in prefix:
            if char.isalnum() or "\u4e00" <= char <= "\u9fff":
                return True
        return False

    def _restore_wake_text(
        self,
        event: AstrMessageEvent,
        messages: list,
        wake_prefixes: list[str],
    ) -> None:
        """把唤醒检查阶段剪掉的唤醒词补回消息文本。"""
        if not self._cfg("restore_wake_word", True):
            return
        original = self._plain_text(messages)
        if not original:
            return

        current = (event.message_str or "").strip()
        candidates: list[str] = []
        for raw_prefix in wake_prefixes:
            prefix = str(raw_prefix or "").strip()
            if not prefix or not self._is_wordlike_prefix(prefix):
                continue
            if not original.startswith(prefix):
                continue
            suffix = original[len(prefix) :].strip()
            if current == suffix:
                candidates.append(prefix)

        if not candidates:
            return
        prefix = max(candidates, key=len)
        event.message_str = original
        self.logger.debug(
            "补回唤醒词：%s（补回后 %s 字）",
            prefix,
            len(original),
        )

    def _empty_text(
        self,
        event: AstrMessageEvent,
        messages: list,
        wake_prefixes: list[str],
    ) -> str | None:
        """判断是不是“只 @”或“只发唤醒词”，是则返回要交给模型的文本。"""
        if len(messages) != 1:
            return None

        message = messages[0]
        keep_at = bool(self._cfg("empty_at_keep_at", True))
        if isinstance(message, Comp.At):
            if str(message.qq) != str(event.get_self_id()):
                return None
            name = str(getattr(message, "name", "") or "").strip()
            if not name or name.isdigit():
                name = str(self._cfg("at_fallback_name", "机器人") or "机器人").strip()
            name = name.lstrip("@").strip() or "机器人"
            return f"@{name}" if keep_at else name

        if isinstance(message, Comp.Plain):
            text = (getattr(message, "text", "") or "").strip()
            if not text or text not in wake_prefixes:
                return None
            if text in ("/", "／"):
                fallback = str(
                    self._cfg("at_fallback_name", "机器人") or "机器人"
                ).strip()
                fallback = fallback.lstrip("@") or "机器人"
                return f"@{fallback}" if keep_at else fallback
            return text

        return None

    @staticmethod
    def _has_image(messages: list) -> bool:
        for message in messages:
            if isinstance(message, Comp.Image):
                return True
        return False

    @staticmethod
    def _wake_prefixes(cfg: dict) -> list[str]:
        prefixes = cfg.get("wake_prefix", []) if isinstance(cfg, dict) else []
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        if not isinstance(prefixes, (list, tuple, set)):
            return []
        return [str(prefix).strip() for prefix in prefixes if str(prefix).strip()]

    # ------------------------------------------------------------------ 等待

    async def _wait_for_followup(self, event: AstrMessageEvent) -> None:
        wait_seconds = self._int_cfg("wait_seconds", 60, 1, 300)

        @session_waiter(wait_seconds)
        async def empty_mention_waiter(
            controller: SessionController,
            next_event: AstrMessageEvent,
        ) -> None:
            text = (next_event.message_str or "").strip()
            if not text and not self._has_image(next_event.get_messages()):
                return
            next_event.message_obj.message.insert(
                0,
                Comp.At(
                    qq=next_event.get_self_id(),
                    name=next_event.get_self_id(),
                ),
            )
            new_event = copy.copy(next_event)
            self.context.get_event_queue().put_nowait(new_event)
            next_event.stop_event()
            controller.stop()

        try:
            await empty_mention_waiter(
                event,
                session_filter=_SenderSessionFilter(),
            )
        except TimeoutError:
            pass
        except Exception:
            self.logger.exception("空 @ 等待失败。")

    # ------------------------------------------------------------------ 主逻辑

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize)
    async def handle_empty_mention_plus(self, event: AstrMessageEvent):
        if not self._cfg("enabled", True):
            return
        if self._has_command_handler(event):
            return

        try:
            cfg = self.context.get_config(umo=event.unified_msg_origin)
        except Exception:
            cfg = {}
        wake_prefixes = self._wake_prefixes(cfg)
        messages = event.get_messages()

        # 顺序很重要：先判断是不是空 @ / 只唤醒，再决定要不要裁 @。
        raw_text = self._empty_text(event, messages, wake_prefixes)
        if raw_text is None:
            self._restore_wake_text(event, messages, wake_prefixes)
            return

        sender_session = f"{event.unified_msg_origin}:{event.get_sender_id()}"
        if (
            event.unified_msg_origin in USER_SESSIONS
            or sender_session in USER_SESSIONS
        ):
            return

        # 把空 @ / 只唤醒重新摆成“@ + 一句话”的普通消息形状：
        # - 两个消息段可以绕开内置空 @ 处理；
        # - message_str 直接是 @ 文本，正常流程会带着完整上下文回复。
        event.message_str = raw_text
        event.message_obj.message = [
            Comp.At(
                qq=event.get_self_id(),
                name=event.get_self_id(),
            ),
            Comp.Plain(text=raw_text),
        ]

        self.logger.debug(
            "空 @ 改为普通消息：%s，等待 %s 秒",
            raw_text,
            self._int_cfg("wait_seconds", 60, 1, 300),
        )

        asyncio.create_task(self._wait_for_followup(event))
        # 让等待任务先注册，再交回正常流程去带完整上下文请求模型。
        await asyncio.sleep(0)
