# Reproducible capacity measurements

The approved deployment target is 100 users sending one turn per 30 seconds
(3.33 turns/s), no capacity rejection, at least 99% decodable reply audio within
30 seconds, bounded queues, and measured whole-system resources on 2 CPU / 2 GB
Linux. A local fake-provider smoke verifies the tooling and request path; it
does not establish production capacity, provider availability or a spending cap.

## Runtime policy and deployment

The normal application startup initializes the provider runtime and defaults to
32 foreground turns and 64 waiting turns (`SE_CHAT_CONCURRENCY` /
`SE_CHAT_QUEUE_SIZE`). Explicit environment values override these defaults,
including values retained from an older deployment. The `ChatAdmission`
constructor in `backend/app/capacity.py` still falls back to 10/20; that is also
the admission setting used by the earlier single-key measurements below, not
the effective default after `backend/app/providers/runtime.py` initializes the
current application. Runtime initialization applies 32/64 even before individual
provider routes are verified and activated. Saved-key count does not raise these
local caps. See [the current provider-pool policy](PROVIDER_POOLS.md).

Each user can have one accepted turn. The 30-second deadline includes admission,
recognition, response generation and persona audio. Overload returns HTTP 429
with `Retry-After`; timeout returns 504. Audio failure is an explicit failed
state on the existing message, with retry that does not repeat LLM generation.
In-process pending audio is marked failed at restart so it remains retryable.

`SE_CHAT_AUTO_SUGGESTIONS=0` makes polishing an explicit per-message action.
Memory extraction is coalesced and limited to one background generation; both
memory and maintenance yield before further provider requests when chat is busy.
The legacy single-key paths retain LLM 10 active/20 waiting, STT 4/20 and
Fish 5/20 defaults. An activated model pool instead uses verified account/shared
quota-group limits plus the separate local defaults of 32 active provider HTTP
requests, 16 upstream WebSockets, 64 waiting tasks and 16 cached HTTP clients.
Those transport limits do not create provider quota or guarantee chat capacity.
Confirm actual account allowances and any explicit environment overrides before
changing the single-key or pool settings.

Set `SE_LLM_DAILY_REQUESTS` and `SE_LLM_MONTHLY_REQUESTS` from the existing budget.
Zero explicitly disables those ceilings. Counters persist in SQLite by UTC day
and calendar month and count actual request attempts, including retries and
background work, before dispatch. They are conservative request ceilings, not
currency caps: token usage/prices and free-provider limits are still needed to
guarantee a monthly bill. No new paid provider or tier is selected automatically.

Use `backend/requirements-cloud.txt`; build the static frontend on the development
machine (`cd frontend; npm run build:backend`) rather than on the 2GB server.
Keep one Uvicorn worker: provider limits and foreground ownership are process-local.
Database migration is idempotent and adds indexes, count triggers, event membership
and audio metadata. Back up SQLite with its backup API before rollout and allow
the one-time historical backfill to finish. Sequentially copying a live .db,
WAL and SHM is not an atomic backup. Follow the maintenance and restoration
procedure in [the deployment guide](../deploy/README.md).

The nginx example serves only public hashed assets from a worker-readable path.
After installing the build, stage them on the Linux server:

```sh
install -d -m 755 /var/www/speakeasy/_next
cp -a backend/app/static/app/_next/static /var/www/speakeasy/_next/
find /var/www/speakeasy/_next/static -type d -exec chmod 755 {} +
find /var/www/speakeasy/_next/static -type f -exec chmod 644 {} +
nginx -t
```

Do not grant nginx access to the private data directory or publish private audio
as a static alias. API/audio owner checks remain in Python. Update server_name,
TLS configuration and static staging on each deployment. Verify a cold-cache
JS/CSS load, authenticated audio, event sockets and calls after reloading nginx.
Local changes are not a production deployment or 2C2G Linux acceptance test.

## Isolated offline smoke

Use a working Python interpreter with the application's dependencies installed.
The wrapper also appends the repository `.venv/Lib/site-packages` as a fallback
on Windows, after native interpreter packages.

```powershell
python scripts/perf/smoke.py --out-dir .tmp/perf-arrival-001 --users 4 --duration 4 --rate 1
python scripts/perf/smoke.py --out-dir .tmp/perf-burst-001 --users 20 --duration 1 --mode burst
python scripts/perf/smoke.py --out-dir .tmp/perf-tts-3s --users 100 --duration 30 --rate 3.333333 --fish-base 3 --maintenance
```

Each output directory must be new. The orchestrator seeds synthetic accounts
and histories there, starts owned fake-upstream and actual backend processes,
waits for readiness, runs the client sequence, writes `result.json` and logs,
and cleans up owned processes and their descendants. It never clears an existing
output directory. Production environment keys are stripped, `.env` loading is
disabled in both dotenv and BaseSettings, and provider addresses point to
loopback. Python socket audit hooks reject non-loopback DNS and connections.
The external provider budget is exactly zero. Synthetic keys are not real keys.

These commands run the checked-out runtime and its recorded configuration;
they do not automatically reproduce an earlier source snapshot or its 10/20
admission limits. Compare source fingerprints and explicit settings with the
report being reproduced.

Default smoke delays are deliberately short: fake LLM TTFT 10 ms, fake token
rate 2,000/s, fake TTS fixed delay 10 ms. These are test inputs, not vendor claims.
Override TTS delay with `--fish-base` and `--fish-per-char`, LLM first-token delay
with `--llm-ttft`, and enable normal background maintenance with `--maintenance`.
The forwarded fake parameters are recorded under `configuration.fake_upstream`
and `upstream.configuration`; enabled maintenance appears in server configuration.
Fake TTS supports `format=wav` only and returns valid 24 kHz mono PCM silence.
It rejects unsupported formats instead of returning mislabeled pseudo-MP3.
This validates audio delivery/decodability, not intelligibility or voice identity.

## Historical single-key results from November 14, 2026

Aggregate evidence is in `docs/qa/capacity-20261114.json`; each row records source
fingerprint and configuration. These were short Windows tests on a development
host, not 2C2G Linux, real voices, real provider quotas or a 30-minute soak.
They record the earlier capacity phase with 10 active/20 queued chat turns and
one Fish account's 5-way limit; retain those settings when interpreting the
numbers. Later multi-account synthetic results (including their startup-snapshot
and frozen-code limitations) are reported separately in [PROVIDER_POOLS.md](PROVIDER_POOLS.md).

| Scenario | Offered | Completed with decodable audio by 30s | Rejected before model work | Audio-ready P95 |
| --- | ---: | ---: | ---: | ---: |
| 100 online, fixed 3.33 turns/s for 30s, fast fake, maintenance enabled | 100 | 100 | 0 | 0.570s |
| Same offered rate, fake TTS fixed at 3s | 100 | 75 | 25 | 18.02s |
| 100 simultaneous offers, fast fake | 100 | 30 | 70 | 1.793s |

The slow run drained for about 17s after the 30s offering window: its 75 completed
turns are not a sustainable 2.5 turns/s. Completion including drain was 1.598/s,
close to the 5/3 = 1.667/s synthesis upper bound. It did not meet 100-user
acceptance. The burst run demonstrates the then-configured 10 active + 20 waiting
limit; rejected offers remain counted as failures of that capacity target.
Both overload runs ended with zero active/waiting work and no upstream Fish 429.

The maintenance-on fast run used about 0.20 CPU cores and 309 MiB combined
backend + fake-provider process-tree peak RSS on this development host. It
counted 135 LLM attempts across foreground and maintenance, illustrating why
budgets must include background work even after removing per-turn polishing.
The artificial WAV silence differs from production MP3 bandwidth/voice quality.

Capacity-phase verification at that snapshot: 1,445 backend tests passed, one optional real-ffmpeg
integration skipped; 183 frontend tests, TypeScript checks and static production
build passed. That phase copied its bundle into `backend/app/static/app`.
Independent review reproductions for cancellation and out-of-order retries pass.
Production deployment, nginx runtime validation, real-account quota checks and
2C2G Linux soak acceptance remain separate release checks.

## Client and arrival model

The chat sequence follows `frontend/src/w4/api.ts`: send POST, fetch the current
message list, follow only that reply's ID through deferred audio states, fetch
and decode its audio, then mark read. Events coalesce into lightweight
`/api/v2/friends?limit=50` and active-thread refreshes, rather than repeating the
full bootstrap. Deferred completion uses events plus fallback reads at about
2, 6, 14, 22 and 30 seconds, bounded by the same deadline measured from POST.
Startup still performs one normal bootstrap per virtual user.

`loadgen.py --mode arrival --rate 3.333333` offers work on a fixed clock,
independent of completion speed. Users are selected round-robin and a user with
unfinished work is reported as `arrivals.user_busy`; it is never silently
delayed. `--mode burst --burst 100` offers simultaneous work. `--max-inflight`
bounds task count; excess offers are reported as `arrivals.client_dropped`.
These client limitations are distinct from backend HTTP 429 rejections and
must be zero for a meaningful successful capacity run.

`--mode closed` retains think-time/completion-paced behavior for interactive
cost exploration. It cannot establish offered-rate acceptance because slower
responses reduce its offered load. Idle and persistent call scenarios use this
mode; arrival/burst modes require chat, voice, feed or a mix of those actions.

## Reading results

- `arrivals.offered` counts all scheduled work. `turns.offered` counts chat/voice
  attempts that reached the client action. Their difference is visible generator
  loss or other scenario work, not backend success.
- `turns.completed` means a successful text/voice HTTP response. `turns.rejected`
  means HTTP 429. Other request failures are `turns.failed`.
- `turns.playable_audio` requires decoding the exact reply's downloaded audio.
  PCM WAV is checked with the standard library, including complete frame data.
  Other formats require a real ffmpeg decode; an unavailable decoder or invalid
  audio is counted as invalid/unverified and missing audio.
- `time_to_playable` includes POST, deferred processing, client refresh/poll,
  download and decode. Deadline fractions use all offered arrivals as the
  denominator, so dropped/rejected/failed work cannot disappear from the result.
- Offering duration and drain duration are separate. Actual offered rate divides
  arrivals by the measured offering window; completed rate includes drain.
- Git HEAD plus a SHA-256 of tracked/untracked source records dirty-checkout
  provenance. Source hashes at start/end show concurrent source changes. Endpoint
  configuration contains no real secrets.
- Server process CPU/RSS, event-loop lag, admission state and usage budget are
  recorded. Smoke additionally samples backend + fake-upstream process trees,
  whole-host CPU/memory, and Linux cgroup CPU/memory/limits when available. Host
  and cgroup samples can include unrelated processes and are labeled accordingly.
  Process-tree sampling can miss very short-lived child processes between reads.
- `bench_server.py --threadpool-workers N` limits threads only. It does not
  emulate CPU count, RAM or a 2C2G machine. Use actual cgroup/container/VM resource
  limits and record them for production-capacity conclusions.

The standalone fake server supports `--llm-429-every`, `--llm-error-every`,
`--llm-slow-every` and `--llm-slow-seconds` for deterministic failure injection.
Its TTS concurrency is a configurable synthetic cap independent of provider
model names or account tiers. For realistic latency experiments, start the
standalone fake and bench server with explicit latency flags and record the
fake configuration reported in results.
