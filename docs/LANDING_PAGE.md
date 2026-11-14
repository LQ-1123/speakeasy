# Sonder 宣传页

当前 GitHub 版本为 [v4.4.0](https://github.com/LQ-1123/speakeasy/releases/tag/v4.4.0)，该渠道与服务器宣传页、下载目录及 `/api/app/latest` 独立。本文的 4.3.1 线上观察属于 **2026-11-13 历史快照**；本次 GitHub 发布不表示宣传页已经更新。当前安装请以 GitHub Release 的 APK 与校验信息为准，渠道说明见 [APP_UPDATE.md](APP_UPDATE.md)。

宣传页源码：`frontend/public/hello/`。纯 HTML、CSS、JavaScript，无额外依赖；所有图片、截图、二维码都随页面提供，不请求第三方字体或图片服务。

## 本地预览

在项目根目录执行：

```powershell
python -m http.server 4173 --bind 127.0.0.1 --directory frontend/public
```

打开 `http://127.0.0.1:4173/hello/`。也可以直接打开 `frontend/public/hello/index.html`；本地文件模式下，复制链接若不可用，会弹出可手动复制的文本框。

## 2026-11-13 上线记录（历史快照）

页面使用独立 `/hello/` 路径；应用的 `/` 入口保留。当时记录的宣传页地址：

`https://47-104-24-236.sslip.io/hello/`

2026-11-13 的 4.3.1 发布记录中：页面版本文字、下载按钮、复制链接、二维码都指向
`speakeasy-4.3.1-431.apk`；当时 `/api/app/latest` 返回 431 / 4.3.1。
这里保留当时状态，不作为当前服务器版本的查询结果。

## 独立维护宣传页

如需另行发布宣传页，有两种方式；执行前按[安全部署指南](../deploy/README.md)核对目标目录与备份，不覆盖 `.env`、`data/`、service 或 TLS 配置：

1. 正常前端发布：在 `frontend` 执行 `npm run build:backend`，再按现有部署流程上传 `backend/app/static/app/`。Next 静态导出会自动包含 `public/hello/`。
2. 只更新宣传页：将已核对版本的 `hello/` 内容部署到服务器的 `backend/app/static/app/hello/`；同级 `sw.js` 仅在此次确有兼容变更时一起发布，不能用历史交付包覆盖当前应用缓存策略。沿用现有静态路径通常无需改 nginx 或重启 FastAPI，仍需核验实际代理与缓存配置。

例如，服务器仓库路径为 `/root/speakeasy` 时：

```text
/root/speakeasy/backend/app/static/app/
  index.html         # 现有应用首页
  sw.js              # 更新后的应用 service worker
  hello/
    index.html
    styles.css
    main.js
    media/
```

历史 4.3.1 交付为应用 service worker 增加了 `/hello` 的绕过规则，防止宣传页被缓存成应用离线首页。变更缓存策略时，已打开的旧版网页可能需要关闭后重新打开，让浏览器激活对应 service worker；这不会替换 Android APK 内置的网页资源。

也可以把 `hello/` 目录内容单独部署到任何静态站点的根目录。页面资源都用相对路径；若更换宣传页域名，更新 `index.html` 的 `og:url` 与 `og:image` 分享元信息。

## 内容维护

- Android 下载链接：`index.html` 中两处下载按钮、复制弹窗，以及 `main.js` 的 `downloadUrl`。下载版本更新时，应同步修改版本文字、FAQ 和下载二维码。
- 下载二维码（`media/download-qr.svg`、`media/download-qr.png`）编码的是该宣传页版本指定的 APK 完整地址；
  历史 4.3.1 快照使用 `https://47-104-24-236.sslip.io/api/app/download/speakeasy-4.3.1-431.apk`，不是 v4.4.0 GitHub 下载地址。
  若另行更新下载目标，按钮、文字、复制内容和二维码必须一致，并实际扫码核验；旧链接可能已失效，也不能保证旧包的更新器会推荐新 GitHub 版本。
  历史生成参数为 1 单位/模块、4 模块静默边、37 模块符号（viewBox 45×45），PNG 366×366；新地址长度变化时按实际二维码模块数重新生成，不能固定复用旧矩阵。
- 网页版：`https://47-104-24-236.sslip.io/`。
- GitHub：`https://github.com/LQ-1123/speakeasy`。
- 品牌与颜色沿用 Sonder 现有设计：`#4c5e8b` 雾蓝、`#faf8ff` 淡紫白、`#b9cbff` 浅蓝。
- 应用截图来自 `docs/screenshots/redesign/phone-03-discover.png`、`phone-02-chat.png`、`phone-10-call.png`；转为 WebP，保留原始 390×844 分辨率。
- 头像使用项目现有 DiceBear Avataaars 生成；示例对话已标明「对话示例」，不代表用户评价。
- `media/share.jpg` 是 1200×630 的分享预览图。二维码离线生成，内容为用户提供的 APK 原始地址。

## 交互与可访问性

三组功能标签支持上下方向键、Home、End；示例翻译可展开；FAQ 使用原生 details；复制链接支持权限失败后的手动复制。页面支持减少动态效果偏好，图片有替代文本，主要链接不依赖 JavaScript。页面没有埋点、表单或需要登录的接口请求。

历史记录称页面随 2026-11-13 的 4.3.1 流程上传并生效；本次 v4.4.0 GitHub 发版不包含服务器宣传页或更新元数据的部署。本文未重新核验线上宣传页，也不构成服务器更新元数据已变更的证明。
