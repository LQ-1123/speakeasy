import { afterEach, expect, it, vi } from "vitest";

afterEach(() => { vi.unstubAllGlobals(); vi.resetModules(); });

it("refreshes a stale native token using the migrated device key", async () => {
  let stored = { user_id: "existing-user", token: "stale", device_key: "existing-device-key" };
  const fetch = vi.fn(async (_url: unknown, init: RequestInit) => {
    expect(JSON.parse(String(init.body)).device_key).toBe("existing-device-key");
    return new Response(JSON.stringify({ user_id: "existing-user", token: "fresh" }), { status: 200 });
  });
  vi.stubGlobal("window", {
    fetch,
    localStorage: { getItem: () => null, setItem: () => undefined },
    SpeakEasyNative: {
      getCapabilities: () => JSON.stringify({ identityMigration: "ready" }),
      getIdentity: () => JSON.stringify(stored),
      saveIdentity: (json: string) => { stored = JSON.parse(json); },
    },
  });
  (await import("./authMode")).configureEmailCodeAuth(false);
  const { ensureIdentity } = await import("./api");
  expect((await ensureIdentity())?.token).toBe("stale");
  expect(fetch).not.toHaveBeenCalled();
  const [first, second] = await Promise.all([ensureIdentity(true), ensureIdentity(true)]);
  expect(first?.token).toBe("fresh");
  expect(second?.user_id).toBe("existing-user");
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(stored.token).toBe("fresh");
});

it("does not create a replacement user when a stale native identity lacks its migration key", async () => {
  const fetch = vi.fn();
  vi.stubGlobal("window", {
    fetch,
    localStorage: { getItem: () => null },
    SpeakEasyNative: { getCapabilities: () => "{}", getIdentity: () => JSON.stringify({ user_id: "existing", token: "stale" }) },
  });
  (await import("./authMode")).configureEmailCodeAuth(false);
  const { ensureIdentity } = await import("./api");
  await expect(ensureIdentity(true)).rejects.toThrow("existing account");
  expect(fetch).not.toHaveBeenCalled();
});
