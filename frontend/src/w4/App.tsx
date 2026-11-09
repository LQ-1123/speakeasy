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

import { useCallback, useEffect, useState } from "react";
import { Compass, MessageCircle, UserRound, UsersRound } from "lucide-react";

import "./styles/base.css";
import "./styles/shell.css";

import type { Route, Section } from "./model";
import { backRoute, parseRoute as parseHashRoute, routeToHash as serializeRoute, sectionOf as routeSection } from "./navigation";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "./queryClient";
import { CallPanel } from "./v3/CallPanel";
import { installPlatform } from "./v3/platform";
import { ThemeProvider } from "../theme/ThemeProvider";
import { haptic } from "../theme/haptics";
import { AppProvider, useApp } from "./store";
import { closeTopOverlay, useNavigation } from "./navigationStore";
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
function parseRoute(): Route { return parseHashRoute(typeof window === "undefined" ? "" : window.location.hash); }
const routeToHash = serializeRoute;

const TAB_ITEMS: Array<{ key: Section; label: string; icon: React.ReactNode; route: Route }> = [
  { key: "chats", label: "消息", icon: <MessageCircle size={21} strokeWidth={1.8} />, route: { name: "chats" } },
  { key: "contacts", label: "通讯录", icon: <UsersRound size={21} strokeWidth={1.8} />, route: { name: "contacts" } },
  { key: "discover", label: "发现", icon: <Compass size={21} strokeWidth={1.8} />, route: { name: "discover" } },
  { key: "me", label: "我", icon: <UserRound size={21} strokeWidth={1.8} />, route: { name: "me" } },
];

function Shell() {
  const { toast, requests, friends } = useApp(useShallow((state) => ({ toast: state.toast, requests: state.requests, friends: state.friends })));
  const route = useNavigation((state) => state.route);
  const setRoute = useNavigation((state) => state.setRoute);
  const [mobile, setMobile] = useState(false);

  useEffect(() => {
    void usePreferences.getState().load();
  }, []);

  useEffect(() => {
    const mq = window.matchMedia("(max-width: 959.98px)");
    const update = () => setMobile(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    const onHash = () => setRoute(parseRoute());
    window.addEventListener("hashchange", onHash);
    if (!window.location.hash) window.history.replaceState(null, "", "#/chats");
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const navigate = useCallback((next: Route) => {
    haptic("selection");
    window.location.hash = routeToHash(next);
    setRoute(next);
  }, []);

  const back = useCallback(() => navigate(backRoute(route)), [route, navigate]);

  useEffect(() => {
    const teardown = installPlatform(() => {
      if (closeTopOverlay()) return true;
      const dialog = document.querySelector<HTMLElement>('[role="dialog"]');
      const close = dialog?.querySelector<HTMLButtonElement>('button[aria-label*="关闭"],button[aria-label*="取消"],button[data-dialog-close]');
      if (close) { close.click(); return true; }
      const legacy = (window as unknown as { w4HandleBack?: () => string }).w4HandleBack;
      if (legacy?.() === "handled") return true;
      const next = backRoute(route);
      if (routeToHash(next) === routeToHash(route)) return false;
      back();
      return true;
    });
    return teardown;
  }, [route, back]);

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
