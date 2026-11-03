"use client";

/**
 * 前端 API 客户端。
 *
 * 静态导出（`output: 'export'`）下产物是纯静态文件，所有请求必须在**运行时**
 * 走相对路径，构建期绝不读 env —— 否则产物会绑死某个 host。
 * 因此这里一律使用 `/api/...`，同源由 FastAPI 提供；`next dev` 下由 rewrites 反代。
 */

import { asNumber, asString, isRecord, num, str } from "@/lib/guards";
import type { ConversationMode, PersonaPublic } from "@/lib/types";

export interface PersonasPayload {
  personas: PersonaPublic[];
  modes: ConversationMode[];
  defaultPersonaId: string | null;
  ok: boolean;
  /** 失败原因，用于 UI 显示"使用内置角色"的说明 */
  error?: string;
}

const TIMEOUT_MS = 4000;

async function fetchJson(path: string, timeoutMs = TIMEOUT_MS): Promise<unknown> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(path, {
      signal: controller.signal,
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return (await res.json()) as unknown;
  } finally {
    clearTimeout(timer);
  }
}

function parsePersonas(raw: unknown): PersonaPublic[] {
  const list = isRecord(raw) && Array.isArray(raw.personas) ? raw.personas : Array.isArray(raw) ? raw : [];
  const out: PersonaPublic[] = [];
  for (const item of list) {
    if (!isRecord(item)) continue;
    const id = asString(item.id);
    const name = asString(item.name);
    if (!id || !name) continue;
    out.push({
      id,
      name,
      title: asString(item.title),
      scene: asString(item.scene),
      accent: asString(item.accent),
      accent_short: asString(item.accent_short),
      flag: asString(item.flag),
      city: asString(item.city),
      difficulty: asString(item.difficulty),
      tags: Array.isArray(item.tags) ? item.tags.filter((t): t is string => typeof t === "string") : undefined,
      traits: Array.isArray(item.traits) ? item.traits.filter((t): t is string => typeof t === "string") : undefined,
      voice_notes: asString(item.voice_notes),
      opening_line: asString(item.opening_line),
    });
  }
  return out;
}

function parseModes(raw: unknown): ConversationMode[] {
  if (!isRecord(raw) || !Array.isArray(raw.modes)) return [];
  const out: ConversationMode[] = [];
  for (const item of raw.modes) {
    if (!isRecord(item)) continue;
    const id = asString(item.id);
    const name = asString(item.name);
    if (!id || !name) continue;
    out.push({ id, name, description: asString(item.description) });
  }
  return out;
}

/**
 * 拉取角色与模式。失败不抛错 —— 调用方用内置 fallback 兜底，
 * 保证"后端未启动时页面不空"。
 */
export async function fetchPersonas(): Promise<PersonasPayload> {
  try {
    const raw = await fetchJson("/api/personas");
    const personas = parsePersonas(raw);
    const modes = parseModes(raw);
    const defaultPersonaId = isRecord(raw) ? asString(raw.default_persona_id) ?? null : null;
    if (personas.length === 0) {
      return { personas: [], modes: [], defaultPersonaId: null, ok: false, error: "接口返回空列表" };
    }
    return { personas, modes, defaultPersonaId, ok: true };
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err);
    return { personas: [], modes: [], defaultPersonaId: null, ok: false, error: reason };
  }
}

/** 无麦克风调试：纯文本链路。静态导出下同样走相对路径。 */
export async function postEcho(text: string, personaId: string): Promise<unknown | null> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS * 2);
    try {
      const res = await fetch("/api/echo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, persona_id: personaId }),
        signal: controller.signal,
      });
      if (!res.ok) return null;
      return (await res.json()) as unknown;
    } finally {
      clearTimeout(timer);
    }
  } catch {
    return null;
  }
}

/** 健康检查 —— 用于顶栏"引擎信息"在未连接 WS 时也能显示一点东西。 */
export async function fetchHealth(): Promise<unknown | null> {
  try {
    return await fetchJson("/api/health", 2500);
  } catch {
    return null;
  }
}

/* ------------------------------------------------------------------ LLM 配置 */

export interface LlmConfigView {
  base_url: string;
  model: string;
  temperature: number | null;
  max_tokens: number | null;
  suggestion_model: string;
  api_key_set: boolean;
  api_key_masked: string;
  configured: boolean;
  source: string;
}

export interface LlmPreset {
  id: string;
  name: string;
  base_url: string;
  model: string;
  key_hint: string;
  note: string;
}

export interface LlmConfigPayload {
  ok: boolean;
  config: LlmConfigView | null;
  presets: LlmPreset[];
  health: { configured?: boolean; model?: string; base_url?: string } | null;
  error?: string;
}

export interface LlmVerifyResult {
  ok: boolean;
  message: string;
  latency_ms: number;
  model_reply: string;
}

/** 读取当前 LLM 配置（key 已被后端脱敏）。 */
export async function fetchLlmConfig(): Promise<LlmConfigPayload> {
  try {
    const raw = await fetchJson("/api/llm/config", 6000);
    if (!isRecord(raw)) {
      return { ok: false, config: null, presets: [], health: null, error: "响应格式异常" };
    }
    const cfgRaw = isRecord(raw.config) ? raw.config : {};
    const config: LlmConfigView = {
      base_url: str(cfgRaw.base_url),
      model: str(cfgRaw.model),
      temperature: typeof cfgRaw.temperature === "number" ? cfgRaw.temperature : null,
      max_tokens: typeof cfgRaw.max_tokens === "number" ? cfgRaw.max_tokens : null,
      suggestion_model: str(cfgRaw.suggestion_model),
      api_key_set: cfgRaw.api_key_set === true,
      api_key_masked: str(cfgRaw.api_key_masked),
      configured: cfgRaw.configured === true,
      source: str(cfgRaw.source),
    };
    const presets: LlmPreset[] = Array.isArray(raw.presets)
      ? raw.presets.filter(isRecord).map((p) => ({
          id: str(p.id),
          name: str(p.name),
          base_url: str(p.base_url),
          model: str(p.model),
          key_hint: str(p.key_hint),
          note: str(p.note),
        }))
      : [];
    const health = isRecord(raw.health)
      ? {
          configured: raw.health.configured === true,
          model: str(raw.health.model),
          base_url: str(raw.health.base_url),
        }
      : null;
    return { ok: true, config, presets, health };
  } catch (err) {
    return {
      ok: false,
      config: null,
      presets: [],
      health: null,
      error: err instanceof Error ? err.message : String(err),
    };
  }
}

export interface LlmSaveInput {
  base_url?: string;
  api_key?: string;
  model?: string;
  temperature?: number;
  verify?: boolean;
}

/** 保存并立即生效。返回是否成功 + 校验结果。 */
export async function saveLlmConfig(
  input: LlmSaveInput,
): Promise<{ ok: boolean; message: string; verified: LlmVerifyResult | null }> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 45000);
    try {
      const res = await fetch("/api/llm/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
        signal: controller.signal,
      });
      const raw = (await res.json()) as unknown;
      if (!isRecord(raw)) return { ok: false, message: "响应格式异常", verified: null };
      const v = isRecord(raw.verified)
        ? {
            ok: raw.verified.ok === true,
            message: str(raw.verified.message),
            latency_ms: num(raw.verified.latency_ms) ?? 0,
            model_reply: str(raw.verified.model_reply),
          }
        : null;
      return {
        ok: raw.ok === true,
        message: str(raw.message) || (raw.ok === true ? "已保存并生效" : "保存失败"),
        verified: v,
      };
    } finally {
      clearTimeout(timer);
    }
  } catch (err) {
    return {
      ok: false,
      message: err instanceof Error ? err.message : String(err),
      verified: null,
    };
  }
}

/** 只测连通性，不保存。 */
export async function testLlmConfig(input: {
  base_url?: string;
  api_key?: string;
  model?: string;
}): Promise<LlmVerifyResult> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 45000);
    try {
      const res = await fetch("/api/llm/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
        signal: controller.signal,
      });
      const raw = (await res.json()) as unknown;
      if (!isRecord(raw)) return { ok: false, message: "响应格式异常", latency_ms: 0, model_reply: "" };
      return {
        ok: raw.ok === true,
        message: str(raw.message),
        latency_ms: num(raw.latency_ms) ?? 0,
        model_reply: str(raw.model_reply),
      };
    } finally {
      clearTimeout(timer);
    }
  } catch (err) {
    return {
      ok: false,
      message: err instanceof Error ? err.message : String(err),
      latency_ms: 0,
      model_reply: "",
    };
  }
}

/** 还原为 .env 默认配置。 */
export async function resetLlmConfig(): Promise<{ ok: boolean; message: string }> {
  try {
    const res = await fetch("/api/llm/reset", { method: "POST" });
    const raw = (await res.json()) as unknown;
    return {
      ok: res.ok,
      message: isRecord(raw) ? str(raw.message) || "已还原" : "已还原",
    };
  } catch (err) {
    return { ok: false, message: err instanceof Error ? err.message : String(err) };
  }
}

export { asNumber };
