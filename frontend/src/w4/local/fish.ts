/**
 * Fish Audio 音色库（在线，可 VPN 访问）。
 *
 * - 音色列表：GET {base}/model?page_size=100&language=en
 * - 合成：POST {base}/v1/tts  { text, format: "mp3", reference_id, model }
 *
 * 注意：浏览器端受 Fish 的跨域策略限制（预检不返回 CORS 头），
 * Web 版会自动回落到内置 CosyVoice 音色；App（CapacitorHttp 原生请求）
 * 不受跨域限制，可以直接使用 Fish 音色库。
 */

import { loadKeys } from "./keys";

export type FishVoice = { id: string; label: string; gender: "male" | "female" };

const CACHE_KEY = "speakeasy.fish.voices.v1";
const CACHE_TTL_MS = 24 * 3600 * 1000;

type CacheShape = { at: number; items: FishVoice[] };

export function cachedFishVoices(): FishVoice[] {
  try {
    const raw = window.localStorage.getItem(CACHE_KEY);
    if (!raw) return [];
    const data = JSON.parse(raw) as CacheShape;
    if (!data?.items?.length) return [];
    if (Date.now() - (data.at || 0) > CACHE_TTL_MS) return [];
    return data.items;
  } catch {
    return [];
  }
}

function saveCache(items: FishVoice[]): void {
  try {
    window.localStorage.setItem(CACHE_KEY, JSON.stringify({ at: Date.now(), items } satisfies CacheShape));
  } catch {
    /* ignore */
  }
}

function genderOf(tags: unknown): "male" | "female" | null {
  if (!Array.isArray(tags)) return null;
  const lower = tags.map((t) => String(t).toLowerCase());
  if (lower.includes("female")) return "female";
  if (lower.includes("male")) return "male";
  return null;
}

/** 拉取 Fish 音色库（有缓存则先用缓存）；失败返回 [] */
export async function loadFishVoices(force = false): Promise<FishVoice[]> {
  const k = loadKeys();
  if (!k.fishKey.trim()) return [];
  if (!force) {
    const hit = cachedFishVoices();
    if (hit.length) return hit;
  }
  try {
    const base = k.fishBase.replace(/\/+$/, "");
    const resp = await fetch(`${base}/model?page_size=100&language=en`, {
      headers: { Authorization: `Bearer ${k.fishKey.trim()}` },
    });
    if (!resp.ok) return [];
    const data = (await resp.json()) as { items?: Array<Record<string, unknown>> };
    const items: FishVoice[] = [];
    for (const it of data.items ?? []) {
      const id = String(it._id ?? "").trim();
      const title = String(it.title ?? "").trim();
      const gender = genderOf(it.tags);
      if (!id || !title || !gender) continue;
      items.push({ id, label: title.slice(0, 40), gender });
    }
    if (items.length) saveCache(items);
    return items;
  } catch {
    // 浏览器跨域或网络失败：返回空，调用方回落到内置音色
    return [];
  }
}

/** 用 Fish 的 reference_id 合成，返回 blob URL */
export async function fishSynth(text: string, referenceId: string): Promise<{ url: string; durationMs: number } | null> {
  const cleaned = (text || "").trim();
  if (!cleaned) return null;
  const k = loadKeys();
  if (!k.fishKey.trim()) return null;
  try {
    const base = k.fishBase.replace(/\/+$/, "");
    const resp = await fetch(`${base}/v1/tts`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${k.fishKey.trim()}`,
      },
      body: JSON.stringify({
        text: cleaned,
        format: "mp3",
        reference_id: referenceId,
        model: k.fishModel || "s1",
      }),
    });
    if (!resp.ok) return null;
    const bytes = await resp.arrayBuffer();
    if (!bytes.byteLength) return null;
    return {
      url: URL.createObjectURL(new Blob([bytes], { type: "audio/mpeg" })),
      durationMs: Math.min(60000, Math.max(800, cleaned.length * 80)),
    };
  } catch {
    return null;
  }
}

export function fishLabel(voices: FishVoice[], voiceId: string): string | undefined {
  return voices.find((v) => `fish:${v.id}` === voiceId)?.label;
}
