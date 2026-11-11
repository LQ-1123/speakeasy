/**
 * 退出登录时的本地数据清理（多用户共用设备隐私）。
 *
 * 方案要求：退出或切换账号时，统一清理客户端数据、缓存、音频、通话、事件连接和通知，
 * 并重新绑定推送设备。
 *
 * 这里只做**本地**清理；服务端会话撤销由 session.ts 的 logout 完成，
 * 页面 reload 会断开 WebSocket/通话并让推送在新身份下重新注册。
 */

/** 需要清理的 localStorage 前缀（身份、事件游标、已看记录、广告去重、提示音等）。 */
export const CLEAR_PREFIXES = ["speakeasy.", "sonder.", "w4."] as const;

export function selectKeysToClear(keys: string[]): string[] {
  return keys.filter((key) => CLEAR_PREFIXES.some((prefix) => key.startsWith(prefix)));
}

/** 清理 localStorage 中与账号/缓存相关的键，返回被删掉的键名（便于测试/排查）。 */
export function clearLocalClientData(storage?: Storage | null): string[] {
  let store: Storage | null = storage ?? null;
  if (store === null) {
    try {
      store = typeof window !== "undefined" ? window.localStorage : null;
    } catch {
      store = null;
    }
  }
  if (!store) return [];
  let keys: string[] = [];
  try {
    keys = Object.keys(store);
  } catch {
    return [];
  }
  const removed: string[] = [];
  for (const key of selectKeysToClear(keys)) {
    try {
      store.removeItem(key);
      removed.push(key);
    } catch {
      /* 隐私模式忽略 */
    }
  }
  return removed;
}

/** 清空 sessionStorage（广告曝光去重、提醒弹窗标记等页面级状态）。 */
export function clearSessionState(storage?: Storage | null): void {
  let store: Storage | null = storage ?? null;
  if (store === null) {
    try {
      store = typeof window !== "undefined" ? window.sessionStorage : null;
    } catch {
      store = null;
    }
  }
  try {
    store?.clear();
  } catch {
    /* 隐私模式忽略 */
  }
}
