"use client";

/**
 * 主界面：微信风格聊天（接真实引擎）。
 *
 * 旧的分栏界面保留在 /classic，随时可对比或回退。
 */

import Link from "next/link";

import { WeChatApp } from "@/components/WeChatApp";

export default function Page() {
  return (
    <div className="relative">
      <WeChatApp />
      <Link
        href="/classic/"
        className="fixed bottom-3 left-3 z-[60] rounded-[4px] px-2 py-1 text-[10.5px] text-[#9a9a9a] transition-colors hover:bg-black/[0.05] hover:text-[#5a5a5a]"
      >
        ← 经典界面
      </Link>
    </div>
  );
}
