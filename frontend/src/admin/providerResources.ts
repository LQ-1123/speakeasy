import { AdminApiError, apiPatch, apiPost } from "./api";

export const PROVIDER_RESOURCES_PATH = "/api/admin/provider-resources";
export const PROVIDERS = ["fish", "deepgram", "llm", "siliconflow"] as const;
export type ResourceProvider = (typeof PROVIDERS)[number];
export const PROVIDER_LABELS: Record<ResourceProvider, string> = {
  fish: "Fish 语音合成", deepgram: "Deepgram 实时识别", llm: "LLM 文本模型", siliconflow: "SiliconFlow 语音识别",
};

export interface ResourceLimits {
  concurrency_limit: number;
  rpm_limit: number;
  tpm_limit: number;
  daily_limit: number;
  monthly_limit: number;
}
export const LIMIT_FIELDS = [
  { key: "concurrency_limit", label: "并发上限", hint: "同一配额组共享，至少 1" },
  { key: "rpm_limit", label: "RPM", hint: "每分钟请求数；0 表示不额外限制" },
  { key: "tpm_limit", label: "TPM", hint: "每分钟 token 数；0 表示不额外限制" },
  { key: "daily_limit", label: "每日请求上限", hint: "整组累计请求数；0 表示不额外限制" },
  { key: "monthly_limit", label: "每月请求上限", hint: "整组累计请求数；0 表示不额外限制" },
] as const;
export interface ProviderResource extends Partial<ResourceLimits> {
  id: string;
  provider: ResourceProvider;
  name: string;
  base_url: string;
  model: string;
  quota_group_id: string;
  route_group_id: string;
  revision: number;
  state: string;
  enabled: boolean;
  masked_key?: string;
  fingerprint?: string;
  capabilities?: Record<string, unknown>;
  last_error?: string;
  verified_at?: string | number | null;
  balance_value?: number | null;
  balance_source?: string;
  expires_at?: string | number | null;
  runtime?: Record<string, unknown>;
}
export interface ResourcePage {
  items: ProviderResource[];
  total: number;
  page: number;
  page_size: number;
}
export interface ResourceGroup extends Partial<ResourceLimits> {
  id: string;
  provider: ResourceProvider;
  name: string;
  model?: string;
  revision?: number;
}

export function liveResource(resource: ProviderResource, runtime?: Record<string, unknown>): ProviderResource {
  return { ...resource, state: typeof runtime?.state === "string" ? runtime.state : resource.state };
}
export interface ResourceDraft extends Record<keyof ResourceLimits, string> {
  provider: ResourceProvider;
  name: string;
  api_key: string;
  base_url: string;
  model: string;
  quota_group_id: string;
  route_group_id: string;
  voice_ids: string;
  languages: string;
  balance_source: "unknown" | "manual";
  balance_value: string;
}
export type ProviderModes = Partial<Record<ResourceProvider, boolean>>;
export interface PoolStats {
  providers: Partial<Record<ResourceProvider, Record<string, unknown> & { enabled?: boolean }>>;
  resources: Record<string, Record<string, unknown>>;
}

export function errorMessage(error: unknown): string {
  return error instanceof AdminApiError ? error.detail || error.message : error instanceof Error ? error.message : String(error);
}
export function defaultResourceDraft(provider: ResourceProvider, defaults: Partial<ResourceDraft> = {}): ResourceDraft {
  return {
    provider, name: "", api_key: "", base_url: "", model: "", quota_group_id: "", route_group_id: "",
    concurrency_limit: "1", rpm_limit: "0", tpm_limit: "0", daily_limit: "0", monthly_limit: "0",
    voice_ids: "", languages: "", balance_value: "", balance_source: "unknown", ...defaults,
  };
}
function stringList(value: unknown): string {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string").join(", ") : "";
}
export function resourceDraft(resource: ProviderResource): ResourceDraft {
  const draft = defaultResourceDraft(resource.provider, {
    name: resource.name, base_url: resource.base_url, model: resource.model,
    quota_group_id: resource.quota_group_id, route_group_id: resource.route_group_id,
    voice_ids: stringList(resource.capabilities?.voices ?? resource.capabilities?.voice_ids), languages: stringList(resource.capabilities?.languages),
    balance_source: resource.balance_source === "manual" ? "manual" : "unknown",
    balance_value: resource.balance_value == null ? "" : String(resource.balance_value),
  });
  for (const { key } of LIMIT_FIELDS) draft[key] = String(resource[key] ?? (key === "concurrency_limit" ? 1 : 0));
  return draft;
}
export function validateResourceDraft(draft: ResourceDraft, existing?: ProviderResource): string[] {
  const issues: string[] = [];
  if (!draft.name.trim()) issues.push("请填写资源名称");
  if (!existing && !draft.api_key.trim()) issues.push("请填写 API Key");
  if (!draft.model.trim()) issues.push("请填写模型，资源只在同模型内调度");
  try {
    const url = new URL(draft.base_url.trim());
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error();
  } catch { issues.push("Base URL 需为不含凭据、查询参数的 HTTP(S) 地址"); }
  if (!draft.quota_group_id) {
    for (const { key, label } of LIMIT_FIELDS) {
      const number = Number(draft[key]);
      const minimum = key === "concurrency_limit" ? 1 : 0;
      if (!draft[key].trim() || !Number.isInteger(number) || number < minimum || number > 1_000_000_000) {
        issues.push(`${label}需为 ${minimum} 至 1,000,000,000 的整数`);
      }
    }
  }
  if (existing && !draft.quota_group_id) issues.push("请选择已有配额组；新组请先在高级设置中添加");
  if (draft.balance_source === "manual" && (!draft.balance_value.trim() || !Number.isFinite(Number(draft.balance_value)) || Number(draft.balance_value) < 0)) issues.push("手工余额需为不小于 0 的数值");
  return issues;
}
function splitValues(value: string): string[] {
  return [...new Set(value.split(/[,，\n]/).map((item) => item.trim()).filter(Boolean))];
}
export function buildResourcePayload(draft: ResourceDraft, existing?: ProviderResource): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  const baseline = existing ? resourceDraft(existing) : undefined;
  if (!existing) payload.provider = draft.provider;
  else payload.revision = existing.revision;
  for (const key of ["name", "base_url", "model"] as const) {
    const value = draft[key].trim();
    if (!baseline || value !== baseline[key]) payload[key] = value;
  }
  if (draft.api_key.trim()) payload.api_key = draft.api_key.trim();
  for (const key of ["quota_group_id", "route_group_id"] as const) {
    if (draft[key] && (!baseline || draft[key] !== baseline[key])) payload[key] = draft[key];
  }
  if (!draft.quota_group_id) for (const { key } of LIMIT_FIELDS) payload[key] = Number(draft[key]);
  const capabilities = { ...existing?.capabilities, voices: splitValues(draft.voice_ids), languages: splitValues(draft.languages) };
  delete (capabilities as Record<string, unknown>).voice_ids;
  if (!baseline || draft.voice_ids !== baseline.voice_ids || draft.languages !== baseline.languages) payload.capabilities = capabilities;
  if (!baseline || draft.balance_source !== baseline.balance_source || draft.balance_value !== baseline.balance_value) {
    payload.balance_source = draft.balance_source;
    payload.balance_value = draft.balance_source === "manual" ? Number(draft.balance_value) : null;
  }
  return payload;
}
export interface ResourceSaveResult { resource: ProviderResource; ok: boolean; message: string }
export async function saveAndVerifyResource(
  payload: Record<string, unknown>, existing?: ProviderResource, onSecretSubmitted?: () => void,
): Promise<ResourceSaveResult> {
  const saved = existing
    ? await apiPatch<{ resource: ProviderResource }>(`${PROVIDER_RESOURCES_PATH}/${encodeURIComponent(existing.id)}`, payload)
    : await apiPost<{ resource: ProviderResource }>(PROVIDER_RESOURCES_PATH, payload);
  onSecretSubmitted?.();
  const needsVerification = !existing || existing.state === "pending" || ["api_key", "base_url", "model", "capabilities"].some((key) => key in payload);
  if (!needsVerification) return { resource: saved.resource, ok: true, message: "已保存" };
  try {
    return await apiPost<ResourceSaveResult>(`${PROVIDER_RESOURCES_PATH}/${encodeURIComponent(saved.resource.id)}/verify`, { revision: saved.resource.revision });
  } catch (error) {
    return { resource: saved.resource, ok: false, message: errorMessage(error) };
  }
}
function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
export function normalizePoolStats(raw: unknown): PoolStats {
  const input = record(raw);
  const providers: PoolStats["providers"] = {};
  const resources: PoolStats["resources"] = {};
  const providerRows = Array.isArray(input.providers) ? input.providers.map((value) => [record(value).provider, value]) : Object.entries(record(input.providers));
  for (const [key, value] of providerRows) {
    if (!PROVIDERS.includes(key as ResourceProvider)) continue;
    const row = record(value);
    const enabled = typeof row.enabled === "boolean" ? row.enabled : typeof row.pool_enabled === "boolean" ? row.pool_enabled : undefined;
    providers[key as ResourceProvider] = { ...row, enabled };
  }
  const resourceRows = Array.isArray(input.resources) ? input.resources.map((value) => [record(value).id ?? record(value).resource_id, value]) : Object.entries(record(input.resources));
  for (const [key, value] of resourceRows) if (typeof key === "string") resources[key] = record(value);
  return { providers, resources };
}
