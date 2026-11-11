"use client";

/**
 * 首页入口：直接进微信风口语界面（w4）。
 *
 * 注意：这里用 dynamic(ssr:false) 仅客户端挂载。
 * 原因：w4 的首屏依赖 window（localStorage 分栏宽度、innerWidth、matchMedia），
 * 静态导出的 SSR HTML 拿不到这些值 —— 之前就会出现 React #418 hydration 不一致，
 * 表现为首屏闪烁、分栏宽度偶尔卡在默认值、甚至局部不渲染。
 */

import dynamic from "next/dynamic";

const App = dynamic(() => import("@/w4/App").then((m) => m.App), {
  ssr: false,
  loading: () => null,
});

export default function Page() {
  return <App />;
}
