import type { Metadata, Viewport } from "next";

import "../globals.css";
import "../../theme/tokens.css";
import { THEME_BOOTSTRAP_SCRIPT } from "../../theme/bootstrap";

export const metadata: Metadata = {
  title: "Sonder · Every life is a world",
  description: "Sonder：异步语音条的 AI 语伴。捏一个外国朋友，用语音条聊天，顺手把表达变地道。",
  applicationName: "Sonder",
  manifest: "/manifest.webmanifest",
  appleWebApp: {
    capable: true,
    statusBarStyle: "default",
    title: "Sonder",
  },
  icons: {
    icon: [
      { url: "/favicon.ico", sizes: "any" },
      { url: "/icons/brand-32.png", sizes: "32x32", type: "image/png" },
      { url: "/icons/brand-192.png", sizes: "192x192", type: "image/png" },
      { url: "/icons/brand-512.png", sizes: "512x512", type: "image/png" },
    ],
    apple: [{ url: "/icons/brand-180.png", sizes: "180x180", type: "image/png" }],
  },
};

export const viewport: Viewport = {
  themeColor: "#f3f1fa",
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  /* 键盘弹起时让布局视口跟着缩放：配合 platform.ts 的 visualViewport IME 变量，
     Android WebView（adjustNothing）不会再把 composer 挡在键盘下面。 */
  interactiveWidget: "resizes-content",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP_SCRIPT }} />
      </head>
      <body>
        {/* 不再做品牌开屏：手机首帧由原生启动色直接进入应用。
            旧开屏是 104px 位图 + 模糊光晕，在 2x/3x 屏上会被放大得很糊。 */}
        {children}
      </body>
    </html>
  );
}
