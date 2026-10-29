"""Conservative, short-lived follow-up sessions after confirmed bot replies."""

from __future__ import annotations

import math
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

Classification = Literal["followup", "stop", "none"]

# Explicit addressing belongs to the Telegram handler, which knows bot entities.
# A follow-up must never claim a message visibly addressed to someone else.
MENTION = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{3,32}\b")
STOP = re.compile(
    r"(?:(?:请|麻烦)?(?:你)?(?:先)?"
    r"(?:别说话|不要回复|不用回复|别回复|停止回答|停止回复|到此为止|不用回答|结束对话)"
    r"(?:了|吧)?(?:[,，]? ?谢谢)?"
    r"|(?:please )?(?:stop(?: replying| responding)?|don't reply|do not reply|"
    r"no need to (?:reply|answer)|that's all)(?:,? please)?)"
)
FOLLOWUP = re.compile(
    r"(?:还有(?:呢|吗|么)?|然后呢|接着呢|接下来呢|为什么|为啥|为何|"
    r"为什么(?:会这样|这么说|是这样|不行|可以|不可以)|"
    r"(?:换一个|换一种|换个例子|换种说法|再来一个|再给一个)(?:吧)?|"
    r"(?:请|能不能|可以)?(?:继续|展开(?:讲讲|说说)?|说详细(?:点|一点)|"
    r"详细(?:说说|解释(?:一下)?)|举(?:个|一个)例子|解释一下|具体(?:一点|点)|"
    r"再说一遍|没看懂|不太明白|什么意思)(?:一下|吧|呢|吗)?|"
    r"那[^\n]{1,60}呢|"
    r"(?:please )?(?:continue|go on|why|why is that|how so|another(?: one| example)?|"
    r"(?:give|show)(?: me)? (?:an|another) example|explain(?: more| further| that| again)?|"
    r"(?:more|another) details?|i (?:do not|don't) understand)(?:,? please)?)"
)


@dataclass(frozen=True, slots=True)
class FollowupSession:
    user_id: int
    bot_message_id: int
    replied_at: float


def is_stop_signal(text: str) -> bool:
    """A literal ending request; the caller must verify conversation ownership."""
    if MENTION.search(text):
        return False
    normalized = " ".join(text.casefold().split()).strip(" \t\n\r?!？！。.,，;；:：~～")
    return bool(normalized and len(normalized) <= 160 and STOP.fullmatch(normalized))


class FollowupTracker:
    """Keep at most one active speaker per group topic, without model calls.

    Sessions are intentionally not persisted. Only record_reply extends their
    deadline; classifying chatter never extends a session. Zero TTL disables
    implicit follow-ups. Direct mentions remain the handler's responsibility.
    """

    def __init__(
        self,
        ttl_seconds: float = 120,
        *,
        clock: Callable[[], float] = time.time,
        max_sessions: int = 1000,
    ) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds < 0 or max_sessions < 1:
            raise ValueError("Follow-up TTL must be finite and nonnegative; capacity must be positive")
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self.max_sessions = max_sessions
        self._sessions: OrderedDict[tuple[int, int | None], FollowupSession] = OrderedDict()

    def _prune(self, now: float) -> None:
        for key, session in list(self._sessions.items()):
            if not 0 <= now - session.replied_at < self.ttl_seconds:
                self._sessions.pop(key, None)

    def record_reply(
        self, chat_id: int, thread_id: int | None, user_id: int, bot_message_id: int
    ) -> None:
        now = self.clock()
        self._prune(now)
        if self.ttl_seconds == 0 or not math.isfinite(now):
            return
        key = (chat_id, thread_id)
        self._sessions.pop(key, None)
        self._sessions[key] = FollowupSession(user_id, bot_message_id, now)
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)

    def classify(
        self,
        chat_id: int,
        thread_id: int | None,
        user_id: int,
        text: str,
        reply_to_user_id: int | None = None,
        bot_user_id: int | None = None,
    ) -> Classification:
        now = self.clock()
        self._prune(now)
        key = (chat_id, thread_id)
        session = self._sessions.get(key)
        if session is None or session.user_id != user_id:
            return "none"
        if reply_to_user_id is not None and reply_to_user_id != bot_user_id:
            return "none"
        if MENTION.search(text):
            return "none"
        normalized = " ".join(text.casefold().split()).strip(" \t\n\r?!？！。.,，;；:：~～")
        if not normalized or len(normalized) > 160:
            return "none"
        if is_stop_signal(normalized):
            self._sessions.pop(key, None)
            return "stop"
        return "followup" if FOLLOWUP.fullmatch(normalized) else "none"

    def clear(self, chat_id: int, thread_id: int | None = None) -> None:
        """Clear exactly one topic; None selects the main (non-forum) topic."""
        self._sessions.pop((chat_id, thread_id), None)

    def clear_chat(self, chat_id: int) -> None:
        """Clear every topic when a group pauses or its policy changes."""
        for key in [key for key in self._sessions if key[0] == chat_id]:
            self._sessions.pop(key, None)
