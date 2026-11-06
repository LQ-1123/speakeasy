"use client";

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
import { BookMarked, Compass, MessageCircle, UserRound, UsersRound } from "lucide-react";

import "./w4-base.css";
import "./w4.css";
import "./w4-part2.css";

import type { Route, Section } from "./model";
import { AppProvider, useApp } from "./store";
import { isLocalMode } from "./local/keys";
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

/* 设备身份：给所有 /api 请求补 X-User-Id / X-User-Token（浏览器环境幂等） */
installApiIdentity();

export function sectionOf(route: Route): Section {
  switch (route.name) {
    case "chat":
    case "chats":
      return "chats";
    case "contacts":
    case "contact":
    case "requests":
      return "contacts";
    case "discover":
    case "discoverMoments":
    case "discoverPeople":
    case "persona":
    case "events":
    case "moments":
      return "discover";
    case "expressions":
      return "notes";
    case "me":
    case "settings":
      return "me";
    default:
      return "chats";
  }
}

function parseRoute(): Route {
  if (typeof window === "undefined") return { name: "chats" };
  const parts = window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const head = parts[0] ?? "";
  switch (head) {
    case "chats":
      return { name: "chats" };
    case "chat":
      return parts[1] ? { name: "chat", friendId: parts[1] } : { name: "chats" };
    case "contacts":
      return parts[1] ? { name: "contact", friendId: parts[1] } : { name: "contacts" };
    case "discover":
      if (parts[1] === "people") return { name: "discoverPeople" };
      /* 老链接 #/discover/moments 直接落到 Persona 信息流 */
      return { name: "discover" };
    case "persona":
      return parts[1] ? { name: "persona", personaId: parts[1] } : { name: "discover" };
    case "events":
      return { name: "events" };
    case "moments":
      return { name: "moments" };
    case "expressions":
      return { name: "expressions" };
    case "me":
      return { name: "me" };
    case "settings":
      return { name: "settings", section: parts[1] };
    case "requests":
      return { name: "requests" };
    default:
      /* 新用户首屏直接进 Discover 信息流（不再依赖先生成好友） */
      return { name: "discover" };
  }
}

function routeToHash(route: Route): string {
  switch (route.name) {
    case "chat":
      return `#/chat/${route.friendId}`;
    case "contact":
      return `#/contacts/${route.friendId}`;
    case "chats":
      return "#/chats";
    case "contacts":
      return "#/contacts";
    case "discover":
      return "#/discover";
    case "discoverPeople":
      return "#/discover/people";
    case "persona":
      return `#/persona/${route.personaId}`;
    case "events":
      return "#/events";
    case "discoverMoments":
      return "#/discover";
    case "settings":
      return route.section ? `#/settings/${route.section}` : "#/settings";
    default:
      return `#/${route.name}`;
  }
}

const TAB_ITEMS: Array<{ key: Section; label: string; icon: React.ReactNode; route: Route }> = [
  { key: "chats", label: "消息", icon: <MessageCircle size={21} strokeWidth={1.7} />, route: { name: "chats" } },
  { key: "contacts", label: "通讯录", icon: <UsersRound size={21} strokeWidth={1.7} />, route: { name: "contacts" } },
  { key: "discover", label: "发现", icon: <Compass size={21} strokeWidth={1.7} />, route: { name: "discover" } },
  { key: "notes", label: "表达本", icon: <BookMarked size={21} strokeWidth={1.7} />, route: { name: "expressions" } },
  { key: "me", label: "我", icon: <UserRound size={21} strokeWidth={1.7} />, route: { name: "me" } },
];

function Shell() {
  const { toast, requests, friends } = useApp();
  const [route, setRoute] = useState<Route>(() => parseRoute());
  const [mobile, setMobile] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia("(max-width: 900px)");
    const update = () => setMobile(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    const onHash = () => setRoute(parseRoute());
    window.addEventListener("hashchange", onHash);
    if (!window.location.hash) window.history.replaceState(null, "", "#/discover");
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const navigate = useCallback((next: Route) => {
    window.location.hash = routeToHash(next);
    setRoute(next);
  }, []);

  const back = useCallback(() => {
    const section = sectionOf(route);
    const fallback: Route =
      section === "contacts"
        ? { name: "contacts" }
        : section === "chats"
          ? { name: "chats" }
          : section === "discover"
            ? { name: "discover" }
            : { name: "me" };
    navigate(fallback);
  }, [route, navigate]);

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
      case "discoverMoments":
      case "moments":
        /* V3：发现首页 = 以 AI Persona 为中心的信息流 */
        return <FeedView navigate={navigate} />;
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
        return <EmptyState />;
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
              const isDot = t.key === "contacts";
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

      <LocalWelcome />

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
  return (
    <AppProvider>
      <Shell />
    </AppProvider>
  );
}

/** 本地模式首次进入：三步上手引导 */
function LocalWelcome() {
  const { profile } = useApp();
  const [show, setShow] = useState(false);

  useEffect(() => {
    if (!isLocalMode()) return;
    try {
      const dismissed = window.localStorage.getItem("speakeasy.welcome.dismissed") === "1";
      if (!dismissed && !profile?.name) setShow(true);
    } catch {
      /* ignore */
    }
  }, [profile?.name]);

  const dismiss = () => {
    try {
      window.localStorage.setItem("speakeasy.welcome.dismissed", "1");
    } catch {
      /* ignore */
    }
    setShow(false);
  };

  if (!show) return null;
  return (
    <div className="w4-overlay" role="dialog" aria-modal="true">
      <div className="w4-dialog" style={{ maxWidth: 420 }}>
        <div className="w4-dialog-title">欢迎使用 SpeakEasy 本地版</div>
        <p className="w4-hint" style={{ marginTop: 8 }}>
          所有数据只存在这台设备上，不需要服务器。三步就能开始：
        </p>
        <ol className="w4-hint" style={{ margin: "10px 0 0 18px", lineHeight: 1.9 }}>
          <li>设置 → 本地与密钥：填 DeepSeek 和 SiliconFlow 两个 Key</li>
          <li>我 → 右上角铅笔：填昵称、目标语言</li>
          <li>发现 → 偶遇：捏个好友，通过后就能聊天 / 语音 / 通话</li>
        </ol>
        <div style={{ display: "flex", gap: 8, marginTop: 18 }}>
          <button
            className="w4-btn is-primary"
            onClick={() => {
              dismiss();
              window.location.hash = "#/settings/local";
            }}
          >
            去填 Key
          </button>
          <button className="w4-btn" onClick={dismiss}>
            先逛逛
          </button>
        </div>
      </div>
    </div>
  );
}
