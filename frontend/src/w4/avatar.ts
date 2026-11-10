/**
 * 头像：用 DiceBear 的 personas 画风现场生成（扁平插画，不是真人照片）。
 *
 * 为什么换掉真人照片：
 *   这是「和 AI 练口语」的产品。真人照片会让用户误以为是真实社交，
 *   而且照片池只有 15 张，人一多就必然撞脸 / 掉回首字母。
 *
 * 现在的规则：
 *   seed  = 好友 id（同一个人永远是同一张脸，换设备也不变）
 *   发型  = 按性别收窄候选
 *   胡子  = 男性才可能出现
 *   肤色  = 按国籍粗略对应（东亚/南亚/黑人/白人/拉美）
 *
 * 画风作者：Personas by Draftbit（CC BY 4.0），生成器：DiceBear。
 */

import { createAvatar } from "@dicebear/core";
import {
  adventurer,
  avataaars,
  bigSmile,
  bottts,
  croodles,
  funEmoji,
  lorelei,
  micah,
  miniavs,
  notionists,
  openPeeps,
  personas,
  pixelArt,
  thumbs,
} from "@dicebear/collection";

/** 可选头像风格（设置页展示） */
export const AVATAR_STYLES = [
  { id: "personas", label: "插画人物", collection: personas },
  { id: "avataaars", label: "卡通头像", collection: avataaars },
  { id: "bigSmile", label: "笑脸", collection: bigSmile },
  { id: "bottts", label: "机器人", collection: bottts },
  { id: "croodles", label: "涂鸦", collection: croodles },
  { id: "funEmoji", label: "表情包", collection: funEmoji },
  { id: "lorelei", label: "手绘", collection: lorelei },
  { id: "micah", label: "扁平插画", collection: micah },
  { id: "miniavs", label: "迷你", collection: miniavs },
  { id: "notionists", label: "线稿", collection: notionists },
  { id: "openPeeps", label: "线描人物", collection: openPeeps },
  { id: "adventurer", label: "冒险者", collection: adventurer },
  { id: "pixelArt", label: "像素", collection: pixelArt },
  { id: "thumbs", label: "手势", collection: thumbs },
] as const;

const STYLE_KEY = "w4.avatarStyle.v2";
const DEFAULT_AVATAR_STYLE = "avataaars";
let currentStyle =
  (typeof window !== "undefined" && window.localStorage.getItem(STYLE_KEY)) ||
  DEFAULT_AVATAR_STYLE;

export function getAvatarStyle(): string {
  return currentStyle;
}

export function setAvatarStyle(id: string): void {
  currentStyle = id;
  try {
    window.localStorage.setItem(STYLE_KEY, id);
  } catch {
    /* 忽略隐私模式下的存储失败 */
  }
}

function styleCollection(id: string) {
  const found = AVATAR_STYLES.find((x) => x.id === id) || AVATAR_STYLES[0];
  return found.collection;
}

/** 指定风格生成头像（设置页预览用） */
export function avatarWithStyle(seed: string, styleId: string): string {
  if (styleId === "avataaars") {
    const rendered = renderAvataaars(seed, friendlyAvataaarsOptions(seed));
    if (rendered) return rendered;
  }
  try {
    const avatar = createAvatar(styleCollection(styleId) as never, {
      seed: seed || "anonymous",
      size: 128,
    } as never);
    return avatar.toDataUri();
  } catch {
    return "";
  }
}

/** 女性候选发型（含长发、丸子头、刘海等） */
const HAIR_FEMALE = [
  "long", "extraLong", "bobCut", "bobBangs", "curlyBun", "pigtails",
  "straightBun", "curly", "curlyHighTop", "fade", "cap", "beanie",
] as const;

/** 男性候选发型（短发为主，也允许扎发/帽子） */
const HAIR_MALE = [
  "shortCombover", "shortComboverChops", "buzzcut", "fade", "bald",
  "balding", "curlyHighTop", "mohawk", "sideShave", "cap", "beanie",
] as const;

/** 地域 → 肤色候选（personas 的 skinColor 是 6 位 hex） */
const SKIN_BY_REGION: Record<string, string[]> = {
  east_asian: ["f9d9c2", "f0c8a8", "e8b894", "dcae8c"],
  south_asian: ["c68863", "b5764f", "a86a45", "96593a"],
  black: ["8c5a3c", "7a4a30", "69402a", "573522"],
  latino: ["e0ac7e", "d09a6a", "c08858", "a9754a"],
  white: ["fbe0cd", "f3cfae", "e8bd96", "dcae82"],
};

export type AvatarGender = "male" | "female" | "";

/** 同一份输入永远得到同一张脸：核心就是 seed */
export function diceAvatar(
  seed: string,
  gender: AvatarGender = "",
  region: string = "",
): string {
  const skin = (region && SKIN_BY_REGION[region]) || Object.values(SKIN_BY_REGION).flat();
  const hair =
    gender === "male" ? HAIR_MALE : gender === "female" ? HAIR_FEMALE : [...HAIR_MALE, ...HAIR_FEMALE];

  const options = {
    seed: seed || "anonymous",
    hair,
    skinColor: skin,
    // 只用自然发色：之前会随机出粉色/蓝色头发，和「真实的语伴」不搭
    hairColor: ["2c1b18", "4a312c", "724133", "a55728", "b58143", "d6b370", "e8e1e1"],
    // 只有男性才长胡子；概率给低一点，不然一排人全是络腮胡
    facialHairProbability: gender === "male" ? 28 : 0,
    eyes: ["open", "happy", "wink"],
    mouth: ["smile", "bigSmile", "smirk"],
    body: ["squared"],
    size: 128,
  };

  // DiceBear 的 options 类型按画风逐字面量声明，这里值是动态拼的，只能放宽一次
  const avatar = createAvatar(personas, options as unknown as Parameters<typeof createAvatar>[1]);
  return avatar.toDataUri();
}

/** 国家名 → 上面的地域键（和 api.ts 的区域映射保持一致） */
/** 用 Avataaars + LLM 侧写的部件渲染头像（离线、确定性） */
export function renderAvataaars(seed: string, options?: Record<string, unknown>): string {
  try {
    // DiceBear avataaars 的 style=circle 会输出空画布，这里剔除
    const { style: _style, ...rest } = (options || {}) as Record<string, unknown>;
    const avatar = createAvatar(avataaars as never, {
      seed: seed || "anonymous",
      size: 128,
      ...rest,
    } as never);
    return avatar.toDataUri();
  } catch {
    return "";
  }
}

/* ------------------------------------------------------------------ */
/* 统一头像解析（好友 / 人物 / 动态作者 / 好友申请 / 通知都走这里）        */
/* ------------------------------------------------------------------ */

/** 头像解析输入：friends 行 / persona / 动态作者 / 通知人物的字段并集 */
export type AvatarRaw = {
  /** friends 行 id 或 persona id —— 没有 avatar_seed 时的稳定兜底种子 */
  id?: string | null;
  /** friends 行叫 name，persona/作者叫 nickname */
  name?: string | null;
  nickname?: string | null;
  gender?: string | null;
  /** persona 用 country，friends 行用 nationality */
  country?: string | null;
  nationality?: string | null;
  /** 兜底图：friends 行是 avatar_url，persona 是 avatar（服务端 SVG，只给老客户端/非 JS 场景） */
  avatar?: string | null;
  avatar_url?: string | null;
  avatar_seed?: string | null;
  avatar_style?: string | null;
  avatar_options?: unknown;
};

const AVATAR_CACHE_LIMIT = 256;
const AVATAR_CACHE = new Map<string, string>();
const avatarStats = { renders: 0, hits: 0, misses: 0 };

function cacheGet(key: string): string | undefined {
  const value = AVATAR_CACHE.get(key);
  if (value === undefined) return undefined;
  /* LRU：命中后挪到队尾 */
  AVATAR_CACHE.delete(key);
  AVATAR_CACHE.set(key, value);
  avatarStats.hits += 1;
  return value;
}

function cacheSet(key: string, value: string): void {
  AVATAR_CACHE.set(key, value);
  while (AVATAR_CACHE.size > AVATAR_CACHE_LIMIT) {
    const oldest = AVATAR_CACHE.keys().next().value;
    if (oldest === undefined) break;
    AVATAR_CACHE.delete(oldest);
  }
}

/** 测试/调试用：缓存统计（renders = DiceBear 真实渲染次数） */
export function avatarCacheStats(): {
  renders: number;
  hits: number;
  misses: number;
  cacheSize: number;
} {
  return { ...avatarStats, cacheSize: AVATAR_CACHE.size };
}

/** 清空缓存与统计（测试用） */
export function clearAvatarCache(): void {
  AVATAR_CACHE.clear();
  avatarStats.renders = 0;
  avatarStats.hits = 0;
  avatarStats.misses = 0;
}

/** avatar_options 既可能是 DB 里的 JSON 字符串，也可能已经是对象 */
export function normalizeAvatarOptions(value: unknown): Record<string, unknown> | null {
  if (!value) return null;
  let parsed: unknown = value;
  if (typeof value === "string") {
    const text = value.trim();
    if (!text) return null;
    try {
      parsed = JSON.parse(text);
    } catch {
      return null;
    }
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const out = parsed as Record<string, unknown>;
  return Object.keys(out).length ? out : null;
}

/** 稳定序列化：部件集合相同就得到相同缓存键（不同来源的 JSON key 顺序可能不同） */
function canonicalJson(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalJson);
  if (value && typeof value === "object") {
    const source = value as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(source).sort()) out[key] = canonicalJson(source[key]);
    return out;
  }
  return value;
}

/**
 * 稳定种子：显式 avatar_seed 优先，其次 id，最后名字。
 *
 * 服务端保证 friend 行与 persona 的 avatar_seed 一致（见 `_friend_row_for_persona`
 * 与 `sync_persona_friends`），所以同一个人的两种 payload 会得到同一个 seed；
 * 老 payload 缺 avatar_seed 时退回 id，至少保证「同一份 payload 内稳定」。
 * 预览/老数据里 persona id 可能形如 `persona-3`，而好友行 avatar_seed 就是 `3`：
 * 这里把 `persona-<key>` 归一到 `<key>`，两种 payload 才能命中同一个缓存 key。
 */
export function avatarSeedOf(raw: AvatarRaw): string {
  const explicit = String(raw.avatar_seed ?? "").trim();
  if (explicit) return explicit;
  const id = String(raw.id ?? "").trim();
  if (id) return normalizeIdSeed(id);
  const name = String(raw.name ?? raw.nickname ?? "").trim();
  if (!name) return "";
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || name;
}

function normalizeIdSeed(id: string): string {
  const prefixed = /^persona-(.+)$/.exec(id);
  return prefixed ? prefixed[1] : id;
}

function avatarFallbackOf(raw: AvatarRaw): string {
  return String(raw.avatar ?? raw.avatar_url ?? "").trim();
}

function avatarGenderOf(raw: AvatarRaw): AvatarGender {
  const gender = String(raw.gender ?? "").trim().toLowerCase();
  return gender === "male" || gender === "female" ? gender : "";
}

/**
 * 统一头像解析器：好友列表、聊天、通讯录、信息流作者、人物主页、偶遇、
 * 好友申请、通知头像全部走这一个入口。
 *
 * 为什么必须只有一个入口
 * ----------------------
 * 以前聊天的 `friend.avatar` 由 mapFriend 渲染 Avataaars，但 `assignAvatars`
 * 又用 diceAvatar（personas 画风）把它覆盖；信息流/主页各自调 personaAvatarSrc
 * —— 同一个人在不同页面变成两张脸，而且每次好友轮询都重新跑 DiceBear（慢、闪）。
 *
 * 规则（确定性、离线，不请求任何图片服务）：
 *   1. 有 avatar_options  → Avataaars + 这组部件（服务端侧写结果，跨页面一致）
 *   2. 有 seed/id 无部件  → friendlyAvataaarsOptions(seed, gender)，统一 Avataaars 画风
 *   3. 连 seed 都没有     → 服务端兜底 URL（老数据）→ diceAvatar 插画兜底
 *
 * 缓存 key = seed + style + gender + options（+ 兜底 URL），命中返回同一个字符串：
 * React 轮询时 `<img src>` 不变 → 不重载图片、不闪；部件变了 key 变 → 立刻更新。
 */
export function resolveAvatar(raw?: AvatarRaw | null): string {
  if (!raw) return "";
  const seed = avatarSeedOf(raw);
  const style = String(raw.avatar_style ?? "").trim();
  const gender = avatarGenderOf(raw);
  const options = normalizeAvatarOptions(raw.avatar_options);
  const fallback = avatarFallbackOf(raw);
  const key = JSON.stringify([
    seed,
    style,
    gender,
    options ? canonicalJson(options) : null,
    fallback,
  ]);
  const cached = cacheGet(key);
  if (cached !== undefined) return cached;
  avatarStats.misses += 1;

  let resolved = "";
  if (seed) {
    /* 1：有服务端侧写部件 → 直接用（跨页面 / 跨端一致） */
    if (options) {
      resolved = renderAvataaars(seed, options);
      avatarStats.renders += 1;
    }
    /* 2：没有部件（或部件非法渲染失败）→ 确定性友好部件，仍是 Avataaars 画风 */
    if (!resolved) {
      resolved = renderAvataaars(seed, friendlyAvataaarsOptions(seed, gender));
      avatarStats.renders += 1;
    }
  }
  if (!resolved) resolved = fallback;
  if (!resolved) {
    /* 3：什么都没有才回落 personas 插画，不再把服务端 SVG 当主头像 */
    resolved = diceAvatar(
      seed || "anonymous",
      gender,
      avatarRegionOf(String(raw.country ?? raw.nationality ?? "")),
    );
    avatarStats.renders += 1;
  }
  cacheSet(key, resolved);
  return resolved;
}

/** 兼容老调用点：persona 头像（现在统一走 resolveAvatar） */
export function personaAvatarSrc(
  seed: string,
  style?: string,
  options?: Record<string, unknown>,
  fallback?: string,
): string {
  return resolveAvatar({
    id: seed,
    avatar_seed: seed,
    avatar_style: style,
    avatar_options: options,
    avatar: fallback,
  });
}

export function avatarRegionOf(country: string): string {
  const c = (country || "").toLowerCase();
  if (/china|japan|korea|taiwan|hong kong/.test(c)) return "east_asian";
  if (/india|pakistan|bangladesh|sri lanka|nepal/.test(c)) return "south_asian";
  if (/brazil|mexico|argentina|colombia|chile|peru/.test(c)) return "latino";
  if (/nigeria|kenya|ghana|south africa|ethiopia/.test(c)) return "black";
  if (/italy|spain|portugal|greece|france|germany|netherlands|sweden|norway|denmark|finland|poland|ireland|united kingdom|england|scotland|wales|united states|canada|australia|new zealand/.test(c))
    return "white";
  return "";
}

/** mock 里那批名字 → 性别/地域，供 avatarForName 使用 */
const NAME_META: Record<string, { gender: AvatarGender; region: string }> = {
  callum: { gender: "male", region: "white" },
  adrian: { gender: "male", region: "white" },
  finn: { gender: "male", region: "white" },
  haruto: { gender: "male", region: "east_asian" },
  morten: { gender: "male", region: "white" },
  tom: { gender: "male", region: "white" },
  sergio: { gender: "male", region: "white" },
  cian: { gender: "male", region: "white" },
  rohan: { gender: "male", region: "south_asian" },
  mitch: { gender: "male", region: "white" },
  marcus: { gender: "male", region: "white" },
  zihao: { gender: "male", region: "east_asian" },
  matteo: { gender: "male", region: "white" },
  moritz: { gender: "male", region: "white" },
  annika: { gender: "female", region: "white" },
  camila: { gender: "female", region: "latino" },
  giulia: { gender: "female", region: "white" },
  ines: { gender: "female", region: "latino" },
  jiwoo: { gender: "female", region: "east_asian" },
  lin: { gender: "female", region: "east_asian" },
  mei: { gender: "female", region: "east_asian" },
  niamh: { gender: "female", region: "white" },
  rosa: { gender: "female", region: "white" },
  rocio: { gender: "female", region: "white" },
  alba: { gender: "female", region: "white" },
  chiara: { gender: "female", region: "white" },
  lena: { gender: "female", region: "white" },
  ananya: { gender: "female", region: "south_asian" },
  tahlia: { gender: "female", region: "white" },
  freya: { gender: "female", region: "white" },
  chloe: { gender: "female", region: "white" },
};

/** 按名字生成（mock 阶段没有 gender 字段） */
export function avatarForName(name: string): string {
  const slug = (name || "").toLowerCase().replace(/[^a-z]/g, "");
  const meta = NAME_META[slug];
  return diceAvatar(slug || "friend", meta?.gender ?? "", meta?.region ?? "");
}

/** 用户自己的头像：跟随设置里选择的风格 */
/** 自己头像：按 seed 生成一套干净、稳定的 Avataaars 部件（不随机出胡子/墨镜） */
export function friendlyAvataaarsOptions(
  seed: string,
  gender?: string,
): Record<string, unknown> {
  const key = seed || "me";
  let h = 2166136261;
  for (let i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  const pick = <T,>(arr: readonly T[], salt: number) => arr[(h >>> salt) % arr.length];
  const FEMALE_TOPS = ["bob", "longButNotTooLong", "straight01", "straight02", "bun", "curvy"] as const;
  const MALE_TOPS = ["shortFlat", "shortRound", "shortCurly", "shortWaved", "theCaesar", "sides"] as const;
  const tops =
    gender === "female" ? FEMALE_TOPS : gender === "male" ? MALE_TOPS : [...FEMALE_TOPS, ...MALE_TOPS];
  const skin = ["ffdbb4", "edb98a", "f8d25c", "d08b5b", "ae5d29"] as const;
  const hair = ["2c1b18", "4a312c", "724133", "a55728", "b58143", "d6b370"] as const;
  const clothes = ["shirtCrewNeck", "shirtScoopNeck", "collarAndSweater", "hoodie", "blazerAndShirt"] as const;
  const clothesColor = ["262e33", "25557c", "5199e4", "3c4f5c", "929598", "a7ffc4"] as const;
  const mouths = ["smile", "twinkle", "default"] as const;
  const eyes = ["default", "happy", "side"] as const;
  const bg = ["e8f3ec", "eaf0f8", "f8efe6", "f3eaf3", "eef3e6"] as const;
  return {
    top: [pick(tops, 3)],
    topProbability: 100,
    skinColor: [pick(skin, 7)],
    hairColor: [pick(hair, 11)],
    clothing: [pick(clothes, 15)],
    clothesColor: [pick(clothesColor, 19)],
    eyes: [pick(eyes, 23)],
    eyebrows: ["defaultNatural"],
    mouth: [pick(mouths, 27)],
    facialHairProbability: 0,
    accessoriesProbability: 0,
    backgroundColor: [pick(bg, 5)],
  };
}

export function profileAvatar(name: string, gender?: string): string {
  const seed = (name || "me").trim() || "me";
  const rendered = renderAvataaars(seed, friendlyAvataaarsOptions(seed, gender));
  if (rendered) return rendered;
  return diceAvatar(seed);
}
