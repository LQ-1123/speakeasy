import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./api", () => ({
  apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(), apiPut: vi.fn(),
  AdminApiError: class extends Error { detail = this.message; },
}));

import { apiPatch, apiPost } from "./api";
import {
  buildResourcePayload, defaultResourceDraft, normalizePoolStats, liveResource,
  resourceDraft, saveAndVerifyResource, validateResourceDraft,
  type ProviderResource,
} from "./providerResources";

const resource: ProviderResource = {
  id: "resource-1", provider: "fish", name: "Fish A", model: "s1",
  base_url: "https://api.fish.audio", quota_group_id: "quota-a",
  route_group_id: "route-a", revision: 2, state: "active", enabled: true,
  masked_key: "••••abcd", fingerprint: "abcd", concurrency_limit: 3,
  rpm_limit: 30, tpm_limit: 0, daily_limit: 0, monthly_limit: 0,
  capabilities: { voices: ["voice-a"], languages: ["en"] },
};

beforeEach(() => vi.resetAllMocks());

describe("provider resource forms", () => {
  it("uses the latest polled health while retaining the operator enabled flag", () => {
    expect(liveResource(resource, { state: "cooldown", active: 0 })).toMatchObject({ state: "cooldown", enabled: true });
    expect(liveResource(resource, undefined).state).toBe(resource.state);
  });
  it("defaults to one slot and an unknown balance without inventing credit", () => {
    const draft = defaultResourceDraft("fish");
    expect(draft.concurrency_limit).toBe("1");
    expect(draft.balance_source).toBe("unknown");
    expect(draft.balance_value).toBe("");
  });

  it("never copies a public fingerprint into the secret field", () => {
    const draft = resourceDraft(resource);
    expect(draft.api_key).toBe("");
    expect(draft.voice_ids).toBe("voice-a");
    expect(draft.concurrency_limit).toBe("3");
  });

  it("validates secrets, URL, model and nonnegative limits before any request", () => {
    const draft = { ...defaultResourceDraft("fish"), name: "A", base_url: "ftp://unsafe", concurrency_limit: "0", rpm_limit: "-1" };
    const issues = validateResourceDraft(draft);
    expect(issues.join(" ")).toMatch(/API Key/);
    expect(issues.join(" ")).toMatch(/URL/);
    expect(issues.join(" ")).toMatch(/模型/);
    expect(issues.join(" ")).toMatch(/并发/);
    expect(issues.join(" ")).toMatch(/RPM/);
  });

  it("rejects fractional and oversized request budgets and a missing shared group on edit", () => {
    const draft = { ...defaultResourceDraft("fish"), name: "A", api_key: "key", base_url: "https://api.fish.audio", model: "s1", daily_limit: "1.5", monthly_limit: "1000000001" };
    expect(validateResourceDraft(draft).join(" ")).toMatch(/每日/);
    expect(validateResourceDraft(draft).join(" ")).toMatch(/每月/);
    expect(validateResourceDraft({ ...resourceDraft(resource), quota_group_id: "" }, resource).join(" ")).toMatch(/配额组/);
  });

  it("does not reset shared quota limits or send an unchanged secret on edit", () => {
    const draft = resourceDraft(resource);
    const payload = buildResourcePayload({ ...draft, name: "Renamed" }, resource);
    expect(payload).toEqual({ name: "Renamed", revision: 2 });
    expect(payload).not.toHaveProperty("api_key");
    expect(payload).not.toHaveProperty("concurrency_limit");
  });

  it("creates independent caps only for a new quota group and deduplicates capability values", () => {
    const draft = { ...defaultResourceDraft("fish"), name: " A ", api_key: " secret ", base_url: "https://api.fish.audio", model: "s1", voice_ids: "v1, v1\nv2", languages: "en,zh", balance_source: "manual" as const, balance_value: "12.5" };
    const payload = buildResourcePayload(draft);
    expect(payload).toMatchObject({ name: "A", api_key: "secret", concurrency_limit: 1, balance_value: 12.5, balance_source: "manual", capabilities: { voices: ["v1", "v2"], languages: ["en", "zh"] } });
    expect(buildResourcePayload({ ...draft, quota_group_id: "quota-a" })).not.toHaveProperty("rpm_limit");
  });
});

describe("save then business verification", () => {
  it("creates first, clears the local secret, then verifies the returned revision", async () => {
    const pending = { ...resource, state: "pending" };
    vi.mocked(apiPost).mockResolvedValueOnce({ resource: pending }).mockResolvedValueOnce({ ok: true, resource, message: "verified" });
    const clearSecret = vi.fn();
    const result = await saveAndVerifyResource({ provider: "fish", api_key: "secret" }, undefined, clearSecret);
    expect(apiPost).toHaveBeenNthCalledWith(1, "/api/admin/provider-resources", { provider: "fish", api_key: "secret" });
    expect(clearSecret).toHaveBeenCalledOnce();
    expect(clearSecret.mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(apiPost).mock.invocationCallOrder[1]);
    expect(apiPost).toHaveBeenNthCalledWith(2, "/api/admin/provider-resources/resource-1/verify", { revision: 2 });
    expect(result).toMatchObject({ ok: true, resource });
  });

  it("retains a saved pending resource when the probe fails and never force activates", async () => {
    const pending = { ...resource, state: "pending", verified_at: null };
    vi.mocked(apiPost).mockResolvedValueOnce({ resource: pending }).mockRejectedValueOnce(new Error("probe rejected"));
    const result = await saveAndVerifyResource({ provider: "fish", api_key: "secret" });
    expect(result).toMatchObject({ ok: false, resource: pending, message: "probe rejected" });
    expect(apiPatch).not.toHaveBeenCalled();
    expect(apiPost).toHaveBeenCalledTimes(2);
  });

  it("verifies credential revisions but keeps a metadata-only edit separate", async () => {
    vi.mocked(apiPatch).mockResolvedValue({ resource });
    await saveAndVerifyResource({ name: "Renamed", revision: 2 }, resource);
    expect(apiPost).not.toHaveBeenCalled();
    vi.mocked(apiPatch).mockResolvedValueOnce({ resource: { ...resource, revision: 3, state: "pending" } });
    vi.mocked(apiPost).mockResolvedValueOnce({ ok: false, resource: { ...resource, revision: 3, state: "pending" }, message: "bad model" });
    const result = await saveAndVerifyResource({ model: "new-model", revision: 2 }, resource);
    expect(apiPost).toHaveBeenCalledWith("/api/admin/provider-resources/resource-1/verify", { revision: 3 });
    expect(result.ok).toBe(false);
  });

  it("retries verification for a pending saved resource even when no fields changed", async () => {
    const pending = { ...resource, state: "pending", verified_at: null };
    vi.mocked(apiPatch).mockResolvedValueOnce({ resource: pending });
    vi.mocked(apiPost).mockResolvedValueOnce({ ok: true, resource });
    await saveAndVerifyResource({ revision: 2 }, pending);
    expect(apiPost).toHaveBeenCalledWith("/api/admin/provider-resources/resource-1/verify", { revision: 2 });
  });
});

describe("public runtime mapping", () => {
  it("normalizes keyed provider/resource stats and only indexes public values", () => {
    const stats = normalizePoolStats({ providers: { fish: { enabled: true, active: 2, inflight: 1 } }, resources: { "resource-1": { in_flight: 1, cooldown_remaining: 3 } } });
    expect(stats.providers.fish?.enabled).toBe(true);
    expect(stats.resources["resource-1"].in_flight).toBe(1);
  });

  it("supports resource arrays and never guesses enabled mode when the backend omits it", () => {
    const stats = normalizePoolStats({ providers: [{ provider: "llm", pool_enabled: false }], resources: [{ resource_id: "r2", in_flight: 2 }] });
    expect(stats.providers.llm?.enabled).toBe(false);
    expect(stats.providers.fish).toBeUndefined();
    expect(stats.resources.r2.in_flight).toBe(2);
  });
});
