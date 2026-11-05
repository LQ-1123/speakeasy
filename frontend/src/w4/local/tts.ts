/**
 * 本地模式的语音合成：SiliconFlow CosyVoice2（国内直连，WebAudio 播放）。
 *
 * 多音色：CosyVoice2 提供 4 男 4 女共 8 个声线；新建好友按性别随机分配，
 * 也可以在好友资料卡里逐个试听、随机、更换。
 */

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

/** 播放任意音频 URL：先取字节，再用 WebAudio 解码（荣耀/华为 WebView 兼容） */
export async function playUrlWebAudio(url: string): Promise<boolean> {
  try {
    const ctx = audioContext();
    if (!ctx) return false;
    const res = await fetch(url);
    if (!res.ok) return false;
    const buf = await res.arrayBuffer();
    if (!buf.byteLength) return false;
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

export type CosyVoice = { id: string; label: string; gender: "male" | "female" };

/** CosyVoice2 的 8 个内置声线 */
export const COSY_VOICES: CosyVoice[] = [
  { id: "anna", label: "女声 · Anna", gender: "female" },
  { id: "bella", label: "女声 · Bella", gender: "female" },
  { id: "claire", label: "女声 · Claire", gender: "female" },
  { id: "diana", label: "女声 · Diana", gender: "female" },
  { id: "alex", label: "男声 · Alex", gender: "male" },
  { id: "benjamin", label: "男声 · Benjamin", gender: "male" },
  { id: "charles", label: "男声 · Charles", gender: "male" },
  { id: "david", label: "男声 · David", gender: "male" },
];

export function voiceById(id: string | undefined): CosyVoice | undefined {
  return COSY_VOICES.find((v) => v.id === id);
}

/** 按好友 id 稳定分配一个音色（同一好友聊天/通话一致） */
export function voiceForFriend(friendId: string, gender?: "male" | "female"): string {
  return pickVoice(friendId, gender);
}

export function pickVoice(seed: string, gender?: "male" | "female"): string {
  const pool = COSY_VOICES.filter((v) => v.gender === (gender === "male" ? "male" : "female"));
  let h = 0;
  for (let i = 0; i < seed.length; i += 1) h = (h * 31 + seed.charCodeAt(i)) % 1000003;
  return pool[h % pool.length].id;
}

export type TtsResult = { url: string; durationMs: number } | null;

export type TtsOptions = { gender?: "male" | "female"; voiceId?: string };

const cache = new Map<string, Promise<TtsResult>>();

/** 合成语音：返回 blob URL；同一文本+音色会缓存 */
export function fishTts(text: string, opts: TtsOptions = {}): Promise<TtsResult> {
  const cleaned = (text || "").trim();
  if (!cleaned) return Promise.resolve(null);
  const k = loadKeys();
  const picked = (opts.voiceId && voiceById(opts.voiceId)?.id) || voiceForFriend("", opts.gender);
  const cacheKey = `${picked}|${cleaned.slice(0, 120)}`;
  const hit = cache.get(cacheKey);
  if (hit) return hit;

  const task = (async (): Promise<TtsResult> => {
    if (!k.sttKey.trim()) return null;
    try {
      const resp = await fetch(`${k.sttBase.replace(/\/+$/, "")}/audio/speech`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${k.sttKey.trim()}`,
        },
        body: JSON.stringify({
          model: "FunAudioLLM/CosyVoice2-0.5B",
          input: cleaned,
          voice: `FunAudioLLM/CosyVoice2-0.5B:${picked}`,
          response_format: "mp3",
        }),
      });
      if (!resp.ok) return null;
      const bytes = await resp.arrayBuffer();
      if (!bytes.byteLength) return null;
      return {
        url: URL.createObjectURL(new Blob([bytes], { type: "audio/mpeg" })),
        durationMs: Math.min(60000, Math.max(800, cleaned.length * 75)),
      };
    } catch {
      return null;
    }
  })();
  cache.set(cacheKey, task);
  return task;
}
