# SpeakEasy 第二轮 Web 重设计 · 电脑验收报告

> 验收日期：2026-11-10（Asia/Shanghai）
> 验收结论：**Web 重设计已验收通过并部署到公网，95/95 项自动化检查通过；Android 4.1.0 已构建、签名并验证，真机验收项保留。**
> 公网 Web 与 APK 使用同一版前端；部署没有替换 `data/`、`.env`、签名或身份数据。

## 1. 怎么查看

| 入口 | 地址 | 说明 |
|---|---|---|
| 公网 Web（真实后端） | <https://47-104-24-236.sslip.io/> | 已部署 Next 静态产物 + V3 后端，真实身份/内容 |
| 公网设计预览面板 | <https://47-104-24-236.sslip.io/design-preview/> | 同屏桌面/手机、色板导入、图片主题、状态矩阵、指标 |
| 本地 fixture QA | <http://127.0.0.1:4173/> | 离线夹具，不访问真实模型/推送/账户 |
| 路由示例 | `/#/chats`、`/#/chat/fixture-1`、`/#/discover`、`/#/me`、`/#/expressions`、`/#/settings/appearance` | 本地 fixture 使用 `?preview=1` |

公网 Web 使用真实 V3 后端；本地 QA 服务器仅用于可重复的视觉/交互验收。

## 2. 构建与自动化结果

| 检查 | 结果 |
|---|---|
| `npm run typecheck` | **通过**（TypeScript strict） |
| `npm test` | **29/29 通过**（10 个测试文件，含新增 7 项主题测试） |
| `npm run build` | **通过**；`/design-preview` 进入静态导出，9 个路由 |
| `next build` 首屏 JS | 主应用 105 kB；预览面板 114 kB |
| 浏览器 console/pageerror | **0 个**（预览 + 全部交互流程） |
| 交互/布局检查 | **95/95 通过**（[qa-report](qa/redesign-qa-report.json)） |
| 横向溢出记录 | 53 个视图/字体组合，**0 个失败** |
| 触感/主题/Worker 检查 | 通过（见下） |
| 性能记录 | [performance-report](qa/redesign-performance.json) |

### 构建命令

```bash
cd frontend
npm run typecheck
npm test
npm run build
node scripts/qa-server.mjs
```

## 3. 验收矩阵

### 3.1 消息 / 聊天 / 输入

- 搜索：输入 `Giulia` 后列表收窄到 1 行；清空恢复。
- 导航：桌面 rail / 手机底栏统一为「消息 / 通讯录 / 发现 / 我」四入口；通讯录不再作为消息列表里的独立卡片，消息页只保留会话。
- 打开会话：进入聊天页，自己的气泡/对方气泡同时存在。
- 发送：输入并发送后消息出现在列表中，输入区仍在，不会因重渲染丢失。
- AI 文本消息：单击气泡按角色音色朗读；长按（PC 右键）翻译并展开译文；再次单击停止朗读。真实后端走 `/api/v2/tts/sample?friend_id=…`，用带身份的 fetch 取回音频再转 blob URL；预览 fixture 用浏览器语音兜底。
- 输入区：聚焦时只显示整块 composer 的 tonal 焦点态，不再出现 textarea 的方形 outline。
- 通话：发起全屏通话；缩小为布局内的通话条；通话条不覆盖输入区；恢复通话；挂断。三项控制（静音、挂断、扬声器）齐全。
- 返回：聊天 → 消息列表；桌面/手机路由均正常。

### 3.2 发现 / 偶遇 / 人物主页

- 发现信息流：作者、AI 标签、正文、图片、标签、互动 pill、评论/翻译/朗读均可用。
- 发布弹层：可打开/关闭；文本域圆角表面，无方形焦点框。
- 人物主页：132px 头像、身份、单一“发消息”主操作、兴趣与动态。
- 偶遇与通知中心：从侧栏进入；事件卡片可读，接受/拒绝/已读功能保留。

### 3.3 我 / 表达本 / 设置

- 我：资料 hero、语言目标、兴趣、分组入口；无“统计面板”式大数字。
- 表达本：今天该练卡片、复习流程、筛选、收藏列表、掌握度、来源标签。
- 设置：提醒、外观与触感、隐私与数据、关于；主题/半透明/触感与本地持久化联动。
- 深色切换：`html[data-theme="dark"]` 生效；页面不重载。

### 3.4 布局宽度

自动化覆盖：`320 / 360 / 390 / 430 / 600 / 768 / 960 / 1024 / 1440`。

| 路由 | 320 | 360 | 390 | 430 | 600 | 768 | 960 | 1024 | 1440 |
|---|---|---|---|---|---|---|---|---|---|
| `#/chats` | 通过 | 通过 | 通过 | 通过 | 通过 | 通过 | 通过 | 通过 | 通过 |
| `#/chat/fixture-1` | 通过 | 通过 | 通过 | 通过 | 通过 | 通过 | 通过 | 通过 | 通过 |
| `#/discover` | 通过 | 通过 | 通过 | 通过 | 通过 | 通过 | 通过 | 通过 | 通过 |
| `#/me` | 通过 | 通过 | 通过 | 通过 | 通过 | 通过 | 通过 | 通过 | 通过 |

每一格都检查 `document.documentElement.scrollWidth <= innerWidth + 1`；无横向滚动、无越界主按钮/输入框。

### 3.5 字体比例

覆盖 `0.85 / 1.0 / 1.3 / 1.6 / 2.0`（当前荣耀手机实际设置为 0.85）：

| 路由 | 0.85 | 1.0 | 1.3 | 1.6 | 2.0 |
|---|---|---|---|---|---|
| `#/chats` | 通过 | 通过 | 通过 | 通过 | 通过 |
| `#/chat/fixture-1` | 通过 | 通过 | 通过 | 通过 | 通过 |
| `#/discover` | 通过 | 通过 | 通过 | 通过 | 通过 |

额外验证 320px + 2.0×：消息列表、聊天页均无横向溢出。

### 3.6 键盘 / 安全区 / 通话条

- 模拟键盘 240px + 缩小通话条同时出现：发送按钮仍在输入区内且可见；通话条与输入区无重叠。
- 安全区 top 44px / bottom 34px：标题在状态栏下方；底部导航不越出视口。
- 缩小通话条是 `.w4-app` 的独立布局轨道，不是覆盖层；页面内容随之下移。

### 3.7 虚拟化与主题切换

| 场景 | 结果 |
|---|---|
| 90 位好友列表 | 只渲染 13–14 行（虚拟化） |
| 1000 条消息 | 只渲染 12–13 个 `.w4-msg` 节点（虚拟化） |
| 切换深色/浅色 | 不发起 API 请求；列表节点仍是同一 DOM 节点（不重建） |
| 长时间主题/滚动操作 | 无 >50ms 主线程长任务 |

### 3.8 性能（headless Chromium，桌面浏览器）

| 指标 | 结果 |
|---|---|
| 平均帧间隔 | 16.67ms（60Hz 预算内） |
| p95 帧间隔 | 16.7–16.8ms |
| >20ms 掉帧 | 0 |
| 长任务（>50ms） | 0 |
| 主题切换 elapsed | 50–67ms（属性同步；视觉过渡 240ms） |
| 7 组主题对比度审计 | 约 70–85ms 完成，7/7 通过 |
| 图片主题 Worker（128×128） | 49–67ms 完成，6 个候选色 |

这是桌面 headless 浏览器数据，不等同于荣耀真机 WebView/Perfetto 结果；真机阶段会按实际刷新率重新测。

### 3.9 可访问性与对比度

- 新增 `theme.test.ts`：浅/深 Honor 色板全部核心配对断言通过；`#4C5E8B/#F9F8FF`、`#B9CBFF/#30436E` 等均达标。
- 预览面板 7 组主题色（黑白、低饱和、红/橙/绿/蓝/紫）浅深两套对比度检查 **7/7 通过**。
- 实渲染抽查 8 个路由；每个路由抽查 2–6 个代表性文本：无低对比度样本（脚本按实际合成背景计算，未发现低于 4.5:1 / 3:1 阈值的文本）。
- 关键表单/按钮有 `aria-label`、`aria-pressed`、`role="switch"`；移动主路径按钮 ≥48px。
- `prefers-reduced-motion: reduce` 下 `.w4-main` 动画时长 < 0.01ms；减少动态效果设置同样可用。
- 透明/毛玻璃关闭时切换为实色表面（`data-transparency="off"`），布局保持不变。

## 4. 交付物

### 代码

- 语义主题与适配器：`frontend/src/theme/`
- 设计 Token 与领域样式：`frontend/src/w4/styles/`
- 设计预览面板：`frontend/src/app/design-preview/`
- 组件与页面：`frontend/src/w4/components/`、`frontend/src/w4/v3/`
- 荣耀色板：`frontend/public/theme/honor-500-pro.json`
- QA 脚本：`frontend/scripts/qa-redesign-interactions.mjs`、`qa-design-preview.mjs`、`qa-redesign-performance.mjs`

### 截图（[全部截图](screenshots/redesign/)）

| 桌面 | 手机 |
|---|---|
| [消息](screenshots/redesign/desktop-01-messages.png) · [聊天](screenshots/redesign/desktop-02-chat.png) · [发现](screenshots/redesign/desktop-03-discover.png) | [消息](screenshots/redesign/phone-01-messages.png) · [聊天](screenshots/redesign/phone-02-chat.png) · [发现](screenshots/redesign/phone-03-discover.png) |
| [人物主页](screenshots/redesign/desktop-04-persona.png) · [我](screenshots/redesign/desktop-05-me.png) · [表达本](screenshots/redesign/desktop-06-expressions.png) | [人物主页](screenshots/redesign/phone-04-persona.png) · [我](screenshots/redesign/phone-05-me.png) · [表达本](screenshots/redesign/phone-06-expressions.png) |
| [设置](screenshots/redesign/desktop-07-settings.png) · [通知](screenshots/redesign/desktop-08-events.png) · [通话](screenshots/redesign/desktop-09-call.png) | [设置](screenshots/redesign/phone-07-settings.png) · [通知](screenshots/redesign/phone-08-events.png) · [通讯录](screenshots/redesign/phone-09-contacts.png) |
| [预览面板](screenshots/redesign/preview-01-panel.png) · [预览深色](screenshots/redesign/preview-02-dark.png) | [全屏通话](screenshots/redesign/phone-10-call.png) · [缩小通话条](screenshots/redesign/phone-11-mini-call.png) · [发布弹层](screenshots/redesign/phone-12-publish.png) · [全局通知](screenshots/redesign/phone-13-notification.png) · [表达复习](screenshots/redesign/phone-14-expression-review.png) |

深色：[消息](screenshots/redesign/dark-01-messages.png) · [聊天](screenshots/redesign/dark-02-chat.png) · [发现](screenshots/redesign/dark-03-discover.png)。

本轮验收反馈：[输入聚焦](screenshots/redesign/phone-15-composer-focus.png) · [单击朗读](screenshots/redesign/phone-16-read-aloud.png) · [右键翻译](screenshots/redesign/phone-17-context-translate.png) · [长按翻译](screenshots/redesign/phone-19-longpress-translate.png) · [320px + 2.0× 底栏](screenshots/redesign/phone-18-320-font2.png)。

### 交互录像

[SpeakEasy Web 重设计交互录像（WebM，约 21s）](videos/speakeasy-web-redesign.webm)。

> 录像由 headless Chromium 以 4fps 截图序列编码，用于完整流程审阅；实时帧表现以性能报告和阶段 D 的真机 Perfetto 为准。

### 机器可读报告

- [redesign-qa-report.json](qa/redesign-qa-report.json)
- [redesign-performance.json](qa/redesign-performance.json)
- [deployment-20261110.json](qa/deployment-20261110.json)

## 5. 部署与 Android 打包

### 5.1 公网部署（2026-11-10）

| 项 | 结果 |
|---|---|
| Web 入口 | <https://47-104-24-236.sslip.io/> |
| 健康检查 | `/api/v3/health` 200 |
| 静态产物 | `backend/app/static/app`，9.8 MB / 97 文件 |
| 前端静态备份 | `/root/speakeasy-web-backups/20261110-001651/app` |
| 后端代码备份 | `/root/speakeasy-backend-backups/20261110-002034/app` |
| 数据库部署备份 | `/root/speakeasy/data/_backup/deploy-20261110-002038/` |
| Nginx 配置备份 | `/root/nginx-speakeasy-https.20261110-002132.bak` |
| 数据库迁移 | 幂等完成：`personas` / `persona_posts` / `friendships` / `user_posts` / `social_events` |
| 关键接口 | `/`、`/api/v3/health`、`/api/v3/feed?limit=1`、`/api/v3/personas?limit=1` 均 200 |

部署时额外修复了公网 Nginx 的 WebSocket 升级头（`Upgrade` / `Connection`），否则 `/api/v3/client/events` 在 HTTPS 下会 404。公网浏览器回归：4 个底部入口正常；发现页显示 338 条人物动态；通讯录空态正常；`/api/v3/client/events` WebSocket 握手成功并收到 ready；人物动态朗读返回 206 音频流；无 404、无 console error。截图：[公网发现页](screenshots/redesign/deployed-public-discover.png)、[公网通讯录](screenshots/redesign/deployed-public-contacts.png)。

### 5.2 Android 4.1.0

| 项 | 结果 |
|---|---|
| 产物 | `deliverables/SpeakEasy-v4.1.0-release.apk`（15.78 MB） |
| package | `com.speakeasy.app` |
| versionCode / versionName | `410` / `4.1.0` |
| minSdk / targetSdk | 24 / 36 |
| API base | `https://47-104-24-236.sslip.io` |
| 签名 | 复用原 release keystore；SHA-256 `fac7e179…69b1`，与历史发布证书一致 |
| APK SHA-256 | `ed3ab7c40aae19381e8cc23ca5e6adbc51c1d52a565651116edcb55fcb7d0fad` |
| 校验 | `apksigner verify --print-certs` 通过；`aapt dump badging` 版本/包名正确 |
| 构建脚本 | `mobile/build-apk.ps1` 修复了 Windows PowerShell 5.1 将 Gradle stderr 当作终止错误的兼容问题 |

构建命令：

```powershell
.\mobile\build-apk.ps1 -ApiBase https://47-104-24-236.sslip.io `
  -Mode release -VersionCode 410 -VersionName "4.1.0" `
  -BuildRoot D:/speakeasy-android-build
```

## 6. 仍需真机验收

- 本次没有连接 Android 设备（`adb devices` 为空），APK 未安装到荣耀 500 Pro；签名与包信息已验证，但真机启动/升级安装未验证。
- 需要验证：已有 4.0.0 登录身份的覆盖升级、系统深浅色/动态色、键盘与安全区、状态栏/手势区、通话前台服务、录音/STT/TTS、通知权限与厂商推送。
- 生产数据中未创建测试好友或测试消息；公网回归只验证了注册、bootstrap、发现/通讯录和 WebSocket 连接。
- 厂商推送、OEM 渠道、蓝牙/来电音频焦点、Perfetto 帧统计仍按 [mobile/README.md](../mobile/README.md) 的真机清单执行。
