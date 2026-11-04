/**
 * 跨端小工具：时间、时长、语言名、头像色（缺图时用）。
 */

export function clockTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** 聊天列表右上角那种「13:27 / 昨天 / 9月22日」。 */
export function listTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date(); // 用真实当前时间：接后端后消息就是「现在」
  const diff = now.getTime() - d.getTime();
  const day = 86_400_000;
  if (diff < 0 || diff < 60_000) return "刚刚";
  if (d.toDateString() === now.toDateString()) return clockTime(iso);
  if (diff < 2 * day) return "昨天";
  if (diff < 7 * day) return ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][d.getDay()];
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

export function dayOf(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return "今天";
  const y = new Date(now.getTime() - 86_400_000);
  if (d.toDateString() === y.toDateString()) return "昨天";
  const diff = Math.round((now.getTime() - d.getTime()) / 86_400_000);
  if (diff < 7) return ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][d.getDay()];
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

export function durationLabel(ms = 0): string {
  const s = Math.max(1, Math.round(ms / 1000));
  return `0:${String(s).padStart(2, "0")}`;
}

/** 语音条宽度随长度变化（微信/WhatsApp 的老做法）。 */
export function voiceBubbleWidth(ms = 0): number {
  const s = Math.min(60, Math.max(1, ms / 1000));
  return Math.round(118 + (s / 60) * 140);
}

export function initial(name: string): string {
  return (name || "?").trim().charAt(0).toUpperCase();
}

/** 头像缺图时的兜底色：低饱和、偏灰，不像彩色字母头像那样刺眼。 */
const AVA_BG = ["#c9c2b6", "#b7c0c6", "#c7c0cf", "#bfc9bc", "#d3c4b8", "#c2c8d0"];
export function avatarTint(id: string): string {
  let h = 0;
  for (let i = 0; i < id.length; i += 1) h = (h * 31 + id.charCodeAt(i)) % 997;
  return AVA_BG[h % AVA_BG.length];
}

export const LANGUAGES: Array<{ code: string; label: string; flag: string }> = [
  { code: "en", label: "英语", flag: "🇬🇧" },
  { code: "ja", label: "日语", flag: "🇯🇵" },
  { code: "ko", label: "韩语", flag: "🇰🇷" },
  { code: "es", label: "西班牙语", flag: "🇪🇸" },
  { code: "pt", label: "葡萄牙语", flag: "🇵🇹" },
  { code: "it", label: "意大利语", flag: "🇮🇹" },
  { code: "fr", label: "法语", flag: "🇫🇷" },
  { code: "de", label: "德语", flag: "🇩🇪" },
];

export function languageLabel(code: string): string {
  return LANGUAGES.find((l) => l.code === code)?.label ?? "外语";
}

export const CLOSENESS: Record<number, string> = {
  1: "刚认识",
  2: "聊过几次",
  3: "常聊",
  4: "熟人",
  5: "老朋友",
};
