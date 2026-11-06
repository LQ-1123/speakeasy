# SpeakEasy V3 · 本地验收记录（阶段 1）

- 时间：2026-11-06
- 入口：**http://localhost:8000**（FastAPI 直接托管 Next 静态导出；前后端同一进程）
- 环境：Windows 11 / Python 3.11（D:\speakeasy-venv）/ Node 24 / RTX 5080
- 真实外部服务：DeepSeek（LLM）、SiliconFlow SenseVoice（STT）、Fish Audio（TTS）

## 1. 自动化测试

| 项 | 命令 | 结果 |
|---|---|---|
| V3 后端测试 | `pytest backend/tests/test_v3_*.py -q` | **43 passed / 0 failed**（1.6s） |
| 全量后端回归 | `pytest backend/tests -q` | **425 passed / 6 failed**；6 个均为历史问题或本地未跟踪数据（HEAD 快照复跑同样失败/或本地 data 配置导致），**无新增失败** |
| 前端类型检查 | `npm run typecheck` | 通过（0 error） |
| 前端构建 | `npm run build:backend` | 通过（静态导出 → backend/app/static/app，91 文件 / 9.5MB） |

覆盖内容：schema 幂等、语料不重复、`extract_tags` 中英文、候选排序稳定、`plan_events` 随机可复现、
概率为 0 时无事件且记录 no_action、feed 游标 0 重 0 漏、三种可见性隔离、加好友幂等、
pending→accepted 升级、延迟事件物化与幂等、每日上限、人物冷却、双用户 API 隔离、
伪造 token 401、越权删除 404、V2 无身份头仍按 local 工作、加好友后 V2 好友/聊天可用。

## 2. 真实浏览器验收（agent-browser + Chrome，http://localhost:8000）

| 场景 | 操作 | 结果 |
|---|---|---|
| A 新用户进入 | 全新浏览器 profile 打开首页 | 首屏直接 `#/discover`；信息流 12 张卡片，全部是**未添加的 AI Persona**（Adrian/Jiwoo/Lin/Annika…），带 AI 标识/国旗/兴趣标签/加好友按钮；点作者进入 Persona 主页（介绍、性格、兴趣、AI 身份说明、动态列表、添加好友） ✅ |
| B 添加好友 | Persona 主页「添加好友」 | 好友关系建立；`/api/v2/friends` 立刻出现；通讯录显示 2 位好友；进入聊天发送 "Hey Adrian, what are you working on today?" → 12s 内收到英式口吻语音条回复（含中文提示、转文字） ✅ |
| C 用户发动态 | 「发动态」弹窗 → 公开 + 正文 | 动态写入并在信息流置顶；兴趣匹配到烹饪类人物；**32 秒后**通知铃铛出现 2 条：Giulia（厨师）点赞、Camila 评论「Flour everywhere is basically the opening scene of a cooking ballet!…」 ✅ |
| C+ 可见性 | 同一用户在浏览器里发 private / friends / public 三条 | 作者自己三条都可见；另一个设备身份的用户只看到 public，private/friends 均不可见 ✅ |
| D 好友申请 | 连发 5 条不同主题公开动态 | 产生 7 条事件，含 1 条 friend_request（Finn 申请语由 LLM 生成、贴合「读书+猫」主题）；通知页点「接受」→ Finn 出现在通讯录且状态 active，DB `friendships.status=accepted`，事件置为已读 ✅ |
| E 用户隔离 | 第二个浏览器 profile（独立 device_key） | 好友 0、事件 0；只能看到公开动态；两种身份的 `user_id` 不同；伪造 token 请求返回 401；删除他人动态 404 ✅ |
| F 原有功能回归 | `scripts/v3_scenario_f.py`（真实服务） | 好友列表 / 好友资料+记忆 / AI 文字聊天 4.5s / TTS 1.4s / **STT（把 TTS 音频喂回）识别正确** / 实时通话流式回合 / 表达本 / bootstrap / total_turns 累加 —— **10/10 通过** ✅ |
| G 信息流翻译 | 点任意人物动态「翻译」 | 英文原文下方出现中文译文（LLM 链路），再点收起 ✅ |
| H 信息流朗读 | 点任意人物动态「朗读」 | 用**该人物自己的声线**合成并播放（Adrian → `en-GB-RyanNeural`，Jiwoo → `ko-KR-SunHiNeural`），按钮显示「播放中」→ 播完恢复 ✅ |
| O 头像切换 Avataaars | 信息流人物头像 | 头像改为 DiceBear Avataaars：LLM 读取人物资料侧写部件（top/hairColor/skinColor/clothing/eyes/accessories…）后存库，客户端离线渲染；12 位人物全部生成成功，信息流/主页/偶遇/聊天同源；无 LLM 时确定性映射兜底 ✅ |
| N 头像维度 | 人种/性别/年龄 | SeedFace 支持三维度：地域→肤色/发色发质，性别→发型/胡须/耳环/唇色，年龄→灰白发/皱纹/眼镜（三档）；/api/v3/avatar/{seed}.svg?g=&r=&a= 全部生效；Avatar Lab 第 4 节矩阵可对比 5 地域×2 性别×3 年龄 ✅ |
| M 种子头像 | 信息流头像来源 | 不再用 mock 图片：personas.avatar_seed → /api/v3/avatar/{seed}.svg 确定性生成（同 seed 同脸，12 人物各不相同）；信息流/主页/偶遇/聊天同一 URL；老库启动自动回填种子 ✅ |
| L 朗读毫秒级 | 预热后点「朗读」 | 后台预生成 + mp3 + 浏览器预取：接口 3–17ms（冷启动 8400ms → 约 500×），浏览器 canplay 11–16ms；/health.audio 可看预热进度 ✅ |
| K 朗读声线跟随语言 | Lin 中文帖点「朗读」 | 声线按**帖子语言**选择：中文帖 → zh-CN-XiaoyiNeural；韩语帖 → ko-KR-SunHiNeural；跨语言帖子自动切换（测试覆盖）。语音 URL 带内容哈希，语料升级后不再播放旧缓存音频 ✅ |
| J 语言一致性修复 | Jiwoo(KR) 动态应为韩语 | 修复后信息流显示韩语原文；「朗读」用 ko-KR 声线读韩语；「翻译」给中文；偶遇/主页显示「母语：韩语」；加好友后聊天用韩语回复（实测西语人物 Ines 同样用西语回复） ✅ |
| I 偶遇（V3 重做） | 发现 → 偶遇 | 不再现场捏人：直接从已生成世界里挑人（Annika→Finn→Lin 连续「换一个」不重复）；头像/资料/声音与信息流完全一致；点「认识 TA」直接加好友进聊天 ✅ |

截图：`docs/screenshots/v3/`
- `v3-desktop-feed.png` / `v3-mobile-feed.png`：信息流（AI 人物卡片、AI 标识、兴趣标签）
- `v3-desktop-persona.png` / `v3-mobile-persona.png`：Persona 主页（AI 身份说明、加好友）
- `v3-persona-add-friend.png`、`v3-chat-ai-reply.png`：加好友后进入聊天 + 真实 AI 回复
- `v3-events-with-interactions.png` / `v3-events-mobile.png`：通知中心（点赞/评论/好友申请）
- `v3-desktop-events-empty.png`：空状态

## 3. 关键链路与安全

- 前端不持有任何服务商 Key：LLM/STT/TTS 全部由后端调用，浏览器只访问同源 `/api/*`。
- 身份：`X-User-Id` + `X-User-Token`（token 只存 sha256）；无身份头 → `local`（兼容老客户端）。
- 越权：所有好友/动态/事件按 user_id 过滤，越权一律 404；伪造 token 401。
- 老数据保留：`friends` 等表通过幂等 `ALTER TABLE` 增加 owner 列，历史数据默认归 `local`。

## 4. 已知问题 / 说明

1. 全量测试中 6 个失败为**历史问题**（discover 返回 pending、422 文案、call_stream 音频拼接、
   ko-KR 声线断言）或**本机未跟踪数据**（`data/custom.json` 空 opening_line、`data/tts_config.json` 用 fish 导致 audio/mpeg），
   均与本轮 V3 改动无关（`git archive HEAD` 干净快照复跑确认）。
2. 信息流顶部会显示本机联调期间产生的用户公开动态（真实多用户行为）；
   可用 `scripts/reset_v3_test_data.py` 清理测试数据（保留 Persona 语料与 local 历史数据）。
3. 云端部署与公网验收本轮未执行（缺 SSH 凭据，已与用户确认暂缓）；
   `deploy/deploy_v3.sh` 已就绪（备份 → 幂等迁移 → 重启 → 健康检查 → 失败回滚）。
