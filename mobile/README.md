# SpeakEasy Android

The Android app packages the frontend export and serves it from `https://localhost`. It does not load a remote web shell. The application ID remains `com.speakeasy.app`; the existing ignored keystore and updater remain in use. Capacitor is pinned to 8.6.0, Android min SDK is 24, and compile/target SDK is 36. Use JDK 21 and Gradle wrapper 8.14.3.

## Build and verify

```powershell
.\mobile\build-apk.ps1 -ApiBase https://your-api.example -Mode release
cd mobile\android
.\gradlew.bat :app:testDebugUnitTest :app:assembleDebug -PbuildRoot=D:/speakeasy-android-build --console=plain
```

`testDebugUnitTest` covers the route allow-list used by notification opens and the semantic haptic mapping (`NativePolicyTest`, `HapticPolicyTest`). Verify the merged manifest before shipping:

```powershell
Select-String -Path D:\speakeasy-android-build\app\intermediates\merged_manifests\debug\processDebugManifest\AndroidManifest.xml -Pattern 'JPUSH_PKGNAME|JPUSH_APPKEY|JPUSH_CHANNEL|VIBRATE'
```

The three `meta-data` nodes are required by JPush and are declared by this app (the AAR only contributes `JPUSH_APPKEY`/`JPUSH_CHANNEL` placeholders, never `JPUSH_PKGNAME`). Without credentials they still merge with an empty value; nobody should edit these nodes out.

The API must use HTTPS. `apiBase` can also come from `SPEAKEASY_API_BASE`; its default is the existing HTTPS deployment. Keep this origin the same as the frontend's `NEXT_PUBLIC_API_BASE`. No long-term token goes in a URL. Signing configuration remains in the ignored `android/keystore.properties`. Do not regenerate the signing key when shipping an upgrade.

On Windows, Gradle's JVM test worker may fail to load test classes under a non-ASCII directory. Use `-PbuildRoot=D:/speakeasy-android-build` (or `build-apk.ps1 -BuildRoot D:/speakeasy-android-build`) to put outputs in an ASCII path. This changes output directories, not source files.

## Identity and native calls

`SpeakEasyNative` implements the version-1 JSON bridge in `docs/MOBILE_REBUILD.md`. Android stores identity using Android Keystore-backed encrypted preferences. A controlled hidden WebView reads old `http://localhost`, current `https://localhost`, configured API-origin storage, and the historic HTTP API-origin storage without requesting remote HTML or executing remote code. A recovered device key is registered against the same account. The bridge reports `identityMigration: pending|ready|new|failed`; an upgrade that cannot recover its identity fails explicitly and must not create a replacement account. Existing WebView/user data is never cleared. Encrypted preferences are excluded from backup because their Keystore key is not portable.

`CallService` owns authenticated socket tickets, continuous PCM16LE 16kHz mono recording, AEC/NS when supported, local VAD, serialized encoded audio playback with real completion acknowledgements, interruption/mute/speaker routing, audio focus, wake lock and an ongoing notification with return/end actions. Calls survive Activity destruction while the process/service remains alive. Process death ends the call; the app deliberately does not restart a microphone automatically after process death. Audio is downloaded only from the configured HTTPS API origin with redirects disabled, decoded locally, and temporary files are deleted after playback/cancellation. The service fetches fresh single-use tickets when reconnecting for up to 20 seconds. It does not replay audio from an older socket/turn.

## JPush and OEM push configuration

JPush 6.3.0/JCore 5.6.0 and Xiaomi/Huawei/Honor/Oppo/Vivo/Meizu 6.3.0 adapters are included. Put credentials only in ignored `android/local.properties` or environment variables:

```properties
JPUSH_APPKEY=your-24-character-app-key
XIAOMI_APPID=
XIAOMI_APPKEY=
HUAWEI_APPID=
HONOR_APPID=
OPPO_APPID=
OPPO_APPKEY=
OPPO_APPSECRET=
VIVO_APPID=
VIVO_APPKEY=
MEIZU_APPID=
MEIZU_APPKEY=
```

Create/register this exact package ID and release certificate fingerprint in JPush and each OEM developer console; enable their push APIs and configure each OEM server credential in JPush. Huawei/Honor applications require their own app IDs and matching certificate registrations; follow vendor console instructions for any project service configuration required by the chosen SDK/account. Keep vendor configuration files and keys out of git. Do not use another package's credentials or debug signing fingerprints for the release package.

The backend that actually sends pushes also needs the JPush credentials in its ignored `.env` (never in the APK): `JPUSH_APP_KEY` (same 24-character AppKey) and `JPUSH_MASTER_SECRET` (server-only secret), then restart the `speakeasy` service. The client AppKey alone only lets the app register a device; without the server pair the outbox marks events `push_status=unconfigured` and no push leaves the server.

With no JPush key, the APK still builds and the bridge reports `push:false`, `pushState:"unconfigured"`; it does not initialize JPush. With a key, `SpeakEasyApp` initialises the SDK at process start, `requestPush()` requests Android notification permission, and registration is polled for up to ~10 seconds. Exactly one `registered` event (with `registration_id` and `vendor`) is emitted per registration id; the frontend binds it through `/api/v3/client/devices`. Registration is never represented as successful before an SDK registration ID exists. Notification extras use `{event_id,route}`; opens are deduplicated and routes must be internal hash paths (allow-list in `NativePolicy.validRoute`, default `#/chats`).

## Local notifications, notification status and haptics

The bridge exposes four notification methods in addition to `requestPush`/`cancelNotification`:

- `getNotificationStatus()` → JSON `{permission, enabled, channelReady, push, pushEnabled, pushState, registrationId, sound, messages, interactions}` observed from the OS.
- `postNotification({id,title,body,route})` → posts a local notification for one client event. Body is restricted to generic strings; message text never reaches the lock screen. The event id is shared with JPush so local and push notifications for the same event are deduplicated and `cancelNotification(eventId)` clears both.
- `requestNotificationPermission()` / `openNotificationSettings()` → drive the Android 13+ permission prompt and the app notification settings page.

Channels (`speakeasy_messages`, `speakeasy_social` and their `_silent` variants) are created by `SpeakEasyApp` at startup. The `GlobalNotification` banner posts a local notification only while the page is hidden, so the foreground banner and a system notification never double up. Push/notification failures (`unavailable`, `denied`, `error`) surface as toasts.

`performHaptic(type)` maps `selection/toggle/longPress/recordStart/recordCancel/send/save/success/error/callControl` to distinct short vibration patterns (`HapticPolicy`), honours `Settings.System.HAPTIC_FEEDBACK_ENABLED`, and never throws. The frontend falls back to `navigator.vibrate` only when no native bridge exists.

## Required device acceptance checks

No physical device or OEM credentials were available during implementation. JVM tests cover origin restriction, exact back boolean handling, VAD transitions, the notification route allow-list and the haptic mapping. Instrumentation tests cover encrypted storage and controlled historic-origin extraction; run on an emulator/test installation, then verify upgrading an existing signed install without clearing data. Build instrumentation with `:app:assembleDebugAndroidTest`; run `:app:connectedDebugAndroidTest` when a device is attached.

Credential/device gates that a source build cannot prove: a packaged APK with real `JPUSH_APPKEY`/`JPUSH_MASTER_SECRET`, device registration binding, lock-screen delivery, notification-open routing, deduplication between JPush and local notifications, Android 13 permission denial/guidance, and vibration feel on real hardware.

Before rollout, test Android 24/29/33/35/36, both navigation modes and IME; an existing-account signed upgrade; screen lock/background/UI recreation during a call; Bluetooth/wired headset changes; incoming telephone calls and audio focus recovery; mute/interruption/turn acknowledgements; offline/reconnect/20-second expiry; notification permission denial; and delivery/open/deduplication through each configured OEM channel. OEM delivery, microphone processing quality, predictive-back animation, incoming telephone behavior and device battery policies cannot be established by a successful APK compile.
