#!/usr/bin/env node
/**
 * 把 `next build` 的静态导出产物 `out/` 拷到 `backend/app/static/app/`。
 *
 * 这个脚本是「交付物 2」的落地环节：FastAPI 用 StaticFiles 在根路径挂载该目录，
 * 因此 `out/index.html` 必须变成 `backend/app/static/app/index.html`。
 *
 * 安全性：
 *  - 只清理目标目录内**由本脚本管理**的内容（整个 app/ 子目录），
 *    绝不触碰 `backend/app/static/standalone.html` 或其他后端文件。
 *  - 目标路径写死相对 frontend 的上级，避免 cwd 不同导致误删。
 */

import { existsSync, rmSync, mkdirSync, copyFileSync, statSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(HERE, "..");
const OUT_DIR = join(FRONTEND_ROOT, "out");
const BACKEND_STATIC = resolve(FRONTEND_ROOT, "..", "backend", "app", "static");
const TARGET_DIR = join(BACKEND_STATIC, "app");

function fail(message) {
  console.error(`\n[copy-out] ✗ ${message}\n`);
  process.exit(1);
}

function humanSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function dirSize(dir) {
  let total = 0;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) total += dirSize(full);
    else if (entry.isFile()) total += statSync(full).size;
  }
  return total;
}

// ---- 前置校验 -------------------------------------------------------------

if (!existsSync(OUT_DIR)) {
  fail(`找不到静态导出产物：${OUT_DIR}\n         请先运行 \`npm run build\`（需 next.config.ts 中 output: "export"）`);
}

if (!statSync(OUT_DIR).isDirectory()) {
  fail(`${OUT_DIR} 不是目录`);
}

const outIndex = join(OUT_DIR, "index.html");
if (!existsSync(outIndex)) {
  fail(`产物缺少 index.html：${outIndex}\n         静态导出的首页应由 src/app/page.tsx 生成`);
}

// 防御：目标必须位于 backend/app/static 之下
if (!TARGET_DIR.startsWith(BACKEND_STATIC)) {
  fail(`目标路径越界（拒绝删除）：${TARGET_DIR}`);
}

// 防御：绝不删到 backend/app/static 本身或更上层
if (TARGET_DIR === BACKEND_STATIC || TARGET_DIR === resolve(BACKEND_STATIC, "..")) {
  fail(`目标路径过于宽泛（拒绝删除）：${TARGET_DIR}`);
}

// ---- 拷贝 -----------------------------------------------------------------

/**
 * 手写递归复制。
 *
 * 不用 `fs.cpSync(src, dst, { recursive: true })`：在本机 Windows + Node 24 上
 * 该调用会以 0xC0000409 (STATUS_STACK_BUFFER_OVERRUN) 直接崩掉进程。
 * 显式递归 + `copyFileSync` 稳定且便于报错定位。
 */
function copyTree(src, dest, stats) {
  const entries = readdirSync(src, { withFileTypes: true });
  for (const entry of entries) {
    const from = join(src, entry.name);
    const to = join(dest, entry.name);

    if (entry.isSymbolicLink()) {
      // 静态产物不该有符号链接；跳过并提示，避免越界复制
      console.warn(`[copy-out] 跳过符号链接：${from}`);
      continue;
    }

    if (entry.isDirectory()) {
      mkdirSync(to, { recursive: true });
      stats.dirs += 1;
      copyTree(from, to, stats);
    } else if (entry.isFile()) {
      copyFileSync(from, to);
      stats.files += 1;
      stats.bytes += statSync(to).size;
    }
  }
}

const sizeBefore = dirSize(OUT_DIR);

if (existsSync(TARGET_DIR)) {
  rmSync(TARGET_DIR, { recursive: true, force: true });
  console.log(`[copy-out] 已清理旧产物 ${TARGET_DIR}`);
}

mkdirSync(TARGET_DIR, { recursive: true });

const stats = { files: 0, dirs: 0, bytes: 0 };
copyTree(OUT_DIR, TARGET_DIR, stats);

// ---- 校验 -----------------------------------------------------------------

const targetIndex = join(TARGET_DIR, "index.html");
if (!existsSync(targetIndex)) {
  fail(`拷贝后仍是缺少 index.html：${targetIndex}`);
}

const sizeAfter = dirSize(TARGET_DIR);
const hasNextAssets = existsSync(join(TARGET_DIR, "_next"));
const hasWorklet = existsSync(join(TARGET_DIR, "worklets", "pcm-processor.js"));

console.log(`[copy-out] ✓ 源        ${OUT_DIR}`);
console.log(`[copy-out] ✓ 目标      ${TARGET_DIR}`);
console.log(`[copy-out] ✓ index.html 存在`);
console.log(`[copy-out]   文件      ${stats.files} 个 / ${stats.dirs} 个目录`);
console.log(`[copy-out]   _next/    ${hasNextAssets ? "存在" : "缺失（JS/CSS 将 404）"}`);
console.log(`[copy-out]   worklet   ${hasWorklet ? "存在" : "缺失（麦克风将不可用）"}`);
console.log(`[copy-out]   体积      ${humanSize(sizeBefore)} → ${humanSize(sizeAfter)}`);

if (!hasNextAssets) fail("产物缺少 _next/ 目录，后端将无法加载 JS/CSS");
if (!hasWorklet) fail("产物缺少 worklets/pcm-processor.js，录音链路会失效");
