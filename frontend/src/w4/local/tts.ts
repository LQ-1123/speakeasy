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

/** 本地 TTS：SiliconFlow（国内直连）优先，Fish 兜底；同文本+音色缓存 */
const cache = new Map<string, Promise<TtsResult>>();

export type TtsOptions = { gender?: "male" | "female"; refId?: string };

export function fishTts(text: string, opts: TtsOptions = {}): Promise<TtsResult> {
  const cleaned = (text || "").trim();
  if (!cleaned) return Promise.resolve(null);
  const k = loadKeys();
  const cacheKey = `${opts.gender || "f"}|${cleaned.slice(0, 120)}`;
  const hit = cache.get(cacheKey);
  if (hit) return hit;

  const task = (async (): Promise<TtsResult> => {
    // 1) SiliconFlow CosyVoice（国内可直连，无需 VPN）
    if (k.sttKey.trim()) {
      try {
        const voice = `FunAudioLLM/CosyVoice2-0.5B:${opts.gender === "male" ? "alex" : "anna"}`;
        const resp = await fetch(`${k.sttBase.replace(/\/+$/, "")}/audio/speech`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${k.sttKey.trim()}`,
          },
          body: JSON.stringify({
            model: "FunAudioLLM/CosyVoice2-0.5B",
            input: cleaned,
            voice,
            response_format: "mp3",
          }),
        });
        if (resp.ok) {
          const bytes = await resp.arrayBuffer();
          if (bytes.byteLength) {
            const blob = new Blob([bytes], { type: "audio/mpeg" });
            return {
              url: URL.createObjectURL(blob),
              durationMs: Math.min(60000, Math.max(800, cleaned.length * 65)),
            };
          }
        }
      } catch {
        /* 继续尝试 Fish */
      }
    }
    // 2) Fish 兜底（需要能访问外网）
    const ref = (opts.refId || k.fishRef || "").trim();
    if (k.fishKey.trim()) {
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
        if (resp.ok) {
          const bytes = await resp.arrayBuffer();
          if (bytes.byteLength) {
            return {
              url: URL.createObjectURL(new Blob([bytes], { type: "audio/mpeg" })),
              durationMs: Math.min(60000, Math.max(800, cleaned.length * 65)),
            };
          }
        }
      } catch {
        /* 失败返回 null */
      }
    }
    return null;
  })();

  cache.set(cacheKey, task);
  return task;
}
