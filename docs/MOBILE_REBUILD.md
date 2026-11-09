# SpeakEasy mobile rebuild — implementation ledger

Approved scope: Android-first bundled Web client, neutral surfaces with Monet accents, three tabs (messages/discover/me), expression book under me, contacts under messages. Capacitor 8.6.0 / Android 24–36. Native foreground call service with interruption, explicit errors and session recovery. One stable ready voice per active persona. JPush + vendor channels. Preserve identities, signing and user data. Capacity target: 20 online / 3 calls.

## Ownership

- Root: backend call sessions, client bootstrap, event/push outbox, integration and QA.
- Frontend agent: frontend/ only (including dependencies, browser call adapter, client platform bridge and redesign).
- Android agent: mobile/ only, native service/bridge, JPush, Capacitor upgrade and packaging.
- Voice agent: backend/app/voice, v3/seed_data.py, daily_world.py, service.py, schema.py, v2/spawner.py, admin/voice_pool.py and tests; backend call_service.py remains root-owned.

## Locked integration contract

API origin: configured HTTPS origin, bundled Android assets served at https://localhost. Existing X-User-Id / X-User-Token headers remain. Android persists identity securely; Web uses current storage. No long-term tokens in URLs.

POST /api/v3/calls {friend_id, request_id} -> {call_id, ticket, ws_path, expires_in:60}. POST /api/v3/calls/{call_id}/ticket -> same short-lived ticket (authenticated). DELETE /api/v3/calls/{call_id} ends session. POST /api/v3/calls/{call_id}/turn {text,turn_id} is NOT used: all turn controls use WS.

WS /api/v3/calls/{call_id}/stream: first message {type:'auth',ticket,last_sequence?:number}; ticket single-use. Server sends JSON {type,call_id,turn_id,sequence,...}; client sends JSON {type:'ping'|'utterance_end'|'interrupt'|'playback_done'|'playback_failed'|'mute'|'end',turn_id?,audio_id?,muted?} and PCM16LE/16000Hz/mono binary frames (20–100 ms). Client can send {type:'text',text,turn_id} for accessibility/test; server derives friend/language from session. Native/browser continuously capture with echo cancellation, use local VAD for interruption, and send utterance_end on silence. On interrupt immediately stop audio and invalidate older turn playback.

Server events: ready {language}, transcript {text,final}, responding, audio {audio_id,text,audio_url,duration_ms,index}, turn_done, interrupted, error {code,message,recoverable}, ended, pong. Audio is sequenced playable URL; download/decode through native audio queue, report playback_done only on actual completion. No estimated playback timers. Server acknowledges played text for context. Socket disconnect: stop current generation, hold session 20s; obtain fresh ticket and reconnect. No old audio replay. Server audio events include absolute HTTPS URL if request origin is known. Caption/translation is optional, never on critical voice path.

GET /api/v3/client/bootstrap -> {profile,friends,requests,conversations:[{friend_id,last_message,unread}],expressions,capabilities,unread_events}. friends/profile/raw messages match existing v2 bootstrap public mappings. Server does not fetch per-friend history on bootstrap.

POST /api/v3/client/event-ticket -> {ticket,ws_path:'/api/v3/client/events'}; WS first auth message as above with last_sequence. Event {id,sequence,type:'message'|'social'|'friend_request',friend_id?,post_id?,payload,created_at}; replay supported, dedupe by id. PATCH /api/v3/client/events/{id}/read. POST /api/v3/client/devices {registration_id,platform:'android',vendor?,enabled:true}. GET/PATCH /api/v3/client/preferences {messages,interactions,sound,theme:'system'|'light'|'dark'}. APIs require actual authenticated identity (no anonymous local fallback).

Native JS bridge: window.SpeakEasyNative optional object with methods getCapabilities():string (JSON {version:1,nativeCalls:true,dynamicColor:true,push:true}), getTheme():string ({accent,onAccent,dark,insets:{top,bottom,ime}}), getIdentity():string (JSON identity or null), saveIdentity(json):void, startCall(json):void ({apiOrigin,callId,ticket,wsPath,friendName,userId,token}), callAction(json):void ({type:'mute'|'speaker'|'interrupt'|'end',...}), getCallState():string, requestPush():void, setPreferences(json):void, playSound(kind):void. JS receives CustomEvent('speakeasy:native',{detail:{type:'theme'|'call'|'push'|'identity'|'notification_open',...}}); call detail.event is server event, call detail.state is lifecycle snapshot. getTheme insets are CSS pixels. Native requestPush emits registration_id; frontend registers with server. On notification_open detail.route is internal hash route. Return handling: web installs window.speakeasyBack():boolean, native first handles IME then calls this; boolean exact equality, never substring.

## Execution

- [complete] Baseline and pinned Python cloud constraints; isolated temporary test data.
- [complete] Native audio/session, server protocol, playback acknowledgements, strict voice allocation/readiness.
- [complete] Three-entry UI, system theme/insets/back, virtual lists and shared event connection; browser screenshots verified, minimized call composer remains visible.
- [complete] Durable JPush outbox/provider integration, identity migration implementation, user profile isolation, call-preemptible maintenance.
- [complete] Browser QA, same-origin Web export and final signed APK asset sync/release packaging. Physical-device and credentialed-provider release gates remain open.

Ruling: create feature branch in the user's clean current checkout rather than relocate their workspace; user authorized full implementation. Keep secrets/data unchanged. No deployment or push.
Ruling: live vendor push and OEM call verification require credentials/devices not currently available; implement code and test adapters now, retain explicit acceptance blockers rather than claim delivery.
Baseline: 109 selected offline backend tests passed in planning; frontend tsc passed. python-multipart missing, installing in ignored local venv.

## Verification and review ledger

- Final full backend regression: 933 passed, 1 existing Pydantic warning (26.45s), with Python 3.11 and installed cloud requirements including OpenAI 1.109.1. pip check: no broken requirements.
- Latest scoped server changes (private audio directory, strict auth, PCM session, preferences/outbox): 21 passed.
- Frontend final: 22 tests/9 files pass, TypeScript and production export pass; see frontend/QA_REBUILD.md for viewport/virtualization/scroll evidence. 90 bundled APK files byte-match mobile/www.
- Android final: Capacitor 8.6.0, version 4.0.0/400, minSDK24/target36; 6 unit tests, release and instrumentation APK compile. Signing certificate matches previous release. Final artifact: deliverables/SpeakEasy-v4.0.0-release.apk (SHA256 90cc2e052d2ec510805a194125a02c3cb5884ef15e70bf057d03f6d80c600a13).
- Read-only real DB backup migration: IDs preserved for 91 users / 17 friends / 86 messages / 0 expressions / 31 personas; Fish primary bindings preserved; SQLite integrity ok. Source DB unchanged.
- Independent review rounds corrected whitespace authentication, stale socket takeover, generation/ACK cancellation, private call audio sharing/deletion, recognition-time barge-in, initial push registration, event replay storms, active voice-owner precedence and waiting-candidate starvation.
- No deployment, remote write, provider call, or real push was used as part of verification. Real-device acceptance is listed in REBUILD_ACCEPTANCE.md.
- Proactive followups run as durable server jobs without requiring an open client; due social interactions materialize before notification collection. Unrelated existing/unrecognized workspace artifacts were left untouched.

Protocol amendments: native startCall/state includes friendId. Call audio URLs use 90-second per-resource tickets at `/api/v3/calls/{call_id}/audio/{audio_id}`; signed URLs hold no long-term identity credentials. PCM is speech plus 200ms pre-roll and trailing silence, not indefinite idle audio. Event tickets include current_sequence and events include is_read. Native cancelNotification(eventId) clears a read notification. Chat `before` and `cursor` accept `created_at|id` and return next_cursor.

## 4.1.0 release and first public deployment

- Web redesign A–C accepted; deployed same-origin Next export to `/root/speakeasy/backend/app/static/app` at `https://47-104-24-236.sslip.io/` on 2026-11-10. Kept `data/`, `.env`, service unit, signing keys and identities.
- Backend code from the local working tree was deployed with `backend/app` backup; `/api/v3/client/bootstrap` and preferences now return 200. Deploy health check updated from hard-coded `p/adrian` to `GET /api/v3/personas?limit=1`.
- Nginx HTTPS location now forwards WebSocket `Upgrade` / `Connection`; `/api/v3/client/events` handshake succeeds on the public origin.
- Android: versionCode 410 / versionName 4.1.0, API base `https://47-104-24-236.sslip.io`, minSdk 24 / target 36. Release signing certificate unchanged (`fac7e179…69b1`). Artifact: `deliverables/SpeakEasy-v4.1.0-release.apk`, SHA-256 `ed3ab7c40aae19381e8cc23ca5e6adbc51c1d52a565651116edcb55fcb7d0fad`.
- `mobile/build-apk.ps1` now temporarily relaxes `$ErrorActionPreference` around Gradle stderr; previous run reported failure despite a successful build.
- Public smoke: 4 tabs, 338 discover posts, contacts empty state, no 404/console/WebSocket errors. No Android device was attached; physical-device upgrade, call, microphone, theme, haptic and OEM push checks remain open.
