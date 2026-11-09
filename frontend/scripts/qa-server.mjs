/** Offline visual QA fixture. It never proxies to a real API or model provider. */
import http from "node:http";
import { readFile } from "node:fs/promises";
import { resolve, extname } from "node:path";

const root = resolve("out");
const now = new Date().toISOString();
const longLists = process.env.QA_LONG_LISTS === "1";
const friends = Array.from({ length: 90 }, (_, index) => ({
  id: `fixture-${index}`, name: ["Callum", "Giulia", "Annika", "Adrian", "Camila", "Finn"][index % 6] + (index > 5 ? ` ${index}` : ""),
  nationality: "United Kingdom", city: ["London", "Milan", "Berlin"][index % 3], occupation: "Designer", reply_language: "en", avatar_url: `/mock/avatars/${["callum", "giulia", "annika", "adrian", "camila", "finn"][index % 6]}.jpg`,
  avatar_seed: String(index), gender: index % 2 ? "female" : "male", timezone: "Europe/London", age: 28,
  total_turns: 12, closeness_level: 2, unread: index === 0 ? 2 : 0, is_awake: index < 3,
  local_time: "11:20", bio: "Coffee, design and everyday conversations.", voice_id: "ready-voice", last_interaction_at: now,
}));
const message = (friend, index = 0) => ({ id: `message-${friend.id}-${index}`, friend_id: friend.id, sender: index % 2 ? "user" : "friend", msg_type: "text", transcript: index % 2 ? "That sounds lovely. What was it like?" : "I found a quiet little coffee shop this morning. Have you tried anything new lately?", created_at: now, is_read: true });
const expression = { id: "expression-1", original_text: "I very like it", better_expression: "I really like it", explanation: "Use really before a verb.", status: "learning", mastery_count: 1, created_at: now };
let preferences = { messages: true, interactions: true, sound: true, theme: "system" };
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://127.0.0.1");
  if (url.pathname.startsWith("/api/")) {
    let body = ""; for await (const chunk of req) body += chunk;
    let value = {};
    if (url.pathname.endsWith("auth/register")) value = { user_id: "fixture-user", token: "fixture-token" };
    else if (url.pathname === "/api/v3/client/bootstrap") value = { profile: { nickname: "小林", target_languages: ["en"], native_language: "zh", fact_sheet: { profile: { city: "上海", major_or_job: "设计师" }, interests: ["旅行", "音乐"] } }, friends, requests: [], conversations: friends.map((friend) => ({ friend_id: friend.id, last_message: message(friend), unread: friend.unread })), expressions: [expression], capabilities: { calls: true }, unread_events: 0 };
    else if (url.pathname === "/api/v3/client/preferences") { if (body) preferences = { ...preferences, ...JSON.parse(body) }; value = preferences; }
    else if (url.pathname.endsWith("/messages")) { const friend = friends.find((f) => url.pathname.includes(f.id)) || friends[0]; value = { messages: Array.from({ length: longLists ? 1000 : 8 }, (_, index) => message(friend, index)) }; }
    else if (url.pathname === "/api/v2/friends") value = { friends, requests: [] };
    else if (url.pathname === "/api/v2/expressions") value = { expressions: [expression], expression };
    else if (url.pathname === "/api/v3/client/event-ticket") value = { ticket: "offline-fixture", ws_path: "/fixture-events" };
    else if (url.pathname === "/api/v3/calls") value = { call_id: "fixture-call", ticket: "fixture-ticket", ws_path: "/api/v3/calls/fixture-call/stream", expires_in: 60 };
    else if (url.pathname === "/api/v3/feed") value = { items: [{ id: "post1", kind: "persona_post", content: "A slow morning, good coffee, and no plans. Sometimes that's all you need.", media: ["/mock/photos/cafe.jpg"], tags: ["daily"], language: "en", visibility: "public", created_at: now, like_count: 0, comment_count: 0, liked_by_me: false, is_mine: false, author: { id: "persona1", nickname: "Callum", avatar: "/mock/avatars/callum.jpg", bio: "Coffee and conversations", country: "United Kingdom", city: "London", occupation: "Designer", age: 28, gender: "male", language: "en", interests: ["coffee"], is_ai: true, ai_label: "AI" }, comments: [] }], next_cursor: "" };
    else if (url.pathname === "/api/v3/feed/summary") value = { unread_events: 0, friend_count: 90, post_count: 1 };
    else if (url.pathname === "/api/v3/events") value = { items: [], unread: 0 };
    if (longLists && url.pathname === "/api/v3/feed" && value.items?.length) value.items = Array.from({ length: 1000 }, (_, index) => ({ ...value.items[0], id: `long-post-${index}` }));
    res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" }); res.end(JSON.stringify(value)); return;
  }
  try {
    let path = resolve(root, `.${decodeURIComponent(url.pathname)}`);
    if (!path.startsWith(root)) throw new Error("Invalid path");
    if (!extname(path)) path = resolve(path, "index.html");
    const data = await readFile(path);
    const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".jpg": "image/jpeg", ".png": "image/png", ".svg": "image/svg+xml", ".wav": "audio/wav", ".json": "application/json" }[extname(path)] || "application/octet-stream";
    res.writeHead(200, { "Content-Type": mime }); res.end(data);
  } catch { res.writeHead(404); res.end("Not found"); }
});
const port = Number(process.env.QA_PORT) || 4173;
server.listen(port, "127.0.0.1", () => console.log(`Offline QA fixture http://127.0.0.1:${port}`));
