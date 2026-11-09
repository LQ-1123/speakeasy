"use client";

import {
  PREVIEW_EXPRESSIONS,
  PREVIEW_PROFILE,
  PREVIEW_SETTINGS,
  previewConversations,
  previewDiscoverNext,
  previewEvents,
  previewFeed,
  previewFriends,
  previewPersona,
  previewThread,
} from "./previewFixtures";

export type PreviewVariant = "normal" | "loading" | "empty" | "error" | "long" | "call";

const VARIANTS: PreviewVariant[] = ["normal", "loading", "empty", "error", "long", "call"];

export function isPreviewMode(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return new URLSearchParams(window.location.search).get("preview") === "1";
  } catch {
    return false;
  }
}

export function previewVariant(): PreviewVariant {
  if (typeof window === "undefined") return "normal";
  const value = new URLSearchParams(window.location.search).get("state") as PreviewVariant | null;
  return value && VARIANTS.includes(value) ? value : "normal";
}

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

function pathOf(input: RequestInfo | URL): string {
  try {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(raw, typeof window !== "undefined" ? window.location.origin : "http://localhost");
    return url.pathname;
  } catch {
    return "";
  }
}

function methodOf(input: RequestInfo | URL, init?: RequestInit): string {
  if (init?.method) return init.method.toUpperCase();
  if (typeof input === "object" && "method" in input) return String(input.method).toUpperCase();
  return "GET";
}

/**
 * Preview-only network adapter. It intercepts same-origin API calls and returns
 * deterministic fixtures so the real components and routes render without a
 * backend. It is installed only when ?preview=1.
 */
export function installPreviewAdapter(variant = previewVariant()): () => void {
  if (typeof window === "undefined") return () => undefined;
  const originalFetch = window.fetch.bind(window);
  const originalWebSocket = window.WebSocket;
  const friendCount = variant === "long" ? 90 : 12;
  const friends = previewFriends(friendCount);
  const threads = new Map<string, ReturnType<typeof previewThread>>();
  friends.forEach((_, index) => {
    threads.set(`fixture-${index + 1}`, previewThread(index, variant === "long" && index === 0 ? 1000 : 12));
  });
  let feed = previewFeed(variant === "long" ? 30 : 7);
  let requestCount = 0;
  const activity: Array<{ at: number; duration: number }> = [];

  const baseBootstrap = () => ({
    profile: variant === "empty" ? { nickname: "", target_languages: ["en"], native_language: "zh", fact_sheet: {} } : PREVIEW_PROFILE,
    friends: variant === "empty" ? [] : friends,
    requests: variant === "empty" ? [] : [friends[7] ?? friends[0]].map((friend) => ({
      ...friend,
      id: `request-${friend.id}`,
      name: "Ines",
      request_message: "Your note about tide pools made me smile. Want to trade small observations?",
    })),
    conversations: variant === "empty" ? [] : previewConversations(friends),
    expressions: variant === "empty" ? [] : PREVIEW_EXPRESSIONS,
    capabilities: { calls: false, dynamicColor: false },
    unread_events: variant === "empty" ? 0 : 3,
  });

  const previewFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const path = pathOf(input);
    const method = methodOf(input, init);
    if (!path.startsWith("/api/")) return originalFetch(input, init);
    requestCount += 1;
    activity.push({ at: performance.now(), duration: 0 });
    if (activity.length > 200) activity.shift();

    if (variant === "error" && (path.includes("bootstrap") || path.includes("/feed") || path.includes("/personas") || path.includes("/events"))) {
      return json({ detail: "Preview fixture: the server is unavailable." }, 503);
    }

    if (path === "/api/v3/auth/register") {
      return json({ user_id: "fixture-user", token: "fixture-token", device_key: "fixture-device" });
    }

    if (path === "/api/v3/client/bootstrap") return json(baseBootstrap());

    if (path === "/api/v3/client/preferences") {
      return json({ messages: true, interactions: true, sound: true, theme: "system" });
    }

    if (path === "/api/v3/client/event-ticket") {
      return json({ ticket: "fixture-ticket", ws_path: "/api/v3/client/events", expires_in: 60, current_sequence: 0 });
    }

    if (path === "/api/v3/client/devices") return json({});

    if (path === "/api/v3/client/events" || path.startsWith("/api/v3/client/events/")) return json({});

    if (path === "/api/v3/feed" || path.startsWith("/api/v3/feed/")) {
      if (path.endsWith("/summary")) {
        return json({ persona_posts: feed.length, my_posts: 0, friends: friends.length, unread_events: 3 });
      }
      if (variant === "loading") return new Promise<Response>(() => undefined);
      if (variant === "empty") return json({ items: [], next_cursor: "" });
      return json({ items: feed, next_cursor: "" });
    }

    if (path === "/api/v3/events") return json({ items: variant === "empty" ? [] : previewEvents(), unread: 3 });
    if (path.startsWith("/api/v3/events/")) return json({});
    if (path === "/api/v3/discover/next") return json(previewDiscoverNext());
    if (path.startsWith("/api/v3/personas/")) {
      const id = path.split("/")[4] || "persona-1";
      const index = Math.max(0, Number(id.replace("persona-", "")) - 1);
      if (path.endsWith("/friend") && method === "POST") return json({ friend_id: `fixture-${index + 1}`, status: "accepted" });
      if (path.endsWith("/friend") && method === "DELETE") return json({});
      return json(previewPersona(index));
    }
    if (path === "/api/v3/personas") return json({ items: friends.map((f, i) => previewPersona(i)), next_cursor: "" });
    if (path === "/api/v3/posts" && method === "POST") {
      const body = JSON.parse(String(init?.body ?? "{}")) as { content?: string; media?: string[]; visibility?: string; tags?: string[] };
      const post = {
        ...previewFeed(1)[0],
        id: `preview-post-${++requestCount}`,
        kind: "user_post",
        content: body.content ?? "",
        media: body.media ?? [],
        tags: body.tags ?? [],
        is_mine: true,
        friendship_status: "none",
      } as (typeof feed)[number];
      feed = [post, ...feed];
      return json({ post });
    }
    if (path.startsWith("/api/v3/posts/")) return json({ ok: true });
    if (path.includes("/like")) {
      return json({ liked: true, like_count: 1 });
    }
    if (path.includes("/comments") && method === "POST") {
      const body = JSON.parse(String(init?.body ?? "{}")) as { content?: string };
      return json({ id: `comment-${Date.now()}`, content: body.content ?? "", created_at: new Date().toISOString(), author: { nickname: "我吗", is_ai: false } });
    }

    if (/^\/api\/v2\/friends\/[^/]+\/messages$/.test(path)) {
      const friendId = decodeURIComponent(path.split("/")[4]);
      const all = threads.get(friendId) ?? previewThread(0);
      return json({ messages: all.slice(-30), has_more: all.length > 30 });
    }
    if (/^\/api\/v2\/friends\/[^/]+\/messages\?/.test(path)) {
      const friendId = decodeURIComponent(path.split("/")[4]);
      return json({ messages: threads.get(friendId) ?? [], has_more: false });
    }
    if (/^\/api\/v2\/chat\/[^/]+\/text$/.test(path) && method === "POST") {
      const friendId = decodeURIComponent(path.split("/")[4]);
      const body = JSON.parse(String(init?.body ?? "{}")) as { text?: string };
      const list = threads.get(friendId) ?? previewThread(0);
      list.push({
        id: `preview-sent-${Date.now()}`,
        friend_id: friendId,
        sender: "user",
        msg_type: "text",
        transcript: body.text ?? "",
        created_at: new Date().toISOString(),
        is_read: true,
      });
      return json({ ok: true });
    }
    if (/^\/api\/v2\/chat\/[^/]+\/(image|voice|call)/.test(path)) return json({ ok: true });
    if (/^\/api\/v2\/friends\/[^/]+\/read$/.test(path)) return json({ ok: true });
    if (path === "/api/v2/friends" || path.startsWith("/api/v2/friends?")) {
      return json({ friends, requests: [] });
    }
    if (path === "/api/v2/expressions") {
      if (method === "POST") {
        const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, string>;
        return json({ expression: { ...PREVIEW_EXPRESSIONS[0], id: `expr-${Date.now()}`, original_text: body.original_text ?? "", better_expression: body.better_expression ?? "", explanation: body.explanation ?? "" } });
      }
      return json({ expressions: PREVIEW_EXPRESSIONS });
    }
    if (path.startsWith("/api/v2/expressions/")) return json({});
    if (path === "/api/v2/translate") return json({ translation: "（预览译文）这家咖啡馆真的很不错。" });
    if (path === "/api/v2/settings") return json(PREVIEW_SETTINGS);
    if (path.startsWith("/api/v2/settings")) return json(PREVIEW_SETTINGS);
    if (path === "/api/v2/moments") return json({ moments: [] });
    if (path.includes("/tts/")) return json({ ok: false });
    if (path === "/api/v3/calls") return json({ call_id: "preview-call", ticket: "preview", ws_path: "/api/v3/calls/preview-call/stream", expires_in: 60 }, 503);
    return json({});
  };

  window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => previewFetch(input, init)) as typeof fetch;

  // Event sockets would otherwise retry forever in a static preview.
  class PreviewSocket {
    static readonly CONNECTING = 0;
    static readonly OPEN = 1;
    static readonly CLOSING = 2;
    static readonly CLOSED = 3;
    readonly url: string;
    readyState = 0;
    onopen: ((event: Event) => void) | null = null;
    onclose: ((event: CloseEvent) => void) | null = null;
    onmessage: ((event: MessageEvent) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;
    constructor(url: string | URL) {
      this.url = String(url);
      window.setTimeout(() => {
        this.readyState = 1;
        this.onopen?.(new Event("open"));
      }, 0);
    }
    send(): void {}
    close(): void {
      this.readyState = 3;
      this.onclose?.(new CloseEvent("close"));
    }
    addEventListener(): void {}
    removeEventListener(): void {}
    dispatchEvent(): boolean {
      return false;
    }
  }
  window.WebSocket = PreviewSocket as unknown as typeof WebSocket;

  window.dispatchEvent(new CustomEvent("speakeasy:preview-ready", { detail: { variant, requests: requestCount } }));

  return () => {
    window.fetch = originalFetch;
    window.WebSocket = originalWebSocket;
  };
}
