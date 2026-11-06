/**
 * Avatar Lab 入口：把 DiceBear 全部画风 + 若干「非 DiceBear」算法头像
 * 打包成浏览器可直接用的 window.AvatarLab（由 esbuild 打成 IIFE）。
 */

import { createAvatar } from "@dicebear/core";

import * as adventurer from "@dicebear/adventurer";
import * as adventurerNeutral from "@dicebear/adventurer-neutral";
import * as avataaars from "@dicebear/avataaars";
import * as avataaarsNeutral from "@dicebear/avataaars-neutral";
import * as bigEars from "@dicebear/big-ears";
import * as bigEarsNeutral from "@dicebear/big-ears-neutral";
import * as bigSmile from "@dicebear/big-smile";
import * as bottts from "@dicebear/bottts";
import * as botttsNeutral from "@dicebear/bottts-neutral";
import * as croodles from "@dicebear/croodles";
import * as croodlesNeutral from "@dicebear/croodles-neutral";
import * as dylan from "@dicebear/dylan";
import * as funEmoji from "@dicebear/fun-emoji";
import * as glass from "@dicebear/glass";
import * as icons from "@dicebear/icons";
import * as identicon from "@dicebear/identicon";
import * as initials from "@dicebear/initials";
import * as lorelei from "@dicebear/lorelei";
import * as loreleiNeutral from "@dicebear/lorelei-neutral";
import * as micah from "@dicebear/micah";
import * as miniavs from "@dicebear/miniavs";
import * as notionists from "@dicebear/notionists";
import * as notionistsNeutral from "@dicebear/notionists-neutral";
import * as openPeeps from "@dicebear/open-peeps";
import * as personas from "@dicebear/personas";
import * as pixelArt from "@dicebear/pixel-art";
import * as pixelArtNeutral from "@dicebear/pixel-art-neutral";
import * as rings from "@dicebear/rings";
import * as shapes from "@dicebear/shapes";
import * as thumbs from "@dicebear/thumbs";
import * as toonHead from "@dicebear/toon-head";

const STYLES = {
  personas,
  avataaars,
  "avataaars-neutral": avataaarsNeutral,
  adventurer,
  "adventurer-neutral": adventurerNeutral,
  "big-ears": bigEars,
  "big-ears-neutral": bigEarsNeutral,
  "big-smile": bigSmile,
  openPeeps,
  lorelei,
  "lorelei-neutral": loreleiNeutral,
  micah,
  miniavs,
  notionists,
  "notionists-neutral": notionistsNeutral,
  croodles,
  "croodles-neutral": croodlesNeutral,
  bottts,
  "bottts-neutral": botttsNeutral,
  "fun-emoji": funEmoji,
  thumbs,
  dylan,
  pixelArt,
  "pixel-art-neutral": pixelArtNeutral,
  identicon,
  initials,
  rings,
  shapes,
  glass,
  icons,
  toonHead,
};

/* ------------------------------------------------------------------ */
/* 哈希 / 随机数（确定性）                                             */
/* ------------------------------------------------------------------ */
function cyrb128(str) {
  let h1 = 1779033703, h2 = 3144134277, h3 = 1013904242, h4 = 2773480762;
  for (let i = 0, k; i < str.length; i++) {
    k = str.charCodeAt(i);
    h1 = h2 ^ Math.imul(h1 ^ k, 597399067);
    h2 = h3 ^ Math.imul(h2 ^ k, 2869860233);
    h3 = h4 ^ Math.imul(h3 ^ k, 951274213);
    h4 = h1 ^ Math.imul(h4 ^ k, 2716044179);
  }
  h1 = Math.imul(h3 ^ (h1 >>> 18), 597399067);
  h2 = Math.imul(h4 ^ (h2 >>> 22), 2869860233);
  h3 = Math.imul(h1 ^ (h3 >>> 17), 951274213);
  h4 = Math.imul(h2 ^ (h4 >>> 19), 2716044179);
  return [h1 >>> 0, h2 >>> 0, h3 >>> 0, h4 >>> 0];
}

function mulberry32(a) {
  return function () {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function rngOf(seed) {
  const [a] = cyrb128("avatar-lab:" + seed);
  const rand = mulberry32(a);
  return {
    rand,
    pick: (arr) => arr[Math.floor(rand() * arr.length)],
    chance: (p) => rand() < p,
    int: (min, max) => min + Math.floor(rand() * (max - min + 1)),
    hue: () => Math.floor(rand() * 360),
  };
}

/* ------------------------------------------------------------------ */
/* 非 DiceBear：GitHub 风 Identicon（5x5 镜像像素）                    */
/* ------------------------------------------------------------------ */
function identiconSvg(seed, size = 96) {
  const [h] = cyrb128("identicon:" + seed);
  const r = rngOf(seed);
  const hue = (h >>> 7) % 360;
  const fg = `hsl(${hue} 58% 42%)`;
  const bg = `hsl(${hue} 30% 96%)`;
  const n = 5;
  const cell = size / (n + 2);
  const ox = cell;
  let rects = "";
  const grid = [];
  for (let y = 0; y < n; y++) {
    grid[y] = [];
    for (let x = 0; x < Math.ceil(n / 2); x++) grid[y][x] = r.chance(0.5);
  }
  for (let y = 0; y < n; y++) {
    for (let x = 0; x < n; x++) {
      const v = grid[y][Math.min(x, n - 1 - x)];
      if (v) rects += `<rect x="${(ox + x * cell).toFixed(1)}" y="${(ox + y * cell).toFixed(1)}" width="${cell.toFixed(1)}" height="${cell.toFixed(1)}"/>`;
    }
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}"><rect width="${size}" height="${size}" fill="${bg}"/><g fill="${fg}">${rects}</g></svg>`;
}

/* ------------------------------------------------------------------ */
/* 非 DiceBear：Blockies（8x8 镜像像素，4 色调色板）                    */
/* ------------------------------------------------------------------ */
function blockiesSvg(seed, size = 96) {
  const r = rngOf("blockies:" + seed);
  const baseHue = r.hue();
  const palette = [
    `hsl(${baseHue} 55% 45%)`,
    `hsl(${(baseHue + 40) % 360} 55% 55%)`,
    `hsl(${(baseHue + 120) % 360} 60% 40%)`,
  ];
  const bg = `hsl(${baseHue} 25% 94%)`;
  const n = 8;
  const cell = size / n;
  let rects = "";
  for (let y = 0; y < n; y++) {
    for (let x = 0; x < n; x++) {
      const mx = Math.min(x, n - 1 - x);
      const pick = r.int(0, 9);
      if (pick < 4) continue;
      const color = palette[Math.min(palette.length - 1, Math.floor((pick - 4) / 2))];
      rects += `<rect x="${(x * cell).toFixed(1)}" y="${(y * cell).toFixed(1)}" width="${cell.toFixed(1)}" height="${cell.toFixed(1)}" fill="${color}"/>`;
    }
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}"><rect width="${size}" height="${size}" fill="${bg}"/>${rects}</svg>`;
}

/* ------------------------------------------------------------------ */
/* 非 DiceBear：Jazzicon（钱包风：渐变圆盘叠加）                        */
/* ------------------------------------------------------------------ */
function jazziconSvg(seed, size = 96) {
  const r = rngOf("jazzicon:" + seed);
  const c = size / 2;
  let shapes = "";
  const count = r.int(3, 4);
  for (let i = 0; i < count; i++) {
    const hue = r.hue();
    const radius = size * (0.28 + r.rand() * 0.3);
    const dx = (r.rand() - 0.5) * size * 0.5;
    const dy = (r.rand() - 0.5) * size * 0.5;
    const id = `jz${i}`;
    shapes += `<radialGradient id="${id}" cx="35%" cy="30%" r="75%"><stop offset="0%" stop-color="hsl(${hue} 75% 72%)"/><stop offset="100%" stop-color="hsl(${hue} 70% 40%)"/></radialGradient><circle cx="${(c + dx).toFixed(1)}" cy="${(c + dy).toFixed(1)}" r="${radius.toFixed(1)}" fill="url(#${id})" opacity="0.9"/>`;
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}"><rect width="${size}" height="${size}" rx="${size * 0.18}" fill="hsl(${r.hue()} 30% 92%)"/>${shapes}</svg>`;
}

/* ------------------------------------------------------------------ */
/* 非 DiceBear：Minidenticon（单色像素脸）                              */
/* ------------------------------------------------------------------ */
function minidenticonSvg(seed, size = 96) {
  const r = rngOf("minidenticon:" + seed);
  const hue = r.hue();
  const color = `hsl(${hue} 55% ${r.int(38, 52)}%)`;
  const n = 5;
  const cell = size / (n + 1);
  const ox = cell / 2;
  let rects = "";
  for (let y = 0; y < n; y++) {
    for (let x = 0; x < n; x++) {
      const mx = Math.min(x, n - 1 - x);
      if (!r.chance(0.42)) continue;
      rects += `<rect x="${(ox + x * cell).toFixed(1)}" y="${(ox + y * cell).toFixed(1)}" width="${(cell * 0.9).toFixed(1)}" height="${(cell * 0.9).toFixed(1)}" rx="${(cell * 0.16).toFixed(1)}"/>`;
    }
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}"><rect width="${size}" height="${size}" fill="hsl(${hue} 25% 95%)"/><g fill="${color}">${rects}</g></svg>`;
}

/* ------------------------------------------------------------------ */
/* 非 DiceBear：Boring Avatars（大理石几何风）                          */
/* ------------------------------------------------------------------ */
function boringSvg(seed, size = 96) {
  const r = rngOf("boring:" + seed);
  const bg = `hsl(${r.hue()} 18% 88%)`;
  let shapes = "";
  for (let i = 0; i < 3; i++) {
    const hue = r.hue();
    const radius = size * (0.22 + r.rand() * 0.22);
    shapes += `<circle cx="${(size * 0.3 + r.rand() * size * 0.4).toFixed(1)}" cy="${(size * 0.3 + r.rand() * size * 0.4).toFixed(1)}" r="${radius.toFixed(1)}" fill="hsl(${hue} 45% ${50 + i * 8}%)" opacity="0.72"/>`;
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}"><rect width="${size}" height="${size}" fill="${bg}"/>${shapes}</svg>`;
}

/* ------------------------------------------------------------------ */
/* 本项目自研：SeedFace（扁平插画人像，服务端 avatar.py 的简化版）      */
/* ------------------------------------------------------------------ */
const SKIN = ["#ffdbac", "#f1c27d", "#e0ac69", "#c68642", "#8d5524", "#f5d0c5", "#e8b89a"];
const HAIR = ["#2c1b18", "#3a2418", "#4a312c", "#5a3a24", "#724133", "#8a4b2a", "#a55728", "#b58143"];
const SHIRT = ["#3f6f57", "#2f5d8a", "#8a5a2f", "#7a3f4f", "#4b4b6a", "#2f7a6a", "#6a4a8a", "#8a6a2f"];
const BG = [
  ["#e8f3ec", "#cfe6d8"], ["#eaf0f8", "#d4e0f0"], ["#f8efe6", "#f0dcc8"],
  ["#f3eaf3", "#e4d4e8"], ["#eef3e6", "#dcead0"], ["#f6eaea", "#f0d6d6"],
];

const SKIN_MAP = {
  east_asian: ["#f5d0c5", "#f0c8b4", "#e8b89a", "#f7d9c4"],
  south_asian: ["#c68642", "#a8683a", "#8d5524", "#b5713f"],
  latino: ["#e0ac69", "#c68642", "#d4956a", "#b97a4a"],
  black: ["#8d5524", "#6b4226", "#a26a42", "#7a4a2b"],
  white: ["#ffdbac", "#f1c27d", "#e0ac69", "#f7d6b5"],
};
const HAIR_MAP = {
  east_asian: ["#1c1410", "#2c1b18", "#3a2418", "#241a14"],
  south_asian: ["#1c1410", "#2c1b18", "#3a2418"],
  latino: ["#1c1410", "#2c1b18", "#3a2418", "#4a312c"],
  black: ["#1c1410", "#2c1b18", "#33231b"],
  white: ["#2c1b18", "#4a312c", "#724133", "#a55728", "#b58143", "#8a3b1f"],
};
const GRAY_HAIR = ["#9b9b9b", "#b9b9b9", "#d8d8d8", "#cfcfcf"];
const SHIRT_PALETTE = ["#3f6f57", "#2f5d8a", "#8a5a2f", "#7a3f4f", "#4b4b6a", "#2f7a6a", "#6a4a8a", "#8a6a2f"];
const BG_PALETTE = [["#e8f3ec", "#cfe6d8"], ["#eaf0f8", "#d4e0f0"], ["#f8efe6", "#f0dcc8"], ["#f3eaf3", "#e4d4e8"], ["#eef3e6", "#dcead0"], ["#f6eaea", "#f0d6d6"]];

function ageBand(age) {
  const a = Number(age);
  if (!isFinite(a) || a <= 0) return "adult";
  return a < 25 ? "young" : a < 33 ? "adult" : "mature";
}

function seedFaceSvg(seed, opts) {
  opts = opts || {};
  const gender = (opts.gender || "").toLowerCase();
  const region = (opts.region || "").toLowerCase();
  const band = ageBand(opts.age);

  const r = rngOf("seedface:" + seed + "|" + gender + "|" + region + "|" + band);
  const skinList = SKIN_MAP[region] || [].concat(SKIN_MAP.white, SKIN_MAP.east_asian, SKIN_MAP.latino, SKIN_MAP.south_asian, SKIN_MAP.black);
  const hairList = HAIR_MAP[region] || HAIR_MAP.white;
  const skin = r.pick(skinList);
  const grayChance = { young: 0, adult: 8, mature: 38 }[band];
  const hair = r.chance(grayChance / 100) ? r.pick(GRAY_HAIR) : r.pick(hairList);
  const shirt = r.pick(SHIRT_PALETTE);
  const bg = r.pick(BG_PALETTE);

  let stylePool;
  if (region === "black") stylePool = ["afro", "curly", "buzz", "short"];
  else if (region === "east_asian") stylePool = ["short", "side", "bob", "long", "bun"];
  else if (region === "south_asian") stylePool = ["short", "side", "long", "bun", "bob"];
  else stylePool = ["short", "side", "bob", "long", "bun", "curly", "ponytail", "buzz"];

  let hairStyle;
  if (gender === "female") {
    let pool = stylePool.filter(function (x) { return x !== "buzz" && x !== "afro"; });
    if (!pool.length) pool = stylePool;
    if (band === "mature") {
      const maturePool = pool.filter(function (x) { return ["bob", "bun", "short", "side"].indexOf(x) >= 0; });
      if (maturePool.length) pool = maturePool;
    }
    hairStyle = r.pick(pool);
  } else if (gender === "male") {
    const pool = stylePool.filter(function (x) { return ["long", "bob", "ponytail", "bun"].indexOf(x) < 0; });
    hairStyle = r.pick(pool.length ? pool : stylePool);
  } else {
    hairStyle = r.pick(stylePool);
  }

  const glasses = r.chance({ young: 10, adult: 16, mature: 32 }[band] / 100);
  const beard = gender === "male" && r.chance({ young: 16, adult: 24, mature: 20 }[band] / 100);
  const earrings = gender === "female" && r.chance({ young: 34, adult: 26, mature: 22 }[band] / 100);
  const blush = band !== "mature";
  const lip = gender === "female" ? "#b0564f" : "#a4544b";
  const eyes = r.pick(["open", "happy", "wink", "closed"]);
  const mouth = r.pick(["smile", "bigSmile", "smirk", "neutral"]);

  const hairShape = {
    short: '<path d="M24 46c-2-15 9-25 24-25s26 10 24 25c-1 5-3 5-5 3-2-9-10-14-19-14s-17 5-19 14c-2 2-4 2-5-3z" fill="' + hair + '"/>',
    side: '<path d="M23 45c0-14 10-24 25-24 12 0 21 7 23 18 1 4-1 6-4 4-4-7-12-11-21-11-7 0-13 2-18 7-3 2-5 1-5-4z" fill="' + hair + '"/>',
    bob: '<path d="M20 46c0-16 12-26 27-26s27 10 27 26c0 9-4 13-7 13-1 0-2-2-2-5 0-10-8-17-18-17s-18 7-18 17c0 3-1 5-2 5-3 0-7-4-7-13z" fill="' + hair + '"/>',
    long: '<path d="M19 47c0-17 12-28 28-28s28 11 28 28c0 12-2 19-4 22-2 2-5 1-5-2 1-14 1-24-1-30-3-7-9-12-18-12s-15 5-18 12c-2 6-2 16-1 30 0 3-3 4-5 2-2-3-4-10-4-22z" fill="' + hair + '"/>',
    bun: '<circle cx="48" cy="18" r="7" fill="' + hair + '"/><path d="M25 44c0-13 10-22 23-22s23 9 23 22c0 3-2 4-4 2-4-7-11-11-19-11s-15 4-19 11c-2 2-4 1-4-2z" fill="' + hair + '"/>',
    curly: '<g fill="' + hair + '"><circle cx="30" cy="33" r="9"/><circle cx="48" cy="25" r="10"/><circle cx="66" cy="33" r="9"/><circle cx="22" cy="44" r="7"/><circle cx="74" cy="44" r="7"/></g>',
    afro: '<g fill="' + hair + '"><circle cx="48" cy="22" r="12"/><circle cx="32" cy="29" r="10"/><circle cx="64" cy="29" r="10"/><circle cx="22" cy="40" r="8"/><circle cx="74" cy="40" r="8"/></g>',
    ponytail: '<path d="M25 45c0-14 10-24 23-24s23 10 23 24c0 3-2 4-4 2-4-8-11-13-19-13s-15 5-19 13c-2 2-4 1-4-2z" fill="' + hair + '"/><path d="M72 38c6 3 9 9 9 17s-4 12-7 12c-3 0-4-2-3-4 2-4 3-8 3-12 0-5-1-9-3-11z" fill="' + hair + '"/>',
  }[hairStyle];

  const eyeShape = {
    open: '<ellipse cx="40" cy="55" rx="2.6" ry="3.4" fill="#3a2b24"/><ellipse cx="56" cy="55" rx="2.6" ry="3.4" fill="#3a2b24"/>',
    happy: '<path d="M35 56q4-4 8 0" stroke="#3a2b24" stroke-width="2.2" fill="none" stroke-linecap="round"/><path d="M52 56q4-4 8 0" stroke="#3a2b24" stroke-width="2.2" fill="none" stroke-linecap="round"/>',
    wink: '<ellipse cx="40" cy="55" rx="2.6" ry="3.4" fill="#3a2b24"/><path d="M53 55q4 3 8 0" stroke="#3a2b24" stroke-width="2.2" fill="none" stroke-linecap="round"/>',
    closed: '<path d="M35 55q4 3 8 0" stroke="#3a2b24" stroke-width="2.2" fill="none" stroke-linecap="round"/><path d="M52 55q4 3 8 0" stroke="#3a2b24" stroke-width="2.2" fill="none" stroke-linecap="round"/>',
  }[eyes];

  const mouthShape = {
    smile: '<path d="M41 68q7 6 14 0" stroke="' + lip + '" stroke-width="2.4" fill="none" stroke-linecap="round"/>',
    bigSmile: '<path d="M40 67q8 9 16 0" stroke="' + lip + '" stroke-width="2.6" fill="none" stroke-linecap="round"/>',
    smirk: '<path d="M42 69q7 5 14-2" stroke="' + lip + '" stroke-width="2.3" fill="none" stroke-linecap="round"/>',
    neutral: '<path d="M43 69h11" stroke="' + lip + '" stroke-width="2.2" stroke-linecap="round"/>',
  }[mouth];

  const wrinkleColor = shadeHex(skin, 0.82);
  let wrinkles = "";
  if (band !== "young") {
    const op = band === "mature" ? 0.9 : 0.5;
    wrinkles =
      '<path d="M34 62q3 2 6 0" stroke="' + wrinkleColor + '" stroke-width="1" fill="none" opacity="' + op + '" stroke-linecap="round"/>' +
      '<path d="M56 62q3 2 6 0" stroke="' + wrinkleColor + '" stroke-width="1" fill="none" opacity="' + op + '" stroke-linecap="round"/>';
    if (band === "mature") {
      wrinkles += '<path d="M38 71q5 2 10 0" stroke="' + wrinkleColor + '" stroke-width="0.9" fill="none" opacity="0.55" stroke-linecap="round"/>';
    }
  }

  return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="96" height="96">' +
    '<defs><linearGradient id="sf-bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="' + bg[0] + '"/><stop offset="1" stop-color="' + bg[1] + '"/></linearGradient></defs>' +
    '<rect width="96" height="96" fill="url(#sf-bg)"/>' +
    '<path d="M13 96c0-14 15-22 35-22s35 8 35 22z" fill="' + shirt + '"/>' +
    '<rect x="42" y="70" width="12" height="10" rx="5" fill="' + skin + '" opacity="0.95"/>' +
    '<ellipse cx="48" cy="54" rx="20" ry="23" fill="' + skin + '"/>' +
    '<ellipse cx="28" cy="55" rx="3.4" ry="4.5" fill="' + skin + '"/>' +
    '<ellipse cx="68" cy="55" rx="3.4" ry="4.5" fill="' + skin + '"/>' +
    hairShape +
    '<path d="M34 47q5-3 10 1" stroke="' + shadeHex(hair, 0.8) + '" stroke-width="1.9" fill="none" stroke-linecap="round"/>' +
    '<path d="M52 48q5-3 10-1" stroke="' + shadeHex(hair, 0.8) + '" stroke-width="1.9" fill="none" stroke-linecap="round"/>' +
    '<path d="M48 59v4" stroke="' + shadeHex(skin, 0.85) + '" stroke-width="1.6" stroke-linecap="round"/>' +
    eyeShape + mouthShape + wrinkles +
    (glasses ? '<g stroke="#2f2f33" stroke-width="1.6" fill="none"><rect x="32" y="50" width="16" height="11" rx="4"/><rect x="50" y="50" width="16" height="11" rx="4"/><path d="M48 54h2M32 53l-4-2M66 53l4-2"/></g>' : "") +
    (beard ? '<path d="M34 62c0 10 6 16 14 16s14-6 14-16c-4 4-9 6-14 6s-10-2-14-6z" fill="' + shadeHex(hair, 0.85) + '" opacity="0.9"/>' : "") +
    (earrings ? '<circle cx="27" cy="61" r="1.8" fill="#d8b55a"/><circle cx="69" cy="61" r="1.8" fill="#d8b55a"/>' : "") +
    (blush ? '<ellipse cx="34" cy="63" rx="3.6" ry="2.2" fill="#e08a7d" opacity="0.22"/><ellipse cx="62" cy="63" rx="3.6" ry="2.2" fill="#e08a7d" opacity="0.22"/>' : "") +
    "</svg>";
}

function shadeHex(hex, factor) {
  const h = (hex || "#000000").replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  const to = function (c) { return Math.max(0, Math.floor(c * factor)).toString(16).padStart(2, "0"); };
  return "#" + to(r) + to(g) + to(b);
}

/* 对外 API                                                            */
/* ------------------------------------------------------------------ */
function dicebearSvg(name, seed, size = 96) {
  const style = STYLES[name];
  if (!style) return "";
  const avatar = createAvatar(style, { seed: String(seed), size });
  return avatar.toString();
}

const CUSTOM = {
  identicon: identiconSvg,
  blockies: blockiesSvg,
  jazzicon: jazziconSvg,
  minidenticon: minidenticonSvg,
  "boring-marble": boringSvg,
  "seed-face": seedFaceSvg,
};

function dicebearSvgRaw(name, seed, opts) {
  const style = STYLES[name];
  if (!style) return "";
  return createAvatar(style, Object.assign({ seed: String(seed) }, opts || {})).toString();
}

window.AvatarLab = { styles: Object.keys(STYLES), custom: Object.keys(CUSTOM), dicebearSvg, dicebearSvgRaw, customSvg: (k, s, opts) => (CUSTOM[k] ? (k === "seed-face" ? seedFaceSvg(s, opts) : CUSTOM[k](s)) : ""), rngOf, cyrb128, ageBand, avatarRegions: ["east_asian", "south_asian", "latino", "black", "white"] };
