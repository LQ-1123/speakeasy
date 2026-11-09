/**
 * SpeakEasy Web redesign acceptance script.
 *
 * Prerequisites:
 *   1. cd frontend && npm run build
 *   2. node scripts/qa-server.mjs     # http://127.0.0.1:4173
 *   3. set PLAYWRIGHT_CORE_PATH to a playwright-core package if it is not resolvable:
 *        PLAYWRIGHT_CORE_PATH=C:/path/to/node_modules/playwright-core node scripts/qa-redesign-interactions.mjs
 *
 * The script uses deterministic fixtures from ?preview=1 and never calls a real
 * backend, model provider or device. Output: artifacts/qa-report.json + screenshots.
 */
import { createRequire } from "node:module";
import fs from "node:fs";
const require = createRequire(import.meta.url);
let playwright;
try { playwright = require(process.env.PLAYWRIGHT_CORE_PATH || "playwright-core"); } catch {
  console.error("Install playwright-core or set PLAYWRIGHT_CORE_PATH.");
  process.exit(2);
}
const { chromium } = playwright;

const browser = await chromium.launch({ executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe", headless: true, args: ["--disable-gpu", "--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
await page.addInitScript(() => {
  window.__perf = { longTasks: [], frames: [], start: performance.now() };
  try {
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) window.__perf.longTasks.push({ duration: entry.duration, start: entry.startTime });
    }).observe({ type: "longtask", buffered: true });
  } catch {}
  const tick = (time) => {
    window.__perf.frames.push(time);
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
});
const base = process.env.QA_BASE_URL || "http://127.0.0.1:4173/";

async function metrics() {
  return page.evaluate(() => {
    const { longTasks, frames } = window.__perf;
    const diffs = [];
    for (let i = 1; i < frames.length; i += 1) diffs.push(frames[i] - frames[i - 1]);
    const sorted = diffs.slice().sort((a, b) => a - b);
    const p95 = sorted[Math.floor(sorted.length * 0.95)] ?? 0;
    const avg = diffs.length ? diffs.reduce((a, b) => a + b, 0) / diffs.length : 0;
    return {
      longTasks: longTasks.map((t) => Math.round(t.duration)),
      maxLongTask: longTasks.length ? Math.max(...longTasks.map((t) => t.duration)) : 0,
      frames: frames.length,
      avgFrameMs: Number(avg.toFixed(2)),
      p95FrameMs: Number(p95.toFixed(2)),
      droppedOver20ms: diffs.filter((d) => d > 20).length,
      domNodes: document.querySelectorAll("*").length,
      messagesRendered: document.querySelectorAll(".w4-msg").length,
      rowsRendered: document.querySelectorAll(".w4-row").length,
    };
  });
}

const report = {};
await page.goto(`${base}?preview=1#/chats`, { waitUntil: "load" });
await page.waitForTimeout(3000);
report.messages = await metrics();
await page.locator(".w4-row").first().click();
await page.waitForTimeout(1800);
report.chat = await metrics();
await page.locator(".w4-composer-input, .w4-composer textarea").first().fill("Performance test message.");
await page.getByRole("button", { name: "发送" }).click();
await page.waitForTimeout(800);
report.afterSend = await metrics();

await page.goto(`${base}?preview=1&state=long#/chat/fixture-1`, { waitUntil: "load" });
await page.waitForTimeout(3200);
report.longThread = await metrics();

await page.goto(`${base}?preview=1#/settings/appearance`, { waitUntil: "load" });
await page.waitForTimeout(2600);
const themeStarted = Date.now();
await page.getByRole("button", { name: "深色" }).click();
await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
const themeMs = Date.now() - themeStarted;
await page.waitForTimeout(500);
report.themeSwitch = { ...(await metrics()), elapsedMs: themeMs };
await page.getByRole("button", { name: "浅色" }).click();
await page.waitForTimeout(300);

await page.goto(`${base}design-preview/`, { waitUntil: "load" });
await page.waitForTimeout(3500);
const auditStart = Date.now();
await page.getByRole("button", { name: /运行 7 组主题对比度审计/ }).click();
await page.waitForFunction(() => document.querySelectorAll(".preview-audit button").length === 7, null, { timeout: 30000 });
report.workerAudit = { elapsedMs: Date.now() - auditStart };
const uploadStart = Date.now();
await page.locator('input[type="file"][accept^="image/"]').setInputFiles("frontend/public/mock/photos/cafe.jpg");
await page.waitForFunction(() => document.querySelectorAll(".preview-candidates button").length > 0, null, { timeout: 30000 });
report.workerImage = { elapsedMs: Date.now() - uploadStart, candidates: await page.locator(".preview-candidates button").count() };

fs.writeFileSync("artifacts/performance-report.json", JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));
await browser.close();
