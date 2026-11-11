# Prompt 控制台与「酒馆 / SillyTavern」文风迁移

> 对应 `backend/app/v3/prompt_composer.py`（sonder-prompt-3.3.0）与管理台「对话与 Prompt → Prompt 控制台」。

## 1. 模块结构（A–H，顺序固定）

| 层 | key | 内容 | 来源 | 可否改写 |
|---|---|---|---|---|
| A | `global` | 平台基础规则：反模板化、输出格式、不假装人类 | 内置 | ✅ |
| A | `language` | 回复语言 + 学习者水平 + 纠错偏好 | 好友 `reply_language` / 用户档案 | ✅ |
| B | `character_card` | 结构化角色卡：姓名/年龄/城市/职业/Bio/性格/Speech DNA | `personas` | ✅ |
| B | `style` | **角色文风**（新增）：叙事口吻、句式、禁用词等 | 运营手填 | ✅ |
| C | `scenario` | 此刻生活：当地时间/在做什么/情绪/既有生活事实 | 运行时 plan | ❌ 只读 |
| F | `user_persona` | 用户画像（昵称/兴趣/目标语言/水平） | `user_profile` | ❌ 只读 |
| G | `relationship_memory` | 熟悉度 + 本轮注入的长期记忆 | 运行时 memory | ❌ 只读 |
| E | `examples` | 按情境检索的 1–2 组示例声线 | `persona_example_dialogs` | ❌ 只读 |
| D | `opening` | 首次开场风格（仅无历史时） | `first_message` | ✅ |
| H | `behavior` | 本轮行为倾向（plan 指令） | 运行时 plan | ❌ 只读 |
| H | `author_note` | 末位动态指令（post-history） | 运行时 plan | ❌ 只读 |

预算：文字模式 5600 字符、通话 1300；超预算按 examples → opening → style → 生活事实 → memory 的顺序压缩/丢弃。

## 2. 和酒馆的对应关系

| 酒馆（SillyTavern） | 本项目 | 说明 |
|---|---|---|
| Main Prompt | `global` | 平台硬规则，改写会全局生效（按角色 × 模式存） |
| Character Card（description / personality / scenario / first_mes / mes_example） | `character_card` + `opening` + `examples` | 角色卡字段进 persona；开场进 `opening`；示例对话进 `persona_example_dialogs` |
| Persona Description | `user_persona` | 由用户档案动态生成，**不可写死** |
| Author's Note / Post-History Instructions | `author_note` | 运行时动态度层，**不可改写** |
| World Info / Lorebook | 无对应 | 没有关键词触发式世界书；生活事实由 `scenario` 注入 |
| Style / 文风条目 | `style` | 新增；可粘贴酒馆 style 文本 |
| Prompt Manager 的 prompt_order / 插入深度 | 无 | 模块顺序固定 A–H，不可增删/排序 |
| 宏 `{{char}}` `{{user}}` | 部分支持 | `style` 模块里会替换成角色名 / 用户昵称；其它模块不支持宏引擎 |
| 预设 JSON 直接导入 | 仅文风部分 | 见下一节 |

## 3. 迁移酒馆文风（操作步骤）

1. 打开管理台 → 对话与 Prompt → **Prompt 控制台**；
2. 选角色 + 模式（文字/通话），点左侧 **「角色文风」(`style`)**；
3. 点 **「从酒馆预设导入文风」**；
4. 粘贴酒馆预设 JSON（形如 `{"prompts":[{"identifier":"style","content":"...","enabled":true}, ...]}`），点 **解析并填入**；
   - 会优先取 identifier/name 含 `style` / `main` / `jailbreak` / `author` / `writer` / `narrat` 的启用条目；
   - 找不到就取全部启用条目；
   - `{{char}}` → 角色名，`{{user}}` → 用户昵称；
5. 检查文本框内容（超过 900 字会被截断），点 **保存改写**；
6. **预览** 确认 `style` 模块出现在 `character_card` 之后；真实聊天立即生效。

## 4. 边界

- 文风只影响**表达方式**，不能覆盖角色事实/生活状态/记忆（只读模块优先级更高，且 global 有反编造规则）；
- 不支持酒馆的 `prompt_order`、深度插入、正则脚本、变量、世界书触发；需要这些行为要改代码；
- 文风按「角色 × 模式」保存；模板套用只对可改写模块生效；
- 单模块改写上限 4000 字符；`style` 实际注入上限 900 字符，超出按预算裁剪。
