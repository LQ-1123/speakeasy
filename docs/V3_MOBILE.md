# SpeakEasy V3 · 移动端（Capacitor Android）

> 本轮云端部署按用户要求暂缓，因此 APK 只做到「可安装产物 + 真实设备安装验证」，
> 端到端功能验收（需连云端 V3 后端）留到云端部署完成后执行。

## 1. 构建

```powershell
# 正式包（默认指向云端 http://47.104.24.236:8000）
.\mobile\build-apk.ps1 -Mode release

# 局域网联调包（指向本机后端，需 -AllowLocalhost 才不会触发防呆）
.\mobile\build-apk.ps1 -ApiBase http://192.168.0.108:8000 -Mode debug -AllowLocalhost
```

脚本做了什么：`NEXT_PUBLIC_API_BASE=<ApiBase> next build` → 产物拷到 `mobile/www` →
`npx cap sync android` → `gradlew assembleRelease/Debug` → 收进 `deliverables/`。

## 2. 产物

| 项 | 值 |
|---|---|
| 路径 | `deliverables/SpeakEasy-v3.0.0-release.apk` |
| 大小 / SHA-256 | 10.89 MB / `be26d9ccee24133c5b6630569b80e406…` |
| package | `com.speakeasy.app` |
| versionName / versionCode | `3.0.0` / `300` |
| minSdk / targetSdk | 22 / 34 |
| API 地址 | `http://47.104.24.236:8000`（**不是 localhost**） |
| 签名 | 本地 release keystore（`mobile/android/speakeasy-release.jks`，**不入库**） |
| 证书 SHA-256 | `fac7e179e0365593908c632b2e4b71b87a6bfa6e66aa8fd86a33a416658369b1` |

调试包（局域网联调，指向 `192.168.0.108:8000`）：`deliverables/SpeakEasy-v3.0.0-debug.apk`。

## 3. 检查项

- 权限：`INTERNET` / `RECORD_AUDIO` / `MODIFY_AUDIO_SETTINGS` / `WAKE_LOCK`（manifest 已确认）
- 明文 HTTP：`usesCleartextTraffic=true` + Capacitor `cleartext: true`
  （云端目前是 `http://IP:8000`；配好域名/证书后应改 HTTPS，并把 `cleartext` 关掉）
- 主应用不请求 localhost：主 chunk 里的 API base 是云端地址；产物中残留的 `127.0.0.1:8000`
  只出现在 `/classic`、`/v2` 两个旧页面的 dev 兜底文案/WS 回退里，W4 主界面不会走到。
- 身份与持久化：设备身份存在 WebView localStorage（`speakeasy.identity.v1`），
  卸载重装会重新注册；服务端 401 时前端自动丢弃旧身份并重注册，不会卡死在错误页。
- 语音权限：WebView 内 `getUserMedia` 需要系统授予麦克风权限（首次录音时弹窗）。
- WebSocket（流式 STT）URL 由 `API_ORIGIN` 推导为 `ws://<云端>:8000/api/v2/stt/stream`。

## 4. 真实设备验证

- 设备：Meta Quest 3（Android 14，adb `2G0YC1ZFB500RT`）
- `adb install -r` → **Success**；`dumpsys package` 显示 `versionName=3.0.0 / versionCode=300`
- 局限：安装时该头显正被使用者占用（前台是 SteamLink VR），为避免打断使用者，
  **未执行抢占前台的 UI 启动/截图**；APK 启动与页面渲染待设备空闲或换手机复测。
- 待云端部署后复测项：登录（设备身份）、Discover 加载、点赞/评论、发动态与可见性、
  通知与好友申请、语音条播放、录音转写（需麦克风权限）、实时通话。

## 5. 常见问题

| 现象 | 处理 |
|---|---|
| Gradle 报 "project path contains non-ASCII characters" | 已在 `mobile/android/gradle.properties` 打开 `android.overridePathCheck=true`（本项目无 NDK 构建） |
| Gradle 报 SDK location not found | 设置 `ANDROID_HOME`，或让脚本自动写 `mobile/android/local.properties` |
| 页面能开但请求全 401 | 服务端数据库重置过：前端会自动重注册；仍失败则清 App 数据重开 |
| 录音没反应 | 未授予麦克风权限；或在非安全上下文——APK 内 `http://localhost` 属安全上下文，不受影响 |
