# Android 发布与更新渠道

## v4.4.0 GitHub Release

- 版本：`versionName=4.4.0` / `versionCode=440`，包名保持 `com.speakeasy.app`。
- 下载：[Sonder-v4.4.0-release.apk](https://github.com/LQ-1123/speakeasy/releases/download/v4.4.0/Sonder-v4.4.0-release.apk)。
- [发布说明与 SHA-256 校验信息](https://github.com/LQ-1123/speakeasy/releases/tag/v4.4.0)；Release 仅附一个 APK 资产，源码从仓库获取。
- 在线体验：<https://47-104-24-236.sslip.io>。邮箱验证码登录，新账号需要邀请码；获取内测码请联系 [yulinsun547@gmail.com](mailto:yulinsun547@gmail.com)。该邮箱仅为本项目内测联系用途。

**GitHub Release 与应用服务器的更新渠道相互独立。** 发布 tag、Release 和 APK 不会自动上传 `/api/admin/app-release`，也不会改写 `/api/app/latest`、服务器下载目录或宣传页。不要把历史记录中的线上版本当作 v4.4.0 已经上线的证明。

## 1. 当前客户端结构

```text
APK
 ├─ 内置 Next.js 静态导出（Capacitor 从 https://localhost 加载）
 ├─ 原生桥：加密邮箱会话、通话、通知、主题与触感
 └─ HTTPS API / WebSocket → 构建时指定的后端源

浏览器 → 服务器上的同源静态网页 → 同源 API
```

Android 不再使用旧版“本地 bootstrap 跳转远程网页”的结构。服务端部署网页只更新浏览器/PWA 的网页资源；已安装 APK 内置的界面需要重建并安装新 APK。离线页是网络错误提示，不是能够离线使用云端聊天的承诺。

正式账号使用邮箱验证码与可撤销会话：Web 保存 HttpOnly Cookie，Android 使用原生加密存储与 `X-Session-Token`。同邮箱恢复原账号，历史设备身份不会自动并入新邮箱。长期令牌不放入 URL；旧版 `?se_device_key=` 传递设备身份的做法不适用于当前发布。账号迁移和归档边界见[邮箱部署指南](EMAIL_CODE_AUTH_ROLLOUT.md)。

## 2. 打包与校验

```powershell
.\mobile\build-apk.ps1 -ApiBase https://47-104-24-236.sslip.io -Mode release `
  -VersionCode 440 -VersionName 4.4.0 -BuildRoot D:/speakeasy-android-build
```

脚本会构建前端、复制到 `mobile/www`、同步 Capacitor 并打包 Android。只执行 Gradle 而未更新 assets，可能携带旧界面或旧账号流程。脚本的本地输出使用 `deliverables/SpeakEasy-v4.4.0-release.apk`，属于被 Git 忽略的构建产物；GitHub 附件使用公开名称 `Sonder-v4.4.0-release.apk`。

发布前核对：

1. 清单与运行时版本均为 `440 / 4.4.0`，API 为指定 HTTPS 源，内置静态资源来自本次构建。
2. 使用原包名和原签名。`keystore.properties` 缺失时 Gradle 可退回 debug 签名，因此编译成功不等于正式签名通过；用 `apksigner verify --print-certs` 比对原发行证书。
3. 对最终附件计算 SHA-256，并将完整值写进 Release 正文；重命名不会改变散列，但重建后必须重新计算。
4. 检查 APK 不含服务端上游秘密、SMTP 密码或 JPush Master Secret；JPush 客户端 AppKey 与服务端秘密分开管理。厂商适配器仅在相应配置存在时打包，不能声称每个包都含全部厂商通道。
5. 只上传最终 APK；不上传 keystore、源码临时包、测试数据、日志、配置文件或用户资料。

JVM、编译、模拟器与真机结果应分别记录。历史版本验收不自动适用于本次附件；覆盖安装、真实邮件、锁屏推送、OEM 通道、通话音频焦点与设备电池策略仍需有明确证据。详见 [Android 指南](../mobile/README.md)。

## 3. 可选的应用服务器更新渠道

现有后端仍保留独立的更新器 API。以下说明功能，并不表示 v4.4.0 已通过此渠道发布：

| 接口 | 作用 |
| --- | --- |
| `GET /api/app/latest?platform=android&channel=release` | 公共版本元数据：versionCode、apkUrl、sha256、size、changelog、force、minVersionCode |
| `GET /api/app/download/<file>.apk` | 服务器所持 APK 的公共下载，支持 Range |
| `GET /api/admin/app-release` | 需管理员认证的当前版本与历史列表 |
| `POST /api/admin/app-release/upload` | 需管理员认证的 APK 上传/发布操作 |
| `POST /api/admin/app-release/rollback` | 需管理员认证的版本元数据回滚 |

服务器状态位于 `data/app_release.json` 和 `data/releases/`。要向已安装 App 推送更新，维护者需要另行执行并验收服务端发布，不应把 GitHub 发布结果当作此操作的成功记录。所有公网更新入口使用 HTTPS，发布元数据必须包含最终 APK 的正确 SHA-256。

客户端启动时请求 `/api/app/latest`，仅对更高 `versionCode` 提示升级；`force` 或 `minVersionCode` 可触发强制提示。下载优先使用 DownloadManager，无进度或不可用时可回退应用内直连。下载后按发布元数据校验散列，再通过 FileProvider 拉起系统安装器。缺失散列不能作为可验证发布，实际完整性校验必须与 Release 最终附件相符。

Android 侧载必须由用户在系统安装器确认；Android 8+ 可能要求“安装未知应用”权限。原签名决定能否覆盖安装。服务端把 latest 改回低版本只会停止继续推荐问题版，不会让已安装客户端自动降级；需要修复时优先发布更高的 versionCode。

## 4. 发布范围与回滚

- GitHub 发布：仓库、tag、Release 正文和 APK 附件；校验和与验证边界见对应 Release。
- Web/后端部署：按[安全部署指南](../deploy/README.md)维护，保留 `.env`、`data/`、provider 主密钥、service 与 TLS。
- 服务端更新渠道/宣传页：独立操作、独立验证，不由本次 GitHub 发布自动执行。

容量与供应商状态不是 APK 编译能证明的事项。当前 100 用户 30 分钟 6,000/6,000 轮结果来自开发机回环假上游的启动时源码快照；最终冻结代码另做 60 秒 200/200 复测，不代表真实 2C2G Linux 或供应商额度保证，见[资源池记录](PROVIDER_POOLS.md)和[容量测量说明](PERFORMANCE_CAPACITY.md)。

## 历史发布记录

以下保留当时记录的 4.3.x 版本、散列和线上观察，属于历史状态；本次文档整理未重新验证这些下载地址、服务端 latest 或宣传页，不构成 v4.4.0 发布/设备验收证明。3.0.x 时代使用过远程网页 bootstrap 结构，该结构已被内置 Web 客户端替代。

### 4.3.1 历史发版记录（2026-11-13 02:00 CST）

- 版本：versionCode `431` / versionName `4.3.1`，`force=false`、`minVersionCode=0`（普通可选更新）。
- 安装包：`speakeasy-4.3.1-431.apk`，15,452,077 字节，SHA-256 `ef23fe2c8cad54bfe8526ad91d35b9b6435430d1ac45a784fb936eee6c6fd6f3`。
- 签名：证书 SHA-256 `fac7e179e0365593908c632b2e4b71b87a6bfa6e66aa8fd86a33a416658369b1`，与 4.3.0 相同，可覆盖安装。
- 服务端：`GET /api/app/latest` 返回 431；`GET /api/app/download/speakeasy-4.3.1-431.apk` 200，`Range: bytes=0-1023` → 206；430 旧包保留在历史里可回滚。
- 宣传页：`https://47-104-24-236.sslip.io/hello/` 的版本文字、下载按钮、复制链接、二维码都已指向 431。
- 手机端流程：打开 App → `UpdateChecker` 请求 `/api/app/latest` → 本机 430 低于 431 → 弹更新 → 下载 → 校验 SHA-256 → 拉起系统安装器（Android 侧载不允许静默安装，需在系统界面点一次「安装」）。

### 4.3.2 历史发版记录（2026-11-13 16:12 CST）

- 版本：versionCode `432` / versionName `4.3.2`，`force=false`、`minVersionCode=0`（普通可选更新）。
- 安装包：`speakeasy-4.3.2-432.apk`，15,522,195 字节，SHA-256 `33eeec0c75e7170ddb64ebf6b76b8a04ecf70a7c1c55cdb04b916b9b9b4c1e93`。
- 签名：证书 SHA-256 `fac7e179e0365593908c632b2e4b71b87a6bfa6e66aa8fd86a33a416658369b1`，与 4.3.0 / 4.3.1 相同，可覆盖安装。
- 修复：用户性别/水平/沟通偏好在记忆整理后保持；开屏模糊页移除；好友语音先合成再落语音消息，音频 URL 带签名，点击直接播放。
- 服务端：`GET /api/app/latest` 返回 432；下载 200，`Range: bytes=0-1023` → 206；首页不再包含 `sonder-splash`。
- 宣传页：`https://47-104-24-236.sslip.io/hello/` 的版本文字、下载按钮、复制链接和二维码均已指向 432。
- 说明：本次先部署网站和后端修复，已安装 431 的 App 会通过更新器收到 432；侧载安装仍需用户在系统界面确认一次。

### 4.3.4 历史发版记录（2026-11-13 23:46 CST）

- 版本：versionCode `434` / versionName `4.3.4`，`force=false`、`minVersionCode=0`（普通可选更新）。
- 安装包：`speakeasy-4.3.4-434.apk`，15,293,854 字节，SHA-256 `82ee11a09860701b521a2f4663f545ef976c9b0d911bb6ae06795f1a2a2cb84a`。
- 签名：证书 SHA-256 `fac7e179e0365593908c632b2e4b71b87a6bfa6e66aa8fd86a33a416658369b1`，与 4.3.0–4.3.3 相同，可覆盖安装。
- 更新面板：替换系统 AlertDialog 为品牌化底部卡片（圆角、图标、版本号、更新说明、强制更新标签、下载进度/校验/安装状态），适配深浅色与全面屏安全区；下载在后台预取，完成校验后按钮变为「立即安装」。
- 服务端：`GET /api/app/latest` 返回 434；下载 200，`Range: bytes=0-1023` → 206；4.3.3 进入历史可回滚。
- 宣传页：`https://47-104-24-236.sslip.io/hello/` 的版本文字、下载按钮、复制链接均指向 434。

### 4.3.5 历史发版记录（2026-11-14 00:25 CST）

- 版本：versionCode `435` / versionName `4.3.5`，`force=false`、`minVersionCode=0`（普通可选更新）。
- 安装包：`speakeasy-4.3.5-435.apk`，15,293,854 字节，SHA-256 `76761bd5b9aeccdcebecdd2589d8adb4c49db1e2439bd864dd919e4b27960a8c`。
- 签名：证书 SHA-256 `fac7e179e0365593908c632b2e4b71b87a6bfa6e66aa8fd86a33a416658369b1`，与 4.3.4 相同，可覆盖安装。
- 更新面板：品牌化底部卡片保持 4.3.4 的设计（圆角、图标、版本号、说明、强制标签、进度/校验/安装状态，深浅色与全面屏安全区）；修正 4.3.4 运行时仍报告 433 导致反复提示更新的问题。
- 下载兜底：系统 DownloadManager 无进度时自动切换应用内直连下载；DownloadManager 不可用也走直连，避免面板卡在「下载中」。
- 服务端：`GET /api/app/latest` 返回 435；公网下载 200 且 SHA-256 与本地一致，`Range: bytes=0-99` → 206；4.3.4 进入历史可回滚。
- 宣传页：`https://47-104-24-236.sslip.io/hello/` 的版本文字、下载按钮、复制链接均指向 435。
- 验证：435 在 Android 模拟器覆盖安装成功，启动后直接进入登录页、不再提示更新；APK 清单 `versionCode=435`、dex 内 `BuildConfig.VERSION_CODE=435`。
