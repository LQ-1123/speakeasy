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

const browser = await chromium.launch({
  executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
  headless: true,
  args: ["--disable-gpu", "--no-sandbox"],
});
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 }, colorScheme: "light" });
const errors = [];
page.on("console", (msg) => { if (msg.type() === "error") errors.push(msg.text()); });
page.on("pageerror", (err) => errors.push(`PAGEERROR ${err.message}`));
await page.goto(`${process.env.QA_BASE_URL || "http://127.0.0.1:4173"}/design-preview/`, { waitUntil: "load" });
await page.waitForFunction(() => document.body.innerText.includes("预览已连接"), null, { timeout: 20000 });

const out = {};
// 1. Iframe real app loaded
const desktop = page.frameLocator('iframe[title="桌面预览"]');
const phone = page.frameLocator('iframe[title="手机预览"]');
out.desktopText = (await desktop.locator("body").innerText()).slice(0, 120);
out.phoneText = (await phone.locator("body").innerText()).slice(0, 120);

// 2. Dark mode through the preview bridge
await page.getByRole("button", { name: /深色/ }).click();
await page.waitForTimeout(900);
out.phoneTheme = await phone.locator("html").getAttribute("data-theme");
await page.screenshot({ path: "artifacts/shots/preview-dark.png", fullPage: false });

// 3. Contrast audit with worker
await page.getByRole("button", { name: /运行 7 组主题对比度审计/ }).click();
await page.waitForTimeout(8000);
out.audit = await page.locator(".preview-audit button").allTextContents();
out.auditClass = await page.locator(".preview-audit button").evaluateAll((els) => els.map((el) => el.className));
await page.screenshot({ path: "artifacts/shots/preview-audit.png", fullPage: false });

// 4. Image theme worker
const input = page.locator('input[type="file"][accept^="image/"]');
await input.setInputFiles(fs.existsSync("public/mock/photos/cafe.jpg") ? "public/mock/photos/cafe.jpg" : "frontend/public/mock/photos/cafe.jpg");
await page.waitForTimeout(8000);
out.imageBusy = await page.locator(".preview-drop span").innerText();
out.candidates = await page.locator(".preview-candidates button").count();
if (out.candidates > 0) {
  await page.locator(".preview-candidates button").nth(1).click();
  await page.waitForTimeout(1500);
}
out.currentPalette = await page.locator(".preview-current-palette span").innerText();
await page.screenshot({ path: "artifacts/shots/preview-image-theme.png", fullPage: false });

// 5. narrow phone + 2x font
await page.getByRole("button", { name: "320", exact: true }).click();
await page.locator(".preview-form select").last().selectOption("2");
await page.waitForTimeout(1200);
out.fontScale = await phone.locator("html").getAttribute("data-font-scale");
out.phoneWidth = await phone.locator("html").evaluate(() => innerWidth);
out.phoneScroll = await phone.locator("html").evaluate(() => ({ scroll: document.documentElement.scrollWidth, inner: innerWidth }));
await page.screenshot({ path: "artifacts/shots/preview-font-320.png", fullPage: false });

// 6. Call state
await page.getByRole("button", { name: "410", exact: true }).click().catch(() => undefined);
await page.getByRole("button", { name: "通话中", exact: true }).click();
await page.waitForTimeout(1800);
out.callPanel = await phone.locator(".se-call-panel").count();
await page.getByRole("button", { name: "缩小通话条" }).click().catch(() => undefined);
await page.waitForTimeout(500);
out.callDock = await phone.locator(".se-call-dock").count();
await page.screenshot({ path: "artifacts/shots/preview-call.png", fullPage: false });
await page.getByRole("button", { name: "结束通话" }).click().catch(() => undefined);

out.errors = errors.slice(0, 10);
console.log(JSON.stringify(out, null, 2));
await browser.close();
