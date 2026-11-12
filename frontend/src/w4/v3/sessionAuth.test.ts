/**
 * 账号会话接入测试：
 * 1) 登录请求本身不带身份头（避免被设备身份抢先）；
 * 2) 登录成功后所有 /api 请求改用 X-Session-Token，写请求再带 X-CSRF-Token；
 * 3) 退出会清空本地会话。
 */
import { afterEach, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

function memoryStorage(initial: Record<string, string> = {}) {
  const data = new Map(Object.entries(initial));
  return {
    data,
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => void data.set(key, value),
    removeItem: (key: string) => void data.delete(key),
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

it("login stores the session and later requests use session headers", async () => {
  const store = memoryStorage();
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const mockFetch = vi.fn(async (input: unknown, init: RequestInit = {}) => {
    const url = typeof input === "string" ? input : String(input);
    calls.push({ url, init });
    if (url.includes("/api/v3/auth/login")) {
      return jsonResponse({
        user_id: "u_1",
        session_token: "tok_1",
        csrf_token: "csrf_1",
        email: "a@example.com",
        email_verified: true,
      });
    }
    return jsonResponse({ ok: true });
  });
  vi.stubGlobal("fetch", mockFetch);
  vi.stubGlobal("window", { fetch: mockFetch, localStorage: store });

  (await import("./authMode")).configureEmailCodeAuth(false);
  const api = await import("./api");
  api.installApiIdentity();
  const session = await import("./session");

  const result = await session.loginEmail("a@example.com", "pa55word!");
  expect(result.token).toBe("tok_1");
  expect(session.readSession()?.csrf).toBe("csrf_1");

  const loginCall = calls.find((call) => call.url.includes("/api/v3/auth/login"));
  expect(loginCall).toBeTruthy();
  const loginHeaders = new Headers(loginCall?.init.headers);
  expect(loginHeaders.get("X-Session-Token")).toBeNull();
  expect(loginHeaders.get("X-User-Id")).toBeNull();

  const windowFetch = (window as unknown as { fetch: typeof fetch }).fetch;
  await windowFetch("/api/v3/feed");
  const getHeaders = new Headers(calls[calls.length - 1].init.headers);
  expect(getHeaders.get("X-Session-Token")).toBe("tok_1");
  expect(getHeaders.get("X-CSRF-Token")).toBeNull();
  expect(getHeaders.get("X-User-Id")).toBeNull();

  await windowFetch("/api/v3/posts", {
    method: "POST",
    body: "{}",
    headers: { "Content-Type": "application/json" },
  });
  const postHeaders = new Headers(calls[calls.length - 1].init.headers);
  expect(postHeaders.get("X-Session-Token")).toBe("tok_1");
  expect(postHeaders.get("X-CSRF-Token")).toBe("csrf_1");
});

it("logout revokes the session and clears local state", async () => {
  const store = memoryStorage();
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const mockFetch = vi.fn(async (input: unknown, init: RequestInit = {}) => {
    calls.push({ url: String(input), init });
    return jsonResponse({ ok: true });
  });
  vi.stubGlobal("fetch", mockFetch);
  vi.stubGlobal("window", { fetch: mockFetch, localStorage: store });
  (await import("./authMode")).configureEmailCodeAuth(false);
  await import("./api");
  const session = await import("./session");

  session.saveSession({ session_token: "tok_2", csrf_token: "csrf_2", user_id: "u_2" });
  expect(session.hasSession()).toBe(true);

  await session.logoutSession();
  expect(session.readSession()).toBeNull();
  const logoutCall = calls.find((call) => call.url.includes("/api/v3/auth/logout"));
  expect(logoutCall).toBeTruthy();
  const headers = new Headers(logoutCall?.init.headers);
  expect(headers.get("X-Session-Token")).toBe("tok_2");
});

it("login failure surfaces the server detail", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "邮箱或密码不正确" }, 401)));
  vi.stubGlobal("window", { fetch: globalThis.fetch, localStorage: memoryStorage() });
  const session = await import("./session");
  await expect(session.loginEmail("a@example.com", "bad")).rejects.toThrow("邮箱或密码不正确");
  expect(session.readSession()).toBeNull();
});
