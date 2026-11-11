/**
 * 管理台路由：URL（hash）是唯一事实来源。
 *
 * 与 `src/w4/navigation.ts` 同样的做法——纯函数，不碰 history。
 * 单页 + hash 路由的原因：管理台是静态导出（`output: "export"`），
 * 没有服务端路由，path 级深链接需要后端配合；hash 则天然可用，
 * 且刷新/直接访问都不依赖额外的 f)allback 规则。
 */

export type AdminRoute =
  | { name: "overview" }
  | { name: "users" }
  | { name: "userDetail"; userId: string }
  | { name: "worldEvents" }
  | { name: "worldPosts" }
  | { name: "worldMedia" }
  | { name: "worldRhythm" }
  | { name: "personaStudio" }
  | { name: "personaDna" }
  | { name: "personaPool" }
  | { name: "personaVoices" }
  | { name: "personaPrompts" }
  | { name: "personaScenes" }
  | { name: "systemConfig" }
  | { name: "systemRelease" }
  | { name: "systemJobs" }
  | { name: "systemAudit" }
  | { name: "commerceAds" };

/** 侧栏按域分组。 */
export type AdminDomain = "overview" | "users" | "world" | "personas" | "system" | "commerce";

export const DOMAIN_LABEL: Record<AdminDomain, string> = {
  overview: "总览",
  users: "用户",
  world: "AI 世界",
  personas: "捏人",
  system: "系统",
  commerce: "商业运营",
};

export interface NavItem {
  /** 稳定的导航 key，同时用作 React key 与测试选择器。 */
  key: string;
  label: string;
  route: AdminRoute;
  /** 该分区属于哪一期交付；未交付的显示"即将上线"，避免误点后空白。 */
  phase: 1 | 2 | 3;
  /** 该分区一句话说明，用于空态与悬停提示。 */
  hint?: string;
}

export interface NavGroup {
  domain: AdminDomain;
  items: NavItem[];
}

/**
 * 侧栏导航树。顺序即信息架构：总览 → 用户 → AI 世界 → 捏人 → 系统。
 * phase 用来在实现完成前把未交付项标灰，而不是隐藏——运营能提前看到全貌。
 */
export const ADMIN_NAV: NavGroup[] = [
  {
    domain: "overview",
    items: [
      { key: "overview", label: "总览", route: { name: "overview" }, phase: 1, hint: "服务健康、今日产出、后台任务" },
    ],
  },
  {
    domain: "users",
    items: [
      { key: "users", label: "用户列表", route: { name: "users" }, phase: 3, hint: "搜索、分页、好友与设备状态" },
    ],
  },
  {
    domain: "world",
    items: [
      { key: "world.events", label: "事件", route: { name: "worldEvents" }, phase: 3, hint: "公共池 · 角色事件" },
      { key: "world.posts", label: "动态", route: { name: "worldPosts" }, phase: 3, hint: "待发 · 已发 · 写一条" },
      { key: "world.media", label: "图片素材", route: { name: "worldMedia" }, phase: 2, hint: "图库、搜图入库、上传" },
      { key: "world.rhythm", label: "世界节奏", route: { name: "worldRhythm" }, phase: 2, hint: "事件与动态的产出参数" },
    ],
  },
  {
    domain: "personas",
    items: [
      { key: "personas.studio", label: "角色工坊", route: { name: "personaStudio" }, phase: 2, hint: "新建、编辑、批量工具" },
      { key: "personas.dna", label: "表达 DNA", route: { name: "personaDna" }, phase: 2, hint: "全局词库 · 按角色覆盖" },
      { key: "personas.pool", label: "捏人素材池", route: { name: "personaPool" }, phase: 2, hint: "名字 / 职业 / 城市 / 兴趣" },
      { key: "personas.voices", label: "音色池", route: { name: "personaVoices" }, phase: 2, hint: "池管理 · 按角色换音色" },
      { key: "personas.prompts", label: "Prompt 控制台", route: { name: "personaPrompts" }, phase: 3, hint: "按角色改写固定模块 · 模板" },
      { key: "personas.scenes", label: "场景与口头禅", route: { name: "personaScenes" }, phase: 2, hint: "示例场景 · 口头禅池 · 禁用词" },
    ],
  },
  {
    domain: "system",
    items: [
      { key: "system.config", label: "API 与密钥", route: { name: "systemConfig" }, phase: 2, hint: "LLM / STT / TTS / Deepgram / JPush" },
      { key: "system.release", label: "版本发布", route: { name: "systemRelease" }, phase: 2, hint: "APK 上传、强制更新、回滚" },
      { key: "system.jobs", label: "后台任务", route: { name: "systemJobs" }, phase: 1, hint: "定时任务状态与立即执行" },
      { key: "system.audit", label: "操作日志", route: { name: "systemAudit" }, phase: 1, hint: "谁在什么时候改了什么" },
    ],
  },
  {
    domain: "commerce",
    items: [
      {
        key: "commerce.ads",
        label: "商业运营",
        route: { name: "commerceAds" },
        phase: 3,
        hint: "品牌 / 活动 / 素材审核 / 角色分配 / 投放记录",
      },
    ],
  },
];

/** 路由 → 所属域（侧栏高亮用）。 */
export function domainOf(route: AdminRoute): AdminDomain {
  switch (route.name) {
    case "overview":
      return "overview";
    case "users":
    case "userDetail":
      return "users";
    case "worldEvents":
    case "worldPosts":
    case "worldMedia":
    case "worldRhythm":
      return "world";
    case "personaStudio":
    case "personaDna":
    case "personaPool":
    case "personaVoices":
    case "personaPrompts":
    case "personaScenes":
      return "personas";
    case "systemConfig":
    case "systemRelease":
    case "systemJobs":
    case "systemAudit":
      return "system";
    case "commerceAds":
      return "commerce";
  }
}

/** 路由 → hash。这是唯一的编码入口。 */
export function adminRouteToHash(route: AdminRoute): string {
  switch (route.name) {
    case "overview":
      return "#/overview";
    case "users":
      return "#/users";
    case "userDetail":
      return `#/users/${encodeURIComponent(route.userId)}`;
    case "worldEvents":
      return "#/world/events";
    case "worldPosts":
      return "#/world/posts";
    case "worldMedia":
      return "#/world/media";
    case "worldRhythm":
      return "#/world/rhythm";
    case "personaStudio":
      return "#/personas/studio";
    case "personaDna":
      return "#/personas/dna";
    case "personaPool":
      return "#/personas/pool";
    case "personaVoices":
      return "#/personas/voices";
    case "personaPrompts":
      return "#/personas/prompts";
    case "personaScenes":
      return "#/personas/scenes";
    case "systemConfig":
      return "#/system/config";
    case "systemRelease":
      return "#/system/release";
    case "systemJobs":
      return "#/system/jobs";
    case "systemAudit":
      return "#/system/audit";
    case "commerceAds":
      return "#/commerce/ads";
  }
}

/** hash → 路由。无法识别时回落到总览，永不抛异常。 */
export function parseAdminRoute(hash: string): AdminRoute {
  const parts = (hash || "")
    .replace(/^#\/?/, "")
    .split("/")
    .filter(Boolean)
    .map((part) => {
      try {
        return decodeURIComponent(part);
      } catch {
        return part;
      }
    });
  const [head, second, third] = parts;
  switch (head) {
    case "users":
      return second ? { name: "userDetail", userId: second } : { name: "users" };
    case "world":
      switch (second) {
        case "events":
          return { name: "worldEvents" };
        case "posts":
          return { name: "worldPosts" };
        case "media":
          return { name: "worldMedia" };
        case "rhythm":
          return { name: "worldRhythm" };
        default:
          return { name: "worldEvents" };
      }
    case "personas":
      switch (second) {
        case "studio":
          return { name: "personaStudio" };
        case "dna":
          return { name: "personaDna" };
        case "pool":
          return { name: "personaPool" };
        case "voices":
          return { name: "personaVoices" };
        case "prompts":
          return { name: "personaPrompts" };
        case "scenes":
          return { name: "personaScenes" };
        default:
          return { name: "personaStudio" };
      }
    case "system":
      switch (second) {
        case "config":
          return { name: "systemConfig" };
        case "release":
          return { name: "systemRelease" };
        case "jobs":
          return { name: "systemJobs" };
        case "audit":
          return { name: "systemAudit" };
        default:
          return { name: "systemConfig" };
      }
    case "commerce":
      return { name: "commerceAds" };
    case "overview":
      return { name: "overview" };
    default:
      // 空 hash、未知路径、旧链接都落到总览。
      void third;
      return { name: "overview" };
  }
}

/** 归一化地址栏 hash（空/#/#/ 都当总览）。 */
export function normalizeAdminHash(hash: string): string {
  const raw = (hash || "").trim();
  if (!raw || raw === "#" || raw === "#/") return "#/overview";
  if (raw.startsWith("#/")) return raw;
  const path = raw.startsWith("#") ? raw.slice(1) : raw;
  return `#/${path.replace(/^\/+/, "")}`;
}

/** 当前路由在导航树里对应的 NavItem（找不到返回 undefined）。 */
export function activeNavItem(route: AdminRoute): NavItem | undefined {
  const domain = domainOf(route);
  const group = ADMIN_NAV.find((entry) => entry.domain === domain);
  if (!group) return undefined;
  const hash = adminRouteToHash(route);
  return (
    group.items.find((item) => adminRouteToHash(item.route) === hash) ??
    // 详情页（如 #/users/u_x）高亮其列表项
    group.items[0]
  );
}

/**
 * 已接入真实分区的路由。
 *
 * 为什么不用 `item.phase` 判断：phase 是"计划在第几期交付"，
 * 而这里要的是"现在到底有没有页面"——两者会短暂不一致
 * （比如某一期只做了一半）。以实际存在的 section 为准，
 * 导航才不会把用户带进空壳。
 */
const IMPLEMENTED_ROUTES: ReadonlySet<AdminRoute["name"]> = new Set([
  "overview",
  "systemJobs",
  // P2：平移旧功能
  "personaDna",
  "personaScenes",
  "worldRhythm",
  "personaPool",
  "personaStudio",
  "personaVoices",
  "worldMedia",
  "systemConfig",
  "systemRelease",
  // P3：新能力
  "users",
  "userDetail",
  "worldEvents",
  "worldPosts",
  "personaPrompts",
  // P4：操作日志
  "systemAudit",
  // 商业化：广告控制台
  "commerceAds",
]);

/** 该路由是否已经有可用的分区页面。 */
export function isImplemented(route: AdminRoute): boolean {
  return IMPLEMENTED_ROUTES.has(route.name);
}
