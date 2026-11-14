import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { expect, it, vi } from "vitest";

function serviceWorker(cached?: Response) {
  const listeners = new Map<string, (event: unknown) => void>();
  const put = vi.fn(async () => undefined);
  const fetch = vi.fn(async () => new Response("fresh"));
  const match = vi.fn(async () => cached);
  const worker = { location: { origin: "https://test.example" }, addEventListener: (type: string, cb: (event: unknown) => void) => listeners.set(type, cb) };
  runInNewContext(readFileSync(new URL("../../public/sw.js", import.meta.url), "utf8"), { self: worker, URL, fetch, caches: { match, open: async () => ({ put, match }) } });
  return { fetch, put, handle: (path: string, mode = "cors") => {
    let response: Promise<Response> | undefined;
    listeners.get("fetch")!({ request: { method: "GET", url: `https://test.example${path}`, mode }, respondWith: (value: Promise<Response>) => { response = value; }, waitUntil: () => undefined });
    return response;
  } };
}

it("serves hashed static cache hits without a network revalidation", async () => {
  const sw = serviceWorker(new Response("cached"));
  expect(await (await sw.handle("/_next/static/chunks/app-123abc.js"))!.text()).toBe("cached");
  expect(sw.fetch).not.toHaveBeenCalled();
});

it("never intercepts protected audio, account routes or APIs", () => {
  const sw = serviceWorker();
  for (const path of ["/api/v2/audio/private.mp3", "/api/v3/calls/c/audio/a", "/admin", "/admin/a", "/hello"]) expect(sw.handle(path, "navigate")).toBeUndefined();
  expect(sw.put).not.toHaveBeenCalled();
});

it("fetches navigation updates without replacing the cached shell with unrelated HTML", async () => {
  const sw = serviceWorker(new Response("old"));
  expect(await (await sw.handle("/", "navigate"))!.text()).toBe("fresh");
  expect(sw.fetch).toHaveBeenCalledOnce();
  expect(sw.handle("/account/private", "navigate")).toBeUndefined();
});
