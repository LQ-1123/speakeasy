/** 本地模式：直接调用 Fish Audio 合成语音，并用 WebAudio 播放（荣耀兼容）。 */

import { loadKeys } from "./keys";

let _ctx: AudioContext | null = null;

export function audioContext(): AudioContext | null {
  try {
    _ctx = _ctx ?? new AudioContext();
    if (_ctx.state === "suspended") void _ctx.resume();
    return _ctx;
  } catch {
    return null;
  }
}

/** 播放任意音频 URL（先用 fetch 取字节，再用 WebAudio 解码） */
export async function playUrlWebAudio(url: string): Promise<boolean> {
  try {
    const ctx = audioContext();
    if (!ctx) return false;
    let buf: ArrayBuffer | null = null;
    if (url.startsWith("blob:") || url.startsWith("data:")) {
      const res = await fetch(url);
      buf = await res.arrayBuffer();
    } else {
      const res = await fetch(url);
      if (!res.ok) return false;
      buf = await res.arrayBuffer();
    }
    if (!buf) return false;
    const audio = await ctx.decodeAudioData(buf);
    const src = ctx.createBufferSource();
    src.buffer = audio;
    src.connect(ctx.destination);
    src.start();
    return true;
  } catch {
    return false;
  }
}

export type TtsResult = { url: string; durationMs: number } | null;

/** Fish 合成：返回 blob URL（同一条文本+音色会缓存） */
const cache = new Map<string, Promise<TtsResult>>();

export function fishTts(text: string, refId?: string): Promise<TtsResult> {
  const cleaned = (text || "").trim();
  if (!cleaned) return Promise.resolve(null);
  const k = loadKeys();
  const ref = (refId || k.fishRef || "").trim();
  const cacheKey = `${ref}|${k.fishModel}|${cleaned.slice(0, 120)}`;
  const hit = cache.get(cacheKey);
  if (hit) return hit;
  const task = (async (): Promise<TtsResult> => {
    if (!k.fishKey.trim()) return null;
    try {
      const resp = await fetch(`${k.fishBase.replace(/\/+$/, "")}/v1/tts`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${k.fishKey.trim()}`,
        },
        body: JSON.stringify({
          text: cleaned,
          format: "mp3",
          model: k.fishModel || "s2.1-pro-free",
          ...(ref ? { reference_id: ref } : {}),
        }),
      });
      if (!resp.ok) return null;
      const bytes = await resp.arrayBuffer();
      if (!bytes.byteLength) return null;
      const blob = new Blob([bytes], { type: "audio/mpeg" });
      const url = URL.createObjectURL(blob);
      const durationMs = Math.min(60000, Math.max(800, cleaned.length * 65));
      return { url, durationMs };
    } catch {
      return null;
    }
  })();
  cache.set(cacheKey, task);
  return task;
}
