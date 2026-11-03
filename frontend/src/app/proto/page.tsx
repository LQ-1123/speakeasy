"use client";

/**
 * 微信风格原型页（独立路由 /proto）。
 *
 * 与主页面完全隔离：现有 app/page.tsx 不动，这个页面纯 UI 示例数据。
 * 你确认设计后，我再把真实对话引擎接进来。
 */

import Link from "next/link";

import { WeChatPrototype } from "@/components/WeChatPrototype";

export default function ProtoPage() {
  return (
    <div className="relative">
      <WeChatPrototype />

      {/* 返回主应用的入口（原型页专用）：放在左下角空白处，不遮挡输入区 */}
      <Link
        href="/"
        className="fixed bottom-3 left-3 z-[60] rounded-[4px] px-2 py-1 text-[10.5px] text-[#9a9a9a] transition-colors hover:bg-black/[0.05] hover:text-[#5a5a5a]"
      >
        ← 返回原版
      </Link>
    </div>
  );
}
