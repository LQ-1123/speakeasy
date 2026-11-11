import type { Metadata } from "next";

import "../globals.css";
import "../../theme/tokens.css";

/**
 * 管理台独立根布局：与客户端 (app) 布局完全隔离。
 * - 没有开屏动画（管理台不需要品牌开屏，且会拖慢首屏）
 * - 没有壁纸取色脚本（管理台固定一套配色，不受用户主题影响）
 * - robots: noindex（管理台不应被搜索引擎收录）
 */
export const metadata: Metadata = {
  title: "Sonder · 管理台",
  description: "Sonder 运营控制台",
  robots: { index: false, follow: false },
  icons: {
    icon: [{ url: "/favicon.ico", sizes: "any" }],
  },
};

export default function AdminRootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
