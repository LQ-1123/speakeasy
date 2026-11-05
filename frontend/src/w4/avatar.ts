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

const STYLE_KEY = "w4.avatarStyle";
let currentStyle =
  (typeof window !== "undefined" && window.localStorage.getItem(STYLE_KEY)) || "personas";

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
  if (currentStyle !== "personas") {
    const avatar = createAvatar(styleCollection(currentStyle) as never, {
      seed: seed || "anonymous",
      size: 128,
    } as never);
    return avatar.toDataUri();
  }
  const avatar = createAvatar(personas, options as unknown as Parameters<typeof createAvatar>[1]);
  return avatar.toDataUri();
}

/** 国家名 → 上面的地域键（和 api.ts 的区域映射保持一致） */
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
