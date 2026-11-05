/**
 * 本地模式（APK 内独立运行）的配置与开关。
 *
 * 本地模式下所有 key 存在手机本地，不发到任何服务器；
 * 数据存 IndexedDB，云端只用于：DeepSeek（对话）、Fish（语音合成）、
 * SiliconFlow（语音识别）。
 */

export type LocalKeys = {
  llmBase: string;
  llmKey: string;
  llmModel: string;
  fishKey: string;
  fishRef: string;
  fishModel: string;
  fishBase: string;
  sttKey: string;
  sttModel: string;
  sttBase: string;
};

const KEY = "speakeasy.local.keys.v1";
const MODE_KEY = "speakeasy.mode";

export const DEFAULT_KEYS: LocalKeys = {
  llmBase: "https://api.deepseek.com/v1",
  llmKey: "",
  llmModel: "deepseek-flash",
  fishKey: "",
  fishRef: "",
  fishModel: "s2.1-pro-free",
  fishBase: "https://api.fish.audio",
  sttKey: "",
  sttModel: "FunAudioLLM/SenseVoiceSmall",
  sttBase: "https://api.siliconflow.cn/v1",
};

export function loadKeys(): LocalKeys {
  if (typeof window === "undefined") return { ...DEFAULT_KEYS };
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULT_KEYS };
    return { ...DEFAULT_KEYS, ...(JSON.parse(raw) as Partial<LocalKeys>) };
  } catch {
    return { ...DEFAULT_KEYS };
  }
}

export function saveKeys(patch: Partial<LocalKeys>): LocalKeys {
  const next = { ...loadKeys(), ...patch };
  try {
    window.localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    /* 空间不足时忽略 */
  }
  return next;
}

/** 是否本地模式：构建时变量优先，其次看运行时开关（设置里可切） */
export function isLocalMode(): boolean {
  if (typeof window === "undefined") return process.env.NEXT_PUBLIC_LOCAL_MODE === "1";
  if (process.env.NEXT_PUBLIC_LOCAL_MODE === "1") return true;
  try {
    return window.localStorage.getItem(MODE_KEY) === "local";
  } catch {
    return false;
  }
}

export function setMode(mode: "local" | "server"): void {
  try {
    window.localStorage.setItem(MODE_KEY, mode);
  } catch {
    /* ignore */
  }
}
