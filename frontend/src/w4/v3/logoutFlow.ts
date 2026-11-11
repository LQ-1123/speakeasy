/** 退出登录流程：先撤销服务端会话，再清理本地缓存/音频/页面状态（调用方负责 reload）。 */

import { stopAudio } from "../audioBus";

import { clearLocalClientData, clearSessionState } from "./logoutCleanup";
import { logoutAllSessions, logoutSession } from "./session";

export async function performLogout(all = false): Promise<{ clearedKeys: string[] }> {
  try {
    if (all) await logoutAllSessions();
    else await logoutSession();
  } finally {
    try {
      stopAudio();
    } catch {
      /* 无音频/浏览器限制 */
    }
    const clearedKeys = clearLocalClientData();
    clearSessionState();
    return { clearedKeys };
  }
}
