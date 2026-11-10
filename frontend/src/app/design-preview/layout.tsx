import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Sonder Design Preview",
  description: "仅用于设计/验收的 Sonder 主题与响应式预览面板。",
  robots: { index: false, follow: false },
};

export default function DesignPreviewLayout({ children }: { children: React.ReactNode }) {
  return children;
}
