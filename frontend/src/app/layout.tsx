import type { Metadata, Viewport } from "next";

import "./globals.css";
import "../theme/tokens.css";
import { THEME_BOOTSTRAP_SCRIPT } from "../theme/bootstrap";

export const metadata: Metadata = {
  title: "SpeakEasy · 和世界聊聊天",
  description: "异步语音条的 AI 语伴：捏一个外国朋友，用语音条聊天，顺手把表达变地道。",
  applicationName: "SpeakEasy",
  manifest: "/manifest.webmanifest",
  appleWebApp: {
    capable: true,
    statusBarStyle: "default",
    title: "SpeakEasy",
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
  themeColor: "#f4f3fa",
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP_SCRIPT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
