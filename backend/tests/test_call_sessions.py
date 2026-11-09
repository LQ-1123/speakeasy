from __future__ import annotations

import pytest

from app.v3.call_sessions import CallSessionRegistry


def test_call_ticket_is_single_use_and_only_owner_can_refresh():
    sessions = CallSessionRegistry(max_active=3)
    call = sessions.create("u_alice", "friend-1", "en", now=100)
    ticket = sessions.take_initial_ticket(call)
    assert sessions.resolve_ticket(call.call_id, ticket, now=101) is call
    assert sessions.resolve_ticket(call.call_id, ticket, now=101) is None

    with pytest.raises(PermissionError):
        sessions.issue_ticket(call.call_id, "u_bob", now=102)


def test_call_ticket_expires_but_session_can_get_a_new_ticket():
    sessions = CallSessionRegistry(max_active=3)
    call = sessions.create("u_alice", "friend-1", "en", now=100)
    ticket = sessions.take_initial_ticket(call)

    assert sessions.resolve_ticket(call.call_id, ticket, now=160) is None
    renewed = sessions.issue_ticket(call.call_id, "u_alice", now=160)
    assert sessions.resolve_ticket(call.call_id, renewed, now=160) is call


def test_disconnected_call_can_resume_only_within_grace_period():
    sessions = CallSessionRegistry(max_active=3, resume_grace_s=20)
    call = sessions.create("u_alice", "friend-1", "en", now=100)
    sessions.take_initial_ticket(call)
    sessions.disconnect(call.call_id, now=100)

    assert sessions.get_resumable(call.call_id, "u_alice", now=119) is call
    assert sessions.get_resumable(call.call_id, "u_bob", now=119) is None
    assert sessions.get_resumable(call.call_id, "u_alice", now=121) is None


def test_call_capacity_is_bounded_and_released_on_end():
    sessions = CallSessionRegistry(max_active=1)
    call = sessions.create("u_alice", "friend-1", "en", now=100)
    sessions.take_initial_ticket(call)

    with pytest.raises(OverflowError):
        sessions.create("u_bob", "friend-2", "en", now=100)

    assert sessions.end(call.call_id, "u_alice")
    assert sessions.create("u_bob", "friend-2", "en", now=101)
