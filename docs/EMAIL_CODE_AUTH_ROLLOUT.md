# 邮箱验证码账号：部署与验收

当前实现复用 FastAPI、SQLite、现有邀请码和 SMTP_SSL。正式应用必须邮箱验证码登录，
新邮箱需要邀请码；同邮箱始终复用账号。设备、会话、旧身份分别统计，不声称统计自然人数。
代码交付不自动修改部署 `.env` 或运行生产归档。

## 配置与发信

在部署环境配置以下内容；真实配置不得入 Git：

```dotenv
SE_EMAIL_CODE_AUTH=1
SE_REQUIRE_AUTH=1
SE_INVITE_REQUIRED=1
SE_MAIL_DEV=0
SE_EMAIL_CODE_SECRET=<至少32字节的随机密钥，各进程保持一致>
SE_MEDIA_SECRET=<另一个至少32字节的随机密钥>
SE_LEGACY_SHARED_OWNER=0
SE_SMTP_HOST=<邮箱服务商SMTP服务器>
SE_SMTP_PORT=465
SE_SMTP_USER=<发信邮箱>
SE_SMTP_PASSWORD=<邮箱SMTP授权码>
SE_SMTP_FROM=<服务商允许的发信地址>
SE_ADS_ANONYMOUS=0
```

使用提供 SSL/TLS 465 端口的邮箱服务，不将 STARTTLS 587 当作 SSL 465 使用。
通过 Python `secrets.token_urlsafe(48)` 生成密钥并保存到受保护的环境配置。
Web 静态资源与 API 同源，HTTPS 反向代理正确传递 `X-Forwarded-Proto`；Android API base 使用同一个 HTTPS 源。
Cookie 保持 SameSite=Lax，Web 不在 localStorage 存储会话令牌。Android 依赖新 APK 的原生加密存储桥。
验证码 IP 限频采用 ASGI `request.client.host`；反向代理场景仅在服务器信任的代理列表中恢复原始 IP，
不要将互联网任意客户端发来的 X-Forwarded-For 当作可信值。

上线前使用真实测试邮箱在登录页发起一次验证码请求，确认收件、6位代码、重发和有效期，再完成注册。
验证码不在响应或日志返回，SMTP 缺配置/失败返回 503，不产生账号或消费邀请。
发信默认30秒总期限，发送资格租约120秒；等待中的同邮箱请求限流；发送在数据库锁外完成。
不能以本地 SMTP stub 单测通过替代真实收信验证。

## 接口与会话

| 接口 | 行为 |
| --- | --- |
| GET `/api/v3/auth/config` | 返回邮箱验证码模式、邀请开关；不需要登录 |
| POST `/api/v3/auth/email-code/request` | `{email}` → `{challenge_id,retry_after:60,expires_in:600}`；不创建用户 |
| POST `/api/v3/auth/email-code/verify` | `{challenge_id,code,invite_code?,device_key?,platform}`；首次原子注册/扣邀请，已有邮箱仅登录 |
| GET `/api/v3/auth/identity` | 恢复当前账号、过期时间与 CSRF |
| POST `/api/v3/auth/logout`、`/logout-all` | 撤销会话、解除关联推送绑定 |
| GET `/api/v3/auth/sessions` | 当前账号会话列表，`current` 标记当前会话 |
| DELETE `/api/v3/auth/sessions/{id}` | 撤销指定会话并解除它的推送设备绑定 |

Web 验证响应设置 HttpOnly Cookie，只返回 CSRF、账号和过期信息。Android 指定 `platform=android`，
响应令牌写进 Android Keystore 支持的加密偏好存储；业务和原生通话使用 `X-Session-Token`。
会话有效期30天。验证码错误最多5次，重发成功使旧码失效；每邮箱5次/小时、每IP30次/小时，失败发信也计入额度。
缺少有效邀请码返回 403 `INVITE_REQUIRED`，正确验证码仍可继续提交邀请码。429 带 Retry-After，SQLite忙返回503。
验证码消费、账号与会话创建、邀请扣减在一个事务中完成，不能通过重试重复创建账号。

新模式拒绝设备注册和旧设备头，关闭密码注册/登录/绑定及旧全局 `/api/conversations*`、`/api/uploads/*`、`/ws`。
保留 V2 业务接口的会话认证。媒体元素使用账号授权的短期票据，归档账号和未登记历史媒体不能公开读取。
健康检查、版本更新、APK下载及管理台保留独立入口；管理台继续使用原有管理凭据。

## 历史归档

**不得用 cleanup_empty_users.py 做这次迁移。** 先在生产一致性副本上演练，再在维护窗口停止应用及所有后台 worker。
停止进程也会清除旧的内存 WebSocket、媒体和通话 ticket，避免停发后仍在使用的旧连接。

生成明确身份清单（源数据库只读，不更改 journal mode 或初始化 schema）：

```powershell
.\.venv\Scripts\python.exe scripts/archive_legacy_users.py --database data/speakeasy_v2.db --prepare D:/speakeasy-backups/legacy-users.json
```

目标清单文件必须不存在；先创建它的父目录。核对清单中的身份与创建时间。
不要在新账号开放注册后重新生成“全部旧身份”清单；只执行已审阅的切换前清单。

演练副本或维护窗口内执行：

```powershell
.\.venv\Scripts\python.exe scripts/archive_legacy_users.py --database data/speakeasy_v2.db --manifest D:/speakeasy-backups/legacy-users.json --apply --maintenance-confirmed --backup-dir D:/speakeasy-backups/snapshots --media-root data
```

备份目录必须位于媒体根目录之外、不能由 Web 服务公开。工具通过 SQLite backup API 生成一致性数据库快照，
复制媒体及清单，备份成功后再执行单事务归档；数据库路径、身份存在性及创建时间必须与清单匹配。
旧资料、好友、消息、帖子、媒体保持原归属。旧账号凭据与设备信息转存 `v3_identity_archives`，
原用户标记 archived_at，旧邮箱从活动凭据表释放；旧会话/邮箱令牌失效，待发通知和用户任务停用，当前推送绑定释放。
同一清单重复执行不会重复归档；新注册账号不在原清单中，不受影响。
后台“历史身份记录”可查询旧账号资料、好友与帖子；管理台沿用既有不展示聊天正文的约定。
备份数据库保留完整历史聊天用于恢复，不能借此次改造新增公开历史媒体入口。

## 发布与回滚

1. 在隔离环境确认配置和真实收信，创建测试邀请码，完成网页与新 APK 的注册、登录及退出。
2. 生产维护窗口制作并检查清单，停止 worker，运行已演练归档并核对备份。
3. 同步发布后端、新网页和 APK；按既有应用版本发布机制设定新版本并保持下载入口可用。
4. 启用上述正式配置并重启服务；在原手机使用相同邮箱重新登录，确认新资料引导、推送绑定及新数据。
5. 观察邮件请求/失败、验证成功/失败、账号创建和旧设备注册被拒等结构化日志，以及后台正式账号增长。

回滚时先停止注册及 worker 并为当前状态再次做一致性备份，保留已经产生的新账号和业务数据。
优先回滚到支持邮箱会话的应用构建；不能直接覆盖旧数据库快照丢弃新账号，也不能重新开启匿名自动建号。
恢复旧数据应从备份复制到隔离实例核对后制定迁移，不自动合并用户数据。

## 验证命令与运行边界

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q
cd frontend
npm test
npm run typecheck
npm run build
cd ../mobile/android
.\gradlew.bat :app:testDebugUnitTest :app:assembleDebug :app:assembleDebugAndroidTest -PbuildRoot=D:/speakeasy-email-auth-build --console=plain
```

发布 APK 必须包含本次构建的网页资源：使用 `mobile/build-apk.ps1` 完成前端构建、同步资源及 Android 构建，
不要把仅通过原生编译、仍携带旧 assets 的 APK 当作完整账号客户端。
保持原包名、签名密钥，按既有发布机制增加发行版本。正式发布前还需运行连接设备测试并手工核验覆盖升级、
清应用数据后邮箱恢复、锁屏推送、延迟旧通知过滤、原生通话重连、退出全部设备及音视频媒体。
没有连接设备时只能报告编译/JVM结果，不能宣称完成真机验收。
