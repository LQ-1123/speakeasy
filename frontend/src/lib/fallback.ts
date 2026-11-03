import type { ConversationMode, PersonaPublic } from "@/lib/types";

/**
 * 内置兜底角色 —— 与 `backend/app/personas/personas.json` 的 `public_list()` 字段一致。
 *
 * 用途：
 *  - 后端未启动 / `/api/personas` 请求失败时，页面仍然完整可看、可点（不空白）。
 *  - 一旦 `ready` 或 `/api/personas` 返回真实角色，立即覆盖。
 *
 * 故意**不**包含 system_prompt / voice 等私有字段：前端拿不到也不需要。
 */
export const FALLBACK_PERSONAS: PersonaPublic[] = [
  {
    id: "oliver",
    name: "Oliver",
    title: "伦敦独立咖啡师",
    scene: "一家 Soho 的独立咖啡馆。你在点单，Oliver 站在吧台后面，手上有活儿但很乐意闲聊。",
    accent: "British RP（标准英音 / 伦敦中产腔）",
    accent_short: "British RP",
    flag: "🇬🇧",
    city: "London",
    difficulty: "easy",
    tags: ["coffee", "daily-life", "small-talk"],
    traits: ["wry", "warm", "brief"],
    opening_line: "Morning! What can I get you?",
  },
  {
    id: "raj",
    name: "Raj",
    title: "班加罗尔软件工程师",
    scene: "一个线上技术面试 / 项目同步会。Raj 是你的印度同事，正在和你 review 一个 API 设计。",
    accent: "Indian English（印度英语 / 班加罗尔 IT 口音）",
    accent_short: "Indian English",
    flag: "🇮🇳",
    city: "Bengaluru",
    difficulty: "medium",
    tags: ["work", "tech", "interview"],
    traits: ["brisk", "pragmatic", "technical"],
    opening_line: "Hey, you're on? Great. So I wanted to walk you through the API design — got five minutes?",
  },
  {
    id: "marco",
    name: "Marco",
    title: "罗马平面设计师",
    scene: "罗马 Trastevere 的一家小酒馆，晚上。Marco 是你刚认识的朋友，正在等你对一件设计的看法。",
    accent: "Italian English（意大利英语 / 罗马口音）",
    accent_short: "Italian English",
    flag: "🇮🇹",
    city: "Rome",
    difficulty: "hard",
    tags: ["design", "culture", "opinion"],
    traits: ["expressive", "opinionated", "theatrical"],
    opening_line: "Allora — you saw the poster? Tell me honestly. Too much, no?",
  },
  {
    id: "emily",
    name: "Emily",
    title: "纽约招聘官 / 面试教练",
    scene: "一场模拟的北美求职面试。Emily 是纽约一家公司的 recruiter，正在做第一轮 screening。",
    accent: "American English（通用美音 / 纽约）",
    accent_short: "American English",
    flag: "🇺🇸",
    city: "New York",
    difficulty: "medium",
    tags: ["interview", "career", "professional"],
    traits: ["crisp", "professional", "probing"],
    opening_line: "Hi, thanks for hopping on. Before we dive in — tell me a little about yourself.",
  },
  {
    id: "priya",
    name: "Priya",
    title: "新加坡产品经理",
    scene: "一个跨时区的产品评审会。Priya 在新加坡，风格直接高效，正在和你确认需求边界。",
    accent: "Singapore English（新加坡英语 / 略带华语腔）",
    accent_short: "Singapore English",
    flag: "🇸🇬",
    city: "Singapore",
    difficulty: "hard",
    tags: ["work", "product", "decision"],
    traits: ["efficient", "direct", "pragmatic"],
    opening_line: "Okay, quick one — the scope for this sprint, can or cannot?",
  },
];

export const FALLBACK_MODES: ConversationMode[] = [
  { id: "free", name: "自由闲聊", description: "随意聊，角色会主动带话题。" },
  { id: "task", name: "任务达成", description: "你有一个具体目标（点单/面试/说服对方），角色会设障碍。" },
  { id: "drill", name: "表达特训", description: "角色会刻意重复使用一种句式/时态，逼你模仿。" },
];

/** 难度 → 色点颜色（唯一强调色体系内的层级，不引入新色相） */
export const DIFFICULTY_COLOR: Record<string, string> = {
  easy: "var(--accent-ok)",
  medium: "var(--accent-warn)",
  hard: "var(--accent-danger)",
};

export const DIFFICULTY_LABEL: Record<string, string> = {
  easy: "入门",
  medium: "进阶",
  hard: "挑战",
};

export const REGISTER_LABEL: Record<string, string> = {
  neutral: "中性",
  casual: "口语",
  formal: "正式",
};
