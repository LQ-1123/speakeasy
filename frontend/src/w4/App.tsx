"use client";

import { useShallow } from "zustand/react/shallow";

/**
 * W4 · SpeakEasy 原型
 *
 * 布局参考微信桌面版 / WhatsApp Web 的信息结构：
 *   左侧图标栏 → 列表列（会话 / 通讯录）→ 主内容列（会话 / 动态 / 资料）
 * 手机宽度下折叠成「列表 ↔ 全屏内容」两层，跟原生微信一致。
 *
 * 原型只跑 mock，不请求后端。全部交互（发消息、语音、翻译、收藏、
 * 偶遇、动态、通话）在 store.tsx 里模拟。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Compass, MessageCircle, UserRound, UsersRound } from "lucide-react";

import "./styles/base.css";
import "./styles/shell.css";

import type { Route, Section } from "./model";
import { isInternalNotificationRoute, routeToHash, sectionOf as routeSection } from "./navigation";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "./queryClient";
import { CallPanel } from "./v3/CallPanel";
import { installPlatform } from "./v3/platform";
import { ThemeProvider } from "../theme/ThemeProvider";
import { haptic } from "../theme/haptics";
import { AppProvider, useApp } from "./store";
import { backToParent, closeTopOverlay, navigateTo, syncFromHash, useNavigation } from "./navigationStore";
import { usePreferences } from "./v3/preferences";
import { PushOptIn } from "./v3/PushOptIn";
import { GlobalNotification } from "./v3/GlobalNotification";
import { installAudioBus } from "./audioBus";
import { installApiIdentity } from "./v3/api";
import { Icon } from "./ui";
import { Rail } from "./components/Rail";
import { ChatList } from "./components/ChatList";
import { ContactsList } from "./components/ContactsList";
import { ChatPane } from "./components/ChatPane";
import { ContactsIntro } from "./components/ContactsIntro";
import { ContactDetailPane } from "./components/ContactDetailPane";
import { PeopleView } from "./components/DiscoverPane";
import { DiscoverIndex } from "./components/DiscoverIndex";
import { NotesPane } from "./components/NotesPane";
import { MePane } from "./components/MePane";
import { SettingsPane } from "./components/SettingsPane";
import { RequestsPane } from "./components/RequestsPane";
import { EmptyState } from "./components/EmptyState";
import { FeedView } from "./v3/FeedView";
import { EventsView } from "./v3/EventsView";
import { PersonaProfileView } from "./v3/PersonaProfileView";
import { installPreviewAdapter, isPreviewMode, previewVariant } from "./previewAdapter";
import { PreviewBridge } from "./previewBridge";

/* 设备身份：给所有 /api 请求补 X-User-Id / X-User-Token（浏览器环境幂等） */
installApiIdentity();

/* 设计验收 iframe：必须在任何请求发出前安装本地 fixture 适配器。 */
if (typeof window !== "undefined" && isPreviewMode()) {
  installPreviewAdapter();
}

export const sectionOf = routeSection;

const TAB_ITEMS: Array<{ key: Section; label: string; icon: React.ReactNode; route: Route }> = [
  { key: "chats", label: "消息", icon: <MessageCircle size={21} strokeWidth={1.8} />, route: { name: "chats" } },
  { key: "contacts", label: "通讯录", icon: <UsersRound size={21} strokeWidth={1.8} />, route: { name: "contacts" } },
  { key: "discover", label: "发现", icon: <Compass size={21} strokeWidth={1.8} />, route: { name: "discover" } },
  { key: "me", label: "我", icon: <UserRound size={21} strokeWidth={1.8} />, route: { name: "me" } },
];

const MOBILE_QUERY = "(max-width: 959.98px)";

/** 首帧就用 matchMedia 判定布局，避免「先桌面双栏 → 再切手机单栏」的跳帧。 */
function prefersMobileLayout(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia(MOBILE_QUERY).matches
  );
}

/** store 还没提供 booted 字段时的兜底超时：绝不停在空白占位页。 */
const BOOT_PLACEHOLDER_TIMEOUT_MS = 5000;

/** bootstrap 未完成时的中性占位：不渲染任何依赖数据的路由页面（避免闪「会话不存在」/空列表）。 */
function BootPlaceholder() {
  return (
    <div className="w4-root" style={{ height: "100%" }}>
      <div className="w4-app" data-mobile-view="list" data-nav-placeholder="boot">
        <div className="w4-centered" style={{ height: "100%", width: "100%" }}>
          <div className="se-muted-text" role="status" aria-live="polite">
            正在加载…
          </div>
        </div>
      </div>
      <PushOptIn />
      <GlobalNotification />
    </div>
  );
}


function Shell() {
  const { toast, requests, friends, booted, bootError, friendCount } = useApp(
    useShallow((state) => {
      /* chat-ui 在 store 里加 booted 之前，这里按可选字段读取；缺失时走回退启发式。 */
      const optional = state as unknown as { booted?: unknown };
      return {
        toast: state.toast,
        requests: state.requests,
        friends: state.friends,
        bootError: state.bootError,
        friendCount: state.friends.length,
        booted: typeof optional.booted === "boolean" ? optional.booted : null,
      };
    }),
  );
  const route = useNavigation((state) => state.route);
  /** 移动端首帧就用 matchMedia，避免先渲染桌面双栏再切手机单栏的跳帧。 */
  const [mobile, setMobile] = useState(prefersMobileLayout);

  useEffect(() => {
    void usePreferences.getState().load();
  }, []);

  /* 品牌开屏（静态 HTML 注入，见 app/layout.tsx）：React 挂载后淡出。 */
  useEffect(() => {
    (window as unknown as { __hideSonderSplash?: () => void }).__hideSonderSplash?.();
  }, []);

  useEffect(() => {
    const mq = window.matchMedia(MOBILE_QUERY);
    const update = () => setMobile(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);

  /* URL 是唯一事实来源：只处理「外部」hash 变化（浏览器前进/后退、通知、深链）。
     程序内导航统一走 navigationStore.navigateTo，避免 hash 赋值 + setRoute 各写一次。 */
  useEffect(() => {
    const onHash = () => {
      const hash = window.location.hash;
      /* 与原生通知白名单对齐：非法 hash 不导航，恢复当前路由的 URL（防注入/误跳）。 */
      if (hash && !isInternalNotificationRoute(hash)) {
        window.history.replaceState(null, "", routeToHash(useNavigation.getState().route));
        return;
      }
      syncFromHash();
    };
    window.addEventListener("hashchange", onHash);
    if (!window.location.hash) window.history.replaceState(null, "", "#/chats");
    syncFromHash();
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const navigate = useCallback((next: Route) => {
    haptic("selection");
    return navigateTo(next);
  }, []);

  const back = useCallback(() => {
    haptic("selection");
    return backToParent();
  }, []);

  /* bootstrap 完成前不渲染依赖数据的路由页：深链/通知打开时先中性占位，
     避免首帧闪「会话不存在」/空会话列表。chat-ui 负责加载数据本身。 */
  const [bootTimeout, setBootTimeout] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setBootTimeout(true), BOOT_PLACEHOLDER_TIMEOUT_MS);
    return () => window.clearTimeout(timer);
  }, []);
  const fallbackSettled = friendCount > 0 || Boolean(bootError) || bootTimeout;
  /* 只锁存「曾经完成过一次 bootstrap」：后续 reloadAll（加好友后刷新）即使把
     booted 临时置回 false，也不会把整个界面换成占位页。 */
  const [settledOnce, setSettledOnce] = useState(false);
  useEffect(() => {
    if (booted === true || bootTimeout || (booted === null && fallbackSettled)) {
      setSettledOnce(true);
    }
  }, [booted, bootTimeout, fallbackSettled]);
  const bootSettled = settledOnce;

  /* 系统返回键：监听只安装一次，回调通过 ref 读取最新导航函数，
     避免路由每变一次就重建监听（旧实现会短暂丢失 native 事件）。 */
  const backRef = useRef(back);
  backRef.current = back;
  useEffect(() => {
    const teardown = installPlatform(() => {
      if (closeTopOverlay()) return true;
      const dialog = document.querySelector<HTMLElement>('[role="dialog"]');
      const close = dialog?.querySelector<HTMLButtonElement>('button[aria-label*="关闭"],button[aria-label*="取消"],button[data-dialog-close]');
      if (close) { close.click(); return true; }
      const legacy = (window as unknown as { w4HandleBack?: () => string }).w4HandleBack;
      if (legacy?.() === "handled") return true;
      /* true/false 语义不变：真的发生了导航才吞掉系统返回，否则交给原生兜底。 */
      const result = backRef.current();
      return result.routeChanged || result.history !== "none";
    });
    return teardown;
  }, []);

  const unread = friends.reduce((sum, f) => sum + f.unread, 0);
  const section = sectionOf(route);

  const showList =
    section === "chats" ||
    section === "contacts" ||
    (section === "discover" && !mobile && route.name !== "persona");
  /** 这两个是无列表列、直接显示内容的分区，手机上不需要「推入」动画 */
  const listIndex = route.name === "chats" || route.name === "contacts";

  const main = (() => {
    switch (route.name) {
      case "chat":
        return <ChatPane friendId={route.friendId} onBack={back} navigate={navigate} />;
      case "contact":
        return <ContactDetailPane friendId={route.friendId} onBack={back} navigate={navigate} />;
      case "requests":
        return <RequestsPane navigate={navigate} />;
      case "discover":
        /* 手机：发现 = 二级菜单（动态 / 偶遇 / 通知）；桌面：左列菜单 + 右侧动态 */
        return <FeedView navigate={navigate} />;
      case "discoverFeed":
      case "discoverMoments":
      case "moments":
        return (
          <FeedView
            navigate={navigate}
            onBack={mobile ? () => navigate({ name: "discover" }) : undefined}
          />
        );
      case "discoverPeople":
        return <PeopleView navigate={navigate} />;
      case "events":
        return <EventsView navigate={navigate} />;
      case "persona":
        return <PersonaProfileView personaId={route.personaId} navigate={navigate} />;
      case "expressions":
        return <NotesPane />;
      case "me":
        return <MePane navigate={navigate} />;
      case "settings":
        return <SettingsPane section={route.section} navigate={navigate} />;
      case "contacts":
        return <ContactsIntro navigate={navigate} />;
      default:
        return <EmptyState navigate={navigate} />;
    }
  })();

  const [listW, setListW] = useState<number>(() => {
    if (typeof window === "undefined") return 300;
    const v = Number(window.localStorage.getItem("w4.listw"));
    return Number.isFinite(v) && v >= 240 && v <= 480 ? v : 300;
  });

  /* 窗口宽度：分栏要随窗口收缩，不能把主内容挤成 0 宽 */
  const [vw, setVw] = useState<number>(() =>
    typeof window === "undefined" ? 1280 : window.innerWidth,
  );
  useEffect(() => {
    const onResize = () => setVw(window.innerWidth);
    window.addEventListener("resize", onResize);
    onResize();
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const RAIL_W = 80;
  const MIN_MAIN_W = 380;
  const maxListW = Math.max(240, Math.min(480, vw - RAIL_W - MIN_MAIN_W));
  const effListW = Math.max(240, Math.min(listW, maxListW));

  /** 拖动分隔条调整列表宽度（微信桌面版没有，但桌面端普遍需要） */
  const startDrag = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = listW;
    const onMove = (ev: PointerEvent) => {
      const limit = Math.max(
        240,
        Math.min(480, window.innerWidth - 80 - 380),
      );
      const next = Math.min(limit, Math.max(240, startW + (ev.clientX - startX)));
      setListW(next);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      setListW((w) => {
        try {
          window.localStorage.setItem("w4.listw", String(w));
        } catch {
          /* 忽略 */
        }
        return w;
      });
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }, [listW]);

  /* 所有 hooks 已执行完，再决定是否显示 bootstrap 占位（避免 hooks 顺序变化）。 */
  if (!bootSettled) return <BootPlaceholder />;

  const dataView = mobile && !listIndex ? "main" : "list";

  return (
    <div className="w4-root" style={{ height: "100%" }}>
      <div className="w4-app" data-mobile-view={dataView}>
        <CallPanel />
        <div className="w4-shell">
          <Rail active={section} unread={unread} requests={requests.length} navigate={navigate} />

          {showList ? (
            <>
              <div
                className="w4-listcol"
                style={mobile ? undefined : { width: effListW, flex: "none" }}
              >
                {section === "contacts" ? (
                  <ContactsList route={route} navigate={navigate} requestsCount={requests.length} />
                ) : section === "discover" ? (
                  <DiscoverIndex
                    active={
                      route.name === "discoverPeople"
                        ? "people"
                        : route.name === "events"
                          ? "events"
                          : "feed"
                    }
                    navigate={navigate}
                  />
                ) : (
                  <ChatList route={route} navigate={navigate} />
                )}
              </div>
              {!mobile ? (
                <div
                  className="w4-splitter"
                  role="separator"
                  aria-orientation="vertical"
                  aria-label="拖动调整列表宽度"
                  onPointerDown={startDrag}
                  onDoubleClick={() => setListW(300)}
                />
              ) : null}
            </>
          ) : null}

          {main}
        </div>

        {/* 手机底部导航：单聊页不显示 */}
        {route.name !== "chat" ? (
          <nav className="w4-tabbar w4-only-mobile" aria-label="主导航">
            {TAB_ITEMS.map((t) => {
              const badge =
                t.key === "chats" ? unread : t.key === "contacts" ? requests.length : 0;
              const isDot = false;
              return (
                <button
                  key={t.key}
                  className={`w4-tabbar-btn ${section === t.key ? "on" : ""}`}
                  onClick={() => navigate(t.route)}
                >
                  {t.icon}
                  {t.label}
                  {badge ? (
                    isDot ? (
                      <span
                        className="w4-tabbar-dot"
                        aria-label={`${badge} 条待处理`}
                        title={`${badge} 条待处理`}
                      />
                    ) : (
                      <span className="w4-tabbar-badge">{badge > 99 ? "99+" : badge}</span>
                    )
                  ) : null}
                </button>
              );
            })}
          </nav>
        ) : null}
      </div>

      <PushOptIn />
      <GlobalNotification />

      {toast ? (
        <div className="w4-toast" key={toast.id} role="status">
          {toast.tone === "success" ? (
            <Icon size={14} strokeWidth={2.4}>
              <path d="M20 6L9 17l-5-5" />
            </Icon>
          ) : null}
          {toast.text}
        </div>
      ) : null}
    </div>
  );
}

// 全局音频总线：同一时刻只允许一个声音播放（朗读切换下一句即停止上一句）
installAudioBus();

export function App() {
  const preview = isPreviewMode();
  return (
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <AppProvider>
          <Shell />
          {preview ? <PreviewBridge variant={previewVariant()} /> : null}
        </AppProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}

/** 本地模式首次进入：三步上手引导 */
