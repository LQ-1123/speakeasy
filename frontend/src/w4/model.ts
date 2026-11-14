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

export type MessageKind = "voice" | "text" | "image" | "call" | "system" | "ad";

/** 聊天里的商业合作段落元数据（服务端审核模板 + 投放记录）。 */
export interface MessageAd {
  delivery_id: string;
  campaign_id?: string;
  campaign_version?: number;
  brand_name?: string;
  label?: string;
  landing_url?: string;
  facts?: string[];
  persona_id?: string;
}

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
  /** 好友语音仍在后台合成：气泡显示语音形态，等音频补全后可直接播放 */
  audioPending?: boolean;
  audioStatus?: "queued" | "generating" | "ready" | "failed";
  audioError?: string;
  image?: string;
  time: string;
  suggestion?: Suggestion;
  read?: boolean;
  /** 客户端生成的消息 id：把乐观消息和服务端消息对齐，避免重复发送/重复显示 */
  clientId?: string;
  /** 乐观消息发送失败：气泡显示「重试」，而不是永远停在「发送中」 */
  failed?: boolean;
  /** 商业合作段落元数据（kind="ad" 时存在） */
  ad?: MessageAd;
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
  /**
   * 首次引导是否已完成。
   *
   * 只来自后端 profile.profile_setup_completed_at：
   *  - true  = 已完成
   *  - false = 后端明确说未完成（Shell 这时才展示 Onboarding）
   *  - undefined = 还不知道（bootstrap 未完成 / fixture），不展示引导，避免闪屏
   */
  setupCompleted?: boolean;
  /** 希望语伴如何纠错：natural（自然聊，不打断）/ occasional（偶尔纠正）/ improve（帮我改进表达）；"" = 未设置 */
  communicationPreference?: "natural" | "occasional" | "improve" | "";
  /** 引导完成时间（后端 profile_setup_completed_at 原样） */
  setupCompletedAt?: string;
}

export interface DiscoverCandidate {
  id: string;
  name: string;
  avatar: string;
  age: number;
  city: string;
  country: string;
  flag: string;
  language: string;
  languageLabel: string;
  occupation: string;
  bio: string;
  greeting: string;
  greetingZh: string;
  /** 真实后端返回的打招呼音频地址（mock 数据没有） */
  greetingAudio?: string;
  voiceLabel: string;
  accent: string;
  online: boolean;
  tags: string[];
}
