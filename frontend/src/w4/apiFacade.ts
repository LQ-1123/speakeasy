/** Authenticated server API facade. Provider keys belong to the server only. */
import * as server from "./api";
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
export async function exportLocalData(): Promise<string | null> { return null; }
export async function importLocalData(_json: string): Promise<boolean> { return false; }
export async function wipeLocalData(): Promise<boolean> { return false; }
export async function callOpening(_friendId: string): Promise<string> { return ""; }
export async function setFriendVoice(_friendId: string, _voice: string): Promise<boolean> { return false; }
export async function previewVoice(text: string, voice: string, friendId?: string): Promise<string | null> { return server.ttsSampleUrl(text, voice, friendId); }
