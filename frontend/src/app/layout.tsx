import type { Metadata, Viewport } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "SpeakEasy · 英语口语陪练",
  description: "实时语音对话的英语口语陪练台：任意口音的角色、逐句朗读、即时润色建议。",
};

export const viewport: Viewport = {
  themeColor: "#0b0d12",
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body>{children}</body>
    </html>
  );
}
