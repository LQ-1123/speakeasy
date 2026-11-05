"use client";

/**
 * 原型状态：所有交互都在这里发生，组件只负责展示。
 *
 * 之所以把「等待好友回复 / 生成润色建议 / AI 评论」也做出来，
 * 是因为这些异步状态决定了产品用起来像不像真的：
 * 没有「对方正在说话…」的停顿，再好看的界面也只是静态稿。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  DISCOVER_POOL,
  EXPRESSIONS,
  FRIENDS,
  ME,
  MESSAGES,
  MOMENTS,
  REQUESTS,
  type DiscoverCandidate,
} from "./mock";
import {
  loadAll as apiLoadAll,
  sendText as apiSendText,
  sendVoiceBlob as apiSendVoiceBlob,
  fetchMessages as apiFetchMessages,
  acceptFriend as apiAcceptFriend,
  callWarmup as apiCallWarmup,
  markThreadRead as apiMarkThreadRead,
  saveCallSummary as apiSaveCallSummary,
  declineFriend as apiDeclineFriend,
  removeFriend as apiRemoveFriend,
  deleteExpressionOnServer as apiDeleteExpression,
  generateMoments as apiGenerateMoments,
  loadExpressions as apiLoadExpressions,
  loadMoments as apiLoadMoments,
  loadSettings as apiLoadSettings,
  patchExpression as apiPatchExpression,
  publishMomentToServer as apiPublishMoment,
  saveTts as apiSaveTts,
  translate as apiTranslate,
  deleteMoment as apiDeleteMoment,
  loadFriendList as apiLoadFriendList,
  setFriendVoice as apiSetFriendVoice,
} from "./apiFacade";
import type { Expression, Friend, Message, Moment, MomentComment, Profile, Suggestion } from "./model";
import { isLocalMode } from "./local/keys";

export interface Settings {
  llm: {
    preset: string;
    baseUrl: string;
    model: string;
    keyMasked: string;
    hasKey: boolean;
  };
  stt: {
    provider: "auto" | "local" | "siliconflow" | "custom";
    model: string;
    keyMasked: string;
    hasKey: boolean;
  };
  tts: {
    provider: "system" | "fish";
    emotion: boolean;
    keyMasked: string;
    hasKey: boolean;
    cloned: boolean;
  };
  notify: { messages: boolean; practice: boolean };
}

interface Toast {
  id: number;
  text: string;
  tone?: "default" | "success" | "error";
}

interface Store {
  friends: Friend[];
  requests: Friend[];
  messages: Message[];
  moments: Moment[];
  expressions: Expression[];
  profile: Profile;
  settings: Settings;
  toast: Toast | null;
  typing: Record<string, boolean>;
  /** 哪些消息/动态展开了中文（视图状态，不写进数据） */
  zhOpen: Record<string, boolean>;
  showToast: (text: string, tone?: Toast["tone"]) => void;
  messagesOf: (friendId: string) => Message[];
  friendById: (id: string) => Friend | undefined;
  sendText: (friendId: string, text: string) => void;
  /** 真实录音发送（走后端 STT + LLM + TTS） */
  sendVoiceBlob: (
    friendId: string,
    blob: Blob,
    durationMs: number,
    opts?: { openTranscript?: boolean },
  ) => Promise<void>;
  sendVoice: (friendId: string, durationMs: number) => void;
  sendImage: (friendId: string, image: string) => void;
  markRead: (friendId: string) => void;
  toggleZh: (key: string) => void;
  /** 单条消息翻译：没有译文就现翻，有就收起/展开 */
  translateMessage: (messageId: string) => Promise<void>;
  translateAll: (friendId: string) => void;
  translateAllMoments: () => void;
  toggleShownText: (messageId: string) => void;
  saveSuggestion: (messageId: string) => void;
  dismissSuggestion: (messageId: string) => void;
  likeMoment: (momentId: string) => void;
  addComment: (momentId: string, text: string) => void;
  /** 删除自己发的动态 */
  deleteMoment: (momentId: string) => Promise<void>;
  publishMoment: (text: string, images: string[], mood: string) => void;
  /** 重新拉一次朋友圈（发布后 AI 评论是后台生成的） */
  refreshMoments: () => Promise<void>;
  /** 让 AI 好友生成今天的朋友圈 */
  generateMoments: () => Promise<void>;
  acceptRequest: (friendId: string) => void;
  declineRequest: (friendId: string) => void;
  addDiscovered: (candidate: DiscoverCandidate) => Friend;
  practiceExpression: (id: string, remembered: boolean) => void;
  toggleMastered: (id: string) => void;
  removeExpression: (id: string) => void;
  updateProfile: (patch: Partial<Profile>) => void;
  updateSettings: (patch: Partial<Settings>) => void;
  updateFriendVoice: (friendId: string, voice: string, voiceLabel: string) => void;
  deleteFriend: (friendId: string) => void;
  callSummary: (friendId: string, seconds: number, turns?: number) => void;
  /** 偶遇捏到新人后，刷新好友/会话，让新朋友能被打开 */
  reloadAll: () => Promise<void>;
  discoverPool: DiscoverCandidate[];
}

/**
 * AI 好友之间不互相评论、不互相点赞。
 *
 * 原因：
 *  - 对练习没有价值（用户只能旁观）
 *  - 会让「这些是真实母语者」的设定显得像剧本
 *  - 每条 AI 评论都是一次 LLM 调用，钱花在用户不会看的地方
 *
 * 所以：好友发的动态只保留「用户自己的赞/评论」；用户发的动态才接收好友评论。
 */
function sanitizeMoment(m: Moment): Moment {
  if (m.authorId === "me") return m;
  const mineLiked = Array.isArray(m.likedBy) ? m.likedBy.includes("me") : false;
  return {
    ...m,
    comments: [],
    commentsPending: false,
    likedBy: mineLiked ? ["me"] : [],
    liked: mineLiked,
  };
}

/** 后端 /settings + /settings/tts → store 里的 Settings（只做展示需要的映射） */
function mapLiveSettings(
  core: { llm?: Record<string, any>; stt?: Record<string, any>; tts?: Record<string, any> },
  tts: Record<string, any> | null,
): Partial<Settings> {
  const llmCfg = (core.llm?.config ?? {}) as Record<string, any>;
  const sttCfg = (core.stt?.config ?? {}) as Record<string, any>;
  const ttsCfg = (tts ?? core.tts ?? {}) as Record<string, any>;
  const provider = String(ttsCfg.provider ?? "");
  return {
    llm: {
      preset: "deepseek",
      baseUrl: String(llmCfg.base_url ?? ""),
      model: String(llmCfg.model ?? ""),
      keyMasked: String(llmCfg.api_key_masked ?? ""),
      hasKey: Boolean(llmCfg.api_key_set),
    },
    stt: {
      provider: (sttCfg.provider === "local" || sttCfg.provider === "siliconflow"
        ? sttCfg.provider
        : "auto") as Settings["stt"]["provider"],
      model: String(sttCfg.model ?? ""),
      keyMasked: String(sttCfg.api_key_masked ?? ""),
      hasKey: Boolean(sttCfg.api_key_set),
    },
    tts: {
      provider: provider === "fish" ? "fish" : "system",
      emotion: Boolean(ttsCfg.fish_reference_id || ttsCfg.emotion),
      keyMasked: String(ttsCfg.fish_api_key ?? ""),
      hasKey: Boolean(ttsCfg.fish_api_key),
      cloned: Boolean(ttsCfg.fish_reference_id),
    },
  };
}

const AppCtx = createContext<Store | null>(null);

export function useApp(): Store {
  const ctx = useContext(AppCtx);
  if (!ctx) throw new Error("useApp must be used inside <AppProvider>");
  return ctx;
}

/* --------------------------------------------------------------- mock 回复 */

const REPLIES: Record<string, Array<{ text: string; zh: string; ms?: number }>> = {
  callum: [
    {
      text: "Ha — fair enough. Right, I'm off to sort the grinder. Give us a shout later, yeah?",
      zh: "哈——行吧。好了，我去弄磨豆机了。晚点再聊？",
      ms: 6900,
    },
    {
      text: "Honestly, that flat's been in my head all week. If the landlord's decent I might actually take it.",
      zh: "说实话，那套房这周一直在我脑子里。要是房东靠谱，我可能真租了。",
      ms: 8200,
    },
    {
      text: "Right, I've got a queue of people who all want oat milk. Back in a bit.",
      zh: "好了，一堆人都要燕麦奶。我一会儿回来。",
      ms: 5400,
    },
  ],
  giulia: [
    {
      text: "Eh, piano piano. Non importa se sbagli — l'importante è che parli.",
      zh: "哎，慢慢来。说错没关系，重要的是你在说。",
      ms: 6100,
    },
    {
      text: "Allora, domenica mattina? Il mercato è meglio prima delle dieci.",
      zh: "那周天早上？市场十点前更好逛。",
      ms: 5800,
    },
  ],
  haruto: [
    {
      text: "いいですね。じゃあ、写真は明日の朝に送ります。楽しみにしていてください。",
      zh: "好啊。那我明天早上把照片发给你，期待着吧。",
      ms: 7000,
    },
    {
      text: "あなたの日本語、前より自然になりましたよ。少しずつで大丈夫です。",
      zh: "你的日语比之前自然了。一点一点来就好。",
      ms: 6600,
    },
  ],
  camila: [
    {
      text: "Obrigada! Vou tentar terminar hoje. Se eu conseguir, te mando uma foto da maquete pronta.",
      zh: "谢谢！我今天试着做完。要是做完了，拍张成品给你看。",
      ms: 7400,
    },
    {
      text: "Ah, e lembrei — você queria correr de manhã? Podemos correr juntos, virtualmente.",
      zh: "哦对了，你之前说想晨跑？我们可以「云跑步」。",
      ms: 7800,
    },
  ],
  jiwoo: [
    {
      text: "맞아요, 저도 그래요. 같이 연습하면 조금 덜 어색할 거예요.",
      zh: "对呀，我也一样。一起练的话会没那么尴尬。",
      ms: 5200,
    },
    {
      text: "오늘은 여기까지 공부하고 집에 갈 거예요. 내일 또 봐요!",
      zh: "今天学到这儿就回家了。明天见！",
      ms: 5400,
    },
  ],
  morten: [
    {
      text: "Wise. The umbrella was never going to survive Bergen anyway.",
      zh: "明智。反正那把伞在卑尔根也活不下来。",
      ms: 4700,
    },
    {
      text: "Right. Back to counting fish. Don't lose another umbrella.",
      zh: "好了，回去数鱼了。别再丢伞了。",
      ms: 4600,
    },
  ],
  tom: [
    {
      text: "Sure, no bother at all. I'll leave it at the front desk with your name on it.",
      zh: "当然，一点也不麻烦。我会放在前台，写上你的名字。",
      ms: 6200,
    },
    {
      text: "Good man. And if the first chapter's slow, skip to chapter three — I won't tell anyone.",
      zh: "好样的。第一章要是慢，直接跳第三章，我不告诉别人。",
      ms: 7100,
    },
  ],
  ines: [
    {
      text: "Bom dia! Se quiser, passo aí na quinta e levo um buquê para você.",
      zh: "早上好！要的话我周四路过，给你带一束花。",
      ms: 6100,
    },
  ],
  rosa: [{ text: "¡Genial! Pues hablamos mañana, ¿vale?", zh: "太好了！那我们明天聊，好吗？", ms: 4200 }],
  finn: [{ text: "Sweet as. Talk soon, eh!", zh: "好极了。回头聊！", ms: 3600 }],
  niamh: [{ text: "Grand! Chat later so.", zh: "好！那晚点聊。", ms: 3200 }],
  meimei: [
    {
      text: "Great. Let's do a quick mock meeting next time — ten minutes, no pressure.",
      zh: "好。下次来个十分钟的模拟会议，别有压力。",
      ms: 5200,
    },
  ],
};

const DEFAULT_REPLY = [
  {
    text: "That sounds good. Tell me more when you have a minute.",
    zh: "听起来不错。有空再多跟我说说。",
    ms: 5000,
  },
];

/** 我发语音时「识别」出来的话 */
const MY_TRANSCRIPTS = [
  "Yeah, that sounds good — this weekend works for me.",
  "I'm not sure yet. Let me think about it and get back to you.",
  "Sorry, could you say that again? I didn't catch the last part.",
  "Actually, I'd love to. What time were you thinking?",
];

const SUGGESTION_POOL: Array<Omit<Suggestion, "saved">> = [
  {
    better: "Sounds good — this weekend works for me.",
    why: "口语里更常说 sounds good，比 that sounds good 轻快；works for me 是约时间最自然的说法。",
    tag: "自然",
  },
  {
    better: "Let me get back to you on that.",
    why: "「我再回复你」的固定说法，比 think about it and tell you 更简洁，也更像成年人说话。",
    tag: "地道",
  },
  {
    better: "Sorry, could you say that again? I didn't quite catch it.",
    why: "didn't catch it 指没听清，比 didn't hear 更准确——不是没听见，是没听懂。",
    tag: "自然",
  },
];

const CALL_LINES = [
  "聊到周末计划，TA 问你要不要一起去市场。",
  "你听懂了大部分，有一句请对方重复了一次。",
  "结束时 TA 说下次再聊。",
];

let seq = 1000;
const nextId = (prefix: string) => `${prefix}-${(seq += 1)}`;

export function AppProvider({ children }: { children: ReactNode }) {
  const [friends, setFriends] = useState<Friend[]>([]);
  const [requests, setRequests] = useState<Friend[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [moments, setMoments] = useState<Moment[]>(() => []);
  const [expressions, setExpressions] = useState<Expression[]>([]);
  const [profile, setProfile] = useState<Profile>({ name: '', avatar: '', city: '', job: '', targets: ['en'], level: '能聊几句', interests: [], weaknesses: [], birthday: '' });
  /* 是否已拿到真实后端数据（拿不到就一直用 mock，原型仍可浏览） */
  const [live, setLive] = useState(false);
  const [typing, setTyping] = useState<Record<string, boolean>>({});
  const [zhOpen, setZhOpen] = useState<Record<string, boolean>>({});
  const [toast, setToast] = useState<Toast | null>(null);
  const [settings, setSettings] = useState<Settings>({
    llm: {
      preset: "deepseek",
      baseUrl: "https://api.deepseek.com/v1",
      model: "deepseek-flash",
      keyMasked: "sk-••••••••••••3f7a",
      hasKey: true,
    },
    stt: {
      provider: "auto",
      model: "FunAudioLLM/SenseVoiceSmall",
      keyMasked: "",
      hasKey: false,
    },
    tts: {
      provider: "fish",
      emotion: true,
      keyMasked: "fa-••••••••2c91",
      hasKey: true,
      cloned: false,
    },
    notify: { messages: true, practice: false },
  });

  /* 后端可用时用真数据覆盖 mock；不可用则保持 mock 不变 */
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      /* 旧版用 localStorage 记「已看过」，现在红点直接跟后端 unread 走，清掉遗留键 */
      try {
        Object.keys(window.localStorage)
          .filter((k) => k.startsWith("w4.seen.") || k === "w4.baseline")
          .forEach((k) => window.localStorage.removeItem(k));
      } catch {
        /* 隐私模式忽略 */
      }

      const snap = await apiLoadAll();
      if (cancelled || !snap) return;
      setFriends(snap.friends);
      setMessages(snap.messages);
      setRequests(snap.requests ?? []);
      setProfile((prev) => ({ ...prev, ...snap.profile }));

      /* 设置 / 朋友圈 / 表达本 也换成真数据（接口失败就保持 mock） */
      const [core, tts, momentsLive, expressionsLive] = await Promise.all([
        apiLoadSettings(),
        fetch("/api/v2/settings/tts")
          .then((r) => (r.ok ? r.json() : null))
          .then((j) => (j?.tts ?? null))
          .catch(() => null),
        apiLoadMoments(),
        apiLoadExpressions(),
      ]);
      if (cancelled) return;
      if (core) setSettings((prev) => ({ ...prev, ...mapLiveSettings(core, tts) }));
      /* 真后端返回空就是空：朋友圈会显示「让好友们发几条」，不再拿 mock 冒充 */
      if (momentsLive) setMoments(momentsLive);
      if (expressionsLive) setExpressions(expressionsLive);
      setProfile((prev) => ({ ...prev, ...snap.profile }));
      setLive(true);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const timers = useRef<number[]>([]);

  /* 好友/申请轮询：别人来加你好友、好友裂变推荐，不用刷新页面就能看到 */
  useEffect(() => {
    let stopped = false;
    const refresh = async () => {
      if (stopped || document.visibilityState === "hidden") return;
      const snap = await apiLoadFriendList();
      if (stopped || !snap) return;
      setFriends(snap.friends);
      setRequests(snap.requests);
    };
    const timer = window.setInterval(() => void refresh(), isLocalMode() ? 2_500 : 12_000);
    const onVisible = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    const onKick = () => void refresh();
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("speakeasy:refresh", onKick);
    return () => {
      stopped = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("speakeasy:refresh", onKick);
    };
  }, []);
  const toastTimer = useRef<number | null>(null);
  useEffect(() => {
    const list = timers.current;
    return () => {
      list.forEach((t) => window.clearTimeout(t));
      if (toastTimer.current) window.clearTimeout(toastTimer.current);
    };
  }, []);

  const later = useCallback((fn: () => void, ms: number) => {
    const t = window.setTimeout(fn, ms);
    timers.current.push(t);
  }, []);

  const showToast = useCallback((text: string, tone: Toast["tone"] = "default") => {
    setToast({ id: Date.now(), text, tone });
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 2400);
  }, []);

  const messagesOf = useCallback(
    (friendId: string) =>
      [...messages]
        .filter((m) => m.friendId === friendId)
        .sort((a, b) => new Date(a.time).getTime() - new Date(b.time).getTime()),
    [messages],
  );

  const friendById = useCallback((id: string) => friends.find((f) => f.id === id), [friends]);

  const replyIndex = useRef<Record<string, number>>({});
  const transcriptIndex = useRef(0);
  const suggestionIndex = useRef(0);

  const markRead = useCallback((friendId: string) => {
    setFriends((list) => list.map((f) => (f.id === friendId && f.unread ? { ...f, unread: 0 } : f)));
    setMessages((list) => list.map((m) => (m.friendId === friendId && !m.read ? { ...m, read: true } : m)));
    /* 同步到后端：否则刷新一次红点又回来了，看起来就像假红点 */
    void apiMarkThreadRead(friendId);
  }, []);

  const scheduleReply = useCallback(
    (friendId: string) => {
      setTyping((t) => ({ ...t, [friendId]: true }));
      const pool = REPLIES[friendId] ?? DEFAULT_REPLY;
      const index = replyIndex.current[friendId] ?? 0;
      replyIndex.current[friendId] = index + 1;
      const reply = pool[index % pool.length];
      /* 只有「我」发的动态才安排好友评论 */
      later(() => {
        setTyping((t) => ({ ...t, [friendId]: false }));
        setMessages((list) => [
          ...list,
          {
            id: nextId("f"),
            friendId,
            from: "friend",
            kind: reply.ms ? "voice" : "text",
            text: reply.text,
            translation: reply.zh,
            durationMs: reply.ms,
            time: new Date().toISOString(),
            read: true,
          },
        ]);
        setFriends((list) =>
          list.map((f) => (f.id === friendId ? { ...f, lastActive: "刚刚" } : f)),
        );
      }, 1500 + Math.random() * 1300);
    },
    [later],
  );

  const attachSuggestion = useCallback(
    (messageId: string) => {
      later(() => {
        const s = SUGGESTION_POOL[suggestionIndex.current % SUGGESTION_POOL.length];
        suggestionIndex.current += 1;
        setMessages((list) =>
          list.map((m) => (m.id === messageId ? { ...m, suggestion: { ...s, saved: false } } : m)),
        );
      }, 2600);
    },
    [later],
  );

  const sendText = useCallback(
    (friendId: string, text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;

      /* 接了真后端就走真接口：乐观显示 → 等回复 → 用真实消息替换 */
      if (live) {
        const tempId = nextId("u");
        setMessages((list) => [
          ...list,
          {
            id: tempId,
            friendId,
            from: "me",
            kind: "text",
            text: trimmed,
            time: new Date().toISOString(),
            read: false,
          },
        ]);
        setTyping((t) => ({ ...t, [friendId]: true }));
        void (async () => {
          const fresh = await apiSendText(friendId, trimmed);
          setTyping((t) => ({ ...t, [friendId]: false }));
          if (!fresh) {
            setMessages((list) =>
              list.map((m) =>
                m.id === tempId
                  ? { ...m, translation: "没发出去，检查一下网络" }
                  : m,
              ),
            );
            return;
          }
          setMessages((list) => {
            const mine = list.filter((m) => m.friendId !== friendId);
            return [...mine, ...fresh];
          });
          setFriends((list) =>
            list.map((f) =>
              f.id === friendId ? { ...f, lastActive: "刚刚", unread: 0 } : f,
            ),
          );
        })();
        return;
      }
      const id = nextId("u");
      setMessages((list) => [
        ...list,
        {
          id,
          friendId,
          from: "me",
          kind: "text",
          text: trimmed,
          time: new Date().toISOString(),
          read: false,
        },
      ]);
      setFriends((list) => list.map((f) => (f.id === friendId ? { ...f, lastActive: "刚刚" } : f)));
      scheduleReply(friendId);
      if (trimmed.split(/\s+/).length >= 3) attachSuggestion(id);
    },
    [attachSuggestion, scheduleReply, live],
  );

  /** 真实语音：先放一条占位气泡，等后端返回后整体替换 */
  const sendVoiceBlob = useCallback(
    async (
      friendId: string,
      blob: Blob,
      durationMs: number,
      opts?: { openTranscript?: boolean },
    ) => {
      const tempId = nextId("u");
      setMessages((list) => [
        ...list,
        {
          id: tempId,
          friendId,
          from: "me",
          kind: "voice",
          text: "（正在识别…）",
          durationMs,
          time: new Date().toISOString(),
          read: false,
          shownText: Boolean(opts?.openTranscript),
        },
      ]);
      setTyping((t) => ({ ...t, [friendId]: true }));

      const fresh = await apiSendVoiceBlob(friendId, blob, durationMs);
      setTyping((t) => ({ ...t, [friendId]: false }));

      if (!fresh) {
        setMessages((list) => list.filter((m) => m.id !== tempId));
        showToast("这条没发出去，再试一次", "error");
        return;
      }
      /* 后端返回的是该好友的完整消息列表：用它替换这一栏 */
      let merged = fresh;
      if (opts?.openTranscript) {
        const idx = fresh.reduce(
          (acc: number, m: Message, i: number) =>
            m.from === "me" && m.kind === "voice" ? i : acc,
          -1,
        );
        if (idx >= 0) {
          merged = fresh.map((m: Message, i: number) =>
            i === idx ? { ...m, shownText: true } : m,
          );
        }
      }
      setMessages((list) => [...list.filter((m) => m.friendId !== friendId), ...merged]);

    },
    [showToast],
  );

  const sendVoice = useCallback(
    (friendId: string, durationMs: number) => {
      const text = MY_TRANSCRIPTS[transcriptIndex.current % MY_TRANSCRIPTS.length];
      transcriptIndex.current += 1;
      setMessages((list) => [
        ...list,
        {
          id: nextId("u"),
          friendId,
          from: "me",
          kind: "voice",
          text,
          durationMs,
          time: new Date().toISOString(),
          read: false,
          shownText: false,
        },
      ]);
      setFriends((list) => list.map((f) => (f.id === friendId ? { ...f, lastActive: "刚刚" } : f)));
      scheduleReply(friendId);
    },
    [scheduleReply],
  );

  const sendImage = useCallback(
    (friendId: string, image: string) => {
      setMessages((list) => [
        ...list,
        {
          id: nextId("u"),
          friendId,
          from: "me",
          kind: "image",
          text: "",
          image,
          time: new Date().toISOString(),
          read: false,
        },
      ]);
      scheduleReply(friendId);
    },
    [scheduleReply],
  );

  const toggleZh = useCallback((key: string) => {
    setZhOpen((open) => ({ ...open, [key]: !open[key] }));
  }, []);

  const translateMessage = useCallback(
    async (messageId: string) => {
      const msg = messages.find((m) => m.id === messageId);
      if (!msg) return;
      /* 已有译文：只切换显示 */
      if (msg.translation) {
        setZhOpen((open) => ({ ...open, [messageId]: !open[messageId] }));
        return;
      }
      showToast("正在翻译…");
      const zh = await apiTranslate(msg.text, msg.friendId);
      if (!zh) {
        showToast("翻译失败，稍后再试", "error");
        return;
      }
      setMessages((list) =>
        list.map((m) => (m.id === messageId ? { ...m, translation: zh } : m)),
      );
      setZhOpen((open) => ({ ...open, [messageId]: true }));
    },
    [messages, showToast],
  );

  const translateAll = useCallback(
    (friendId: string) => {
      setZhOpen((open) => {
        const next = { ...open };
        messages
          .filter((m) => m.friendId === friendId && m.from === "friend" && m.translation)
          .forEach((m) => {
            next[m.id] = true;
          });
        return next;
      });
      showToast("已翻译最近消息", "success");
    },
    [messages, showToast],
  );

  const translateAllMoments = useCallback(() => {
    setZhOpen((open) => {
      const next = { ...open };
      moments.forEach((m) => {
        if (m.authorId !== "me" && m.translation) next[`mo:${m.id}`] = true;
      });
      return next;
    });
    showToast("已翻译全部动态", "success");
  }, [moments, showToast]);

  const toggleShownText = useCallback((messageId: string) => {
    setMessages((list) =>
      list.map((m) => (m.id === messageId ? { ...m, shownText: !m.shownText } : m)),
    );
  }, []);

  const saveSuggestion = useCallback(
    (messageId: string) => {
      const msg = messages.find((m) => m.id === messageId);
      if (!msg || !msg.suggestion || msg.suggestion.saved) return;
      const s = msg.suggestion;
      setMessages((list) =>
        list.map((m) => (m.id === messageId ? { ...m, suggestion: { ...m.suggestion!, saved: true } } : m)),
      );
      setExpressions((list) => [
        {
          id: nextId("e"),
          original: msg.text,
          better: s.better,
          why: s.why,
          sourceFriendId: msg.friendId,
          createdAt: "刚刚",
          status: "learning",
          mastery: 0,
          due: 100,
        },
        ...list,
      ]);
      showToast("已加入表达本", "success");
    },
    [messages, showToast],
  );

  const dismissSuggestion = useCallback((messageId: string) => {
    setMessages((list) => list.map((m) => (m.id === messageId ? { ...m, suggestion: undefined } : m)));
  }, []);

  const likeMoment = useCallback((momentId: string) => {
    setMoments((list) =>
      list.map((m) => {
        if (m.id !== momentId) return m;
        const liked = !m.liked;
        return {
          ...m,
          liked,
          likedBy: liked ? [...m.likedBy, "me"] : m.likedBy.filter((id) => id !== "me"),
        };
      }),
    );
  }, []);

  const deleteMoment = useCallback(
    async (momentId: string) => {
      const ok = await apiDeleteMoment(momentId);
      if (!ok) {
        showToast("删除失败，稍后再试");
        return;
      }
      setMoments((list) => list.filter((m) => m.id !== momentId));
      showToast("已删除");
    },
    [showToast],
  );

  const addComment = useCallback((momentId: string, text: string) => {
    const comment: MomentComment = { id: nextId("cm"), friendId: "me", text, time: "刚刚" };
    setMoments((list) =>
      list.map((m) => (m.id === momentId ? { ...m, comments: [...m.comments, comment] } : m)),
    );
  }, []);

  const publishMoment = useCallback(
    (text: string, images: string[], mood: string) => {
      const id = nextId("mo");
      /* 真后端：先落库，AI 好友评论由后端后台生成，稍后拉回 */
      void apiPublishMoment(text, mood).then(() => {
        later(() => {
          void apiLoadMoments().then((fresh) => {
            if (fresh && fresh.length) setMoments(fresh);
          });
        }, 6000);
      });
      setMoments((list) => [
        { id, authorId: "me", images, text, mood, time: "刚刚", comments: [], likedBy: [], commentsPending: true },
        ...list,
      ]);
      later(() => {
        setMoments((list) =>
          list.map((m) =>
            m.id === id
              ? {
                  ...m,
                  commentsPending: false,
                  comments: [
                    {
                      id: nextId("cm"),
                      friendId: "callum",
                      text: "Looks decent. Next time bring one to the shop and I'll judge it properly.",
                      time: "刚刚",
                    },
                    {
                      id: nextId("cm"),
                      friendId: "camila",
                      text: "Que bonito! Quero ver mais fotos.",
                      time: "刚刚",
                    },
                  ],
                }
              : m,
          ),
        );
      }, 3400);
    },
    [later],
  );

  const acceptRequest = useCallback(
    (friendId: string) => {
      const req = requests.find((r) => r.id === friendId);
      if (!req) return;
      setRequests((list) => list.filter((r) => r.id !== friendId));
      setFriends((list) => [
        { ...req, isRequest: false, requestNote: undefined, closeness: 1, unread: 1, lastActive: "刚刚" },
        ...list,
      ]);
      setMessages((list) => [
        ...list,
        {
          id: nextId("f"),
          friendId,
          from: "friend",
          kind: "voice",
          text: req.greeting,
          translation: req.greetingZh,
          durationMs: 5200,
          time: new Date().toISOString(),
          read: false,
        },
      ]);
      showToast(`已添加 ${req.name}`, "success");
      void apiAcceptFriend(friendId);
    },
    [requests, showToast],
  );

  const declineRequest = useCallback((friendId: string) => {
    setRequests((list) => list.filter((r) => r.id !== friendId));
    showToast("已忽略");
    void apiDeclineFriend(friendId);
  }, [showToast]);

  const reloadAll = useCallback(async () => {
    const snap = await apiLoadAll();
    if (!snap) return;
    setFriends(snap.friends);
    setMessages(snap.messages);
    setRequests(snap.requests ?? []);
  }, []);

  const addDiscovered = useCallback(
    (candidate: DiscoverCandidate) => {
      const friend: Friend = {
        id: candidate.id,
        name: candidate.name,
        avatar: candidate.avatar,
        city: candidate.city,
        country: candidate.country,
        flag: candidate.flag,
        language: candidate.language,
        languageLabel: candidate.languageLabel,
        timezone: "",
        localTime: "",
        localPeriod: "",
        localActivity: "",
        occupation: candidate.occupation,
        age: candidate.age,
        bio: candidate.bio,
        quirks: candidate.tags,
        closeness: 1,
        turns: 1,
        sharedEpisodes: [],
        openLoops: [],
        unread: 0,
        online: candidate.online,
        lastActive: "刚刚",
        voice: "",
        voiceLabel: candidate.voiceLabel,
        greeting: candidate.greeting,
        greetingZh: candidate.greetingZh,
      };
      setFriends((list) => (list.some((f) => f.id === friend.id) ? list : [friend, ...list]));
      setMessages((list) => [
        ...list,
        {
          id: nextId("f"),
          friendId: friend.id,
          from: "friend",
          kind: "voice",
          text: candidate.greeting,
          translation: candidate.greetingZh,
          durationMs: 5600,
          time: new Date().toISOString(),
          read: true,
        },
      ]);
      return friend;
    },
    [],
  );

  const practiceExpression = useCallback((id: string, remembered: boolean) => {
    const cur = expressions.find((e) => e.id === id);
    const nextMastery = Math.min(3, (cur?.mastery ?? 1) + (remembered ? 1 : 0));
    void apiPatchExpression(id, {
      mastery_count: nextMastery,
      status: nextMastery >= 3 ? "mastered" : "learning",
    });
    setExpressions((list) =>
      list.map((e) => {
        if (e.id !== id) return e;
        const mastery = Math.min(3, e.mastery + (remembered ? 1 : 0));
        return {
          ...e,
          mastery,
          due: Math.max(0, e.due - (remembered ? 45 : 8)),
          status: mastery >= 3 ? "mastered" : "learning",
          lastPracticed: "刚刚",
        };
      }),
    );
  }, [expressions]);

  const toggleMastered = useCallback((id: string) => {
    const cur = expressions.find((e) => e.id === id);
    const toMastered = cur?.status !== "mastered";
    setExpressions((list) =>
      list.map((e) =>
        e.id === id
          ? e.status === "mastered"
            ? { ...e, status: "learning", mastery: 1, due: 60 }
            : { ...e, status: "mastered", mastery: 3, due: 0 }
          : e,
      ),
    );
    void apiPatchExpression(id, {
      status: toMastered ? "mastered" : "learning",
      mastery_count: toMastered ? 3 : 1,
    });
  }, [expressions]);

  const removeExpression = useCallback((id: string) => {
    setExpressions((list) => list.filter((e) => e.id !== id));
    void apiDeleteExpression(id);
  }, []);

  const updateProfile = useCallback((patch: Partial<Profile>) => {
    setProfile((p) => ({ ...p, ...patch }));
  }, []);

  const updateSettings = useCallback((patch: Partial<Settings>) => {
    setSettings((s) => ({ ...s, ...patch }));
    // 只同步后端真正支持的字段（TTS provider）；密钥类不在这里回写
    if (patch.tts) {
      void apiSaveTts({ provider: patch.tts.provider === "fish" ? "fish" : "system" });
    }
  }, []);

  const updateFriendVoice = useCallback((friendId: string, voice: string, voiceLabel: string) => {
    setFriends((list) => list.map((f) => (f.id === friendId ? { ...f, voice, voiceLabel } : f)));
    // 本地模式：音色写回本地引擎，聊天 / 通话都立即生效
    if (isLocalMode() && voice) {
      void apiSetFriendVoice(friendId, voice);
    }
  }, []);

  const deleteFriend = useCallback((friendId: string) => {
    setFriends((list) => list.filter((f) => f.id !== friendId));
    setMessages((list) => list.filter((m) => m.friendId !== friendId));
    void apiRemoveFriend(friendId);
  }, []);

  const refreshMoments = useCallback(async () => {
    const fresh = await apiLoadMoments();
    if (fresh && fresh.length) setMoments(fresh);
  }, []);

  const generateMoments = useCallback(async () => {
    showToast("正在让好友们发动态…");
    const fresh = await apiGenerateMoments();
    if (fresh && fresh.length) {
      setMoments(fresh);
      showToast("今天的朋友圈来了", "success");
    } else {
      showToast("生成失败，稍后再试", "error");
    }
  }, [showToast]);

  const callSummary = useCallback(
    (friendId: string, seconds: number, turns = 1) => {
      const mm = String(Math.floor(seconds / 60)).padStart(2, "0");
      const ss = String(seconds % 60).padStart(2, "0");
      /* 先本地落一条，保证 UI 立刻可见 */
      setMessages((list) => [
        ...list,
        {
          id: nextId("call"),
          friendId,
          from: "system",
          kind: "call",
          text: `通话 ${mm}:${ss}｜聊了几句`,
          time: new Date().toISOString(),
        },
      ]);
      /* 后端把摘要写进聊天记录；成功后用真实数据替换这一栏 */
      void apiSaveCallSummary(friendId, seconds, Math.max(1, turns)).then(async (ok) => {
        if (!ok) return;
        const fresh = await apiFetchMessages(friendId);
        if (fresh?.length) {
          setMessages((list) => [...list.filter((m) => m.friendId !== friendId), ...fresh]);
        }
      });
    },
    [],
  );

  const value = useMemo<Store>(
    () => ({
      friends,
      requests,
      messages,
      moments,
      expressions,
      profile,
      settings,
      toast,
      typing,
      zhOpen,
      showToast,
      messagesOf,
      friendById,
      sendText,
      sendVoiceBlob,
      sendVoice,
      sendImage,
      markRead,
      toggleZh,
      translateMessage,
      translateAll,
      translateAllMoments,
      toggleShownText,
      saveSuggestion,
      dismissSuggestion,
      likeMoment,
      addComment,
      deleteMoment,
      publishMoment,
      refreshMoments,
      generateMoments,
      acceptRequest,
      declineRequest,
      addDiscovered,
      practiceExpression,
      toggleMastered,
      removeExpression,
      updateProfile,
      updateSettings,
      updateFriendVoice,
      deleteFriend,
      callSummary,
      reloadAll,
      discoverPool: DISCOVER_POOL,
    }),
    [
      friends,
      requests,
      messages,
      moments,
      expressions,
      profile,
      settings,
      toast,
      typing,
      zhOpen,
      showToast,
      messagesOf,
      friendById,
      sendText,
      sendVoiceBlob,
      sendVoice,
      sendImage,
      markRead,
      toggleZh,
      translateAll,
      translateAllMoments,
      toggleShownText,
      saveSuggestion,
      dismissSuggestion,
      likeMoment,
      addComment,
      publishMoment,
      refreshMoments,
      generateMoments,
      acceptRequest,
      declineRequest,
      addDiscovered,
      practiceExpression,
      toggleMastered,
      removeExpression,
      updateProfile,
      updateSettings,
      updateFriendVoice,
      deleteFriend,
      callSummary,
    ],
  );

  return <AppCtx.Provider value={value}>{children}</AppCtx.Provider>;
}
