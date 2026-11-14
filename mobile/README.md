# Sonder Android

Current release: **v4.4.0 / versionCode 440**. Download [Sonder-v4.4.0-release.apk](https://github.com/LQ-1123/speakeasy/releases/download/v4.4.0/Sonder-v4.4.0-release.apk); the [GitHub Release notes](https://github.com/LQ-1123/speakeasy/releases/tag/v4.4.0) contain the SHA-256 and verification scope. This Release has one attached APK asset. A GitHub release does not publish to the application's server-side updater or change its landing page.

Web experience: <https://47-104-24-236.sslip.io>. Sign in with an email verification code; a new account needs an invitation. To request an internal test code, contact [yulinsun547@gmail.com](mailto:yulinsun547@gmail.com). This project contact does not represent a third-party service provider.

The Android app packages the frontend export and serves it from `https://localhost`. Updating the website does not replace the frontend bundled in an installed APK; rebuild the APK for client UI changes. The application ID remains `com.speakeasy.app`; the existing ignored keystore and updater remain in use. Capacitor is pinned to 8.6.0, Android min SDK is 24, and compile/target SDK is 36. Use Node.js 22+, JDK 21, the project's Gradle wrapper and the package lockfiles. Run `npm ci` in both `frontend/` and `mobile/` before the first build.

## Build and verify

```powershell
.\mobile\build-apk.ps1 -ApiBase https://47-104-24-236.sslip.io -Mode release `
  -VersionCode 440 -VersionName 4.4.0 -BuildRoot D:/speakeasy-android-build
cd mobile\android
.\gradlew.bat :app:testDebugUnitTest :app:assembleDebug -PbuildRoot=D:/speakeasy-android-build --console=plain
```

`testDebugUnitTest` covers the route allow-list used by notification opens and the semantic haptic mapping (`NativePolicyTest`, `HapticPolicyTest`). Verify the merged manifest before shipping:

```powershell
Select-String -Path D:\speakeasy-android-build\app\intermediates\merged_manifests\debug\processDebugManifest\AndroidManifest.xml -Pattern 'JPUSH_PKGNAME|JPUSH_APPKEY|JPUSH_CHANNEL|VIBRATE'
```

The three `meta-data` nodes are required by JPush and are declared by this app (the AAR only contributes `JPUSH_APPKEY`/`JPUSH_CHANNEL` placeholders, never `JPUSH_PKGNAME`). Without credentials they still merge with an empty value; nobody should edit these nodes out.

The API must use an HTTPS origin with no path, query, fragment or credentials. `apiBase` can also come from `SPEAKEASY_API_BASE`; its default is the existing HTTPS deployment. Keep this origin the same as the frontend's `NEXT_PUBLIC_API_BASE`. No long-term token goes in a URL. Signing configuration remains in the ignored `android/keystore.properties`. Do not regenerate the signing key when shipping an upgrade. A local release build can fall back to debug signing if that file is absent: a successful `assembleRelease` alone is not evidence of release signing. Check the manifest version and `apksigner verify --print-certs` against the original certificate before distribution.

The script writes an ignored local `deliverables/SpeakEasy-v4.4.0-release.apk`. The public GitHub asset is named `Sonder-v4.4.0-release.apk`; use the Release download above for installation. Keep keystores, passwords, `local.properties`, provider secrets and user data out of source and Release assets. See [the update guide](../docs/APP_UPDATE.md) for the separate server update channel.

On Windows, Gradle's JVM test worker may fail to load test classes under a non-ASCII directory. Use `-PbuildRoot=D:/speakeasy-android-build` (or `build-apk.ps1 -BuildRoot D:/speakeasy-android-build`) to put outputs in an ASCII path. This changes output directories, not source files.

## Identity and native calls

`SpeakEasyNative` implements the version-1 JSON bridge described in [MOBILE_REBUILD.md](../docs/MOBILE_REBUILD.md), whose earlier device-identity contract is a historical implementation record. The current formal mode uses [email-code sessions](../docs/EMAIL_CODE_AUTH_ROLLOUT.md): Android saves the session in Android Keystore-backed encrypted preferences and sends `X-Session-Token`; logging in again with the same email restores the same account. New emails require an invitation. Legacy device registration and `X-User-Id` / `X-User-Token` are rejected in this mode, and historic device data is not silently assigned to a new email account.

The controlled historic-origin storage migration remains for older installations; it does not bypass email authentication. Do not clear WebView/user data during upgrades. Encrypted preferences are excluded from backup because their Keystore key is not portable. Test both an existing signed upgrade and email recovery after a clean installation.

`CallService` owns authenticated socket tickets, continuous PCM16LE 16kHz mono recording, AEC/NS when supported, local VAD, serialized encoded audio playback with real completion acknowledgements, interruption/mute/speaker routing, audio focus, wake lock and an ongoing notification with return/end actions. Calls survive Activity destruction while the process/service remains alive. Process death ends the call; the app deliberately does not restart a microphone automatically after process death. Audio is downloaded only from the configured HTTPS API origin with redirects disabled, decoded locally, and temporary files are deleted after playback/cancellation. The service fetches fresh single-use tickets when reconnecting for up to 20 seconds. It does not replay audio from an older socket/turn.

## JPush and OEM push configuration

JPush 6.3.0/JCore 5.6.0 are dependencies. Xiaomi/Huawei/Honor/Oppo/Vivo/Meizu 6.3.0 adapters are opt-in: Gradle includes each adapter only when its required matching credentials exist. The presence of adapter source/configuration does not mean every adapter is in a particular APK or that OEM delivery is configured. Put client configuration only in ignored `android/local.properties` or environment variables:

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

JVM tests cover origin restriction, exact back boolean handling, VAD transitions, the notification route allow-list and the haptic mapping. Instrumentation tests cover encrypted storage and controlled historic-origin extraction; run on an emulator/test installation, then verify upgrading an existing signed install without clearing data. Build instrumentation with `:app:assembleDebugAndroidTest`; run `:app:connectedDebugAndroidTest` when a device is attached. Historical device checks in earlier release ledgers do not establish v4.4.0 hardware acceptance; report only the checks actually run for this APK.

Credential/device gates that a source build cannot prove: an APK with its configured client `JPUSH_APPKEY` plus a backend with `JPUSH_APP_KEY`/`JPUSH_MASTER_SECRET`, device registration binding, lock-screen delivery, notification-open routing, deduplication between JPush and local notifications, Android 13 permission denial/guidance, and vibration feel on real hardware. The Master Secret belongs only on the backend and must never be packaged in the APK.

Before rollout, test Android 24/29/33/35/36, both navigation modes and IME; an existing-account signed upgrade; screen lock/background/UI recreation during a call; Bluetooth/wired headset changes; incoming telephone calls and audio focus recovery; mute/interruption/turn acknowledgements; offline/reconnect/20-second expiry; notification permission denial; and delivery/open/deduplication through each configured OEM channel. OEM delivery, microphone processing quality, predictive-back animation, incoming telephone behavior and device battery policies cannot be established by a successful APK compile.
