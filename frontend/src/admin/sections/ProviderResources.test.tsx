import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import ConfigSection from "./ConfigSection";
import { normalizePoolStats, type ProviderResource } from "../providerResources";

vi.stubGlobal("React", React);

const resource: ProviderResource = {
  id: "public-r1", provider: "fish", name: "生产 Fish A", base_url: "https://api.fish.audio",
  model: "s1", quota_group_id: "quota-a", route_group_id: "route-a", revision: 4,
  state: "pending", enabled: false, masked_key: "••••abcd", verified_at: null,
  concurrency_limit: 3,
};
const clients: QueryClient[] = [];
afterEach(() => { clients.forEach((client) => client.clear()); clients.length = 0; });

function renderConfig(config: Record<string, unknown>, row = resource) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  clients.push(client);
  client.setQueryData(["admin", "config"], config);
  client.setQueryData(["admin", "provider-resources", "list", "fish", "", "", 1], { items: [row], total: 28, page: 1, page_size: 25 });
  client.setQueryData(["admin", "provider-resources", "groups"], { quota: [{ id: "quota-a", provider: "fish", name: "共享账号", concurrency_limit: 3 }], routing: [{ id: "route-a", provider: "fish", name: "Fish s1", model: "s1" }] });
  client.setQueryData(["admin", "provider-resources", "stats", row.id], normalizePoolStats({ providers: { fish: { enabled: true, active: 1 } }, resources: [{ id: row.id, active: 1, ewma_seconds: 2, cooldown_seconds: 0 }] }));
  return renderToStaticMarkup(<QueryClientProvider client={client}><ConfigSection /></QueryClientProvider>);
}

describe("API 与密钥 resource management", () => {
  it("retains four legacy model/default cards in a collapsed section and the existing JPush card", () => {
    const html = renderConfig({ llm: {}, stt: {}, tts: {}, deepgram: {}, jpush: {} });
    expect(html).toContain("Fish 语音合成");
    expect(html).toContain("Deepgram 实时识别");
    expect(html).toContain("LLM 文本模型");
    expect(html).toContain("SiliconFlow 语音识别");
    expect(html).toContain('<details class="admin-card"><summary>旧配置、模型默认设置与回退</summary>');
    expect(html).toContain('id="llm-suggestion_model"');
    expect(html).toContain('id="tts-fish_reference_id"');
    expect(html).toContain("JPush 推送");
    expect(html).toContain("第 1/2 页");
    expect(html).toContain("••••abcd");
    expect(html).not.toContain("强制激活");
  });

  it("disables pooled legacy secret editing without prefilling secrets or disabling model defaults", () => {
    const html = renderConfig({ provider_modes: { fish: true, llm: true }, tts: { fish_api_key: "must-not-prefill" }, llm: { api_key: "must-not-prefill", model: "same-model" }, jpush: {} });
    expect(html).not.toContain("must-not-prefill");
    expect(html).toMatch(/id="tts-fish_api_key"[^>]*disabled=""/);
    expect(html).toMatch(/id="llm-api_key"[^>]*disabled=""/);
    expect(html).not.toMatch(/id="llm-model"[^>]*disabled=""/);
    expect(html).toContain('value="disabled"');
    expect(html).toContain('title="请先通过验证"');
    expect(html).toContain('for="pool-provider-filter"');
  });
});
