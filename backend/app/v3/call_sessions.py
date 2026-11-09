"""Bounded, short-lived in-memory call session and resume-ticket registry."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass


@dataclass
class CallSession:
    call_id: str
    user_id: str
    friend_id: str
    language: str
    ticket_hash: str
    ticket_expires_at: float
    connected: bool = False
    disconnected_at: float | None = None
    ending: bool = False
    created_at: float = 0
    request_id: str = ""


class CallSessionRegistry:
    """Sessions live only in the issuing worker and expire instead of growing unbounded."""

    def __init__(
        self,
        *,
        max_active: int = 3,
        ticket_ttl_s: int = 60,
        resume_grace_s: int = 20,
    ) -> None:
        self.max_active = max(1, int(max_active))
        self.ticket_ttl_s = max(10, int(ticket_ttl_s))
        self.resume_grace_s = max(1, int(resume_grace_s))
        self._calls: dict[str, CallSession] = {}

    @staticmethod
    def _digest(ticket: str) -> str:
        return hashlib.sha256(ticket.encode("utf-8")).hexdigest()

    def _purge(self, now: float) -> None:
        for call_id, session in list(self._calls.items()):
            disconnected_expired = (
                not session.connected
                and session.disconnected_at is not None
                and now - session.disconnected_at > self.resume_grace_s
            )
            initial_expired = not session.connected and session.disconnected_at is None and now > session.created_at + 60
            if session.ending or disconnected_expired or initial_expired:
                self._calls.pop(call_id, None)

    def create(
        self,
        user_id: str,
        friend_id: str,
        language: str,
        *,
        now: float | None = None,
        request_id: str = "",
    ) -> CallSession:
        now = time.time() if now is None else float(now)
        self._purge(now)
        if request_id:
            for existing in self._calls.values():
                if existing.user_id == user_id and existing.request_id == request_id:
                    if existing.friend_id != friend_id:
                        raise ValueError("request-id-conflict")
                    existing._issued_ticket = self.issue_ticket(existing.call_id, user_id, now=now)
                    return existing
        if len(self._calls) >= self.max_active:
            raise OverflowError("call-capacity-reached")
        ticket = secrets.token_urlsafe(32)
        call = CallSession(
            call_id="call_" + uuid.uuid4().hex,
            user_id=user_id,
            friend_id=friend_id,
            language=language,
            ticket_hash=self._digest(ticket),
            ticket_expires_at=now + self.ticket_ttl_s,
            created_at=now,
            request_id=request_id,
        )
        self._calls[call.call_id] = call
        call._issued_ticket = ticket  # type: ignore[attr-defined]
        return call

    def take_initial_ticket(self, call: CallSession) -> str:
        ticket = getattr(call, "_issued_ticket", "")
        if ticket:
            del call._issued_ticket
        return ticket

    def get_resumable(
        self, call_id: str, user_id: str, *, now: float | None = None
    ) -> CallSession | None:
        now = time.time() if now is None else float(now)
        self._purge(now)
        call = self._calls.get(call_id)
        if call is None or call.user_id != user_id or call.ending:
            return None
        if call.disconnected_at is not None and now - call.disconnected_at > self.resume_grace_s:
            return None
        return call

    def issue_ticket(
        self, call_id: str, user_id: str, *, now: float | None = None
    ) -> str:
        now = time.time() if now is None else float(now)
        self._purge(now)
        call = self._calls.get(call_id)
        if call is not None and not hmac.compare_digest(call.user_id, user_id):
            raise PermissionError("call-session-owner-mismatch")
        if self.get_resumable(call_id, user_id, now=now) is None or call is None:
            raise KeyError("call-session-unavailable")
        ticket = secrets.token_urlsafe(32)
        call.ticket_hash = self._digest(ticket)
        call.ticket_expires_at = now + self.ticket_ttl_s
        return ticket

    def resolve_ticket(
        self,
        call_id: str,
        ticket: str,
        *,
        now: float | None = None,
    ) -> CallSession | None:
        now = time.time() if now is None else float(now)
        self._purge(now)
        call = self._calls.get(call_id)
        if (
            call is None
            or call.ending
            or call.connected
            or now >= call.ticket_expires_at
            or not hmac.compare_digest(call.ticket_hash, self._digest(ticket))
        ):
            return None
        call.ticket_hash = ""
        call.ticket_expires_at = 0
        call.connected = True
        call.disconnected_at = None
        return call

    def disconnect(self, call_id: str, *, now: float | None = None) -> None:
        call = self._calls.get(call_id)
        if call is not None:
            call.connected = False
            call.disconnected_at = time.time() if now is None else float(now)

    def end(self, call_id: str, user_id: str) -> bool:
        call = self._calls.get(call_id)
        if call is None or call.user_id != user_id:
            return False
        self._calls.pop(call_id, None)
        return True

    def owned(self, call_id: str, user_id: str) -> CallSession | None:
        """Administrative cleanup must still see an expired session for summary."""
        call = self._calls.get(call_id)
        return call if call and call.user_id == user_id else None

    def active_for(self, user_id: str) -> int:
        return sum(call.user_id == user_id and not call.ending for call in self._calls.values())

    def active_count(self) -> int:
        self._purge(time.time())
        return len(self._calls)

    def contains(self, call_id: str) -> bool:
        return call_id in self._calls
