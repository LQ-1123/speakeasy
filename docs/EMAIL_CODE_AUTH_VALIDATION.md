# 邮箱验证码账号交付验证（2026-11-12）

改动保留在 `feat/email-code-accounts` 功能分支。未执行生产数据库归档、部署或真实发信。

| 检查 | 结果 |
| --- | --- |
| 完整后端 `python -m pytest backend/tests -q` | 1314 passed；1条既有 Pydantic 字段重名警告 |
| 完整前端 Vitest | 26个文件、163 passed |
| TypeScript | `tsc --noEmit -p tsconfig.json` 通过 |
| 同源网页 | `npm run build:backend` 通过，127个文件复制到后端静态目录 |
| Android JVM | 14 tests，0 failures/errors |
| Android 编译 | debug APK、release APK和 instrumentation test APK编译通过 |
| APK签名与资源 | apksigner verify 通过；新验证码接口与加密会话桥存在于内置网页 |
| 浏览器冒烟 | 本地模拟HTTP，验证码→邀请码→登录→首次引导；401回登录；无控制台错误 |
| 独立代码复核 | 发现的数据库发信锁、媒体目录遗漏、旧Cookie干扰媒体、旧local共享问题均已修复并回归 |

浏览器启动在认证完成前仅调用账号配置和身份恢复，不启动业务 bootstrap；不在 localStorage 中保存账号 bearer。
归档工具的只读清单、完整备份、原子归档和重跑幂等在临时数据库/媒体上验证。
完整回归覆盖验证码重发、限流、过期、尝试次数、并发注册和邀请名额、SMTP失败与锁外发信、
稳定邮箱身份、旧认证拒绝、账号统计、跨日活跃、Cookie/CSRF、推送绑定释放及媒体授权。

## 安装包

- 文件：`deliverables/SpeakEasy-v4.3.0-release.apk`（约14.51 MiB）
- 包名：`com.speakeasy.app`；versionCode：430；versionName：4.3.0；min SDK：24；target SDK：36
- API源：`https://47-104-24-236.sslip.io`（沿用既有部署源；本次未向该服务部署）
- SHA-256：`33badede6262005af70850ab32faa7df73e532aabe92f95e26e6008f24070464`
- 签名证书 SHA-256：`fac7e179e0365593908c632b2e4b71b87a6bfa6e66aa8fd86a33a416658369b1`

网页已单独重建为同源 API 版本并复制到 `backend/app/static/app`；APK使用构建时指定的HTTPS API源，
包含本次新网页资源。后台和APK必须按[上线说明](EMAIL_CODE_AUTH_ROLLOUT.md)同步发布，不能只安装APK就认定旧服务器已支持新认证。

## 尚待实际环境验收

本地尚未配置SMTP发信邮箱/授权码与验证码密钥，未执行真实收信测试。
`adb devices` 无连接设备，因此没有运行 instrumentation 或完成物理设备升级、后台通话及OEM推送验收。
配置真实SMTP、提供测试收件地址并完成设备验收后，才进入生产维护窗口归档与切换。
