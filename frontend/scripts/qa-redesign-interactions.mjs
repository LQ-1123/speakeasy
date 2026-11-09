import { createRequire } from "node:module";
import fs from "node:fs";
const require = createRequire(import.meta.url);
let playwright;
try { playwright = require(process.env.PLAYWRIGHT_CORE_PATH || "playwright-core"); } catch {
  console.error("Install playwright-core or set PLAYWRIGHT_CORE_PATH.");
  process.exit(2);
}
const { chromium } = playwright;
const OUT = process.env.QA_SCREENSHOT_DIR || "artifacts/shots/qa";
fs.mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch({
  executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
  headless: true,
  args: ["--disable-gpu", "--no-sandbox"],
});
const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
const page = await context.newPage();
const consoleErrors = [];
page.on("console", (msg) => {
  if (msg.type() === "error" && !msg.text().includes("WebSocket")) consoleErrors.push(msg.text());
});
page.on("pageerror", (err) => consoleErrors.push(`PAGEERROR ${err.message}`));

const base = process.env.QA_BASE_URL || "http://127.0.0.1:4173/";
const result = { checks: [], overflows: [], shots: [] };
function check(name, pass, detail = "") {
  result.checks.push({ name, pass, detail });
  if (!pass) console.log("FAIL:", name, detail);
}
let navigation = 0;
async function open(route, width = 390, height = 844) {
  await page.setViewportSize({ width, height });
  navigation += 1;
  await page.goto(`${base}?preview=1&_qa=${navigation}${route}`, { waitUntil: "load" });
  await page.waitForTimeout(2800);
}
async function overflow(label) {
  const data = await page.evaluate(() => ({
    viewport: innerWidth,
    scroll: document.documentElement.scrollWidth,
    els: (() => {
      const list = [];
      if (document.documentElement.scrollWidth <= innerWidth + 1) return list;
      for (const el of Array.from(document.querySelectorAll("*"))) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && (r.right > innerWidth + 1 || r.left < -1)) {
          list.push({
            cls: typeof el.className === "string" ? el.className : "",
            tag: el.tagName,
            left: Math.round(r.left),
            right: Math.round(r.right),
          });
        }
      }
      return list.slice(0, 5);
    })(),
  }));
  result.overflows.push({ label, ...data });
  check(`${label}: no horizontal overflow`, data.scroll <= data.viewport + 1, JSON.stringify(data.els));
}
async function shot(name) {
  const path = `${OUT}/${name}.png`;
  await page.screenshot({ path });
  result.shots.push(path);
}

/* interactions */
await open("#/chats");
await shot("01-chats-390");
check("messages title", (await page.locator(".w4-list-title").innerText()).includes("消息"));
check("conversation rows render", (await page.locator(".w4-row").count()) > 5, String(await page.locator(".w4-row").count()));

await page.getByRole("button", { name: "搜索" }).click();
const search = page.getByRole("textbox", { name: "搜索好友、城市或消息" });
await search.fill("Giulia");
await page.waitForTimeout(400);
check("search filters", (await page.locator(".w4-row").count()) === 1, String(await page.locator(".w4-row").count()));
await shot("02-search");
await search.fill("");
await page.getByRole("button", { name: "搜索" }).click();

await page.locator(".w4-row").first().click();
await page.waitForTimeout(1400);
check("chat opens", (await page.locator(".se-chat-head").count()) === 1);
check("bubble sides", (await page.locator(".w4-msg.is-me").count()) > 0 && (await page.locator(".w4-msg:not(.is-me)").count()) > 0);
const before = await page.getByRole("textbox").count();
await page.locator(".w4-composer-input, .w4-composer textarea").first().fill("This is a QA message.");
await page.getByRole("button", { name: "发送" }).click();
await page.waitForTimeout(1000);
check("send adds message", (await page.getByText("This is a QA message.").count()) > 0);
check("composer still present", (await page.getByRole("textbox").count()) >= before);
await shot("03-chat-sent");

await page.getByRole("button", { name: "语音通话" }).click();
await page.waitForTimeout(1000);
check("call starts", (await page.locator(".se-call-panel").count()) === 1);
await shot("04-call");
await page.getByRole("button", { name: "收起通话" }).click();
await page.waitForTimeout(700);
check("call dock in flow", (await page.locator(".se-call-dock").count()) === 1);
const dockBox = await page.locator(".se-call-dock").boundingBox();
const composerBox = await page.locator(".w4-composer").boundingBox();
check(
  "dock does not overlap composer",
  Boolean(dockBox && composerBox && dockBox.y + dockBox.height <= composerBox.y + 1),
  JSON.stringify({ dockBox, composerBox }),
);
await page.locator(".se-call-dock-main").click();
await page.waitForTimeout(600);
check("call expands", (await page.locator(".se-call-panel").count()) === 1);
await page.getByRole("button", { name: "结束通话" }).click();
await page.waitForTimeout(700);
check("call ends", (await page.locator(".se-call-panel").count()) === 0);

await page.locator(".se-chat-head .w4-icon-btn").first().click();
await page.waitForTimeout(700);
check("back returns to list", (await page.locator(".w4-list-title").innerText()).includes("消息"));

/* discover */
await page.getByRole("button", { name: "发现" }).click();
await page.waitForTimeout(1600);
check("discover loads", (await page.locator(".disc-post").count()) > 0);
await shot("05-discover-390");
const likeButton = page.getByRole("button", { name: /^赞/ }).first();
if (await likeButton.count()) {
  await likeButton.click();
  await page.waitForTimeout(500);
}
await page.getByRole("button", { name: /发动态/ }).click();
await page.waitForTimeout(500);
check("publish dialog opens", (await page.getByRole("dialog").count()) > 0);
await page.screenshot({ path: `${OUT}/06-publish.png` });
const close = page.getByRole("button", { name: "关闭" }).last();
if (await close.count()) await close.click();
await page.waitForTimeout(300);

/* me + settings */
await page.getByRole("button", { name: "我", exact: true }).click();
await page.waitForTimeout(900);
check("me loads", await page.getByText("小林").first().isVisible());
await shot("07-me-390");
await page.getByRole("button", { name: /外观与触感/ }).click();
await page.waitForTimeout(900);
check("settings loads", (await page.getByText("主题", { exact: true }).count()) > 0);
await shot("08-settings-390");
await page.getByRole("button", { name: "深色" }).click();
await page.waitForTimeout(800);
check("dark theme applied", (await page.locator("html").getAttribute("data-theme")) === "dark");
await shot("09-settings-dark");
await page.getByRole("button", { name: "浅色" }).click();
await page.waitForTimeout(400);

/* notification */
await open("#/me");
await page.evaluate(() => {
  window.dispatchEvent(new CustomEvent("speakeasy:client-event", {
    detail: [{ id: "qa-notification", sequence: 99, type: "message", friend_id: "fixture-1", payload: { name: "Callum", text: "Are you free for a quick call?", route: "#/chat/fixture-1" } }],
  }));
});
await page.waitForTimeout(900);
check("global notification appears", (await page.locator(".se-global-notification").count()) === 1);
if (await page.locator(".se-global-notification").count()) await shot("10-notification");

/* width matrix */
const routes = ["#/chats", "#/chat/fixture-1", "#/discover", "#/me"];
for (const route of routes) {
  for (const width of [320, 360, 390, 430, 600, 768, 960, 1024, 1440]) {
    await open(route, width, width < 600 ? 844 : 900);
    await overflow(`${route}@${width}`);
  }
}
/* font scale matrix */
for (const route of ["#/chats", "#/chat/fixture-1", "#/discover"]) {
  for (const scale of ["0.85", "1", "1.3", "1.6", "2"]) {
    await open(route, 390, 844);
    await page.evaluate((value) => {
      document.documentElement.dataset.fontScale = value;
      document.documentElement.style.setProperty("--font-scale", value);
    }, scale);
    await page.waitForTimeout(400);
    await overflow(`${route}@390-font${scale}`);
  }
}
/* keyboard + dock */
await open("#/chat/fixture-1");
await page.getByRole("button", { name: "语音通话" }).click();
await page.waitForTimeout(700);
await page.getByRole("button", { name: "收起通话" }).click();
await page.waitForTimeout(500);
await page.evaluate(() => {
  document.documentElement.style.setProperty("--ime-height", "240px");
  window.dispatchEvent(new Event("resize"));
});
await page.waitForTimeout(600);
const layout = await page.evaluate(() => {
  const composer = document.querySelector(".w4-composer")?.getBoundingClientRect();
  const dock = document.querySelector(".se-call-dock")?.getBoundingClientRect();
  const send = document.querySelector(".w4-send-btn")?.getBoundingClientRect();
  return {
    vh: innerHeight,
    composer: composer && { top: composer.top, bottom: composer.bottom },
    dock: dock && { top: dock.top, bottom: dock.bottom },
    send: send && { top: send.top, bottom: send.bottom },
  };
});
check("keyboard + dock: send visible", Boolean(layout.send && layout.composer && layout.send.bottom <= layout.composer.bottom + 1 && layout.send.bottom <= layout.vh), JSON.stringify(layout));
check("keyboard + dock: no overlap", Boolean(layout.dock && layout.composer && layout.dock.bottom <= layout.composer.top), JSON.stringify(layout));
await page.screenshot({ path: `${OUT}/11-keyboard-call-dock.png` });

/* safe area insets */
await open("#/chats");
await page.evaluate(() => {
  document.documentElement.style.setProperty("--safe-top", "44px");
  document.documentElement.style.setProperty("--safe-bottom", "34px");
  window.dispatchEvent(new Event("resize"));
});
await page.waitForTimeout(500);
const safeArea = await page.evaluate(() => {
  const app = document.querySelector(".w4-app")?.getBoundingClientRect();
  const title = document.querySelector(".w4-list-title")?.getBoundingClientRect();
  const tab = document.querySelector(".w4-tabbar")?.getBoundingClientRect();
  return { appTop: app?.top, titleTop: title?.top, tabBottom: tab?.bottom, vh: innerHeight };
});
check("safe area keeps header below top inset", Boolean(safeArea.titleTop && safeArea.titleTop >= 44), JSON.stringify(safeArea));
check("safe area keeps tab bar above bottom edge", Boolean(safeArea.tabBottom && safeArea.tabBottom <= safeArea.vh), JSON.stringify(safeArea));
await page.screenshot({ path: `${OUT}/12-safe-area.png` });

/* virtualization, state coverage and platform details */
await open("#/chats");
const normalRows = await page.locator(".w4-row").count();
await page.goto(`${base}?preview=1&state=long#/chats`, { waitUntil: "load" });
await page.waitForTimeout(2400);
const longRows = await page.locator(".w4-row").count();
check("long friend list stays virtualized", longRows < 30 && longRows >= normalRows, JSON.stringify({ normalRows, longRows }));

await page.goto(`${base}?preview=1&state=long#/chat/fixture-1`, { waitUntil: "load" });
await page.waitForTimeout(2600);
const longMessages = await page.locator(".w4-msg").count();
check("1000 messages stay virtualized", longMessages > 0 && longMessages < 40, String(longMessages));

await page.goto(`${base}?preview=1&state=empty#/chats`, { waitUntil: "load" });
await page.waitForTimeout(2200);
check("empty state renders", await page.getByText("还没有会话").isVisible());
await page.goto(`${base}?preview=1&state=error#/chats`, { waitUntil: "load" });
await page.waitForTimeout(2200);
check("error state renders", (await page.locator(".se-connection-error").count()) === 1 || (await page.locator(".se-empty").count()) === 1);

await page.emulateMedia({ reducedMotion: "reduce" });
await open("#/chats");
const reduced = await page.locator(".w4-main").evaluate((el) => getComputedStyle(el).animationDuration);
check("reduced motion is respected", parseFloat(reduced) < 0.01, reduced);
await page.emulateMedia({ reducedMotion: "no-preference" });

await open("#/chat/fixture-1");
await page.getByRole("button", { name: "语音通话" }).click();
await page.waitForTimeout(900);
check("call has three controls", (await page.locator(".se-call-control").count()) === 3);
const callOpacity = await page.locator(".se-call-panel").evaluate((el) => getComputedStyle(el).backgroundColor);
check("call surface is opaque", !callOpacity.includes("rgba(0, 0, 0, 0)") && !callOpacity.endsWith(", 0)"), callOpacity);
await page.getByRole("button", { name: "结束通话" }).click();
await page.waitForTimeout(500);

await open("#/discover");
await page.getByRole("button", { name: /发动态/ }).click();
await page.waitForTimeout(500);
const outline = await page.locator(".disc-publish-text").evaluate((el) => getComputedStyle(el).outlineStyle);
check("publish textarea has no square focus outline", outline === "none", outline);
await page.getByRole("button", { name: "关闭" }).last().click();
await page.waitForTimeout(300);

/* theme switch makes no API requests */
await open("#/settings/appearance");
const apiBefore = await page.evaluate(() => performance.getEntriesByType("resource").filter((e) => e.name.includes("/api/")).length);
await page.getByRole("button", { name: "深色" }).click();
await page.waitForTimeout(800);
const apiAfter = await page.evaluate(() => performance.getEntriesByType("resource").filter((e) => e.name.includes("/api/")).length);
check("theme switch sends no API requests", apiAfter === apiBefore, JSON.stringify({ apiBefore, apiAfter }));

/* narrow + largest font */
await open("#/chats", 320, 844);
await page.evaluate(() => {
  document.documentElement.dataset.fontScale = "2";
  document.documentElement.style.setProperty("--font-scale", "2");
});
await page.waitForTimeout(500);
await overflow("#/chats@320-font2");
await open("#/chat/fixture-1", 320, 844);
await page.evaluate(() => {
  document.documentElement.dataset.fontScale = "2";
  document.documentElement.style.setProperty("--font-scale", "2");
});
await page.waitForTimeout(500);
await overflow("#/chat@320-font2");

/* theme mutation does not remount */
await open("#/chats");
await page.evaluate(() => {
  const list = document.querySelector(".w4-list-scroll");
  if (list) list.scrollTop = 240;
  window.__qaList = list;
});
await page.evaluate(() => {
  document.documentElement.style.setProperty("--md-primary", "#B3261E");
  document.documentElement.style.setProperty("--md-primary-container", "#FFDAD6");
});
check("theme change keeps list node", await page.evaluate(() => document.querySelector(".w4-list-scroll") === window.__qaList));

/* review feedback: read aloud / long-press translation / composer outline */
await open("#/chat/fixture-1");
const composer = page.locator(".w4-composer-input");
await composer.click();
await page.waitForTimeout(250);
const composerOutline = await composer.evaluate((el) => getComputedStyle(el).outlineStyle);
check("composer input has no square focus outline", composerOutline === "none", composerOutline);
const readable = page.locator(".w4-bubble.is-readable").first();
await readable.click();
await page.waitForTimeout(350);
check("AI text click starts read-aloud", (await page.locator(".w4-bubble.is-reading").count()) === 1);
check("read-aloud cue switches state", (await page.locator(".w4-read-cue.on").first().innerText()).includes("朗读中"));
await readable.click();
await page.waitForTimeout(150);
await readable.click({ button: "right" });
await page.waitForTimeout(1100);
check("AI text right-click opens translation", (await page.locator(".w4-translation").count()) > 0);

/* review feedback: contacts as its own nav destination */
await open("#/chats", 390, 844);
check("mobile tab bar has four destinations", (await page.locator(".w4-tabbar-btn").count()) === 4);
check("contacts is a mobile tab", (await page.locator(".w4-tabbar-btn", { hasText: "通讯录" }).count()) === 1);
check("no duplicated contacts shortcut in messages", (await page.locator(".se-contacts-link").count()) === 0);
await open("#/discover", 1440, 900);
check("discover sidebar hero box removed", (await page.locator(".disc-index-hero").count()) === 0);
check("discover lead uses unified sans font", await page.locator(".disc-index-lead").evaluate((el) => {
  const font = getComputedStyle(el).fontFamily.toLowerCase();
  return font.includes("sans-serif") && !font.includes('"serif"');
}));

result.consoleErrors = consoleErrors.slice(0, 20);
result.summary = {
  checks: result.checks.length,
  passed: result.checks.filter((c) => c.pass).length,
  failed: result.checks.filter((c) => !c.pass).length,
  overflowRecords: result.overflows.length,
  overflowFailures: result.overflows.filter((o) => o.scroll > o.viewport + 1).length,
};
fs.writeFileSync("artifacts/qa-report.json", JSON.stringify(result, null, 2));
console.log(JSON.stringify(result.summary, null, 2));
for (const c of result.checks) if (!c.pass) console.log("FAIL", c.name, c.detail);
console.log("console errors", consoleErrors.slice(0, 5));
await browser.close();
