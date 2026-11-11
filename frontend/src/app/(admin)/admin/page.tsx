"use client";

/**
 * `/admin` 页面：仅客户端挂载管理台。
 *
 * 为什么 ssr:false：管理台首屏完全由会话状态与 hash 决定（window.location.hash、
 * Cookie 会话），静态导出时服务端拿不到这些，SSR 只会产出注定被丢弃的空壳，
 * 还会带来 hydration 不一致的闪烁。
 */

import dynamic from "next/dynamic";

const AdminApp = dynamic(() => import("@/admin/AdminApp"), {
  ssr: false,
  loading: () => (
    <div style={{ padding: 32, fontFamily: "system-ui", fontSize: 14 }}>正在打开管理台…</div>
  ),
});

export default function AdminPage() {
  return <AdminApp />;
}
