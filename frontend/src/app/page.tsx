"use client";

/**
 * SpeakEasy 2.0 主界面：移动端三 Tab 单页（消息 / 动态·偶遇 / 表达本·画像）。
 *
 * 深链：`/?tab=chats|discover|notes`、`/?tab=chats&friend=<id>`。
 * 1.0 的旧界面保留在 `/classic`，不再从新界面链接过去，避免误触。
 */

import { SpeakEasyApp } from "@/components/v2/SpeakEasyApp";

export default function Page() {
  return <SpeakEasyApp />;
}
