# SpeakEasy Web 重设计说明（Material 3 Expressive）

> 状态：**已在公网部署，Web 与 Android 4.1.0 均已产出。**
>
> 线上入口：<https://47-104-24-236.sslip.io/> · 设计预览面板：<https://47-104-24-236.sslip.io/design-preview/>
> 验收报告：[REDESIGN_WEB_ACCEPTANCE.md](REDESIGN_WEB_ACCEPTANCE.md)

## 1. 这轮做了什么

- 用一套**语义主题**替换原来叠加的 `w4-base.css / w4.css / w4-part2.css / mobile.css / v3.css` 颜色与组件覆盖。
- 用荣耀 500 Pro 的 `home_wallpaper` 色板作为第一套 Web 验收主题，不再用黑白按钮模拟动态色。
- 重做消息列表、聊天、输入区、发现信息流、偶遇、人物主页、通话、缩小通话条、我、表达本、设置和全局通知。
- 聊天里 AI 文本消息支持单击朗读、长按/右键翻译；输入框聚焦只保留 composer 整体 tonal 焦点态，不出现方形 outline。
- 通讯录从消息列表的辅助卡片提升为与消息、发现、我并列的第四个导航入口，桌面 rail 和手机底栏保持一致。
- 新增仅验收用的 `/design-preview`：同一套正式组件/路由，通过 iframe + 类型化 postMessage 注入手机色板、图片主题、宽度、字体比例、安全区、键盘、减少动效和 mock 数据。
- 保留 Query 缓存、Zustand 选择器、虚拟列表、独立通话控制器、路由/返回栈和全部业务 API。

## 2. 语义主题

### 2.1 三层结构

```text
Android 系统资源 / Honor 色板 JSON / 本地图片 / Web 回退
                    ↓
        ThemeSnapshot（版本、来源、能力、revision、浅深色角色、对比度）
                    ↓
  CSS 变量 --md-*（组件只使用角色配对）
```

关键文件：

| 文件 | 职责 |
|---|---|
| `frontend/src/theme/types.ts` | `ThemeSnapshot`、`ThemeColors`、能力、设置的类型 |
| `frontend/src/theme/theme-store.tsx` | 验证、缓存、发布主题；根节点 CSS 变量；主题过渡；持久化 |
| `frontend/src/theme/ThemeProvider.tsx` | 安装主题管线，订阅系统变化 |
| `frontend/src/theme/system-theme-adapter.ts` | `getThemeSnapshot()` / 旧 `getTheme()` 兼容读取；前台/配置变化重采样 |
| `frontend/src/theme/preview-theme-adapter.ts` | 导入手机色板/图片主题的预览适配器 |
| `frontend/src/theme/image-theme.ts` | 128px 解码、Worker、摘要缓存、过期请求丢弃 |
| `frontend/src/theme/image-theme.worker.ts` | `QuantizerCelebi` → `Score` → `SchemeTonalSpot` |
| `frontend/src/theme/scheme.ts` | 官方 Material Color Utilities 的 M3 角色生成 |
| `frontend/src/theme/haptics.ts` | `HapticService` 语义事件、70ms 防抖、Web 记录/Android 桥接 |
| `frontend/src/theme/tokens.css` | 全部颜色、字号、间距、形状、材质、动效、布局 Token |
| `frontend/public/theme/honor-500-pro.json` | 荣耀真机采样色板（主角色真机值 + 同源补全） |

### 2.2 荣耀 500 Pro 验收色板

| 角色 | 值 |
|---|---|
| 浅色 primary | `#4C5E8B` |
| 浅色 onPrimary | `#F9F8FF` |
| 浅色 primaryContainer | `#B9CBFF` |
| 浅色 onPrimaryContainer | `#30436E` |
| 深色 primary | `#B8C6EE` |
| 深色 onPrimary | `#323F60` |

真机阶段还会补全完整语义角色；当前 Web 色板 JSON 明确标记 `completeness: partial`，其余角色按同一 source color 的 TonalSpot 生成，不把某台手机的壁纸色同步给其他设备。

### 2.3 对比度与首屏

- 正文目标 ≥ 4.5:1，大字/边界目标 ≥ 3:1；`theme.test.ts` 对浅深两套全部核心配对做断言。
- 玻璃表面按实际合成背景检查；透明关闭时使用实色表面，布局不改变。
- `layout.tsx` 在 React 首屏前执行 `THEME_BOOTSTRAP_SCRIPT`，读取上次有效主题，避免白屏闪烁。
- 主题变化只改根节点变量 `--md-*`；不重建聊天列表、播放器或通话会话；CSS 过渡约 240ms（减少动态效果时为 0）。
- 主题与安全区/键盘事件分离，输入法 inset 变化不会重新采样色板。

## 3. 视觉系统

### 3.1 Token

| 类别 | 规则 |
|---|---|
| 字体 | 系统字体；`--text-body` 16px，辅助 13–14px，标题 24–28px |
| 字重 | 400 / 500 / 600 |
| 间距 | 4 / 8 / 12 / 16 / 20 / 24 / 32 / 40 |
| 触控 | 移动宽度下按钮、行、开关 ≥ 48×48 |
| 圆角 | 小控件 12、气泡/容器 20–22、浮层/工作区 28 |
| 边界 | 优先用表面明度和留白；需要分隔时用 hairline |
| 阴影 | 两级轻阴影，只表示层级 |
| 毛玻璃 | 导航/顶栏/通知最多两处同时可见；列表项、气泡、图片不分别使用 backdrop-filter |

### 3.2 样式组织

`frontend/src/w4/styles/` 以领域拆分，不再新增“最终覆盖文件”：

- `base.css`：元素 reset、按钮/输入/头像/弹层/开关/空态等基础单元。
- `shell.css`：工作区、rail、列表列、主列、移动单栏切换、底部导航。
- `chat.css`：会话列表、聊天、气泡、语音条、输入区、录音、联系人/好友申请/资料。
- `discover.css`：发现信息流、发布弹层、偶遇卡、人物主页、通知中心。
- `profile.css`：我、表达本、设置。
- `notifications.css`：全局通知、推送引导。
- `call.css`：全屏通话与缩小通话条（布局轨道，不覆盖标题/输入框）。

旧的 5 套全局样式已删除；`App.tsx` 只 import `base.css` + `shell.css`。各领域组件只 import 自己的领域样式。

### 3.3 布局

| 宽度 | 布局 |
|---|---|
| `< 960px` | 单栏；消息/通讯录/发现/我是四个并列入口，聊天/详情盖在列表上并保留滑动方向；底部四入口 |
| `960–1279px` | 侧边 rail + 列表 + 主内容；聊天资料面板压缩为浮层 |
| `≥ 1280px` | 侧边 rail + 列表 + 主内容 + 聊天资料侧栏 |
| `≥ 1440px` | 工作区最大 1660px，不无限拉宽；正文 680px，聊天 880px |

键盘、安全区、缩小通话条使用 `--ime-height / --safe-top / --safe-bottom` 与 Flex/Grid 轨道参与布局，不靠多层绝对定位补丁。

## 4. 设计预览面板

`/design-preview` 渲染**正式 App 的同一套路由**（iframe 内是 `/?preview=1#/...`，由预览适配器注入 fixture），不是另写一套假页面。

- 桌面 768 / 1024 / 1440 与手机 320 / 360 / 390 / 430 同屏。
- 浅色 / 深色 / 跟随系统。
- 导入荣耀色板 JSON；从完整 ThemeSnapshot 或部分角色 payload 合并。
- 选择本地图片 → Worker 量化 → 6 个候选主色 → 生成主题。
- 普通 / 加载中 / 空数据 / 失败 / 长列表 / 通话中。
- 安全区顶部/底部、模拟键盘、0.85/1/1.3/1.6/2 字体比例、透明/毛玻璃、减少动态效果。
- 查看溢出元素、渲染节点、图片数、FPS、长任务、主题摘要；触感事件日志明确区分“Web 记录，无振动”和“原生已执行”。
- 7 组主题色（黑白、低饱和、红、橙、绿、蓝、紫）的对比度审计，当前 7/7 通过。

## 5. 本地运行

```bash
cd frontend
npm install
npm run typecheck
npm test
npm run build          # 静态导出到 out/
node scripts/qa-server.mjs
# 打开 http://127.0.0.1:4173/ 与 http://127.0.0.1:4173/design-preview/
```

连接真实后端时使用 `npm run dev`（Next dev 的 `/api` 会反代到 `http://127.0.0.1:8000`），或用 `npm run build:backend` 把同源导出复制到 FastAPI 静态目录。`?preview=1` 只在验收/设计调试地址使用，正常入口不带此参数。

QA 自动化：

```bash
cd frontend
PLAYWRIGHT_CORE_PATH=/path/to/node_modules/playwright-core \
  node scripts/qa-redesign-interactions.mjs
PLAYWRIGHT_CORE_PATH=/path/to/node_modules/playwright-core \
  node scripts/qa-design-preview.mjs
```

报告输出在 `artifacts/`（workspace 根目录），本次验收副本在 [`docs/qa/`](qa/)。

## 6. 已知边界

- 本次 Web 验收使用本地点阵 fixture；真实后端/真实 provider/推送/真机麦克风不在本阶段回放范围。
- 图片生成主题是**设计测试工具**，不等同于手机 Monet 的最终输出；手机精确主题以 ADB 导出的语义色板为准，不会被上传。
- 浏览器触感适配器不振动；真机触感必须由 Android `performHaptic` 完成，预览只记录语义事件。
- 桌面浏览器通话的“扬声器”按钮保留可访问 UI，但浏览器内没有 Android 音频路由，原生路由在阶段 D 接入。
- Android 系统色板读取、状态栏/手势区、系统返回、触感、真机 Perfetto 帧统计属于阶段 D，未在本次修改。
