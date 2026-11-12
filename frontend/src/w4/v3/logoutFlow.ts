/** 退出/失效时清理本地缓存、音频与通话；AuthGate 卸载业务组件及事件连接。 */

import { callSession } from "./callSession";
import { queryClient } from "../queryClient";
import { usePreferences } from "./preferences";
import { stopAudio } from "../audioBus";

import { clearLocalClientData, clearSessionState } from "./logoutCleanup";
import { logoutAllSessions, logoutSession } from "./session";

export function cleanupAccountClient(): string[] {
  callSession.endLocally();
  try { stopAudio(); } catch { /* no audio */ }
  void queryClient.cancelQueries();
  queryClient.clear();
  usePreferences.setState({ value: { messages: true, interactions: true, sound: true, theme: "system" }, error: "" });
  const clearedKeys = clearLocalClientData();
  clearSessionState();
  return clearedKeys;
}

export async function performLogout(all = false): Promise<{ clearedKeys: string[] }> {
  try {
    if (all) await logoutAllSessions();
    else await logoutSession();
  } finally {
    return { clearedKeys: cleanupAccountClient() };
  }
}
