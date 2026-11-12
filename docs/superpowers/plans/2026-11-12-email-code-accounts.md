# 邮箱验证码登录与用户统计整改实施记录

用户批准的目标：强制邮箱验证码登录、邀请码内测、新账号重新开始、旧身份归档；复用 SMTP。不得执行生产归档或真实发信，直到配置和验收具备条件。

## 全局约定

- 功能开关 `SE_EMAIL_CODE_AUTH` 默认 1；0 仅保留旧版兼容/既有测试，生产启用新模式时同时设置 `SE_REQUIRE_AUTH=1`、`SE_INVITE_REQUIRED=1`、`SE_MAIL_DEV=0`。
- 新模式禁用旧设备/密码认证，不按 device_key 继承用户。邮箱 trim/lower 唯一，设备仅属于 session。
- OTP：6 位、600 秒、重发 60 秒、5 次错误、每邮箱 5 次/小时、每 IP 30 次/小时；摘要使用独立 `SE_EMAIL_CODE_SECRET` HMAC，生产必须配置；不日志输出验证码。
- Web 同源 HttpOnly Cookie + CSRF，不持久化 bearer；Android 加密存储 session token。会话 30 天。
- `v3_users.archived_at` 表示存档；业务和后台任务拒绝存档身份；旧数据不迁入新账号。

## Task 1: OTP backend

Own accounts.py/accounts_router.py/security.py/auth registration handler in router.py, new email_codes.py/auth_policy.py and backend auth tests. Implement persistent mail challenges and rate limiting, atomic invite/account/challenge/session success, SMTP failure, new account/session authentication. Endpoint request `{email}` -> `{challenge_id,retry_after:60,expires_in:600}`; verify `{challenge_id,code,invite_code?,device_key?,platform?}` -> existing session result + `{created,email_verified:true}`. Correct code/missing invite -> 403 detail `{code:'INVITE_REQUIRED',message:...}` without consumption; wrong/expired/reused -> 400 structured error; rate 429 Retry-After; mail unavailable -> 503. Native platform android returns session_token; web relies on cookies and returns user_id,email,csrf_token,expires_at (no bearer). GET `/auth/config` returns `{email_code_auth:true,invite_required:true}`. GET `/auth/identity` works for Cookie and header sessions. Protect against missing/deactivated/unverified/archived accounts. Preserve legacy behavior only flag=0. Root owns archive schema initializer and push detach integration. Test first, no commit, no other file changes.

## Task 2: Archive and admin (root)

Add identity archive schema/helper and safe backup/dry-run/apply CLI with explicit cutoff ID manifest, archive credentials, sessions/tokens invalidation, disable/free old push ownership, suppress events/jobs. Never run destructive migration on data/. Add formal/history scopes and metrics with Beijing daily boundary; keep legacy scopes available. Guard scheduled/delivered followups and events. Disable unused classic global history/ws in new mode; preserve authorized signed media. Add meaningful database/API regression tests.

## Task 3: Frontend auth

Own frontend except admin metrics UI handled by root. Replace password UI by email code, auth gate outside business provider, restore Cookie/native session, propagate 401, all current identity accesses use session user_id, no automatic registration in code mode. Implement account/session display/logout and storage isolation. Use new native bridge session methods when available; root implements Android. Existing device registration and session cleanup coordinated with root. Tests first; typecheck/test/build.

## Task 4: Android and integration (root)

Extend Keystore store and bridge with readSession/saveSession/clearSession, session-based HTTP/native call tickets, logout endCall/clear notifications, push account filtering, persistent installation identifier. Web bridge contract carries sessionToken,userId,expiresAt; no token in URLs. Preserve signed-upgrade migration until auth mode known, but no legacy migration failure may block login. Push only after auth. Compile JVM tests/debug APK where dependencies available; adb device checks only if available.

## Task 5: Review, docs and verification

Review all changes against user plan, fix findings, run backend related tests/full suite as practical, frontend tests/typecheck/static build, Android build/tests. Prepare SMTP verification command and operational rollout instructions. Do not claim real mail receipt or physical-device results without evidence. No production deploy/DB migration without tested artifacts and required credentials.
