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

## 4.1.1 reliability fixes (user-reported device issues)

- Frontend/native/backend fix set documented in [MOBILE_RELIABILITY_FIXES.md](MOBILE_RELIABILITY_FIXES.md): IME/composer placement, single-source hash navigation and Android back stack, one avatar resolver + backend persona/friend header sync, WeChat-style voice bubbles without clipped duration, complete M3 palette derived from the wallpaper accent, JPush manifest/init/registration plus local system-notification fallback, idempotent message sends (`client_message_id`), chat scroll-to-latest, and semantic haptics plus notification permission/status UI.
- Follow-up device findings fixed in the same version: (a) JPush never registered because JCore 5.x requires `JCollectionAuth.setAuth(context, true)` before `JPushInterface.init`, and the settings card only read the async status once — registration now polls every 2s, exposes the last real error, and a user retry is never swallowed by the 30s cooldown; (b) switching between the messages/contacts tabs flashed the hidden contacts detail pane because the global page-enter animation overrode the mobile `translateX(104%)` hide transform — the animation is now disabled while the app is in mobile list view.
- Verification: frontend `tsc --noEmit` + 17 files / 109 tests; backend 950 tests; Android 13 JVM tests + `assembleDebug`/`assembleRelease`; production Next export smoke-tested against an isolated real backend (42/42 real-API checks, 24/24 preview checks, zero console errors); mobile CDP frame sampling keeps the detail pane off-screen (`animationName:none`, `left=406 > 390`) across 6 frames after switching to contacts; release manifest verified with aapt2 (three JPush meta-data nodes, VIBRATE, `SpeakEasyApp`); signing certificate unchanged (`fac7e179…69b1`).
- Android: versionCode 411 / versionName 4.1.1. JPush app created 2026-11-10 for `com.speakeasy.app`; `JPUSH_APPKEY`/`JPUSH_MASTER_SECRET` are configured locally (`local.properties` + `.env`), credentials validated with `/v3/push/validate` (1011 = valid account, no devices yet), and the release manifest contains the non-empty AppKey. Artifact: `deliverables/SpeakEasy-v4.1.1-release.apk`, 15,332,338 bytes, SHA-256 `df669dfe52fcb1d69b0c831af33a83c9fddbdde806380146e13e421bc62082ad`. Device logcat on the Honor MEP-AN00 (Android 17) exposed the actual blocker: JCore 5.x requires an app-declared `JCommonService` subclass; without it every init logs `AndroidManifest.xml missing required service: cn.jpush.android.service.JCommonService` and no connection/registration ever starts. Added `SpeakEasyPushService extends JCommonService` plus the `:pushcore` manifest entry, made unconfigured vendor-channel plugins opt-in, and surfaced JPush connection state / a network probe in Settings. Verified on the device: `SpeakEasyPush: registered id=140fe1da…`, `:pushcore` process running, Settings shows 「已开启，推送已注册。」, and a JPush REST test push reached the notification shade (evidence in `artifacts/qa-4.1.1/push/`). The production backend still needs `JPUSH_APP_KEY`/`JPUSH_MASTER_SECRET` in its `.env` plus a `speakeasy` restart for outbox pushes. **Update (same day):** keys are now in the server `.env`, the updated backend + same-origin web were deployed to `47.104.24.236` (`/api/v3/client/devices` live), the Honor device registered itself into `client_devices`, and a synthetic outbox event was dispatched (`push_status=sent`) with the notification confirmed on the phone. Added a startup retry in `platform.ts` so the registrationId is re-reported to the backend on every launch (idempotent) instead of relying on the one-shot JPush `registered` event. Notification upgrade (same day): backend now sends `title`=persona name, `alert`/`big_text`=message text and `large_icon`=<deterministic avatar PNG URL>; new `GET /api/v3/avatar/{seed}.png` rasterizes the SVG with cairosvg (LRU-cached) for JPush to download. Verified on the Honor device: one notification with avatar, name and message text; the frontend now skips the local fallback when push is registered so the two paths never duplicate. Branding update (same day): the app is now **Sonder** (launcher label, task title, web `title`, PWA manifest, About page, notification fallbacks, call notification text, channel descriptions), with the provided Light/Dark logo variants delivered through two `activity-alias` launcher entries switched by system dark mode (`LaunchIcon.apply`, `<monochrome>` layer for Android 13 themed icons), and a static-HTML brand splash (gradient sky, S mark, SONDER wordmark, tagline, hills/silhouette) that fades after React mounts. Per user's final choice the launcher keeps the light/dark switching (`activity-alias` + `LaunchIcon`), while the **Dynamic gradient tile is the notification large icon** (and the monochrome S is the status-bar small icon; chat notifications keep the AI avatar), and the Android 12+ system splash icon is a transparent drawable so the OS starting window is only the brand background color. Rebuilt artifact: `deliverables/SpeakEasy-v4.1.2-release.apk` (versionCode 412 / versionName 4.1.2), 15,180,376 bytes, SHA-256 `1a33e1167370804b9b21e34e8072f0f989f57ee16f5ecbccd5c3ca2ddd92bde4`. On Honor/Huawei the notification's left icon is the system-cached application icon (not `setSmallIcon`); a version bump plus device reboot refreshed it, verified showing the Dynamic gradient icon on the left and the AI avatar (or Dynamic fallback) on the right. After the first on-device registration timeouts on a Honor phone, unconfigured vendor-channel plugins were made opt-in (only packaged when the matching vendor credentials exist) so their empty-appid SDKs cannot break the JPush TCP-channel registration; Settings now reports the JPush connection state and has a "网络检测" probe that resolves/TCP-connects `sdk.jpush.cn:7000`, `sdk.jpush.cn:443` and `api.jpush.cn:443`.
- Remaining device gates: server `.env` still needs the two JPush values plus a `speakeasy` restart; vendor channel credentials/fingerprint registration are optional but recommended for reliable China delivery; real-device delivery/open/deduplication, IME animation, Android 12+ wallpaper-change visual check, notification permission paths and haptic feel. No Android device is attached in this workspace.

## 4.2.1 release (2026-11-12)

- 重新打包最新 Web（角色文风/酒馆预设导入、商业运营广告控制台、免登录、人物昵称、角色语言同步、Fish 情绪标记）到 `mobile/www`；APK 自带 Web 资源，因此 Web 端部署不会自动更新已安装 App，需要重新打包。
- Android：versionCode 421 / versionName 4.2.1；API base `https://47-104-24-236.sslip.io`；签名证书不变（`fac7e179…69b1`），4.2.0 可直接覆盖升级。
- 产物：`deliverables/SpeakEasy-v4.2.1-release.apk`，15,214,446 bytes，SHA-256 `5a619e7de83c16372dcc49c9597dc7d1aefbfbdac9f7320b2ad15ab7c7b8f9c9`。
- 已通过管理台 `/api/admin/app-release/upload` 发布：线上文件名 `speakeasy-4.2.1-421.apk`，`GET /api/app/latest` 返回 421 / 4.2.1。
- 验证：aapt2 包名/版本、apksigner 证书与 4.2.0 一致、公开 HTTPS Range 下载返回 206、管理台上传返回 200。未做真机安装验收。

## 4.2.2 release (2026-11-12)

- 修复 Fish 合成慢导致的「消息迟迟不出现」：后端先落文字消息并立即返回，后台再补 `audio_path/msg_type=voice`；前端发送后短轮询把音频取回。测试环境 `SE_TTS_DEFER_AUDIO=0` 保持同步语义。
- 修复「朗读每次都要等 Fish」：`voice_registry.synthesize` 增加按「角色音色 + 文案 + 模型」的磁盘缓存（`data/audio/.tts_cache`），重复朗读直接命中。实测同一段中文：首次 2.55 s，第二次 54 ms。
- 实测消息：聊天接口返回 **1.94 s**（此前需等 Fish 约 6 s 才落库），音频在 **6.71 s** 后异步补齐；页面先显示文字，不再空白等待。
- Android：versionCode 422 / versionName 4.2.2；签名证书不变（`fac7e179…69b1`）。
- 产物：`deliverables/SpeakEasy-v4.2.2-release.apk`，15,214,618 bytes，SHA-256 `fb091daf2ed243c977c77d0bdb44cdc478192c1a70fbe575bdc1ff769f0d0b73`。
- 已通过管理台发布：线上文件名 `speakeasy-4.2.2-422.apk`，`GET /api/app/latest` 返回 422 / 4.2.2。

## 4.2.3 release (2026-11-12)

- 修复通话字幕「永远只显示两条」：`CallSnapshot` 新增 `captions[]`，用户 final transcript 与角色逐句 audio 全部追加；中间识别结果不写入字幕；按 turn/index 去重，重连/重放不重复。
- 字幕区域限制高度并自动滚到底部，可回看整通电话历史；不再只保留最后一条用户和最后一条角色。
- 前端测试 150 passed（新增完整字幕回归用例）；后端 LLM 配置新增 `extra_body` 透传，生产已切到 SiliconFlow `Qwen/Qwen3-8B`（`enable_thinking=false`）。
- Android：versionCode 423 / versionName 4.2.3；签名证书不变（`fac7e179…69b1`）。
- 产物：`deliverables/SpeakEasy-v4.2.3-release.apk`，15,214,938 bytes，SHA-256 `fc0f9965a968e5552814babbbd15fad3214e9727a9fd30586f5b4f1dffadcf75`。
- 已通过管理台发布：线上文件名 `speakeasy-4.2.3-423.apk`，`GET /api/app/latest` 返回 423 / 4.2.3。

## 4.2.4 release (2026-11-12)

- **真机复现「字幕永远只有两条」的真实根因**：4.2.3 只修了前端渲染（保留 `captions[]`），但手机走原生通话，`turn_id` 由原生层生成，Web 层只能从服务端的第一个事件里学到它。旧逻辑 `eventTurn && this.turnId !== eventTurn → return` 把第一轮之后的所有 `transcript` / `responding` / `audio` 全部丢弃，于是字幕恰好停在「用户第一句 + Chloe 第一句」，状态也卡在「正在聆听」。浏览器通话的 `turn_id` 是本端生成的，所以只在真机出现。
- 修复 `callSession.ts`：新增 `adoptTurn()`，当前轮已结束（`turn_done` / `interrupted` / 错误）时允许新 turn 接管并重置播放；`interrupted` 会把该 turn 记为失效，被打断后迟到的旧 audio 不再混进字幕；原生重连/暂停时 `resetTurn()` 作废旧 turn，重连后仍能继续记字幕。
- 回归测试：新增「原生多轮字幕」与「barge-in 迟到音频隔离」两个用例（先红后绿，修复前分别只有 2 条字幕 / 混入旧回复）；前端 152 passed，`tsc --noEmit` 通过。
- 同时重建同源 Web（`backend/app/static/app`，相对 API origin）与 APK 内置 Web（`https://47-104-24-236.sslip.io`）。
- Android：versionCode 424 / versionName 4.2.4；签名证书不变（`fac7e179…69b1`），4.2.3 可直接覆盖升级。
- 产物：`deliverables/SpeakEasy-v4.2.4-release.apk`，15,215,030 bytes，SHA-256 `6af44bc747bef2d0835e72c45e9fee2ba506cc8d6eb3ae0d10953efbddb9df12`。

## 4.3.0 release（邮箱验证码账号，2026-11-12）

- 账号体系切换为**邮箱验证码登录 + 邀请码内测**：`v3_accounts.email` 为 `UNIQUE COLLATE NOCASE` 主键；同一邮箱始终映射同一 `user_id`（第二次登录 `created=false`、不重复扣邀请），新账号不继承旧设备身份；会话 30 天、Web HttpOnly Cookie + CSRF、Android Keystore 加密会话；旧设备身份/密码登录/旧全局接口在 `SE_EMAIL_CODE_AUTH=1` 下关闭。真实邮件验证：同一邮箱两轮真实验证码（SMTP 发送 + IMAP 读码）得到 `accounts=1 / sessions=2 / invite_used=1`。
- 配置加载根因修复：[config.py](backend/app/config.py) 启动时 `load_dotenv(PROJECT_ROOT/.env, override=False)`，否则 `SE_EMAIL_CODE_* / SE_SMTP_* / SE_MEDIA_SECRET` 对 `os.getenv` 不可见（验证码请求会 503）；`backend/tests/conftest.py` 设 `SE_LOAD_DOTENV=0` 隔离测试（否则本机 `.env` 会污染 40 个既有用例）。
- 发布脚本适配登录模式：[deploy/deploy_v3.sh](deploy/deploy_v3.sh) 关键链路自检接受业务接口 401/403，并新增 `/api/v3/auth/config` 200 检查，避免邮箱模式下误判回滚。
- 生产部署（2026-11-12 21:57，47.104.24.236）：代码 + 同源 Web 静态发布，systemd 服务重启；`/api/v3/health` 200、`/api/v3/auth/config` 返回 `{email_code_auth:true,invite_required:true}`；旧接口 `/api/conversations` 410、`/api/v3/auth/register` 403；数据库迁移前后计数一致（21 身份 / 28 人物 / 142 帖子 / 31 消息），备份 `data/_backup/deploy-20261112-215738`。
- 生产 `.env` 已配置验证码密钥、媒体密钥、QQ SMTP（smtp.qq.com:465）；真实发信成功（`auth_email_code_request outcome=success`）。内测邀请码已通过管理台创建；有效邀请码不提交到源码仓库。
- Android：versionCode 430 / versionName 4.3.0；签名证书不变（`fac7e179…69b1`），4.2.4 可直接覆盖升级。
- 产物：`deliverables/SpeakEasy-v4.3.0-release.apk`，15,219,995 bytes，SHA-256 `33badede6262005af70850ab32faa7df73e532aabe92f95e26e6008f24070464`。
- 已通过管理台发布：线上文件名 `speakeasy-4.3.0-430.apk`，`GET /api/app/latest` 返回 430 / 4.3.0（公开 HTTPS Range 下载 206 验证通过）。
- 未执行：旧身份归档脚本（`scripts/archive_legacy_users.py --apply`）与真机安装验收；旧数据保留但新账号不继承，管理台可用「历史身份」查看。


