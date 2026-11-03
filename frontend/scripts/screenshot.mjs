#!/usr/bin/env node
/**
 * SpeakEasy 2.0 · 移动端截图工具（390×844，真机视口模拟）。
 *
 * 为什么不直接用 `chrome --headless --window-size=390,844 --screenshot`：
 * macOS 上 Chrome 窗口有最小宽度（≈500px），`--window-size` 会被抬高，
 * 截图裁成 390 宽后看起来像「Tab 栏右侧被裁」。这里用 CDP 的
 * `Emulation.setDeviceMetricsOverride` 精确模拟移动视口，布局宽度就是 390。
 *
 * 用法（frontend/ 下）：
 *   node scripts/screenshot.mjs                          # 默认 http://127.0.0.1:8000 → ../artifacts
 *   node scripts/screenshot.mjs --base http://127.0.0.1:3000 --out ../artifacts --wait 4000
 *   node scripts/screenshot.mjs --width 375               # 375 宽也验证一遍
 *
 * 只依赖 Node 24 内置模块（fetch / WebSocket），无需安装 puppeteer。
 */

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(HERE, "..");

const argv = process.argv.slice(2);
function arg(name, fallback) {
  const index = argv.indexOf(`--${name}`);
  return index >= 0 && argv[index + 1] ? argv[index + 1] : fallback;
}
const BASE = arg("base", process.env.SE_BASE ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const OUT_DIR = resolve(FRONTEND_ROOT, arg("out", "../artifacts"));
const WIDTH = Number(arg("width", "390"));
const HEIGHT = Number(arg("height", "844"));
const WAIT_MS = Number(arg("wait", "4500"));
const PORT = Number(arg("port", "9444"));
const PROFILE = join(process.env.TMPDIR ?? "/tmp", `se-shot-profile-${process.pid}`);

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
].filter(Boolean);
const CHROME = CHROME_CANDIDATES.find((path) => existsSync(path));
if (!CHROME) {
  console.error("[screenshot] 未找到 Chrome/Chromium，可通过 CHROME_PATH 指定");
  process.exit(1);
}

const sleep = (ms) => new Promise((resolvePromise) => setTimeout(resolvePromise, ms));

async function getJson(path, options) {
  const res = await fetch(`http://127.0.0.1:${PORT}${path}`, options);
  if (!res.ok) throw new Error(`CDP HTTP ${res.status} ${path}`);
  return res.json();
}

async function waitForChrome() {
  for (let i = 0; i < 120; i += 1) {
    try {
      await getJson("/json/version");
      return;
    } catch {
      await sleep(100);
    }
  }
  throw new Error("Chrome 启动超时");
}

function connect(wsUrl) {
  return new Promise((resolvePromise, reject) => {
    const ws = new WebSocket(wsUrl);
    const handlers = new Map();
    let nextId = 0;
    ws.onopen = () =>
      resolvePromise({
        send(method, params = {}) {
          const id = ++nextId;
          return new Promise((res, rej) => {
            handlers.set(id, { res, rej });
            ws.send(JSON.stringify({ id, method, params }));
          });
        },
        close() {
          ws.close();
        },
      });
    ws.onerror = reject;
    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id && handlers.has(msg.id)) {
        const { res, rej } = handlers.get(msg.id);
        handlers.delete(msg.id);
        if (msg.error) rej(new Error(msg.error.message));
        else res(msg.result);
      }
    };
  });
}

async function firstFriendId() {
  try {
    const res = await fetch(`${BASE}/api/v2/friends`);
    if (!res.ok) return null;
    const data = await res.json();
    return data?.friends?.[0]?.id ?? null;
  } catch {
    return null;
  }
}

async function capture(cdp, url, outPath) {
  await cdp.send("Page.navigate", { url });
  await sleep(WAIT_MS);
  const shot = await cdp.send("Page.captureScreenshot", { format: "png", fromSurface: true });
  writeFileSync(outPath, Buffer.from(shot.data, "base64"));
  console.log(`[screenshot] ${outPath}`);
}

const chrome = spawn(
  CHROME,
  [
    "--headless=new",
    "--disable-gpu",
    "--no-sandbox",
    "--hide-scrollbars",
    "--no-first-run",
    "--no-default-browser-check",
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${PROFILE}`,
    "about:blank",
  ],
  { stdio: "ignore" },
);

try {
  await waitForChrome();
  const target = await getJson("/json/new?url=about:blank", { method: "PUT" });
  const cdp = await connect(target.webSocketDebuggerUrl);
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: WIDTH,
    height: HEIGHT,
    deviceScaleFactor: 1,
    mobile: true,
    screenWidth: WIDTH,
    screenHeight: HEIGHT,
  });
  await cdp.send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 });

  mkdirSync(OUT_DIR, { recursive: true });
  const size = `${WIDTH}x${HEIGHT}`;
  const views = [
    ["chats", `${BASE}/?tab=chats`],
    ["discover", `${BASE}/?tab=discover`],
    ["notes", `${BASE}/?tab=notes`],
  ];
  const friendId = await firstFriendId();
  if (friendId) views.push(["chat", `${BASE}/?tab=chats&friend=${encodeURIComponent(friendId)}`]);
  // 个人设置页（顶部 ⚙ 的深链，见 docs/V2_STT.md §5）
  views.push(["settings", `${BASE}/?tab=chats&settings=1`]);

  for (const [name, url] of views) {
    await capture(cdp, url, join(OUT_DIR, `v2-${name}-${size}.png`));
  }

  const metrics = await cdp.send("Runtime.evaluate", {
    expression: "JSON.stringify({ innerWidth: window.innerWidth, docWidth: document.documentElement.scrollWidth })",
    returnByValue: true,
  });
  console.log(`[screenshot] viewport=${WIDTH} metrics=${metrics.result?.value}`);
  cdp.close();
} catch (err) {
  console.error("[screenshot] 失败：", err);
  process.exitCode = 1;
} finally {
  chrome.kill("SIGKILL");
  await sleep(120);
  rmSync(PROFILE, { recursive: true, force: true });
}
