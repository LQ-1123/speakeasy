# SpeakEasy 4.0 candidate — implementation and acceptance

> **2026-11-10 update:** the 4.1.0 Web/Android redesign has been deployed to `https://47-104-24-236.sslip.io/`; current artifact is `deliverables/SpeakEasy-v4.1.0-release.apk`. The original 4.0 statements below remain historical; deployment details and current residuals are in [REDESIGN_WEB_ACCEPTANCE.md](REDESIGN_WEB_ACCEPTANCE.md).

## What changed

The Android APK contains the exported application instead of redirecting to remote HTML. Three navigation destinations (消息 / 发现 / 我) share neutral light/dark surfaces; Android Monet changes accents, not page backgrounds. Contacts live under messages; expressions live under me. Old page routes redirect to the application. Provider keys and models remain server managed.

Calls use a per-call session, one-time socket tickets, one persistent recognizer, an ordered model/speech pipeline and actual playback acknowledgements. Android owns capture, playback, audio focus and background lifetime in a foreground service. Browser capture has its own adapter. A new utterance interrupts recognition/generation as well as playback. Only recognized user speech and playback-confirmed replies enter call memory. Private call audio uses expiring resource tickets and a private synthesis directory; it is not published by the legacy audio endpoint.

Voice bindings are allocated transactionally and remain pinned during provider outages. Unverified newcomers wait outside recommendations. Active identities win over waiting candidates; retry ordering prevents exhausted voices from blocking unrelated candidates. Public feed speech URLs and files share a revision derived from voice and rendering parameters.

The bootstrap returns summaries rather than every friend's history. Long lists use virtualization and state selectors. One authenticated event stream replaces page timers. Durable SQLite queues own maintenance and notification delivery; active calls preempt background generation. Notifications use JPush and configured OEM channels. Per-user preferences and profiles are isolated.

## Verification record

- Python 3.11 offline backend suite: 933 passed after installing the pinned cloud dependency environment; one pre-existing Pydantic field-name warning. `pip check` reports no broken requirements. Final command/results are recorded in MOBILE_REBUILD.md.
- Browser tests cover route transitions, playback ordering/cancellation, speech gating, native identity refresh, call handshake/reconnect and notification batching. Visual checks use deterministic local fixtures, not real providers.
- Android unit tests cover trusted origins, exact back handling, VAD, interruption during recognition and reply-state reset. Release and instrumentation APKs compile with SDK 36/JDK 21. Device instrumentation is not run without a connected device.
- A read-only SQLite backup of the existing workspace DB was migrated: all user/friend/message/expression/persona IDs preserved; all assigned Fish primary identities preserved; integrity_check returned `ok`. The actual database was untouched.
- Existing release signing certificate was verified unchanged. Both the package ID and the upgrade identity migration are retained.

## Running and upgrading

Use Python 3.11. Install the cloud server dependencies with the verified constraints:

```text
python -m pip install -r backend/requirements-cloud.txt -c backend/constraints-cloud.txt
python -m pip install pytest pytest-asyncio -c backend/constraints-cloud.txt
python -m pytest backend/tests -q
```

On Windows use `PYTHONUTF8=1` if pip interprets UTF-8 requirement comments as GBK. The server also requires ffmpeg on PATH for uploaded audio and Edge audio decoding.

Build the same-origin Web export with NEXT_PUBLIC_API_BASE unset, then `npm run build:backend` in frontend. Build the APK separately using `mobile/build-apk.ps1 -Mode release -BuildRoot D:/speakeasy-android-build -ApiBase https://your-server`. Do not copy an APK-specific export over the same-origin Web deployment. See mobile/README.md for signing and OEM configuration.

Before starting the new server, stop the old process and take a SQLite online backup (or copy the DB together with its WAL/SHM while stopped), plus a backup of data/configuration and previous APK/server artifacts. Run one Uvicorn worker: call sessions and tickets are process-local, while user data, jobs and outboxes are durable. Multi-worker deployments need a shared call coordinator and are outside this 3-call target.

Deploy the new server/API and Web export together before distributing the APK. Existing backend deployments do not contain the new call/bootstrap/event endpoints. Enable WebSocket Upgrade/Connection headers and disable proxy buffering (deploy/nginx.conf). `https://localhost` is explicitly allowed for the packaged Android origin.

Schema changes are additive except explicit conflict repair. Rollback means stop the new server, restore the backup DB/configuration and previous server/APK artifacts together. Do not attempt an in-place downgrade of the repaired voice ownership records. Existing remote users retain their IDs; previously global/local-only profile data stays with the legacy local identity and is not copied into other users' accounts.

Configure server-only JPUSH_APP_KEY and JPUSH_MASTER_SECRET in the server environment/.env. Android needs its app key and each enabled OEM's application credentials in ignored local Gradle properties. Do not commit secrets. Without those credentials, the APK reports push unavailable; the outbox does not pretend it delivered notifications.

## Remaining release gates

These are **not claimed as passed** by build or mock tests:

- Physical Honor/Huawei upgrade from 3.0.4: recover old WebView identity, preserve history, reject failed migration without creating another user.
- Continuous 3 × 30-minute real-provider calls, locking/backgrounding, Bluetooth/wired audio, incoming system calls, Wi-Fi/mobile switching and bad-network recovery.
- Measured first-audio P50/P95, <=250ms interruption latency, <=1.5s device startup, <1% dropped frames and long-session memory stability on the chosen midrange devices.
- OEM/Monet/status bar/gesture/IME/font-scale behavior on actual Android 12 and 15/16 devices.
- Credentialed JPush/OEM delivery when backgrounded, locked and the process is reclaimed, including permission denial and notification deep links. Force-stopped apps and offline devices are separate delivery states, not successes.

The candidate is for review and controlled testing. No remote server was deployed and no production messages were sent during this implementation.
