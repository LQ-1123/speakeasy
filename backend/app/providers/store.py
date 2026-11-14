"""SQLite configuration and request budgets; all credentials stay encrypted at rest.

Methods are synchronous and share the application's connection lock. Callers in
async paths use ``asyncio.to_thread``. Resource state is the last persisted health
observation; ``enabled`` is operator configuration, never a health inference.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .crypto import CredentialCipher

PROVIDERS = frozenset(('fish', 'deepgram', 'llm', 'siliconflow'))
STATES = frozenset(('pending', 'active', 'cooldown', 'exhausted', 'disabled', 'draining'))
LIMITS = ('concurrency_limit', 'rpm_limit', 'tpm_limit', 'daily_limit', 'monthly_limit')
DEFAULT_URLS = {'fish': 'https://api.fish.audio', 'deepgram': 'https://api.deepgram.com',
                'llm': 'https://api.openai.com/v1', 'siliconflow': 'https://api.siliconflow.cn/v1'}


class StoreError(ValueError):
    def __init__(self, message, status_code=400, retry_after=0):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _provider(value):
    value = str(value).lower().strip()
    if value not in PROVIDERS:
        raise StoreError('Unknown provider')
    return value


def _text(value, field, maximum=500, *, empty=False):
    value = str(value or '').strip()
    if (not empty and not value) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise StoreError(f'Invalid {field}')
    return value


def _url(value):
    value = _text(value, 'base_url', 2048)
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise StoreError('Invalid base_url')
    if parsed.query or parsed.fragment:
        raise StoreError('base_url cannot contain query parameters or fragments')
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip('/'), '', ''))


def _limits(payload, existing=None):
    result = dict(existing or {})
    for name in LIMITS:
        default = 1 if name == 'concurrency_limit' else 0
        value = payload.get(name, result.get(name, default))
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            raise StoreError(f'Invalid {name}') from None
        if isinstance(value, bool) or number != float(value) or not default <= number <= 1_000_000_000:
            raise StoreError(f'Invalid {name}')
        result[name] = number
    return {key: result[key] for key in LIMITS}


def _object(value, field):
    if not isinstance(value, dict):
        raise StoreError(f'{field} must be an object')
    try:
        encoded = _json(value)
    except (TypeError, ValueError):
        raise StoreError(f'Invalid {field}') from None
    if len(encoded) > 32000:
        raise StoreError(f'{field} is too large')
    # Credentials belong exclusively in api_key, including nested options.
    if re.search(r'"(?:api[_-]?key|authorization|access[_-]?token|secret|password)"\s*:', encoded, re.I):
        raise StoreError(f'{field} cannot contain credentials')
    return encoded


def _balance(value):
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise StoreError('Invalid balance_value') from None
    if isinstance(value, bool) or not math.isfinite(result):
        raise StoreError('Invalid balance_value')
    return result


class ProviderStore:
    def __init__(self, db, data_dir):
        self.db, self.conn, self._lock = db, db.conn, db._lock
        self.data_dir = Path(data_dir)
        with self._lock, self.conn:
            self.conn.executescript('''
                CREATE TABLE IF NOT EXISTS provider_quota_groups (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, name TEXT NOT NULL,
                    concurrency_limit INTEGER NOT NULL DEFAULT 1, rpm_limit INTEGER NOT NULL DEFAULT 0,
                    tpm_limit INTEGER NOT NULL DEFAULT 0, daily_limit INTEGER NOT NULL DEFAULT 0,
                    monthly_limit INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS provider_route_groups (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, name TEXT NOT NULL, model TEXT NOT NULL,
                    activated_once INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(provider, model)
                );
                CREATE TABLE IF NOT EXISTS provider_modes (
                    provider TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
                    activated_once INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS provider_resources (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, name TEXT NOT NULL, base_url TEXT NOT NULL,
                    model TEXT NOT NULL, quota_group_id TEXT NOT NULL REFERENCES provider_quota_groups(id),
                    route_group_id TEXT NOT NULL REFERENCES provider_route_groups(id),
                    encrypted_key TEXT NOT NULL, fingerprint TEXT NOT NULL, masked_key TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'pending',
                    enabled INTEGER NOT NULL DEFAULT 1, capabilities TEXT NOT NULL DEFAULT '{}',
                    options TEXT NOT NULL DEFAULT '{}', balance_value REAL, balance_source TEXT NOT NULL DEFAULT 'unknown',
                    expires_at TEXT, last_error TEXT NOT NULL DEFAULT '', verified_at TEXT,
                    health_until REAL NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
                    health_updated_ns INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(provider, base_url, model, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_provider_resources_route ON provider_resources(provider,model,enabled,state);
                CREATE INDEX IF NOT EXISTS idx_provider_resources_quota ON provider_resources(quota_group_id);
                CREATE INDEX IF NOT EXISTS idx_provider_resources_fingerprint ON provider_resources(provider,fingerprint);
                CREATE TABLE IF NOT EXISTS provider_group_usage (
                    quota_group_id TEXT NOT NULL, day TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
                    tokens INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(quota_group_id,day)
                );
            ''')
            columns = {row[1] for row in self.conn.execute('PRAGMA table_info(provider_resources)').fetchall()}
            if 'health_updated_ns' not in columns:
                self.conn.execute('ALTER TABLE provider_resources ADD COLUMN health_updated_ns INTEGER NOT NULL DEFAULT 0')
        self._cipher = CredentialCipher(self.data_dir, self._has_encrypted_rows)
        from .metrics import ensure_schema
        ensure_schema(self)

    def _has_encrypted_rows(self):
        with self._lock:
            return self.conn.execute('SELECT 1 FROM provider_resources LIMIT 1').fetchone() is not None

    def _row(self, rid):
        row = self.conn.execute('SELECT r.*, q.concurrency_limit,q.rpm_limit,q.tpm_limit,q.daily_limit,q.monthly_limit '
                                'FROM provider_resources r JOIN provider_quota_groups q ON q.id=r.quota_group_id '
                                'WHERE r.id=?', (rid,)).fetchone()
        if row is None:
            raise KeyError('Provider resource not found')
        return dict(row)

    def _public(self, row, secret=False):
        result = dict(row)
        encrypted = result.pop('encrypted_key')
        result.pop('deleted', None)
        result['enabled'] = bool(result['enabled'])
        for key in ('options', 'capabilities'):
            result[key] = json.loads(result[key])
        if secret:
            result['api_key'] = self._cipher.decrypt(encrypted)
        return result

    def get(self, rid, secret=False):
        with self._lock:
            return self._public(self._row(rid), secret)

    def create(self, payload):
        provider = _provider(payload.get('provider'))
        name = _text(payload.get('name'), 'name', 200)
        key = _text(payload.get('api_key'), 'api_key', 8192)
        model = _text(payload.get('model'), 'model', 250)
        url = _url(payload.get('base_url') or DEFAULT_URLS[provider])
        fingerprint = hashlib.sha256((provider + '\0' + key).encode()).hexdigest()
        capabilities = _object(payload.get('capabilities', {}), 'capabilities')
        options = _object(payload.get('options', {}), 'options')
        limits = _limits(payload)
        with self._lock, self.conn:
            if self.conn.execute('SELECT 1 FROM provider_resources WHERE provider=? AND base_url=? AND model=? '
                                 'AND fingerprint=?', (provider, url, model, fingerprint)).fetchone():
                raise StoreError('This credential already exists for this provider and model', 409)
            encrypted = self._cipher.encrypt(key)
            quota_id = payload.get('quota_group_id')
            if not quota_id:
                existing = self.conn.execute('SELECT quota_group_id FROM provider_resources WHERE provider=? '
                                             'AND fingerprint=? LIMIT 1', (provider, fingerprint)).fetchone()
                quota_id = existing[0] if existing else self.upsert_quota_group(
                    dict(provider=provider, name=name, **limits))['id']
            self._check_group('quota', quota_id, provider)
            route_id = payload.get('route_group_id')
            if not route_id:
                existing = self.conn.execute('SELECT id FROM provider_route_groups WHERE provider=? AND model=?',
                                             (provider, model)).fetchone()
                route_id = existing[0] if existing else self.upsert_route_group(
                    dict(provider=provider, name=model, model=model))['id']
            self._check_group('route', route_id, provider, model)
            rid, now = uuid.uuid4().hex, _now()
            values = (rid, provider, name, url, model, quota_id, route_id, encrypted, fingerprint,
                      '••••' + key[-4:] if len(key) > 4 else '••••', capabilities, options,
                      _balance(payload.get('balance_value')), _text(payload.get('balance_source', 'unknown'), 'balance_source', 100),
                      self._expiry(payload.get('expires_at')), now, now)
            self.conn.execute('INSERT INTO provider_resources '
                              '(id,provider,name,base_url,model,quota_group_id,route_group_id,encrypted_key,fingerprint,'
                              'masked_key,capabilities,options,balance_value,balance_source,expires_at,created_at,updated_at) '
                              'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', values)
            return self._public(self._row(rid))

    @staticmethod
    def _expiry(value):
        if value in (None, ''):
            return None
        try:
            datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            raise StoreError('Invalid expires_at') from None
        return str(value)

    def _check_group(self, kind, gid, provider, model=None):
        table = 'provider_quota_groups' if kind == 'quota' else 'provider_route_groups'
        row = self.conn.execute(f'SELECT * FROM {table} WHERE id=?', (gid,)).fetchone()
        if row is None or row['provider'] != provider or (model is not None and row['model'] != model):
            raise StoreError(f'{kind} group does not match provider and model')

    def update(self, rid, patch, revision=None):
        with self._lock, self.conn:
            row = self._row(rid)
            if row['deleted']:
                raise StoreError('Resource is draining', 409)
            expected = revision if revision is not None else patch.get('revision')
            if expected is not None and int(expected) != row['revision']:
                raise StoreError('Resource revision changed; reload before updating', 409)
            fields = {}
            for key in ('name', 'model', 'balance_source'):
                if key in patch:
                    fields[key] = _text(patch[key], key, 250)
            if 'base_url' in patch:
                fields['base_url'] = _url(patch['base_url'])
            for key in ('capabilities', 'options'):
                if key in patch:
                    fields[key] = _object(patch[key], key)
            if 'expires_at' in patch:
                fields['expires_at'] = self._expiry(patch['expires_at'])
            if 'balance_value' in patch:
                fields['balance_value'] = _balance(patch['balance_value'])
            for key in ('quota_group_id', 'route_group_id'):
                if key in patch:
                    fields[key] = patch[key]
            model = fields.get('model', row['model'])
            if model != row['model'] and 'route_group_id' not in fields:
                fields['route_group_id'] = self.upsert_route_group(dict(provider=row['provider'], model=model, name=model))['id']
            self._check_group('quota', fields.get('quota_group_id', row['quota_group_id']), row['provider'])
            self._check_group('route', fields.get('route_group_id', row['route_group_id']), row['provider'], model)
            if any(k in patch for k in LIMITS):
                self.upsert_quota_group(dict(id=fields.get('quota_group_id', row['quota_group_id']), provider=row['provider'],
                                             **{k: patch[k] for k in LIMITS if k in patch}))
            reset = any(k in patch for k in ('api_key', 'base_url', 'model', 'options', 'capabilities'))
            if 'api_key' in patch:
                key = _text(patch['api_key'], 'api_key', 8192)
                fields.update(encrypted_key=self._cipher.encrypt(key),
                              fingerprint=hashlib.sha256((row['provider'] + '\0' + key).encode()).hexdigest(),
                              masked_key='••••' + key[-4:] if len(key) > 4 else '••••')
            if 'enabled' in patch:
                if not isinstance(patch['enabled'], bool):
                    raise StoreError('enabled must be a boolean')
                fields['enabled'] = int(patch['enabled'])
            enabled = fields.get('enabled', row['enabled'])
            if reset:
                # Validation belongs to the secret/endpoint/capability revision,
                # independently of whether the operator currently paused it.
                fields.update(verified_at=None, last_error='', health_until=0)
            if not enabled:
                fields['state'] = 'disabled'
            elif reset:
                fields.update(state='pending', verified_at=None, last_error='', health_until=0)
            elif 'enabled' in fields and enabled:
                fields.update(state='active' if row['verified_at'] else 'pending', last_error='', health_until=0)
            fields.update(revision=row['revision'] + 1, updated_at=_now())
            try:
                changed = self.conn.execute('UPDATE provider_resources SET ' + ','.join(f'{k}=?' for k in fields)
                                            + ' WHERE id=? AND revision=?', (*fields.values(), rid, row['revision']))
                if changed.rowcount != 1:
                    raise StoreError('Resource revision changed; reload before updating', 409)
            except sqlite3.IntegrityError:
                raise StoreError('This credential already exists for this provider and model', 409) from None
            return self._public(self._row(rid))

    def list_resources(self, provider=None, page=1, page_size=25, status=None, route_group_id=None):
        page, page_size = max(1, int(page)), max(1, min(100, int(page_size)))
        where, params = [], []
        if provider:
            where.append('r.provider=?')
            params.append(_provider(provider))
        if status:
            if status not in STATES:
                raise StoreError('Invalid resource status')
            where.append('r.state=?')
            params.append(status)
        if route_group_id:
            where.append('r.route_group_id=?')
            params.append(route_group_id)
        clause = ' WHERE ' + ' AND '.join(where) if where else ''
        with self._lock:
            total = self.conn.execute('SELECT COUNT(*) FROM provider_resources r' + clause, params).fetchone()[0]
            rows = self.conn.execute('SELECT r.id FROM provider_resources r' + clause + ' ORDER BY r.rowid LIMIT ? OFFSET ?',
                                     (*params, page_size, (page - 1) * page_size)).fetchall()
            return dict(items=[self._public(self._row(r[0])) for r in rows], total=total, page=page, page_size=page_size)

    def _list_groups(self, table, provider=None):
        with self._lock:
            rows = self.conn.execute(f'SELECT * FROM {table}' + (' WHERE provider=?' if provider else '') + ' ORDER BY rowid',
                                     (_provider(provider),) if provider else ()).fetchall()
            return [dict(row) for row in rows]

    def list_quota_groups(self, provider=None):
        return self._list_groups('provider_quota_groups', provider)

    def list_route_groups(self, provider=None):
        return self._list_groups('provider_route_groups', provider)

    def upsert_quota_group(self, payload):
        with self._lock, self.conn:
            gid = payload.get('id') or uuid.uuid4().hex
            old = self.conn.execute('SELECT * FROM provider_quota_groups WHERE id=?', (gid,)).fetchone()
            old = dict(old) if old else {}
            if old and payload.get('revision') is not None and int(payload['revision']) != old['revision']:
                raise StoreError('Quota group revision changed; reload before updating', 409)
            provider = _provider(payload.get('provider', old.get('provider')))
            if old and provider != old['provider']:
                raise StoreError('Quota group provider cannot change')
            name = _text(payload.get('name', old.get('name', 'Account quota')), 'name', 200)
            limits = _limits(payload, old)
            self.conn.execute('INSERT INTO provider_quota_groups VALUES (?,?,?,?,?,?,?,?,?) '
                              'ON CONFLICT(id) DO UPDATE SET name=excluded.name,concurrency_limit=excluded.concurrency_limit,'
                              'rpm_limit=excluded.rpm_limit,tpm_limit=excluded.tpm_limit,daily_limit=excluded.daily_limit,'
                              'monthly_limit=excluded.monthly_limit,revision=provider_quota_groups.revision+1',
                              (gid, provider, name, *(limits[k] for k in LIMITS), 1))
            return dict(self.conn.execute('SELECT * FROM provider_quota_groups WHERE id=?', (gid,)).fetchone())

    def upsert_route_group(self, payload):
        with self._lock, self.conn:
            gid = payload.get('id')
            old = self.conn.execute('SELECT * FROM provider_route_groups WHERE id=?', (gid,)).fetchone() if gid else None
            old = dict(old) if old else {}
            if old and payload.get('revision') is not None and int(payload['revision']) != old['revision']:
                raise StoreError('Routing group revision changed; reload before updating', 409)
            provider = _provider(payload.get('provider', old.get('provider')))
            model = _text(payload.get('model', old.get('model')), 'model', 250)
            name = _text(payload.get('name', old.get('name', model)), 'name', 200)
            if old and (provider != old['provider'] or model != old['model']):
                raise StoreError('Routing group provider and model cannot change')
            existing = self.conn.execute('SELECT * FROM provider_route_groups WHERE provider=? AND model=?',
                                         (provider, model)).fetchone()
            if existing and not gid:
                return dict(existing)
            gid = gid or uuid.uuid4().hex
            try:
                self.conn.execute('INSERT INTO provider_route_groups(id,provider,name,model) VALUES (?,?,?,?) '
                                  'ON CONFLICT(id) DO UPDATE SET name=excluded.name,revision=provider_route_groups.revision+1',
                                  (gid, provider, name, model))
            except sqlite3.IntegrityError:
                raise StoreError('A routing group already exists for this provider and model', 409) from None
            return dict(self.conn.execute('SELECT * FROM provider_route_groups WHERE id=?', (gid,)).fetchone())

    def set_provider_enabled(self, provider, enabled):
        provider = _provider(provider)
        if not isinstance(enabled, bool):
            raise StoreError('enabled must be a boolean')
        with self._lock, self.conn:
            # Explicit mode choice always suppresses future implicit activation.
            self.conn.execute('INSERT INTO provider_modes VALUES (?,?,1) ON CONFLICT(provider) '
                              'DO UPDATE SET enabled=excluded.enabled,activated_once=1', (provider, int(enabled)))
        return dict(provider=provider, enabled=enabled)

    def provider_enabled(self, provider):
        with self._lock:
            row = self.conn.execute('SELECT enabled FROM provider_modes WHERE provider=?', (_provider(provider),)).fetchone()
            return bool(row and row[0])

    def verification_result(self, rid, revision, ok, error='', capabilities=None):
        with self._lock, self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            row = self._row(rid)
            if row['revision'] != int(revision) or row['deleted']:
                raise StoreError('Resource revision changed during verification; verify again', 409)
            # Do not persist arbitrary upstream text, which can echo a credential.
            message = '' if ok else 'Provider verification failed'
            state = ('active' if row['enabled'] else 'disabled') if ok else ('pending' if row['enabled'] else 'disabled')
            fields = dict(state=state, last_error=message, verified_at=_now() if ok else None,
                          health_until=0, health_updated_ns=time.time_ns(), updated_at=_now())
            if capabilities is not None:
                fields['capabilities'] = _object(capabilities, 'capabilities')
            self.conn.execute('UPDATE provider_resources SET ' + ','.join(f'{k}=?' for k in fields) + ' WHERE id=?',
                              (*fields.values(), rid))
            if ok:
                self.conn.execute('UPDATE provider_route_groups SET activated_once=1 WHERE id=?', (row['route_group_id'],))
                self.conn.execute('INSERT INTO provider_modes VALUES (?,1,1) ON CONFLICT(provider) '
                                  'DO UPDATE SET enabled=CASE WHEN provider_modes.activated_once=0 THEN 1 ELSE enabled END,'
                                  'activated_once=1', (row['provider'],))
            return self._public(self._row(rid))

    def delete(self, rid):
        with self._lock, self.conn:
            row = self._row(rid)
            self.conn.execute("UPDATE provider_resources SET state='draining',enabled=0,deleted=1,revision=revision+1,updated_at=? WHERE id=?",
                              (_now(), rid))
            return self._public(self._row(rid))

    def finalize_delete(self, rid):
        with self._lock, self.conn:
            self.conn.execute('DELETE FROM provider_resources WHERE id=? AND deleted=1', (rid,))

    def persist_health(self, rid, revision, state, error='', health_until=0, health_updated_ns=None):
        if state not in STATES:
            raise StoreError('Invalid resource state')
        messages = {'active': '', 'cooldown': 'Provider temporarily unavailable', 'exhausted': 'Provider quota exhausted',
                    'disabled': 'Provider credential rejected', 'pending': 'Provider requires verification', 'draining': ''}
        with self._lock, self.conn:
            stamp = time.time_ns() if health_updated_ns is None else int(health_updated_ns)
            self.conn.execute('UPDATE provider_resources SET state=?,last_error=?,health_until=?,health_updated_ns=? '
                              'WHERE id=? AND revision=? AND enabled=1 AND deleted=0 AND health_updated_ns<?',
                              (state, messages[state], float(health_until), stamp, rid, revision, stamp))

    def snapshot(self):
        with self._lock:
            rows = self.conn.execute('SELECT id FROM provider_resources ORDER BY rowid').fetchall()
            resources = []
            for item in rows:
                row = self._row(item[0])
                public = self._public(row, secret=bool(row['enabled'] and row['verified_at'] and not row['deleted']))
                public['deleted'] = bool(row['deleted'])
                resources.append(public)
            modes = {row['provider']: bool(row['enabled']) for row in self.conn.execute('SELECT * FROM provider_modes').fetchall()}
            groups = self.list_quota_groups()
            day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            usage = self.conn.execute('SELECT quota_group_id, '
                                      'SUM(CASE WHEN day=? THEN requests ELSE 0 END),SUM(requests) '
                                      'FROM provider_group_usage WHERE day>=? AND day<? GROUP BY quota_group_id',
                                      (day, day[:7] + '-01', day[:7] + '-32')).fetchall()
            usage = {row[0]: (int(row[1]), int(row[2])) for row in usage}
            for group in groups:
                group['daily_requests'], group['monthly_requests'] = usage.get(group['id'], (0, 0))
                group['usage_day'] = day
            metrics = {row['resource_id']: dict(row) for row in
                       self.conn.execute('SELECT * FROM provider_metric_snapshots').fetchall()}
            return dict(resources=resources, quota_groups=groups, route_groups=self.list_route_groups(), modes=modes,
                        metrics=metrics)

    def reserve_budget(self, group_id, estimated_tokens=0, *, day=None):
        day = day or datetime.now(timezone.utc).strftime('%Y-%m-%d')
        with self._lock, self.conn:
            # Acquire SQLite's writer reservation before checking the ceiling,
            # including when another connection/process reserves concurrently.
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            group = self.conn.execute('SELECT * FROM provider_quota_groups WHERE id=?', (group_id,)).fetchone()
            if group is None:
                raise StoreError('Quota group no longer exists', 409)
            daily, monthly = self._budget_counts(group_id, day)
            if ((group['daily_limit'] and daily >= group['daily_limit']) or
                    (group['monthly_limit'] and monthly >= group['monthly_limit'])):
                raise StoreError('Provider request budget exhausted', 429, 3600)
            tokens = max(0, int(estimated_tokens))
            self.conn.execute('INSERT INTO provider_group_usage VALUES (?,?,1,?) ON CONFLICT(quota_group_id,day) '
                              'DO UPDATE SET requests=requests+1,tokens=tokens+excluded.tokens', (group_id, day, tokens))
        return dict(quota_group_id=group_id, day=day, estimated_tokens=tokens,
                    daily_requests=daily + 1, monthly_requests=monthly + 1)

    def reconcile_budget(self, reservation, actual_tokens=None):
        if actual_tokens is None:
            return
        difference = max(0, int(actual_tokens)) - reservation['estimated_tokens']
        with self._lock, self.conn:
            self.conn.execute('UPDATE provider_group_usage SET tokens=MAX(0,tokens+?) WHERE quota_group_id=? AND day=?',
                              (difference, reservation['quota_group_id'], reservation['day']))

    def _budget_counts(self, gid, day):
        row = self.conn.execute('SELECT COALESCE(SUM(CASE WHEN day=? THEN requests ELSE 0 END),0),COALESCE(SUM(requests),0) '
                                'FROM provider_group_usage WHERE quota_group_id=? AND day>=? AND day<?',
                                (day, gid, day[:7] + '-01', day[:7] + '-32')).fetchone()
        return int(row[0]), int(row[1])

    def budget_stats(self, group_id, *, day=None):
        day = day or datetime.now(timezone.utc).strftime('%Y-%m-%d')
        with self._lock:
            daily, monthly = self._budget_counts(group_id, day)
            return dict(daily_requests=daily, monthly_requests=monthly)

    def import_legacy(self, provider, payload):
        # Explicit, reviewable import only; importing does not verify or activate.
        provider = _provider(provider)
        key = _text(payload.get('api_key'), 'api_key', 8192)
        fingerprint = hashlib.sha256((provider + '\0' + key).encode()).hexdigest()
        url = _url(payload.get('base_url') or DEFAULT_URLS[provider])
        model = _text(payload.get('model'), 'model', 250)
        with self._lock:
            existing = self.conn.execute('SELECT id FROM provider_resources WHERE provider=? AND base_url=? '
                                         'AND model=? AND fingerprint=?', (provider, url, model, fingerprint)).fetchone()
            if existing:
                return self._public(self._row(existing[0]))
            return self.create(dict(payload, provider=provider))
