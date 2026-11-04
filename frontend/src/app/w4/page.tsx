"use client";

/** 原型入口 /w4：与首页一致，仅客户端挂载（避开 SSR/CSR 首帧不一致） */

import dynamic from "next/dynamic";

const App = dynamic(() => import("@/w4/App").then((m) => m.App), {
  ssr: false,
  loading: () => null,
});

export default function W4Page() {
  return <App />;
}
