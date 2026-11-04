# 全链路冒烟报告

- 时间：2026-11-04 23:00
- 提交：`4172f43 feat(call): 通话回合接真实流式链路（NDJSON + 逐句播放）`
- 后端：http://127.0.0.1:8000（llm `deepseek-flash` / stt `auto`）
- 数据：好友 20 · 朋友圈 6 · 表达本 1

## 一、API 层

| 环节 | 结果 | 实测 |
|---|---|---|
| 健康检查 | ✅ PASS | 200 ok |
| 设置读取 | ✅ PASS | llm=deepseek-flash key=True \| stt=auto |
| 捏人 discover | ✅ PASS | 27.9s → Antoine / male / France / 音色 89e9fdb8 |
| 文字聊天 | ✅ PASS | 7.8s → 回复「Bah, still at the shop, wrapping up a long after」 audio=有 |
| 语音聊天 | ✅ PASS | 6.9s → 转写「Hey, here is mores, asmazobroken in.」 / 回复音频 有 |
| 翻译 | ✅ PASS | 0.8s → 我一直想问你那件事。 |
| 朋友圈读取+发布 | ✅ PASS | 现存 5 条 \| 发布 0.0s → 200 |
| 朋友圈 AI 评论 | ✅ PASS | 我的动态 1 条新增，评论 3 条：「That's a proper Bristol sunset, that」 |
| 表达本增改删 | ✅ PASS | 新增→PATCH(mastered)→列表 2 条→DELETE 200 |
| 换口音 | ✅ PASS | American English (New York) → en-US-AndrewMultilingualNeural (rate +10%, pitch -7Hz) |
| 通话流式回合 | ✅ PASS | warmup 200 \| 3 事件 / 1 句 / 11.6s \| summary 200 |

## 二、UI 层

| 页面 | 结果 | 实测 |
|---|---|---|
| UI 通讯录 | ✅ PASS | 好友 20 人，含新朋友 Antoine；头像 21 张全为 DiceBear(21)，首字母 0 个 |
| UI 聊天记录 | ✅ PASS | 气泡 8 个，语音条 9 个（后端该好友消息含文字+语音回合） |
| UI 语音转写 | ✅ PASS | 展开后文本「Hey! Antoine here, cheesemonger over in Strasbourg」 |
| UI 朋友圈 | ✅ PASS | 渲染 6 条 / 后端 6 条；我的动态可见=True，AI 评论可见=False |
| UI 表达本 | ✅ PASS | 界面 1 条 / 后端 1 条 |
| UI 设置 | ✅ PASS | 模型输入框 ['https://api.deepseek.com/v1', 'deepseek-flash']，后端 deepseek-flash |
| UI 我的资料 | ✅ PASS | 城市/职业来自真实 profile：{'city': True, 'job': True} |

## 三、冒烟中发现并修复的问题

**AI 评论不渲染**（API 有 3 条，界面 0 条）

```
原因：我的动态 friend_id = ''、author_type = 'user'
      mapMoment 把 authorId 映射成 ''（空串）
      → 被「AI 互评清除」规则判为好友动态 → comments 全部清空
修法：author_type === 'user' 时 authorId 映射为 'me'
      评论字段用 c.content / c.friend_id（后端用 content，前端原来读 text）
复测：我的动态作者名 Sunyulin，评论 3 条正常渲染「Tom：That's a proper Bristol sunset…」
```

## 四、本次未覆盖

1. **真实麦克风链路**：调试用浏览器没有音频设备，`按住说话` / 通话里的 `按住说话` 只验证到「界面出现按钮 + 后端流式接口可用」，需要人工点一次确认。
2. **图片动态**：`POST /moments/photo` 未测。
3. **好友申请接受/拒绝**：当前后端无 pending 请求（requests=0），无法触发；接口已在代码中接入。
4. **流式通话多句**：本次触发 1 句语音（后端按句合成，短回复只有 1 句），多句排队播放逻辑未在真实数据下观察。

## 五、截图

| 页面 | 文件 |
|---|---|
| 通讯录（20 人 / DiceBear 头像） | `docs/screenshots/smoke-contacts.png` |
| 聊天（新朋友 Antoine） | `docs/screenshots/smoke-chat.png` |
| 朋友圈（6 条，含我的动态+AI 评论修复后） | `docs/screenshots/smoke-moments-fixed.png` |
| 表达本 | `docs/screenshots/smoke-expressions.png` |
| 设置 | `docs/screenshots/smoke-settings.png` |
| 我的资料 | `docs/screenshots/smoke-me.png` |
