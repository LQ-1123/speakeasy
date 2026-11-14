# 2C2G chat capacity implementation

Approved target: 100 users, one turn every 30 seconds, persona audio for every reply,
maximum 30 seconds from admission to audio completion. Existing 100 Mbps server;
no new paid infrastructure or provider tier. This is a target to measure, not a guarantee.

This is the original capacity-phase implementation ledger. Its short single-key
measurements used 10 active chat turns and 20 waiting turns; they are historical
settings, not the current initialized application's defaults. Current startup
sets chat admission to 32/64 through the provider runtime, unless explicit
`SE_CHAT_CONCURRENCY` / `SE_CHAT_QUEUE_SIZE` values override it. The admission
constructor alone still has a 10/20 fallback. See [current runtime and historical
measurements](PERFORMANCE_CAPACITY.md) and [provider-pool policy](PROVIDER_POOLS.md).

## Tasks

- [x] Bound foreground admission, per-user work, provider queues and deadlines; expose audio state.
- [x] Remove automatic extra LLM calls, coalesce memory and yield background work to chat.
- [x] Index persona event membership and expressions; batch chat writes and friend metadata.
- [x] Bound voice input/decoding, reuse STT connections and correct MP3 duration handling.
- [x] Bound client audio prefetch, target event refresh, improve reconnect and static delivery.
- [x] Align load generator with the client, add fixed arrival/burst tests and resource provenance.
- [x] Run offline regressions and independent review; document deploy checks and measured limits.

## Integration decisions

The capacity phase worked in the existing dirty checkout and targeted its modifications.
Preserve the pre-existing changes; source snapshot is `.tmp/capacity-implementation/before.zip`.
That phase did not authorize commits, deployment or real-provider calls; this
historical boundary is separate from a subsequently authorized GitHub release.
Keep one application process and SQLite WAL. Account/provider quotas remain configurable;
never infer a paid/free account quota from a model name. Missing monetary budget means
no claim of a verified monthly spending ceiling; use optional explicit usage budgets.

## Acceptance

Offline tests cover cancellation, fair bounded queues, deadlines, idempotent sends,
audio status and retries, owner isolation, migrations and large historical data.
Performance reports distinguish offered/completed/rejected work and playable audio.
100-user acceptance requires no capacity rejection at 3.33 turns/s, 99% playable audio
within 30s, bounded queues and whole-system resource measurements on 2C2G Linux.
Local fake-provider tests do not establish that production capacity.
