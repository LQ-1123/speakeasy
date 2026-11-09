# APK 在线更新（把"发版"拆成两层）

> 目标：**改界面不用重装 App；改原生才需要在线升级一次**。
>
> 2026-11 起线上同时支持 HTTP/HTTPS；移动端统一用 **https://47-104-24-236.sslip.io**
> （sslip.io 是免费的通配 DNS，把 IP 变成域名，方便签 Let's Encrypt 证书）。

## 1. 现在的结构

```
APK（很小的原生壳）
 └─ 本地 bootstrap 页（同源 http://localhost，能读旧设备身份）
      ├─ 能连上服务器 → location.replace 到 https://服务器/…?se_device_key=旧身份
      │        └─ 线上 React UI（服务器静态产物，部署即更新）
      └─ 连不上 → 离线页（重试）；局域网/服务器故障不白屏
```

- **界面层（在线）**：服务器 `backend/app/static/app` 的 Next 静态产物；改完部署就生效，APK 不用动。
- **原生层（APK）**：`UpdateChecker.java` 启动时请求 `/api/app/latest`，版本比当前高就弹窗 → 下载 APK → 校验 SHA-256 → 拉起系统安装器。
- **身份迁移**：旧 APK 的 device_key 存在 `http://localhost` 的 localStorage；bootstrap 读出后通过 `?se_device_key=` 传给线上页面（前端消费一次就清掉 URL 参数），所以升级/换壳不会变成新用户。

## 2. 日常发版

### A. 只改界面/交互（最常见，**不需要发 APK**）

```bash
cd frontend
npm run build:backend          # 产物进 backend/app/static/app
# 然后按平常流程部署 backend 到服务器（deploy_v3.sh / 上传 backend 目录）
```

用户下次打开 App（或下拉刷新/重进）就是新界面。

### B. 改了原生（权限、插件、图标、更新逻辑本身）

```powershell
.\mobile\build-apk.ps1 -ApiBase http://47.104.24.236:8000 `
    -Mode release -VersionCode 302 -VersionName "3.0.2"
# 产物：deliverables\SpeakEasy-v3.0.2-release.apk
```

然后打开管理台 → **APK 发布** → 上传 apk + 填 versionCode/Name/更新说明 → 发布。
旧版本自动进历史，可一键回滚。

> 规则：`versionCode` 必须严格递增；必须用同一套签名（当前 `mobile/android/speakeasy-release.jks`，证书 SHA-256 `fac7e179…69b1`）。换签名会导致无法覆盖安装。

## 3. 服务端接口

| 接口 | 说明 |
|---|---|
| `GET /api/app/latest?platform=android&channel=release` | 公网只读：当前版本、apkUrl、sha256、size、changelog、force、minVersionCode |
| `GET /api/app/download/<file>.apk` | 公网下载，支持 `Range`（断点续传） |
| `GET /api/admin/app-release` | 管理端：当前版本 + 历史 + 服务器上的 apk 文件 |
| `POST /api/admin/app-release/upload` | 管理端：multipart 上传并发布（form 字段：file/version_code/version_name/changelog/force/min_version_code） |
| `POST /api/admin/app-release/rollback` | 管理端：回滚到上一版 |

存储：`data/app_release.json`（当前 + 历史，保留 8 个）、`data/releases/*.apk`。

## 4. 客户端更新流程

```
启动
 └─ GET /api/app/latest
      ├─ 版本不高于本机 → 正常用
      └─ 有新版 → 弹窗（force 或 本机 < minVersionCode 时不可跳过）
            └─ DownloadManager 下载到 App 私有目录
                  └─ 校验 SHA-256 → FileProvider → 系统安装器
                        └─ Android 8+ 需"安装未知应用"权限（会引导，回来自动继续）
```

限制（Android 平台本身，绕不过）：
- 侧载应用**无法静默安装**，用户必须在系统安装器里点一次「安装」；
- 部分国产 ROM 需要用户手动打开「安装未知应用」；
- 安装包下载走 HTTP 时至少校验 SHA-256；上公网建议尽快换 HTTPS。

## 5. 离线兜底

- `mobile/bootstrap/index.html`：连不上服务器时显示「正在连接/重试」，不会白屏；
- `mobile/capacitor.config.json` 的 `server.errorPath = "offline.html"`：远程页面加载失败自动回落到本地 `offline.html`（同源、可读设备身份，4 秒后自动重试）。

## 6. 已验证

| 项 | 结果 |
|---|---|
| 后端接口（本地 pytest） | `test_app_release.py` 7 passed（发布/去重/回滚/Range/口令） |
| 公网 `/api/app/latest` | 返回 301 / 3.0.1 / sha256 / size / changelog |
| 公网下载 | 200（11.7MB），`Range: bytes=0-99` → 206 |
| 管理台 APK 发布页 | 上传/发布/回滚/当前版本+历史 正常（已发布 3.0.1） |
| APK 签名/版本 | versionCode 301 / 3.0.1，签名证书与 3.0.0 相同（可覆盖安装） |
| APK 内含 bootstrap/offline | assets/public/index.html、offline.html 已打包 |
| 部署后回归 E2E | 24/24 通过 |

## 7. 首次切换注意

**3.0.0 旧 APK 里没有更新器**，所以这一次需要用户**手动安装一次 3.0.1**：

- 管理台「APK 发布」→「下载当前 APK」，或
- `https://47-104-24-236.sslip.io/api/app/download/speakeasy-3.0.1-301.apk`

装好 3.0.1 之后：
- 以后只改界面 → 服务器部署即生效；
- 改原生 → 发 302 → App 自己弹窗升级。

## 8. 回滚

- 管理台「回滚上一版」→ 线上 latest 回到上一版本（App 不会主动降级，但问题版本不会再推给新用户）；
- 需要强制退回某个版本：把历史版本手工 publish 回去即可；
- 代码回滚见 `deploy/README.md`（服务器备份目录）。
