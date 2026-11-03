"""会话状态：一个 WS 连接一个 :class:`Session`，由 :class:`SessionStore` 托管。

契约见 ``docs/CONTRACTS.md`` 第 9 节末尾。额外字段：

- ``turn_id``：本回合编号（从 1 开始，用于协议里的 ``turn_id``）
- ``mode_id``：当前对话模式（``free`` / ``task`` / ``drill``）
- ``last_user_text``：最近一条用户原话（``suggestion`` 消息不带 text 时用它）
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterator

from app.llm.client import ChatMessage
from app.logging_utils import get_logger

log = get_logger(__name__)

#: 系统提示之外的历史消息上限（防御性，``trimmed`` 才是真正的裁剪点）
_HARD_HISTORY_CAP = 200


@dataclass
class Session:
    """单条连接期间的对话状态。"""

    session_id: str
    persona_id: str
    history: list[ChatMessage] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_user_text: str | None = None
    mode_id: str | None = None
    turn_counter: int = 0
    last_seen: float = field(default_factory=time.time)

    # ---------------- 回合编号 ----------------
    def next_turn_id(self) -> int:
        """分配并返回本回合的 ``turn_id``（从 1 开始递增）。"""
        self.turn_counter += 1
        self.touch()
        return self.turn_counter

    # ---------------- 历史 ----------------
    def add_user(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.history.append(ChatMessage(role="user", content=text))
        self.last_user_text = text
        self._cap()
        self.touch()

    def add_assistant(self, text: str) -> None:
        text = (text or "").strip()
        self.history.append(ChatMessage(role="assistant", content=text))
        self._cap()
        self.touch()

    def add_turn(self, user_text: str, assistant_text: str | None = None) -> None:
        """一次性记录一回合（assistant 可为空，例如被打断）。"""
        self.add_user(user_text)
        if assistant_text is not None:
            self.add_assistant(assistant_text)

    def _cap(self) -> None:
        if len(self.history) > _HARD_HISTORY_CAP:
            del self.history[: len(self.history) - _HARD_HISTORY_CAP]

    def trimmed(self, max_turns: int) -> list[ChatMessage]:
        """返回按"轮"裁剪后的历史副本（一轮 = user + assistant）。

        ``max_turns <= 0`` 表示不带历史。结尾为 user 消息时（正常调用场景），
        这里保证不切断一对 user/assistant。
        """
        if max_turns is None or max_turns <= 0:
            return []
        # 每轮最多 2 条（user / assistant）；留一点余量以防 assistant 缺失
        keep = int(max_turns) * 2
        if len(self.history) <= keep:
            return list(self.history)
        cut = self.history[len(self.history) - keep :]
        # 不要以 assistant 开头（会破坏 user/assistant 交替）
        while cut and cut[0].role != "user":
            cut = cut[1:]
        return list(cut)

    # ---------------- 其他 ----------------
    def touch(self) -> None:
        self.last_seen = time.time()

    @property
    def turns(self) -> int:
        """已经完成的回合数（以 assistant 消息计数）。"""
        return sum(1 for m in self.history if m.role == "assistant")

    def reset_history(self) -> None:
        self.history.clear()
        self.last_user_text = None
        self.touch()

    def to_info(self) -> dict[str, object]:
        """``session_info`` 调试消息载荷。"""
        return {
            "type": "session_info",
            "session_id": self.session_id,
            "turns": self.turns,
            "persona_id": self.persona_id,
            "mode": self.mode_id,
        }

    def __len__(self) -> int:  # pragma: no cover - 便利方法
        return len(self.history)


class SessionStore:
    """内存会话表（线程安全）。

    WS 是多连接 + 多事件循环任务并发访问，因此用一把 ``Lock`` 保护 dict；
    临界区只有几次 dict 操作，锁竞争可以忽略。
    """

    def __init__(self, *, ttl_s: float = 3600.0, max_sessions: int = 256) -> None:
        self.ttl_s = float(ttl_s)
        self.max_sessions = int(max_sessions)
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    # ---------------- 生命周期 ----------------
    def create(
        self,
        persona_id: str,
        mode_id: str | None = None,
        *,
        session_id: str | None = None,
    ) -> Session:
        """创建并登记一个新会话。"""
        sid = session_id or uuid.uuid4().hex
        now = time.time()
        session = Session(
            session_id=sid,
            persona_id=persona_id,
            history=[],
            created_at=now,
            last_user_text=None,
            mode_id=mode_id,
            turn_counter=0,
            last_seen=now,
        )
        with self._lock:
            self._evict_locked()
            self._sessions[sid] = session
        log.debug("session created id=%s persona=%s mode=%s", sid, persona_id, mode_id)
        return session

    def get(self, session_id: str) -> Session | None:
        """取会话；过期视为不存在。"""
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if self.ttl_s > 0 and (time.time() - session.last_seen) > self.ttl_s:
                self._sessions.pop(session_id, None)
                log.debug("session expired id=%s", session_id)
                return None
            session.last_seen = time.time()
            return session

    def drop(self, session_id: str) -> None:
        """移除会话（连接关闭时调用）。"""
        if not session_id:
            return
        with self._lock:
            removed = self._sessions.pop(session_id, None)
        if removed is not None:
            log.debug("session dropped id=%s", session_id)

    # ---------------- 维护 ----------------
    def purge_expired(self) -> int:
        """清理过期会话，返回清理数量。"""
        if self.ttl_s <= 0:
            return 0
        now = time.time()
        with self._lock:
            stale = [
                sid
                for sid, s in self._sessions.items()
                if (now - s.last_seen) > self.ttl_s
            ]
            for sid in stale:
                self._sessions.pop(sid, None)
        if stale:
            log.debug("purged %d expired sessions", len(stale))
        return len(stale)

    def _evict_locked(self) -> None:
        """超过上限时按最久未使用淘汰（调用方已持锁）。"""
        if len(self._sessions) < self.max_sessions:
            return
        overflow = len(self._sessions) - self.max_sessions + 1
        ordered = sorted(self._sessions.items(), key=lambda kv: kv[1].last_seen)
        for sid, _ in ordered[:overflow]:
            self._sessions.pop(sid, None)

    # ---------------- 方便调试 ----------------
    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._sessions))


__all__ = ["Session", "SessionStore"]
