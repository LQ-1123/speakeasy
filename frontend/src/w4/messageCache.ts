import type { Message } from "./model";

/** 乐观消息 id 前缀：store 里 nextId("pending") 用同一约定。 */
const PENDING_PREFIX = "pending-";

/**
 * 客户端消息 id（幂等键）。
 *
 * 发送时随 POST /api/v2/chat/{id}/text 一起提交；服务端在返回的消息里带回来，
 * 于是「乐观气泡」和「服务端气泡」可以按 clientId 对齐，即使两边 id 不同。
 * 契约要求 8–64 位 [A-Za-z0-9._:-]，crypto.randomUUID 的 36 位 UUID 合法。
 */
export function createClientId(): string {
  try {
    const uuid = globalThis.crypto?.randomUUID?.();
    if (uuid) return uuid;
  } catch {
    /* 非安全上下文（http 局域网）没有 randomUUID，走降级随机 */
  }
  const random = () =>
    Math.random().toString(36).slice(2, 10);
  return `c-${Date.now().toString(36)}-${random()}${random()}`.slice(0, 64);
}

/** 是否是还没被服务端确认的本地乐观消息（发送中/发送失败）。 */
export function isOptimisticMessage(message: Message): boolean {
  return message.from === "me" && Boolean(message.clientId) && message.id.startsWith(PENDING_PREFIX);
}

/**
 * 合并一次线程刷新：保留更早的分页与其它线程，同一 id 用新数据覆盖，
 * 并且按 clientId 把服务端消息和本地乐观消息合成一条（id 不同也去重）。
 */
export function mergeMessages(current: Message[], incoming: Message[], removeIds: string[] = []): Message[] {
  const removed = new Set(removeIds);
  const byId = new Map<string, Message>();
  /* clientId → 合并后保留的 id（乐观消息 id 会被服务端 id 取代） */
  const byClientId = new Map<string, string>();

  for (const item of current) {
    if (removed.has(item.id)) continue;
    byId.set(item.id, item);
    if (item.clientId) byClientId.set(item.clientId, item.id);
  }

  for (const item of incoming) {
    const existingId =
      (byId.has(item.id) ? item.id : undefined) ??
      (item.clientId ? byClientId.get(item.clientId) : undefined);
    const previous = existingId ? byId.get(existingId) : undefined;
    if (existingId && existingId !== item.id) {
      byId.delete(existingId);
    }
    const merged: Message = previous ? { ...previous, ...item } : { ...item };
    /* 好友语音在服务端是「先落文字、后台补音频」：短轮询拿到音频前，同一条消息
       会以 text 形态出现。这里把已经确认是语音的消息保留成语音气泡，避免
       气泡先显示文字、过几秒才“变成”语音。 */
    if (previous?.kind === "voice" && merged.kind === "text") {
      merged.kind = "voice";
    }
    if (previous?.durationMs && !item.durationMs) {
      merged.durationMs = previous.durationMs;
    }
    /* 音频补全后清掉「生成中」，避免到达后还显示等待态 */
    if (merged.audio) {
      delete merged.audioPending;
    } else if (merged.audioStatus === "failed" || merged.audioStatus === "ready") {
      merged.audioPending = false;
    } else if (previous?.audioPending && item.audioPending === undefined) {
      merged.audioPending = true;
    }
    /* 服务端确认过 clientId 的消息不再处于失败态 */
    if (item.clientId) {
      byClientId.set(item.clientId, item.id);
      if (merged.failed) delete merged.failed;
    }
    byId.set(item.id, merged);
  }

  return [...byId.values()].sort((a, b) => a.time.localeCompare(b.time) || a.id.localeCompare(b.id));
}

/**
 * 线程刷新后对齐本地乐观消息：
 *
 * - 服务端已经返回同 clientId 的消息 → mergeMessages 已经替换掉乐观气泡；
 * - 乐观消息比服务端最新一条还新 → 视为仍在发送中，保留「发送中」；
 * - 乐观消息不晚于服务端最新一条却仍没被确认 → 这次 POST 实际没落库/丢失，
 *   标记 failed 让用户点重试（复用同一 clientId，后端幂等不会重复落库）。
 */
export function reconcileThread(current: Message[], incoming: Message[], friendId: string): Message[] {
  const merged = mergeMessages(current, incoming);
  const fresh = incoming.filter((item) => item.friendId === friendId);
  if (!fresh.length) return merged;
  let newest = 0;
  for (const item of fresh) {
    const at = Date.parse(item.time);
    if (Number.isFinite(at) && at > newest) newest = at;
  }
  if (!newest) return merged;
  return merged.map((item) => {
    if (item.friendId !== friendId || !isOptimisticMessage(item) || item.failed) return item;
    const at = Date.parse(item.time);
    if (Number.isFinite(at) && at <= newest) return { ...item, failed: true };
    return item;
  });
}
