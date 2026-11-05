/** 本地模式：直接调用 DeepSeek（OpenAI 兼容）接口。 */

import { loadKeys } from "./keys";

export type ChatMsg = { role: "system" | "user" | "assistant"; content: string };

export type LlmOptions = {
  temperature?: number;
  maxTokens?: number;
  json?: boolean;
};

export async function llmChat(messages: ChatMsg[], opts: LlmOptions = {}): Promise<string> {
  const k = loadKeys();
  if (!k.llmKey.trim()) throw new Error("未配置 DeepSeek Key");
  const body: Record<string, unknown> = {
    model: k.llmModel || "deepseek-flash",
    messages,
    temperature: opts.temperature ?? 0.85,
    max_tokens: opts.maxTokens ?? 1400,
    stream: false,
  };
  const resp = await fetch(`${k.llmBase.replace(/\/+$/, "")}/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${k.llmKey.trim()}`,
    },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const detail = await resp.text().catch(() => "");
    throw new Error(`DeepSeek ${resp.status}: ${detail.slice(0, 160)}`);
  }
  const data = (await resp.json()) as {
    choices?: Array<{ message?: { content?: string } }>;
  };
  const text = data.choices?.[0]?.message?.content ?? "";
  return text.trim();
}

/** 从任意文本里抠出一段 JSON（对象或数组） */
export function extractJson(raw: string): unknown {
  const text = (raw || "").trim();
  const attempts: string[] = [text];
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fenced) attempts.push(fenced[1].trim());
  const first = text.search(/[[{]/);
  const lastObj = text.lastIndexOf("}");
  const lastArr = text.lastIndexOf("]");
  const last = Math.max(lastObj, lastArr);
  if (first >= 0 && last > first) attempts.push(text.slice(first, last + 1));
  for (const a of attempts) {
    try {
      return JSON.parse(a);
    } catch {
      /* 继续试 */
    }
  }
  return null;
}

export async function llmJson<T>(messages: ChatMsg[], opts: LlmOptions = {}): Promise<T | null> {
  const text = await llmChat(messages, { ...opts, json: true, temperature: opts.temperature ?? 0.9 });
  return extractJson(text) as T | null;
}
