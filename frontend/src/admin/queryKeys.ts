/**
 * react-query key 工厂。
 *
 * 集中管理的原因：失效（invalidate）需要按前缀批量命中，
 * 手写字符串很容易写错一个字母导致"改了但界面不刷新"。
 */

export const adminKeys = {
  session: () => ["admin", "session"] as const,

  overview: () => ["admin", "overview"] as const,

  users: (query: { search?: string; offset?: number; limit?: number; sort?: string } = {}) =>
    ["admin", "users", query] as const,
  user: (userId: string) => ["admin", "user", userId] as const,
  userFriends: (userId: string) => ["admin", "user", userId, "friends"] as const,
  userDevices: (userId: string) => ["admin", "user", userId, "devices"] as const,
  userPosts: (userId: string) => ["admin", "user", userId, "posts"] as const,

  events: (query: Record<string, unknown> = {}) => ["admin", "events", query] as const,
  eventPool: (query: Record<string, unknown> = {}) => ["admin", "eventPool", query] as const,
  eventPoolPreview: (draft: unknown) => ["admin", "eventPool", "preview", draft] as const,

  postsPending: () => ["admin", "posts", "pending"] as const,
  postsPublished: (query: Record<string, unknown> = {}) => ["admin", "posts", "published", query] as const,

  mediaLibrary: (query: Record<string, unknown> = {}) => ["admin", "media", "library", query] as const,
  mediaStats: () => ["admin", "media", "stats"] as const,

  personas: (query: Record<string, unknown> = {}) => ["admin", "personas", query] as const,
  persona: (personaId: string) => ["admin", "persona", personaId] as const,
  personaPrompt: (personaId: string, mode: string) =>
    ["admin", "persona", personaId, "prompt", mode] as const,
  promptTemplates: () => ["admin", "promptTemplates"] as const,

  lexicons: () => ["admin", "lexicons"] as const,
  lexicon: (name: string) => ["admin", "lexicon", name] as const,
  /** 词条使用统计（哪个取值被多少角色用）。改词库后要一起失效。 */
  usage: () => ["admin", "lexicon-usage"] as const,

  voices: () => ["admin", "voices"] as const,

  config: () => ["admin", "config"] as const,
  release: () => ["admin", "release"] as const,

  jobs: (query: Record<string, unknown> = {}) => ["admin", "jobs", query] as const,
  jobKinds: () => ["admin", "jobs", "kinds"] as const,

  audit: (query: Record<string, unknown> = {}) => ["admin", "audit", query] as const,
};
