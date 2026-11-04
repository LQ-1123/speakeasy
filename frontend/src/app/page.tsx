"use client";

/**
 * 首页入口：直接进微信风口语界面（w4）。
 *
 * 背景：之前根路径挂的是旧的 v3 界面，导致打开 127.0.0.1:8000 看到的是
 * 没接后端的旧原型（聊天/语音都是本地 mock）。真正接入 LLM + TTS 的界面在
 * src/w4 下，现在根路径直接渲染它。
 *
 * 旧的 v2 界面仍保留在 /v2 做对比。
 */

import { App } from "@/w4/App";

export default function Page() {
  return <App />;
}
