import { afterEach, expect, it, vi } from "vitest";

afterEach(() => { vi.unstubAllGlobals(); vi.resetModules(); });
function setup(respond: (url: string, init: RequestInit) => Response, native?: Record<string, unknown>) {
  const data = new Map<string, string>();
  const fetcher = vi.fn(async (url: unknown, init: RequestInit = {}) => respond(String(url), init));
  const events: string[] = [];
  vi.stubGlobal("fetch", fetcher);
  vi.stubGlobal("window", { fetch: fetcher, localStorage: { getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => data.set(k, v), removeItem: (k: string) => data.delete(k) }, SpeakEasyNative: native, dispatchEvent: (event: Event) => { events.push(event.type); } });
  return { data, fetcher, events };
}
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });

it("never registers a device user on first launch, logout or 401", async () => {
  const { fetcher, events } = setup(() => json({ detail: "expired" }, 401));
  const api = await import("./api");
  const session = await import("./session");
  api.installApiIdentity();
  expect(await api.ensureIdentity()).toBeNull();
  session.saveSession({ user_id: "account", csrf_token: "csrf" });
  await window.fetch("/api/v3/client/bootstrap");
  expect(events).toContain("sonder:auth-expired");
  expect(session.hasSession()).toBe(false);
  await session.logoutSession();
  expect(await api.ensureIdentity()).toBeNull();
  expect(fetcher.mock.calls.some(([url]) => String(url).endsWith("/auth/register"))).toBe(false);
});

it("restores Web Cookie identity in memory and includes CSRF without persisting bearer", async () => {
  const { data, fetcher } = setup(() => json({ user_id: "cookie-user", email: "a@example.com", email_verified: true, has_account: true, csrf_token: "csrf" }));
  data.set("speakeasy.identity.v1", JSON.stringify({ user_id: "legacy", token: "legacy-bearer" }));
  const session = await import("./session");
  await session.restoreAccountSession();
  expect(session.readSession()?.user_id).toBe("cookie-user");
  expect(session.sessionHeaders("POST")).toEqual({ "X-CSRF-Token": "csrf" });
  expect(fetcher.mock.calls[0][1]?.credentials).toBe("include");
  expect(data.has("sonder.session.v1")).toBe(false);
  expect(data.has("speakeasy.identity.v1")).toBe(false);
  const { currentClientIdentity } = await import("./clientApi");
  expect(await currentClientIdentity()).toEqual({ user_id: "cookie-user", token: "" });
});

it("restores and saves Android sessions only through secure bridge, ignoring failed legacy migration", async () => {
  const saveSession = vi.fn((_json: string) => true);
  const clearSession = vi.fn();
  const { data } = setup(() => json({ user_id: "native-user", email_verified: true, has_account: true, csrf_token: "csrf" }), {
    getCapabilities: () => JSON.stringify({ accountSessions: true, identityMigration: "failed" }),
    readSession: () => JSON.stringify({ sessionToken: "secure", userId: "native-user", expiresAt: "2099-01-01" }), saveSession, clearSession,
  });
  data.set("sonder.session.v1", JSON.stringify({ token: "unsafe", user_id: "old" }));
  const session = await import("./session");
  await session.restoreAccountSession();
  expect(session.readSession()?.token).toBe("secure");
  session.saveSession({ user_id: "native-user", session_token: "new-secure", email_verified: true });
  expect(JSON.parse(saveSession.mock.calls[0][0] as string).sessionToken).toBe("new-secure");
  expect(data.has("sonder.session.v1")).toBe(false);
  session.clearSession();
  expect(clearSession).toHaveBeenCalled();
});

it("keeps a valid challenge for the invite stage and saves only successful verification", async () => {
  let accepted = false;
  const { fetcher } = setup((url, init) => {
    if (url.endsWith("/request")) return json({ challenge_id: "challenge", retry_after: 60, expires_in: 600 });
    const body = JSON.parse(String(init.body));
    if (!body.invite_code) return json({ detail: { code: "INVITE_REQUIRED", message: "请输入邀请码" } }, 403);
    accepted = body.challenge_id === "challenge" && body.platform === "web";
    return json({ user_id: "new-user", email_verified: true, created: true, csrf_token: "csrf" });
  });
  const session = await import("./session");
  const challenge = await session.requestEmailCode(" Person@Example.com ");
  await expect(session.verifyEmailCode({ challenge_id: challenge.challenge_id, code: "123456" })).rejects.toMatchObject({ code: "INVITE_REQUIRED" });
  expect(session.hasSession()).toBe(false);
  await session.verifyEmailCode({ challenge_id: challenge.challenge_id, code: "123456", invite_code: "invite" });
  expect(accepted).toBe(true);
  expect(session.hasSession()).toBe(true);
  expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body)).email).toBe("person@example.com");
});

it("does not restore a logged-out Web Cookie even if remote logout failed", async () => {
  const { fetcher } = setup(() => { throw new Error("offline"); });
  const session = await import("./session");
  session.saveSession({ user_id: "old-account", csrf_token: "csrf" });
  await session.logoutSession();
  fetcher.mockClear();
  expect(await session.restoreAccountSession()).toBeNull();
  expect(fetcher).not.toHaveBeenCalled();
});

it("ignores an old in-flight 401 after a new login", async () => {
  let complete!: (value: Response) => void;
  const fetcher = vi.fn(() => new Promise<Response>((resolve) => { complete = resolve; }));
  const { events } = setup(() => json({}));
  window.fetch = fetcher;
  const api = await import("./api");
  const session = await import("./session");
  api.installApiIdentity();
  session.saveSession({ user_id: "old-account" });
  const pending = window.fetch("/api/v3/feed");
  session.saveSession({ user_id: "new-account" });
  complete(json({ detail: "expired" }, 401));
  await pending;
  expect(session.readSession()?.user_id).toBe("new-account");
  expect(events).not.toContain("sonder:auth-expired");
});

it("notifies other tabs to discard data after Cookie logout or a different account login", async () => {
  setup(() => json({}));
  const target = new EventTarget();
  window.addEventListener = target.addEventListener.bind(target);
  window.removeEventListener = target.removeEventListener.bind(target);
  const logout = vi.fn();
  const login = vi.fn();
  const session = await import("./session");
  const unsubscribe = session.subscribeAccountChanges(logout, login);
  function change(key: string, value: string) {
    const event = new Event("storage");
    Object.assign(event, { key, newValue: value });
    target.dispatchEvent(event);
  }
  change("se.account.signed-out", "1");
  change("se.account.changed", "next-account");
  expect(logout).toHaveBeenCalledTimes(1);
  expect(login).toHaveBeenCalledTimes(1);
  unsubscribe();
  change("se.account.changed", "later-account");
  expect(login).toHaveBeenCalledTimes(1);
});
