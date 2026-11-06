/**
 * W4 原型的数据模型。
 *
 * 字段刻意对齐后端 v2 契约（docs/V2_API.md）中真正会显示的字段：
 * 接后端时只需把 `mock.ts` 换成 fetch，组件不用改。
 */

/** 底部/左侧导航对应的分区 */
export type Section = "chats" | "contacts" | "discover" | "notes" | "me";

export type Route =
  | { name: "chats" }
  | { name: "chat"; friendId: string }
  | { name: "discover" }
  | { name: "discoverFeed" }
  | { name: "discoverMoments" }
  | { name: "discoverPeople" }
  | { name: "persona"; personaId: string }
  | { name: "events" }
  | { name: "moments" }
  | { name: "contacts" }
  | { name: "contact"; friendId: string }
  | { name: "expressions" }
  | { name: "me" }
  | { name: "settings"; section?: string }
  | { name: "requests" };

export interface Friend {
  id: string;
  name: string;
  /** 后端捏人时定下的性别（male/female），用于匹配头像与声音 */
  gender?: string;
  avatar: string;
  city: string;
  country: string;
  flag: string;
  language: string;
  languageLabel: string;
  timezone: string;
  localTime: string;
  localPeriod: string;
  localActivity: string;
  occupation: string;
  age: number;
  bio: string;
  quirks: string[];
  closeness: 1 | 2 | 3 | 4 | 5;
  turns: number;
  sharedEpisodes: string[];
  openLoops: string[];
  unread: number;
  replying?: boolean;
  online?: boolean;
  lastActive: string;
  voice: string;
  voiceLabel: string;
  /** 一句话招呼，加好友前试听用 */
  greeting: string;
  greetingZh: string;
  /** 招呼语音的静态地址：有就直接播，不用现场合成 */
  greetingAudio?: string;
  isRequest?: boolean;
  requestNote?: string;
  requestedAt?: string;
}

export type MessageKind = "voice" | "text" | "image" | "call" | "system";

export interface Suggestion {
  better: string;
  why: string;
  tag: "地道" | "自然" | "礼貌" | "语法";
  saved: boolean;
}

export interface Message {
  id: string;
  friendId: string;
  from: "me" | "friend" | "system";
  kind: MessageKind;
  text: string;
  translation?: string;
  shownText?: boolean;
  durationMs?: number;
  /** 真实语音 mp3 地址（接后端时才有） */
  audio?: string;
  image?: string;
  time: string;
  suggestion?: Suggestion;
  read?: boolean;
}

export interface MomentComment {
  id: string;
  friendId: string;
  text: string;
  time: string;
}

export interface Moment {
  id: string;
  authorId: string;
  images: string[];
  text: string;
  translation?: string;
  mood: string;
  place?: string;
  time: string;
  comments: MomentComment[];
  likedBy: string[];
  liked?: boolean;
  /** 我发的动态：AI 好友评论可能还在生成 */
  commentsPending?: boolean;
}

export interface Expression {
  id: string;
  original: string;
  better: string;
  why: string;
  sourceFriendId: string;
  createdAt: string;
  status: "learning" | "mastered";
  mastery: number;
  /** 需要复习的程度 0-100，越高越该练 */
  due: number;
  lastPracticed?: string;
}

export interface Profile {
  name: string;
  avatar: string;
  city: string;
  job: string;
  targets: string[];
  level: "刚开始" | "能聊几句" | "日常对话" | "比较流利";
  interests: string[];
  /** 性别：用于生成头像（male/female/other/空） */
  gender?: "male" | "female" | "";
  /** 母语（语言代码，如 zh/en） */
  nativeLanguage?: string;
  /** 各在练语言的当前水平：{ en: "能聊几句", de: "刚开始" } */
  levels?: Record<string, string>;
  weaknesses: string[];
  /** 生日 YYYY-MM-DD（用来匹配年龄相仿的人） */
  birthday?: string;
}
