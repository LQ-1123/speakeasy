"use client";

/** 展示层格式化：确定性头像配色、国旗、当地时间、时长、相对时间。 */

import type { Friend, Message } from "@/lib/v2/types";

/* ------------------------------------------------------------------ 头像 */

/** 由 avatar_seed / 名字确定性地派生一组配色（同一好友永远同一头像）。 */
const AVATAR_PALETTES: Array<[string, string]> = [
  ["#7ad0a1", "#2f9e6a"],
  ["#8fb8f7", "#4468d6"],
  ["#f7b27a", "#d9752f"],
  ["#e89ad8", "#b0439b"],
  ["#8fd6df", "#2b8f9e"],
  ["#f2a2a2", "#c95252"],
  ["#b8a6f2", "#6c51c9"],
  ["#e6d27a", "#b2952b"],
  ["#9fd4a8", "#4d8f58"],
  ["#f2c98a", "#b07d2f"],
];

function hashString(input: string): number {
  let h = 2166136261;
  for (let i = 0; i < input.length; i += 1) {
    h ^= input.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return Math.abs(h);
}

export function avatarColors(seed: string): { from: string; to: string; ink: string } {
  const palette = AVATAR_PALETTES[hashString(seed || "seed") % AVATAR_PALETTES.length];
  const [from, to] = palette ?? ["#c8d6c9", "#7fa886"];
  return { from, to, ink: "#ffffff" };
}

export function initialsOf(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return "?";
  const parts = trimmed.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) {
    return `${parts[0]?.[0] ?? ""}${parts[1]?.[0] ?? ""}`.toUpperCase();
  }
  const first = Array.from(trimmed)[0] ?? "?";
  return first.toUpperCase();
}

/* ------------------------------------------------------------------ 国旗 */

const NATIONALITY_FLAGS: Record<string, string> = {
  italy: "🇮🇹",
  italian: "🇮🇹",
  germany: "🇩🇪",
  german: "🇩🇪",
  france: "🇫🇷",
  french: "🇫🇷",
  spain: "🇪🇸",
  spanish: "🇪🇸",
  japan: "🇯🇵",
  japanese: "🇯🇵",
  korea: "🇰🇷",
  southkorea: "🇰🇷",
  korean: "🇰🇷",
  brazil: "🇧🇷",
  brazilian: "🇧🇷",
  portugal: "🇵🇹",
  portuguese: "🇵🇹",
  mexico: "🇲🇽",
  mexican: "🇲🇽",
  china: "🇨🇳",
  chinese: "🇨🇳",
  usa: "🇺🇸",
  "united states": "🇺🇸",
  america: "🇺🇸",
  american: "🇺🇸",
  uk: "🇬🇧",
  "united kingdom": "🇬🇧",
  british: "🇬🇧",
  england: "🇬🇧",
  canada: "🇨🇦",
  canadian: "🇨🇦",
  australia: "🇦🇺",
  australian: "🇦🇺",
  netherlands: "🇳🇱",
  dutch: "🇳🇱",
  ireland: "🇮🇪",
  irish: "🇮🇪",
  sweden: "🇸🇪",
  swedish: "🇸🇪",
  norway: "🇳🇴",
  poland: "🇵🇱",
  russia: "🇷🇺",
  russian: "🇷🇺",
  turkey: "🇹🇷",
  turkish: "🇹🇷",
  india: "🇮🇳",
  indian: "🇮🇳",
  thailand: "🇹🇭",
  vietnam: "🇻🇳",
  indonesia: "🇮🇩",
  singapore: "🇸🇬",
  egypt: "🇪🇬",
  morocco: "🇲🇦",
  argentina: "🇦🇷",
  chile: "🇨🇱",
  colombia: "🇨🇴",
};

const LANG_FLAGS: Record<string, string> = {
  it: "🇮🇹",
  de: "🇩🇪",
  fr: "🇫🇷",
  es: "🇪🇸",
  ja: "🇯🇵",
  ko: "🇰🇷",
  pt: "🇧🇷",
  en: "🇬🇧",
  zh: "🇨🇳",
  ru: "🇷🇺",
  tr: "🇹🇷",
  nl: "🇳🇱",
  sv: "🇸🇪",
  pl: "🇵🇱",
  ar: "🇸🇦",
};

export function flagOf(friend: Pick<Friend, "nationality" | "native_lang">): string {
  const nat = friend.nationality.trim().toLowerCase();
  if (nat) {
    const direct = NATIONALITY_FLAGS[nat];
    if (direct) return direct;
    const compact = nat.replace(/[^a-z]/g, "");
    const byCompact = NATIONALITY_FLAGS[compact];
    if (byCompact) return byCompact;
  }
  return LANG_FLAGS[friend.native_lang.trim().toLowerCase()] ?? "🌍";
}

const LANG_LABELS: Record<string, string> = {
  en: "英语",
  it: "意大利语",
  de: "德语",
  fr: "法语",
  es: "西班牙语",
  ja: "日语",
  ko: "韩语",
  pt: "葡萄牙语",
  zh: "中文",
  ru: "俄语",
  tr: "土耳其语",
  nl: "荷兰语",
  sv: "瑞典语",
  pl: "波兰语",
  ar: "阿拉伯语",
  "es-mx": "西班牙语（墨西哥）",
};

export interface LangMeta {
  flag: string;
  label: string;
}

/** 语言 → 旗帜 + 母语写法（好友语言标签用）。 */
const LANG_META: Record<string, LangMeta> = {
  en: { flag: "🇬🇧", label: "English" },
  ja: { flag: "🇯🇵", label: "日本語" },
  ko: { flag: "🇰🇷", label: "한국어" },
  pt: { flag: "🇧🇷", label: "Português" },
  de: { flag: "🇩🇪", label: "Deutsch" },
  fr: { flag: "🇫🇷", label: "Français" },
  es: { flag: "🇪🇸", label: "Español" },
  "es-mx": { flag: "🇲🇽", label: "Español (MX)" },
  it: { flag: "🇮🇹", label: "Italiano" },
  zh: { flag: "🇨🇳", label: "中文" },
  ru: { flag: "🇷🇺", label: "Русский" },
  tr: { flag: "🇹🇷", label: "Türkçe" },
  nl: { flag: "🇳🇱", label: "Nederlands" },
  sv: { flag: "🇸🇪", label: "Svenska" },
  pl: { flag: "🇵🇱", label: "Polski" },
  ar: { flag: "🇸🇦", label: "العربية" },
};

export function languageMeta(code: string): LangMeta {
  const key = (code || "").trim().toLowerCase();
  if (!key) return LANG_META.en ?? { flag: "🌐", label: "English" };
  const direct = LANG_META[key];
  if (direct) return direct;
  const base = key.split("-")[0] ?? "";
  const baseMeta = LANG_META[base];
  if (baseMeta) return baseMeta;
  return { flag: "🌐", label: key.toUpperCase() };
}

/** 好友当前用来回复的语言：优先后端 reply_language，回退 native_lang。 */
export function replyLanguageOf(friend: Pick<Friend, "reply_language" | "native_lang">): string {
  const lang = (friend.reply_language || friend.native_lang || "en").trim().toLowerCase();
  return lang || "en";
}

export function replyLanguageMeta(friend: Pick<Friend, "reply_language" | "native_lang">): LangMeta {
  return languageMeta(replyLanguageOf(friend));
}

/** 国家原值（后端 country）→ 旗帜 + 中文名；Discover 筛选项展示用。 */
const COUNTRY_META: Record<string, LangMeta> = {
  "united states": { flag: "🇺🇸", label: "美国" },
  "united kingdom": { flag: "🇬🇧", label: "英国" },
  japan: { flag: "🇯🇵", label: "日本" },
  korea: { flag: "🇰🇷", label: "韩国" },
  "south korea": { flag: "🇰🇷", label: "韩国" },
  brazil: { flag: "🇧🇷", label: "巴西" },
  mexico: { flag: "🇲🇽", label: "墨西哥" },
  italy: { flag: "🇮🇹", label: "意大利" },
  germany: { flag: "🇩🇪", label: "德国" },
  france: { flag: "🇫🇷", label: "法国" },
  spain: { flag: "🇪🇸", label: "西班牙" },
  australia: { flag: "🇦🇺", label: "澳大利亚" },
  india: { flag: "🇮🇳", label: "印度" },
  ireland: { flag: "🇮🇪", label: "爱尔兰" },
  china: { flag: "🇨🇳", label: "中国" },
  canada: { flag: "🇨🇦", label: "加拿大" },
  netherlands: { flag: "🇳🇱", label: "荷兰" },
  sweden: { flag: "🇸🇪", label: "瑞典" },
  norway: { flag: "🇳🇴", label: "挪威" },
  poland: { flag: "🇵🇱", label: "波兰" },
  portugal: { flag: "🇵🇹", label: "葡萄牙" },
  russia: { flag: "🇷🇺", label: "俄罗斯" },
  turkey: { flag: "🇹🇷", label: "土耳其" },
  thailand: { flag: "🇹🇭", label: "泰国" },
  vietnam: { flag: "🇻🇳", label: "越南" },
  indonesia: { flag: "🇮🇩", label: "印度尼西亚" },
  singapore: { flag: "🇸🇬", label: "新加坡" },
  argentina: { flag: "🇦🇷", label: "阿根廷" },
  chile: { flag: "🇨🇱", label: "智利" },
  colombia: { flag: "🇨🇴", label: "哥伦比亚" },
  egypt: { flag: "🇪🇬", label: "埃及" },
  morocco: { flag: "🇲🇦", label: "摩洛哥" },
};

export function countryMeta(country: string): LangMeta {
  const key = (country || "").trim().toLowerCase();
  const meta = COUNTRY_META[key];
  if (meta) return meta;
  return { flag: "🌍", label: country || "不限" };
}

export function langLabel(code: string): string {
  const key = code.trim().toLowerCase();
  return LANG_LABELS[key] ?? LANG_LABELS[key.split("-")[0] ?? ""] ?? code.toUpperCase();
}

/* ------------------------------------------------------------------ 时间 */

interface LocalState {
  time: string;
  period: string;
  icon: string;
  activity: string;
}

const PERIOD_ICONS: Record<string, string> = {
  morning: "🌅",
  afternoon: "☕",
  evening: "🌆",
  night: "🌙",
  late_night: "🌙",
};

/** 优先用后端派生字段；缺失时按 timezone 用 Intl 现算，保证任何情况下都有值。 */
export function localStateOf(friend: Friend, now: Date = new Date()): LocalState {
  let time = friend.local_time.trim();
  let period = friend.local_period.trim();
  if (!time || !period) {
    try {
      if (friend.timezone) {
        const fmt = new Intl.DateTimeFormat("en-GB", {
          timeZone: friend.timezone,
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        });
        time = fmt.format(now);
        const hourStr = new Intl.DateTimeFormat("en-GB", {
          timeZone: friend.timezone,
          hour: "2-digit",
          hour12: false,
        }).format(now);
        const hour = Number(hourStr) || 0;
        period =
          hour < 5 ? "late_night" : hour < 12 ? "morning" : hour < 18 ? "afternoon" : hour < 23 ? "evening" : "night";
      }
    } catch {
      /* 非法时区，忽略 */
    }
  }
  if (!time) {
    time = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false }).format(now);
  }
  const icon = friend.local_icon.trim() || PERIOD_ICONS[period] || "🕒";
  return { time, period, icon, activity: friend.local_activity.trim() };
}

export function durationLabel(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export function clockLabel(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }).format(d);
}

export function relativeLabel(iso: string, now: Date = new Date()): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const diff = now.getTime() - d.getTime();
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return clockLabel(iso);
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return "昨天";
  const diffDays = Math.floor(diff / 86_400_000);
  if (diffDays < 7) return `${diffDays} 天前`;
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(d);
}

/** 聊天气泡里的时间分隔：超过 10 分钟才显示，避免刷屏。 */
export function shouldShowDayDivider(prev: Message | undefined, current: Message): boolean {
  if (!prev) return true;
  const a = new Date(prev.created_at).getTime();
  const b = new Date(current.created_at).getTime();
  if (Number.isNaN(a) || Number.isNaN(b)) return false;
  return b - a > 10 * 60 * 1000;
}

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

export function chatDayLabel(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return clockLabel(iso);
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return `昨天 ${clockLabel(iso)}`;
  return `${d.getMonth() + 1}月${d.getDate()}日 ${WEEKDAYS[d.getDay()] ?? ""} ${clockLabel(iso)}`;
}

/* ------------------------------------------------------------------ 其它 */

export function closenessLabel(level: number): string {
  if (level >= 5) return "挚友";
  if (level === 4) return "熟络";
  if (level === 3) return "常聊";
  if (level === 2) return "相识";
  if (level === 1) return "初识";
  return "初识";
}

/** 由消息 id 派生稳定的波形高度数组（voice bubble 视觉）。 */
export function waveformBars(seed: string, count = 16): number[] {
  let h = hashString(seed || "seed");
  const bars: number[] = [];
  for (let i = 0; i < count; i += 1) {
    h = (h * 1103515245 + 12345) & 0x7fffffff;
    const value = 22 + (h % 78);
    bars.push(value);
  }
  return bars;
}

export function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message;
  if (typeof err === "string" && err) return err;
  return "操作失败，请稍后重试";
}

export type { LocalState };
