"""JPush outbox behaviour: unconfigured deployments and configured delivery payloads.

No network is used; httpx.AsyncClient is replaced with an in-process recorder.
Run with TEMP/TMP pointed at an ASCII path on Windows:
  $env:TEMP="D:\\desktop\\口语\\.tmp"; $env:TMP="D:\\desktop\\口语\\.tmp"
"""
from __future__ import annotations

import json
import logging
import time

import httpx
import pytest

from app.v2.db import DB
from app.v3.schema import ensure_schema
from app.v3.client_api import ensure_client_schema, event_rows
from app.v3 import client_events
from app.v3.client_events import collect_events, dispatch_push, mark_push_unconfigured
from app.v3.identity import set_current_user, reset_current_user

USER = 'u_push'


@pytest.fixture
def db(tmp_path):
    db = DB(str(tmp_path / 'push.db'))
    db.init()
    ensure_schema(db, seed=False)
    ensure_client_schema(db)
    yield db
    db.close()


def seed_event(db, user: str = USER) -> dict:
    """Creates one unread friend message and collects it into the outbox."""
    collect_events(db)  # first launch establishes the historical baseline
    ctx = set_current_user(user)
    friend = db.create_friend({'name': 'Mia'})
    db.add_message(friend['id'], 'friend', 'private transcript text', is_read=False)
    reset_current_user(ctx)
    collect_events(db)
    with db._lock, db.conn:
        db.conn.execute('UPDATE client_events SET next_attempt=0')
    events = event_rows(db, user, 0)
    assert len(events) == 1
    return events[0]


def register_device(db, registration_id: str = 'rid-1', user: str = USER) -> None:
    with db._lock, db.conn:
        db.conn.execute('INSERT INTO client_devices VALUES (?,?,?,?,?,?)',
                        (registration_id, user, 'android', 'xiaomi', 1, time.time()))


def set_preferences(db, value: dict, user: str = USER) -> None:
    with db._lock, db.conn:
        db.conn.execute('INSERT OR REPLACE INTO client_preferences VALUES (?,?)', (user, json.dumps(value)))


def push_row(db) -> dict:
    with db._lock:
        return dict(db.conn.execute('SELECT * FROM client_events').fetchone())


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def raise_for_status(self) -> None:
        if not self.is_success:
            raise httpx.HTTPStatusError('jpush error', request=httpx.Request('POST', 'https://api.jpush.cn/v3/push'),
                                        response=httpx.Response(self.status_code))


class FakeClient:
    def __init__(self, calls: list, status_code: int = 200, raises: Exception | None = None):
        self.calls = calls
        self.status_code = status_code
        self.raises = raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None):
        self.calls.append({'url': url, 'json': json})
        if self.raises is not None:
            raise self.raises
        return FakeResponse(self.status_code)


def configure_credentials(monkeypatch, app_key: str = 'a' * 24, secret: str = 's' * 24) -> None:
    monkeypatch.setenv('JPUSH_APP_KEY', app_key)
    monkeypatch.setenv('JPUSH_MASTER_SECRET', secret)


def clear_credentials(monkeypatch) -> None:
    """Unsets env vars and the .env-backed settings fields for this test process."""
    monkeypatch.delenv('JPUSH_APP_KEY', raising=False)
    monkeypatch.delenv('JPUSH_MASTER_SECRET', raising=False)
    from app.config import get_settings  # noqa: PLC0415

    blank = get_settings().model_copy(update={'jpush_app_key': '', 'jpush_master_secret': ''})
    monkeypatch.setattr('app.config.get_settings', lambda: blank)


def install_fake_httpx(monkeypatch, calls: list, status_code: int = 200, raises: Exception | None = None) -> None:
    monkeypatch.setattr(client_events.httpx, 'AsyncClient',
                        lambda **kwargs: FakeClient(calls, status_code=status_code, raises=raises))


def clear_warned(monkeypatch) -> None:
    monkeypatch.setattr(client_events, '_unconfigured_warned', False)


# ------------------------------------------------------------------ unconfigured

@pytest.mark.asyncio
async def test_missing_credentials_marks_pending_events_unconfigured_and_warns_once(db, monkeypatch, caplog):
    seed_event(db)
    clear_credentials(monkeypatch)
    clear_warned(monkeypatch)

    with caplog.at_level(logging.WARNING):
        assert await dispatch_push(db) == 0
        assert push_row(db)['push_status'] == 'unconfigured'
        # Idempotent: a second worker tick neither fails nor spams the log.
        assert await dispatch_push(db) == 0
        assert push_row(db)['push_status'] == 'unconfigured'

    warnings = [r for r in caplog.records if 'push outbox unconfigured' in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_legacy_pending_rows_are_also_marked_unconfigured(db, monkeypatch):
    seed_event(db)
    with db._lock, db.conn:
        db.conn.execute("UPDATE client_events SET push_status='pending'")
    clear_credentials(monkeypatch)
    clear_warned(monkeypatch)
    assert await dispatch_push(db) == 0
    assert push_row(db)['push_status'] == 'unconfigured'
    # A later successful dispatch of another row must not resurrect this one.
    assert mark_push_unconfigured(db) == 0


# -------------------------------------------------------------------- configured

@pytest.mark.asyncio
async def test_configured_push_sends_account_tagged_generic_notice_and_avatar(db, monkeypatch):
    event = seed_event(db)
    register_device(db)
    configure_credentials(monkeypatch)
    with db._lock, db.conn:
        payload = json.dumps({
            'name': 'Mia',
            'text': 'private transcript text',
            'avatar': '/api/v3/avatar/seed1.svg?g=female',
            'route': '#/chat/f1',
        }, ensure_ascii=False)
        db.conn.execute('UPDATE client_events SET payload=? WHERE id=?', (payload, event['id']))
    calls: list = []
    install_fake_httpx(monkeypatch, calls)

    assert await dispatch_push(db) == 1
    assert len(calls) == 1
    assert calls[0]['url'] == 'https://api.jpush.cn/v3/push'
    body = calls[0]['json']
    assert body['platform'] == 'android'
    assert body['audience'] == {'registration_id': ['rid-1']}
    android = body['notification']['android']
    # Provider-delayed notifications must not expose a previous account's text.
    assert android['alert'] == '你有一条新消息'
    assert android['title'] == 'Sonder'
    assert 'private transcript text' not in json.dumps(body)
    assert android['extras']['user_id'] == USER
    # Deterministic avatar becomes an absolute PNG URL for JPush to download.
    assert android['large_icon'] == 'https://47-104-24-236.sslip.io/api/v3/avatar/seed1.png?g=female'
    assert android['extras']['event_id'] == event['id']
    assert android['extras']['route'].startswith('#/chat/')
    assert android['channel_id'] == 'speakeasy_messages'
    assert push_row(db)['push_status'] == 'sent'


@pytest.mark.asyncio
async def test_sound_disabled_uses_the_silent_channel(db, monkeypatch):
    seed_event(db)
    register_device(db)
    set_preferences(db, {'messages': True, 'interactions': True, 'sound': False, 'theme': 'system'})
    configure_credentials(monkeypatch)
    calls: list = []
    install_fake_httpx(monkeypatch, calls)

    assert await dispatch_push(db) == 1
    assert calls[0]['json']['notification']['android']['channel_id'] == 'speakeasy_messages_silent'


@pytest.mark.asyncio
async def test_disabled_account_preference_suppresses_without_network(db, monkeypatch):
    seed_event(db)
    register_device(db)
    set_preferences(db, {'messages': False, 'interactions': True, 'sound': True, 'theme': 'system'})
    configure_credentials(monkeypatch)
    calls: list = []
    install_fake_httpx(monkeypatch, calls)

    assert await dispatch_push(db) == 0
    assert calls == []
    assert push_row(db)['push_status'] == 'suppressed'


@pytest.mark.asyncio
async def test_permanent_jpush_rejection_marks_failed(db, monkeypatch):
    seed_event(db)
    register_device(db)
    configure_credentials(monkeypatch)
    calls: list = []
    install_fake_httpx(monkeypatch, calls, status_code=401)

    assert await dispatch_push(db) == 0
    row = push_row(db)
    assert row['push_status'] == 'failed'
    assert row['attempts'] == 1


@pytest.mark.asyncio
async def test_transient_network_error_keeps_event_pending_for_retry(db, monkeypatch):
    seed_event(db)
    register_device(db)
    configure_credentials(monkeypatch)
    calls: list = []
    install_fake_httpx(monkeypatch, calls, raises=httpx.ConnectError('offline'))

    assert await dispatch_push(db) == 0
    row = push_row(db)
    assert row['push_status'] == 'pending'
    assert row['attempts'] == 1
    assert row['next_attempt'] > time.time()
