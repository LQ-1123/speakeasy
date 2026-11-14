/** Authenticated server API facade. Provider keys belong to the server only. */
import * as server from "./api";
import { currentIdentity } from "./v3/api";
import { AUTH_EXPIRED_EVENT, AUTH_LOGOUT_EVENT, readSession } from "./v3/session";
import { VoicePrefetchCache } from "./voicePrefetch";
export * from "./api";
/**
 * 读一条 AI 文本：/tts/sample 在有 friend_id 时强制使用该角色的固定音色，
 * 与语音消息、通话里的声音保持一致。
 *
 * 不能把 URL 直接交给 <audio>：V3 身份由 window.fetch 的 X-User-Id/X-User-Token
 * 包装注入，<audio src> 不会带这些头。这里用带身份的 fetch 取回音频再转 blob URL。
 */
export async function synthesizeVoice(text: string, friendId?: string): Promise<string | null> {
  const clean = (text || "").trim().slice(0, 300);
  if (!clean) return null;
  try {
    const resp = await fetch(server.ttsSampleUrl(clean, "", friendId), { cache: "no-store" });
    if (!resp.ok) return null;
    const blob = await resp.blob();
    if (!blob.type.startsWith("audio/") || blob.size === 0) return null;
    return URL.createObjectURL(blob);
  } catch {
    return null;
  }
}
export async function playVoiceUrl(url: string): Promise<boolean> {
  if (!url) return false;
  try {
    await new Promise<void>((resolve, reject) => {
      const audio = new Audio(url);
      audio.onended = () => resolve();
      audio.onerror = () => reject(new Error("Audio unavailable"));
      void audio.play().catch(reject);
    });
    return true;
  } catch { return false; }
}

/**
 * 预取一条语音到本地 blob，返回可直接交给 <audio> 的本地 URL。
 *
 * 为什么不在点击时才加载：`/api/v2/audio` 需要身份（或签名 token），而
 * `<audio src>` 不能带身份头；更关键的是大文件在移动网络下首次加载要几百毫秒
 * 到数秒，用户点一下就会觉得“卡住了”。消息出现时先取回，点击时本地直接响。
 */
function audioOwner(): string {
  const who = currentIdentity();
  const session = readSession();
  return who ? `${who.user_id}:${who.token}:${session?.csrf ?? ""}` : "";
}
const voiceCache = new VoicePrefetchCache(async (url, signal) => {
  const owner = audioOwner();
  const response = await fetch(url, { cache: "no-store", signal });
  if (!response.ok) throw new Error("Audio unavailable");
  const blob = await response.blob();
  if (owner !== audioOwner()) throw new Error("Session changed");
  return blob;
});
if (typeof window !== "undefined") {
  window.addEventListener(AUTH_EXPIRED_EVENT, () => voiceCache.clear());
  window.addEventListener(AUTH_LOGOUT_EVENT, () => voiceCache.clear());
}
export function forgetPrefetchedVoice(url: string): void { voiceCache.forget(url); }
export function prefetchedVoiceUrl(url: string): string | null { return voiceCache.get(url, audioOwner()); }
export function prefetchVoiceUrl(url: string): Promise<string | null> { return voiceCache.prefetch(url, audioOwner()); }
export async function exportLocalData(): Promise<string | null> { return null; }
export async function importLocalData(_json: string): Promise<boolean> { return false; }
export async function wipeLocalData(): Promise<boolean> { return false; }
export async function callOpening(_friendId: string): Promise<string> { return ""; }
export async function setFriendVoice(_friendId: string, _voice: string): Promise<boolean> { return false; }
export async function previewVoice(text: string, voice: string, friendId?: string): Promise<string | null> { return server.ttsSampleUrl(text, voice, friendId); }
