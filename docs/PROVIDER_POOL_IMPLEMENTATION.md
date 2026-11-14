# Multi-account provider resources

Approved scope: Fish, Deepgram, LLM and SiliconFlow STT. Same-model routing only.
Admin adds a key, runs a small real business probe, and successful verification
activates it without restart. No real provider calls during implementation tests.

## Tasks

- [x] Encrypted SQLite store, quota/routing groups, revisions and migration.
- [x] Bounded per-loop leases with normalized least-finish EWMA selection,
      independent/shared quotas, cooldown, breaker, persisted budgets and metrics.
- [x] Fish/STT/Deepgram adapters, pinned streaming sessions and capability handling.
- [x] LLM adapter, lifecycle, admin CRUD/probe APIs and legacy rollback paths.
- [x] Admin provider tables/forms, verify/activate, drain, quotas, route filtering.
- [x] Offline integration, full regressions, bundle build and synthetic pool load.
- [x] Independent review and documentation of release/soak limitations.

## Runtime decisions

Keep one process, SQLite WAL, existing worktree and all previous user edits.
No signup automation, extra paid infrastructure, automatic recharge or real tests.
Registration credits and balances are never invented from a provider name.
Resources have no fixed stored-count cap; pagination25, lazy clients16,
HTTP32, upstream WS16, chat32+queue64, call registry remains3.
Maintain existing full-turn30s deadline and per-user foreground ownership.
Same quota group keys share caps; no active resource means bounded wait/reject,
not a hidden fallback that bypasses quota. A route never activated in the pool
may still use legacy configuration until it is explicitly configured/verified.
Pool disable is an explicit provider rollback switch.

## Core contract for parallel work

`ProviderStore(db, data_dir)` owns schema and encrypted secrets. Sync methods:
`create(payload)`, `update(id, patch, revision=None)`, `get(id, secret=False)`,
`list_resources(provider=None, page=1, page_size=25, status=None, route_group_id=None)`,
`list_quota_groups(provider=None)`, `upsert_quota_group(payload)`,
`list_route_groups(provider=None)`, `upsert_route_group(payload)`,
`set_provider_enabled(provider, enabled)`, `provider_enabled(provider)`,
`verification_result(id, revision, ok, error='', capabilities=None)`,
`delete(id)`, `import_legacy(provider, payload)`. Mutations return public resource
dicts. `get(...,secret=True)` additionally has api_key, only server internals.

Flat create payload: provider enum fish/deepgram/llm/siliconflow; name, api_key,
base_url, model; optional quota_group_id/route_group_id; otherwise create groups.
concurrency_limit1, rpm_limit0, tpm_limit0, daily_limit0, monthly_limit0,
capabilities{}, options{}, balance_value:null, balance_source:unknown, expires_at:null.
Resource public fields include id/provider/name/base_url/model, quota_group_id,
route_group_id, revision, state, enabled, masked_key/fingerprint, capabilities,
last_error, verified_at and group limits. List response items/total/page/page_size.

`ProviderPool(store, http_limit=32, ws_limit=16, queue_limit=64, client_limit=16)`:
`await reload()`, `applies(provider, model)` (active or previously activated route,
even when temporarily unavailable), `stats(provider=None)`, `resource_stats(id)`,
`async with lease(provider, model, capability=None, priority='chat', deadline=None)`.
Lease fields: resource_id/quota_group_id/revision/api_key/base_url/model/options;
`mark_output_started()`, `success(usage=None)`, `failure(status=0, code='', retry_after=None)`;
context always releases exactly once. No await under counter locks.
`async with client(lease, factory, kind='http')` lazily leases bounded cache client;
close idle evictions only; client factory consumes lease and closes via aclose/close.
`await aclose()`. Verification can use a dedicated probe lease or bounded probe
helper without activating a key. Expose server-only `get_pool()/bind_pool()/unbind_pool()`
per running loop; no full credentials in stats or errors. Add `PoolError` with
status_code/retry_after. Pool lease classifies provider HTTP errors when possible;
adapters annotate failures before wrapped errors lose status.

Admin prefix `/api/admin/provider-resources`: GET list, POST create,
PATCH `/{id}`, POST `/{id}/verify`, DELETE `/{id}`;
GET/POST quota-groups and routing-groups; GET stats; PUT `/{provider}/mode` {enabled}.
Static paths registered before /{id}. POST/PATCH response {resource};
verify {ok,resource,message}; groups {items}; stats {providers,resources?};
mode {provider,enabled}. Parent admin auth/CSRF/audit reused.

## Acceptance

Shared3keys at cap5 stays5; independent3groups at5 reaches15. 1000 stored keys
do not instantiate clients at startup. Cancellation, deadlines, leases, drain,
stale verification, duplicate fingerprints, budgets and restart tested offline.
Models/voice identity preserved; no failover after partial output or ambiguous
network timeout. At most two explicitly rejected-before-output candidates.
Synthetic100users/3.33rps/3sTTS/15slots runs available through isolated fake tools;
30-minute real2C2GLinux/provider acceptance remains an explicitly measured release check.
