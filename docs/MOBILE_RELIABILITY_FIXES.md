# 4.1.1 真机问题修复与验收（9 项）

> 来源：用户在 Android 真机上的 9 条反馈。本文记录逐条根因、代码修复、可复现的验收证据，以及仍需「凭据 / 真机」才能闭环的剩余项。
>
> 版本：前端 + Android `4.1.1`（versionCode 411）· 分支 `feat/mobile-reliability`
> 验收时间：2026-11-10

## 0. 结论速览

| # | 用户反馈 | 状态 | 关键修复 |
|---|---------|------|---------|
| 1 | 输入框 / 输入法占位错误 | ✅ 代码修复 + 浏览器验收 | visualViewport 驱动 `--ime-height`、viewport `interactive-widget=resizes-content`、composer 不再叠加 safe-bottom、聚焦/键盘弹出贴底 |
| 2 | 跳转到消息页时先错误跳转再跳对 | ✅ 代码修复 + 浏览器验收 | 导航单一入口 `reduceNavigation`：tab/返回用 replace、详情 push、hash 只一个监听、移动端首帧 matchMedia、bootstrap 前中性占位；另修掉切换 tab 时详情页一闪而过 |
| 3 | 聊天头像不更新 / 两套头像不统一 | ✅ 代码修复 + 真实后端验收 | 统一 `resolveAvatar()`（LRU 缓存）+ 后端 friend 行复制 persona 头像元数据 + 读取时确定性归一化 |
| 4 | 语音消息时长重叠/被裁切（参考图 5） | ✅ 代码修复 + 真实后端验收 | 微信式语音条（左时长、右扬声器、宽度随时长 80–220px），移除固定宽容器 + 密集波形 |
| 5 | 换壁纸后只有部分强调色跟随，其余仍是浅蓝 | ✅ 代码修复 + 浏览器验收 | 从系统 accent 用 M3 `Hct/SchemeTonalSpot/MaterialDynamicColors` 生成完整 35 角色亮/暗色板（不再以 Honor 浅蓝为底） |
| 6 | 安卓全局通知与应用推送不行 | ✅ 代码链路修复 + 客户端 AppKey 已配置；服务端凭据待配 | 声明缺失的 JPush meta-data、`SpeakEasyApp` 启动初始化、`JCollectionAuth.setAuth(true)`、注册轮询、本地系统通知兜底、设置页真实状态与 toast |
| 7 | 对方没回时退出界面会导致消息重复发送 | ✅ 前后端修复 + 真实后端验收 | 客户端 `client_message_id` 幂等键 + 服务端唯一索引/幂等返回 + 乐观消息按 clientId 对齐 + 防重入/失败重试 |
| 8 | 打开聊天页不自动到最新消息 | ✅ 代码修复 + 真实后端/浏览器验收 | 移除聊天页滚动记忆，进入/切好友/新消息/自己发送/键盘弹出统一贴底，用户上翻后不打扰 |
| 9 | 触感反馈、系统通知等没实现 | ✅ 代码修复 + 单测/浏览器验收 | `performHaptic`（10 种语义震动）+ 本地系统通知桥 + 页面隐藏时发通知 + 状态/权限 UI |

## 1. 输入框 / 输入法占位错误

**根因**
- Android WebView `windowSoftInputMode=adjustNothing`，键盘不改变布局视口；旧实现只依赖原生 `--ime-height` 且 composer 又叠加 `safe-bottom`，键盘弹出时输入框被埋进键盘或出现大空白。
- ChatPane 未在键盘/视口变化时保持最新消息可见。

**修复**
- `w4/v3/platform.ts`：新增纯函数 `computeImeHeight({innerHeight, visualViewport})` 与 `installImeTracking()`，监听 `visualViewport` resize/scroll + window resize/orientation，写入 `--ime-height` / `--se-ime`；native `insets.ime` 仅在无 `visualViewport` 时兜底。
- `frontend/src/app/layout.tsx`：viewport 增加 `interactiveWidget: "resizes-content"`。
- `w4/styles/chat.css`：composer 底距改为 `calc(12px + max(0px, var(--safe-bottom) - var(--ime-height)))`，键盘弹出时不再叠加安全区。
- `ChatPane.tsx`：输入框聚焦、键盘弹出、消息变化时贴底（见 #8 的滚动系统）。

**验收**
- `platform.test.ts`：IME 纯函数 7 用例。
- 浏览器模拟 `--ime-height: 320px`：`.w4-app` 高度 844→524，composer 底边 524（键盘上方），无叠加空白；截图 `.qa-tools/.qa-shots/ime-open.png`。
- 真机软键盘仍需一次 Android 实机确认（headless 无法拉起系统键盘）。

## 2. 跳转到消息页时先错误跳转再跳对

**根因**
- `navigate()` 同时写 `location.hash` 与 `setRoute`，`hashchange` 又写一次 → 双渲染/二次跳转。
- tab 切换与返回都用 hash 赋值（push），污染 Android 返回栈；原生兜底 `history.back()` 会退回上一个聊天。
- 移动端 `useState(false)` 首帧渲染桌面双栏再切手机；bootstrap 完成前 ChatPane 会短暂渲染「会话不存在」。
- `installPlatform` 随路由变化重建监听，native 事件可能丢失。
- 切换「消息/通讯录」tab 时，手机列表视图里 `.w4-main` 只是被 `translateX(104%)` 藏到屏外；它挂载时全局 `se-page-in` 入场动画会用 `translateY` 覆盖这个隐藏位移，导致详情页（如通讯录空状态「选一位好友，看看 TA 的资料」）整屏一闪而过。

**修复**
- `w4/navigation.ts`：新增唯一历史决策纯函数 `reduceNavigation`（hash 不变 → 只写 store；回 tab 根/父级 → replace；详情 → push）、`routesEqual`、`isInternalNotificationRoute`。
- `w4/navigationStore.ts`：`setRoute` 相等短路；`navigateTo/backToParent/syncFromHash` 成为唯一导航入口；`hashchange` 只处理外部导航。
- `App.tsx`：移动端首帧用 `matchMedia`；hashchange 单监听 + 非法 hash 恢复；`installPlatform` 只安装一次；bootstrap 前渲染中性「正在加载…」占位并 5s 兜底。
- `platform.ts`：`notification_open` 路由白名单与原生 `NativePolicy.validRoute` 对齐（含 `persona/settings/requests`）。
- `styles/shell.css`：手机列表视图下禁用 `.w4-main` 的入场动画，让 tab 之间直接切换、不再闪详情页。

**验收**
- `navigation.test.ts` 22 例 + `platform.test.ts` 7 例 + store 短路用例。
- 浏览器证据：tab 切换 history 不增长；打开聊天 push 1 条、返回 replace 不增长；修复前「列表返回→又回到上一个聊天」的序列在修复后不再出现；深链先中性占位，不再闪「会话不存在」。
- 移动端 CDP 实测：切到通讯录后连续 6 帧采样，`.w4-main` 的 `animationName=none`、`left=406 > 390`（始终在屏外），不再出现「选一位好友…」整屏闪页。

## 3. 聊天头像不更新 / 两套头像不统一

**根因**
- 客户端两套解析互相覆盖：`mapFriend` 渲染 Avataaars 后，`assignAvatars()` 又把 data-uri 误判为老图片并用 personas 画风覆盖 → bootstrap 路径与信息流/主页两套画风；每次轮询重复渲染（慢、闪）。
- 后端 `_friend_row_for_persona` 用 `persona.id + 姓名前缀` 拼 `avatar_seed`，与 persona 的真实 seed 不同 → 同一个人两张脸。
- 老人物缺 `avatar_style/avatar_options` 时各接口各自回落；已有好友行不随人物头像变化更新。

**修复**
- 前端 `w4/avatar.ts`：统一 `resolveAvatar(raw)`（key = seed/style/gender/options/兜底 URL 的 LRU 缓存）；有 options → Avataaars、有 seed → 确定性 Avataaars、无 seed → 兜底；`api.ts` 的 mapFriend/assignAvatars/loadFriendList 与 FeedView/EventsView/PersonaProfileView/DiscoverPane 全部走同一入口（Leader 的 `client_message_id` 契约保留）。
- 后端 `v3/service.py`：friend 行直接复制 persona 的 seed/style/options；所有 persona/作者/评论/通知/好友关系 payload 同源并含 gender。
- `v3/avatar_style.py`：缺 style/options 时用确定性 `default_options` 读取时归一化（不调 LLM、不写库）。
- `v3/schema.py`：`sync_persona_friends` 幂等同步/修复老好友行 + 回填 persona seed。
- `v3/avatar.py`：兜底 URL 增加 `v=` 渲染版本，破老客户端 immutable 缓存。

**验收**
- `avatar.test.ts` 16 例 + `backend/tests/test_avatar_consistency.py` 6 例；后端全量 950 tests 通过。
- 真实隔离后端页面 DOM：同一人物（Emma c2）在 `#/chats`、`#/chat/{id}`、`#/contacts`、`#/persona/{id}` 的头像 data-uri **逐字节相同**；production 构建下 5/5 检查通过（`qa-real.mjs avatars`）。

## 4. 语音消息时长重叠（参考图 5）

**根因**
- 旧语音条在固定 width 容器里塞「播放圆钮 + 17 根波形 + 时长」三个 `flex:none` 子项，内容宽大于容器 → 时长在右边缘被裁切；窄屏 `max-width:78%` 再夹一次。

**修复**
- `ui.tsx#VoiceBar` 重做为微信式：左侧时长（tabular-nums，不压缩）、右侧扬声器/声波图标；整条可点，播放中图标脉冲；长按转文字菜单、气泡旁「转文字」保留。
- `format.ts`：`voiceBubbleWidth` 1–60s 线性 80→220px；`durationLabel` 支持 1:00 以上。
- `chat.css`：`.w4-bubble.is-voice` 内距/配色（mine = primary-container，对方 = surface-container-high），≤380px 放宽气泡列到 84%。

**验收**
- `format.voice.test.ts` 7 例。
- 真实后端 20/20 检查：每条语音的时长都在 voice bar 内、voice bar 在气泡内、时长不被视口裁切；320px 无横向溢出；截图 `qa-shots-real/real-voice-390.png` / `real-voice-320.png`。

## 5. 强调色全局跟随壁纸

**根因**
- Android 12+ 原生 `getTheme()` 只返回 `accent/onAccent`；旧 `legacyThemeToSnapshot` 以 Honor 浅蓝 fixture 为底，只覆盖 `primary/onPrimary` → 换壁纸后按钮变棕，但气泡容器、tab 选中胶囊、tag、头像底、通知横幅仍是 `#B9CBFF` 浅蓝。

**修复**
- 新增 `theme/palette-from-seed.ts`：用 `Hct + SchemeTonalSpot(2021) + MaterialDynamicColors` 从系统 accent 同步生成 **35 个语义角色 × 亮/暗** 的完整 M3 色板（surface 保持中性，容器/强调色全量跟随）；实测 <1.3ms。
- `system-theme-adapter.ts`：legacy accent 路径改用完整色板；accent 缺失/非法时返回 `null`，ThemeStore 保留已存储主题（不会把暖棕重置回浅蓝）；完整 `getThemeSnapshot()` 路径仍优先。
- `scheme.ts` 改为同一实现的兼容门面（图片取色 worker 共用）；`tokens.css` 复核（静态 Honor 色板仅做 hydration 前 fallback）。
- 原生 `MainActivity.onResume()` 每次回前台 dispatch `{type:"theme"}`，`getTheme()` 无缓存实时读 `system_accent1_600/200`。

**验收**
- `palette-from-seed.test.ts` 6 例 + `system-theme-adapter.test.ts` 8 例 + theme 共 21 例。
- production 浏览器注入 accent `#8C4A2F`：全部角色变为暖棕（primary `#8E4D32`、primaryContainer `#FFDBCE`、气泡 `rgb(255,219,206)`），旧浅蓝角色计数 0；换绿色 accent 全量变绿；dark 色板同步正确。截图 `artifacts/theme-accent/`。
- 真机「换壁纸 → 切回前台」需一次目视确认（已加 onResume 重采样保障）。

## 6. 安卓全局通知与应用推送

**根因（已核实到 APK 清单）**
- 发布包 v4.1.0 的 AndroidManifest 中 `JPUSH_APPKEY` 为空且 **缺少 `JPUSH_PKGNAME`**（JPush AAR 不提供），SDK 拿不到有效 key → 无法注册。
- `JPushInterface.init` 只在用户点「开启通知」后调用，启动不 init；注册 ID 无轮询。
- `PushReceiver` 去重 key 不一致；默认 route `#/messages` 非合法路由。
- 后端 `dispatch_push` 未配置 key 时静默返回，事件永久 pending。
- 真机首次注册还卡在「正在注册推送」：JCore 5.x 需要先 `JCollectionAuth.setAuth(context, true)` 再 `JPushInterface.init`，否则 `getRegistrationID()` 一直为空；同时设置页只读一次状态，异步结果永远不显示。
- 本机 `.env` / `local.properties` 最初无 JPush 与厂商凭据（后来已补客户端与本地服务端）。

**修复**
- `AndroidManifest.xml`：`android:name=".SpeakEasyApp"`、`JPUSH_PKGNAME/JPUSH_APPKEY/JPUSH_CHANNEL` meta-data、显式 `VIBRATE`。
- 新增 `SpeakEasyApp`：进程启动建 4 个通知渠道；`JCollectionAuth.setAuth` 按用户同意状态设置；key 有效才 `init/resumePush`（用户关闭则 `stopPush`）。
- `PushCoordinator`：`JCollectionAuth.setAuth(true)` → init；注册轮询（用户主动重试最长 20s，被动 10s）、单飞 + 冷却，但**用户主动重试不受冷却吞掉**；每个 registration id 只发一次 `registered`；事件持久化一次性投递；最近一次推送结果（`last_status/last_message`）写入状态供 UI 展示。
- `PushReceiver`/`PushEvents`/`NativePolicy`：统一 seen/read/arrived 去重 key、前台抑制系统通知、路由白名单（默认 `#/chats`）。
- 新增 `LocalNotifications`：无 JPush 凭据时也能在页面隐藏/后台存活期间发**本地系统通知**（通用正文，绝不发原文；事件 id 与 JPush 互斥去重，点击清通知并跳到对应路由）。
- `app/build.gradle`：厂商通道插件改为**按凭据按需打包**。未配置 appid 的华为/荣耀等厂商 SDK 被塞进 APK 时，可能在荣耀机型上用空 appid 初始化异常，拖垮极光主通道注册；配置对应凭据后自动启用。设置页同时显示极光长连接状态（已连接/未连接）帮助区分“网络到不了极光”与“SDK 未注册”。
- **真机 logcat 定位到的根因**：JCore 5.x 还要求应用自定义一个继承 `cn.jpush.android.service.JCommonService` 的 Service 并在清单声明（`:pushcore` 进程 + `cn.jpush.android.service.JCommonService` intent-filter）。缺它时每次 init 都报 `AndroidManifest.xml missing required service... Otherwise no service`，长连接与注册永远不会发生。已新增 `SpeakEasyPushService` 并声明；真机验证后设置页应变为「已开启，推送已注册。」。
- 前端：`GlobalNotification` 页面隐藏时调用 native 本地通知、前台仍显示应用内横幅；`SettingsPane` 注册期间每 2s 自动刷新、展示最近一次真实错误并支持「重新注册推送」；`PushOptIn`/`notify.ts` 暴露真实状态与权限引导。
- 后端：未配置 key 时幂等把 pending 事件标记 `unconfigured` 并只 WARN 一次，配置后保持原投递语义（渠道/静音/extras）。

**验收**
- **通知头像 + 消息内容（真实通讯软件样式，2026-11-10）**：后端推送改为 `title=AI 名字`、`alert=消息正文`、`big_text`、`large_icon=<确定性头像 PNG 绝对 URL>`；新增 `GET /api/v3/avatar/{seed}.png`（cairosvg 栅格化 + LRU 缓存）供极光下载。真机通知栏验证：头像（Avataaars 圆形图）、名字与消息正文同时显示，点击进对应聊天；前端在推送已注册时不再发本地兜底通知，避免重复（截图 `artifacts/qa-4.1.1/push/notification-avatar-content.png`）。注意：正文会显示在锁屏上，后续可加「锁屏隐藏内容」开关。
- Android：`BUILD SUCCESSFUL`，13 个 JVM 单测通过；release APK manifest 经 aapt2 确认包含 `JPUSH_PKGNAME="com.speakeasy.app"`、`JPUSH_APPKEY="2b76…628a"（非空）`、`JPUSH_CHANNEL="official"`、`VIBRATE`、`SpeakEasyPushService`（`:pushcore` + `cn.jpush.android.service.JCommonService` intent-filter）；凭据经 `/v3/push/validate` 校验通过。
- **真机验证（HONOR MEP-AN00 / Android 17，2026-11-10）**：修复前 logcat 反复报 `missing required service: cn.jpush.android.service.JCommonService`、`ps` 无 `:pushcore` 进程；修复后 logcat 出现 `SpeakEasyPush: registered id=140fe1da…`、`:pushcore` 进程启动、设置页显示「已开启，推送已注册。」；用 JPush REST API 向该应用发送测试通知，通知栏成功收到（截图 `artifacts/qa-4.1.1/push/`，含 logcat 片段）。
- **后端链路验证（部署到生产服务器后）**：`/api/v3/client/devices` 上线，设备行写入 `client_devices`（`140fe1da…` / vendor HONOR）；向 outbox 插入一条模拟好友消息事件，2 秒内 worker 将其推送并置为 `push_status=sent`，手机通知栏收到「Amelia 发来一条消息」（截图 `artifacts/qa-4.1.1/push/backend-push-notification.png`）。
- 前端 `platform.ts` 增加启动时幂等补报：页面每次启动都读取原生真实 registrationId 并上报 `/api/v3/client/devices`，避免“registered 事件在页面就绪前派发导致后端没有设备”的竞态。
- 前端：`notifyNative` 浏览器验收 6/6（隐藏页发且仅发一条系统通知、无应用内横幅；前台只显示横幅；设置页显示「当前安装包未配置推送服务」与配置指引）。
- 后端：`test_push_outbox.py` 7 例 + `test_client_events.py` 11 例通过；全量 950 tests 通过。

**当前状态（2026-11-10）**：JPush 控制台应用已创建（包名 `com.speakeasy.app`），AppKey/Master Secret 已写入本机 `mobile/android/local.properties` 与本机 `.env`，并已写入生产服务器 `/root/speakeasy/.env`（服务已重启）。生产后端与 Web 已重新部署（`/api/v3/client/devices` 等最新代码）。真机（荣耀 MEP-AN00 / Android 17）验证客户端注册成功：`registered id=140fe1da…`、设置页「已开启，推送已注册。」、设备已写入后端 `client_devices`；后端 outbox 模拟事件推送成功（`push_status=sent`，通知栏收到）。剩余：荣耀厂商通道（可选）用于清后台后的稳定送达；真实好友回复的端到端验证等同一条链路。

**剩余 gate**：服务器推送凭据（客户端已配好）、各厂商 appid/key 与签名指纹在 JPush 控制台注册（厂商通道可选），然后真机做送达/点击/去重验收（详见 `mobile/README.md`）。

## 7. 对方没回时退出界面导致消息重复发送

**根因**
- 发送无幂等键：POST 很慢时用户退出/重进，乐观消息与服务端消息 id 不同而并存；POST 失败后乐观消息永久「发送中」，再次进入又拉回服务端同一条。
- 双击/输入法回车 repeat 可重复触发 send。

**修复**
- 后端：`messages.client_message_id` 列 + `(friend_id, client_message_id)` 部分唯一索引（迁移幂等）；`POST /api/v2/chat/{friend_id}/text` 接受 `client_message_id`（8–64 位 `[A-Za-z0-9._:-]`）；重复提交返回已有 user_message 与已生成回复，不重复落库/调用 LLM；响应消息带 `client_message_id`。
- 前端：发送生成 `clientId` 并随请求发送；`mergeMessages/reconcileThread` 按 clientId 合并去重、清除 failed；600ms 同文防重入 + inflight 防并发；失败气泡显示「重试」（复用同一 clientId）。
- `api.ts`：`mapMessage` → `clientId`；`sendText(friendId, text, clientMessageId?)`。

**验收**
- `backend/tests/test_chat_idempotency.py` 4 例（DB 唯一、重复 POST 仅 1 条、无 key 兼容、非法 id 422）。
- 真实后端端到端：拦截请求延迟 4s → 发送 1 条 → 退出聊天 → 重进 → 气泡仍 1 条；后端 user 消息恰 1 条且返回同一个 `client_message_id`（`qa-real.mjs dup` 5/5）。

## 8. 打开聊天页不自动到最新

**根因**
- `useScrollMemory('thread:'+friendId)` + `initialThreadPosition` ref 在 ChatPane 同一实例切好友时不重置；虚拟列表测量后没有二次贴底，`messageListOffset` 变化也不重贴 → 首屏停在旧位置/顶部。

**修复**
- 删除聊天页滚动记忆，改为贴底系统：进入会话/切好友/首次加载/消息变化/对方输入/偏移变化/自己发消息/输入框聚焦/键盘弹出时 `virtualizer.scrollToIndex(last,{align:'end'})` + 双 rAF 兜底；滚动监听 `isScrollingUp` 退出跟随，程序化滚动窗口内静音；加载更早消息不跳底；列表页（消息/通讯录）原滚动记忆保留。

**验收**
- `scrollMemory.test.ts` 8 例。
- 真机数据（36 条历史）production 构建：打开聊天 `distance=0`；用户上翻后不被强制拉回；自己发消息后回到底部（4/4）。

## 9. 触感反馈与系统通知

**根因**
- NativeBridge 只有 `playSound` 等，没有 `performHaptic`；前端 haptics 永远 `performed=false`。系统通知没有任何本地实现。

**修复**
- Android `NativeBridge.performHaptic(type)`：`VibratorManager/Vibrator + VibrationEffect`，10 种语义震动（`HapticPolicy`），尊重系统触感开关，失败静默；`getCapabilities` 上报 `haptics`。
- 前端 `theme/haptics.ts`：原生返回 boolean 才算 performed；无 native 时 `navigator.vibrate` 兜底。
- 本地系统通知桥（见 #6）：`postNotification/getNotificationStatus/requestNotificationPermission/openNotificationSettings`，配合 `GlobalNotification` 页面隐藏时投递、读/点开时取消。

**验收**
- Android `HapticPolicyTest` 5 例；注入 native bridge 后点击 tab 记录到 `performHaptic("selection")`。
- production 浏览器 6/6 通知场景通过；真机 10 种手感与系统开关需实机确认。

## 10. 构建与回归

| 项目 | 结果 |
|------|------|
| 前端类型检查 | `npx tsc --noEmit` exit 0 |
| 前端单测 | 17 files / 109 tests 通过 |
| 前端生产构建 | `npm run build` 成功（9 静态页，export 成功） |
| 后端全量测试 | 950 passed |
| Android JVM 测试 | 13 passed |
| Android debug 编译 | BUILD SUCCESSFUL（manifest 已核对） |
| 浏览器 production 验收 | 真实后端 42/42；preview 24/24；consoleErrors=0 |
| 移动端 tab 切换闪光 | CDP 连续 6 帧：`animationName=none`、`.w4-main` 始终在屏外 |
| 签名 release APK | `deliverables/SpeakEasy-v4.1.2-release.apk`（versionCode 412 / versionName **4.1.2**，品牌 **Sonder**：桌面图标随明暗切换 Light/Dark、通知栏左侧 Dynamic 渐变图标、右侧 AI 头像（无头像时 Dynamic 兜底）、透明系统开屏、JPush AppKey/注册修复；SHA-256 `1a33e1167370804b9b21e34e8072f0f989f57ee16f5ecbccd5c3ca2ddd92bde4`，15,180,376 bytes） |

## 11. 仍需真机/凭据的剩余项（不可用编译结果替代）

1. **JPush / 厂商推送**：客户端 AppKey 与注册修复已就绪；服务器 `.env` 还需写 `JPUSH_APP_KEY/JPUSH_MASTER_SECRET` 并重启；厂商 appid/key 需按需在各厂商后台注册。就绪后用真机验证注册、锁屏/杀进程送达、点击路由、与本地通知去重。
2. **Android 真机 IME**：`--ime-height` 契约已被浏览器模拟验证；需要在真机点输入框确认 keyboard 动画中 composer 贴键盘、最新消息可见、无大空白。
3. **Android 12+ 换壁纸**：onResume 会重采样 accent；需真机换壁纸后切回前台目视确认全量变色。
4. **触感 / 系统通知权限**：Android 13+ 允许/拒绝/永久拒绝三种引导、10 种震动手感、通知渠道声音/静音。
5. **连续通话/蓝牙/音频焦点**等既有移动端验收项不变（见 `mobile/README.md`）。

## 12. 品牌升级：Sonder（2026-11-10）

- **改名**：桌面名称 / 任务标题 / 网页 title / PWA manifest / 关于页 / 通知兜底标题 / 通话通知文案 / 通知渠道描述全部改为 **Sonder**；包名 `com.speakeasy.app` 与原生桥 `SpeakEasyNative` 等内部标识保持不变（避免破坏已安装数据与接口）。
- **图标（最终方案，2026-11-10）**：桌面图标随系统明暗切换——两个 `activity-alias`（`LauncherLight`/`LauncherDark`，均指向 `MainActivity`）由 `LaunchIcon.apply()` 在进程启动/`onConfigurationChanged` 时以 `DONT_KILL_APP` 切换，浅色=Light 白底、深色=Dark 深底；真机验证 `enabledComponents` 浅色为 `LauncherLight`、深色为 `LauncherDark`。Android 13+ 保留 `<monochrome>` 主题图标层。
- **通知图标**：Dynamic 渐变版用于**通知栏右侧大图**（无发信人头像的互动/系统通知走 `large_icon=/icons/brand-512.png`；聊天消息仍按先前需求显示 AI 头像）；状态栏小图标必须是单色剪影，改用单色 S（`drawable-nodpi/jpush_notification_icon.png`，JPush 按同名资源读取，本地通知同用），避免彩色渐变在状态栏变成白方块。真机验证：无头像通知右侧显示 Dynamic 渐变图（`artifacts/brand/verify/`），消息通知仍显示 AI 头像。
- **通知栏左侧图标（Honor/Huawei）**：国产 ROM 的通知左侧显示的是**应用图标**（由系统缓存），不是 `setSmallIcon`；因此换图标后需要系统刷新缓存，否则会继续显示旧 logo。已通过 4.1.2 版本升级 + 重启设备刷新：真机验证左侧已变为 Dynamic 渐变图标，右侧为 Dynamic 兜底图（真实聊天消息为 AI 头像），截图 `artifacts/brand/verify/notification-dynamic-left-right.png`。
- **开屏**：静态导出 HTML 注入品牌开屏（内联 CSS，先于任何样式生效）：渐变夜空 + 柔光色块 + S 标 + `SONDER` + `Every life is a world.` + 山丘/人影剪影；React 挂载后淡出（最短展示约 1.15s），并设置原生启动主题背景色与 WebView 底色避免白屏。真机截图 `artifacts/brand/verify/splash-sonder.png`。
- **系统开屏去图标**：Android 12+ 的 SplashScreen 默认会把应用图标画在品牌底色上（浅色模式下是一个白底方图标，用户反馈难看）。启动主题已改为 `windowSplashScreenAnimatedIcon=@drawable/splash_empty`（透明）+ `windowSplashScreenIconBackgroundColor=transparent`，系统开屏只保留品牌底色，随后直接进入 Sonder 品牌开屏；真机逐帧复测确认不再出现白底方图标（首 4 帧均为纯 `#F3F1FA` 品牌色，第 5 帧起为渐变开屏）。
- **网页 / 服务器**：同源 Web 已重新构建并部署到 `47.104.24.236`（`/` title 已为 `Sonder · Every life is a world`，含开屏），后端健康检查 200。
- **素材说明**：用户提供的是设计稿导出图（logo 拼图 455×169、开屏 mockup 285×440），本轮按图重建/放大生成图标与开屏；若拿到原始 SVG / 1024 PNG / Figma 源文件，可替换为矢量清晰版（图标前景与小尺寸文字尤其受益）。

