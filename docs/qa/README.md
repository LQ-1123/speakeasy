# QA 证据索引（2026-11 迭代）

本目录保存真实运行产生的验证报告（JSON，含每条检查项与失败详情）；截图在
`docs/screenshots/` 下按环境分组。

## 1. 本机 Web（真实 Chrome + 真实 LLM，8 场景）

- 汇总：`local-web-verification-summary.json`（`all_passed: true`）
- 场景：`local-web-main-*.json`（29 检查）、`reopen`（2）、`legacy`（3）、`mobile`（11）、
  `offline`（16）、`isolation`（14×2 + 5）、`chat`（21）
- 生成命令：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_local_web_verify.ps1`
- 截图：`docs/screenshots/onboarding/`（01–14，桌面/手机/320px/断网/真实聊天）

## 2. AI 对话质量（真实 LLM，3 种性格 × 8 话题）

- `persona-chat-eval.json`：Callum / Mei / Adrian 的完整 transcript、平均词数、提问率、
  模板句命中、跨人物相似度（0.094–0.161）与自动 verdict（6/6 通过）
- 生成命令：`.\.venv\Scripts\python.exe scripts\eval_persona_chat.py`

## 3. 四种交互模式一致性（文字/图片/语音/通话）

- `chat-modes-20261110-190255.json`：20/20 检查通过；含文字/图片/通话/语音条回复、
  文字与通话 Prompt 共享角色卡字段、Prompt 模块顺序、TTS 可用
- 生成命令：`.\.venv\Scripts\python.exe scripts\verify_chat_modes.py`

## 4. 远端（生产服务器）Web

- 汇总：`remote-web-verification-summary.json`（`all_passed: true`）
- 场景：`remote-web-main-*.json`（29）、`reopen`（2）、`legacy`（4）、`mobile`（11）、
  `chat`（22，含通话票据）、`compat`（26，发现/人物/翻译/朋友圈/TTS/头像）
- 生成命令：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_remote_web_verify.ps1`
- 截图：`docs/screenshots/remote/`（含 20/21 兼容性截图）
- 服务端日志巡检：部署后 15 分钟 0 个 5xx、0 traceback（`scripts/remote_health.py`）

## 5. Android APK

- `android-verify.json`：安装/启动/三步引导/生产落库/重启免引导/发现页/「我」页 12 项检查
- `apk-inspect.json`：生产 API 域名存在、无 localhost/LAN 残留、无密钥泄漏（PASS）
- 截图：`docs/screenshots/android/`（01–11）
- APK：`deliverables/SpeakEasy-v4.2.0-release.apk`（15,086,185 B，
  SHA-256 `779f36f5ce9bc774999a37a158bdaec3d3c886ef1a5b5c3984473e4b665aad83`）

## 6. 自动化测试

- 后端：`984 passed`（`pytest backend/tests -q`，含新增 34 个测试）
- 前端：`tsc --noEmit` 0 error；`vitest run` 18 文件 / 126 测试通过
- 构建：`npm run build:backend` 成功（静态产物 9.32 MB）
