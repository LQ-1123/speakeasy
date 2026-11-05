/**
 * API 门面：根据运行模式（本地 / 服务器）分发。
 *
 * 本地模式：所有业务逻辑跑在手机里（local/engine.ts），数据存 IndexedDB。
 * 服务器模式：保持原有行为，请求远端 FastAPI。
 */

import * as server from "./api";
import { isLocalMode } from "./local/keys";

const local = () => import("./local/engine");

export async function loadAll() {
  if (isLocalMode()) return (await local()).localLoadAll();
  return server.loadAll();
}

export async function sendText(friendId: string, text: string) {
  if (isLocalMode()) return (await local()).localSendText(friendId, text);
  return server.sendText(friendId, text);
}

export async function sendVoiceBlob(friendId: string, blob: Blob, durationMs: number) {
  if (isLocalMode()) return (await local()).localSendVoiceBlob(friendId, blob, durationMs);
  return server.sendVoiceBlob(friendId, blob, durationMs);
}

export async function fetchMessages(friendId: string) {
  if (isLocalMode()) return (await local()).localFetchMessages(friendId);
  return server.fetchMessages(friendId);
}

export async function acceptFriend(id: string) {
  if (isLocalMode()) return (await local()).localAcceptFriend(id);
  return server.acceptFriend(id);
}

export async function declineFriend(id: string) {
  if (isLocalMode()) return (await local()).localDeclineFriend(id);
  return server.declineFriend(id);
}

export async function removeFriend(id: string) {
  if (isLocalMode()) return (await local()).localRemoveFriend(id);
  return server.removeFriend(id);
}

export async function markThreadRead(friendId: string) {
  if (isLocalMode()) return (await local()).localMarkRead(friendId);
  return server.markThreadRead(friendId);
}

export async function translate(text: string, friendId: string) {
  if (isLocalMode()) return (await local()).localTranslate(text);
  return server.translate(text, friendId);
}

export async function loadFriendList() {
  if (isLocalMode()) return (await local()).localLoadFriendList();
  return server.loadFriendList();
}

export async function saveProfile(nickname: string, targets: string[], birthday?: string) {
  if (isLocalMode()) return (await local()).localSaveProfile(nickname, targets, birthday);
  return server.saveProfile(nickname, targets, birthday);
}

/* ------- 以下 L1 阶段未实现，本地模式返回空值，保证 UI 不崩 ------- */

export async function callWarmup(friendId: string) {
  if (isLocalMode()) return null;
  return server.callWarmup(friendId);
}

export async function saveCallSummary(friendId: string, durationS: number, turns: number) {
  if (isLocalMode()) return null;
  return server.saveCallSummary(friendId, durationS, turns);
}

export async function deleteExpressionOnServer(id: string) {
  if (isLocalMode()) return (await local()).localDeleteExpression(id);
  return server.deleteExpressionOnServer(id);
}

export async function generateMoments() {
  if (isLocalMode()) return (await local()).localGenerateMoments();
  return server.generateMoments();
}

export async function loadExpressions() {
  if (isLocalMode()) return (await local()).localLoadExpressions();
  return server.loadExpressions();
}

export async function loadMoments() {
  if (isLocalMode()) return (await local()).localLoadMoments();
  return server.loadMoments();
}

export async function loadSettings() {
  if (isLocalMode()) return null;
  return server.loadSettings();
}

export async function patchExpression(id: string, fields: Record<string, unknown>) {
  if (isLocalMode()) return (await local()).localPatchExpression(id, fields);
  return server.patchExpression(id, fields);
}

export async function publishMomentToServer(text: string, mood: string) {
  if (isLocalMode()) return (await local()).localPublishMoment(text, mood);
  return server.publishMomentToServer(text, mood);
}

export async function saveTts(body: Record<string, string | undefined>) {
  if (isLocalMode()) return null;
  return server.saveTts(body);
}

export async function deleteMoment(momentId: string) {
  if (isLocalMode()) return (await local()).localDeleteMoment(momentId);
  return server.deleteMoment(momentId);
}

export async function discover(language?: string) {
  if (isLocalMode()) return (await local()).localDiscover(language);
  return server.discover(language);
}

export async function discoverLanguages() {
  if (isLocalMode()) return ["en", "ja", "ko", "es", "fr", "de", "it", "pt"];
  return server.discoverLanguages();
}

export async function loadArchetypes() {
  if (isLocalMode()) return [];
  return server.loadArchetypes();
}

export async function changeFriendVoice(friendId: string, archetypeId: string) {
  if (isLocalMode()) return null;
  return server.changeFriendVoice(friendId, archetypeId);
}

/** 本地模式没有服务器音频 URL（语音由本地 Fish 合成 blob），返回空串 */
export function ttsSampleUrl(text: string, voice: string, friendId?: string): string {
  if (isLocalMode()) return "";
  return server.ttsSampleUrl(text, voice, friendId);
}

/** 本地模式：现场合成语音并返回可播放 URL；服务器模式回落到 /tts/sample */
export async function synthesizeVoice(text: string, friendId?: string): Promise<string | null> {
  if (!text.trim()) return null;
  if (isLocalMode()) {
    const eng = await local();
    const f = friendId ? await eng.localFriendById(friendId) : null;
    const { fishTts } = await import("./local/tts");
    const r = await fishTts(text, { gender: f?.gender === "male" ? "male" : "female" });
    return r?.url ?? null;
  }
  return null;
}

export async function callTurnStream(
  friendId: string,
  blob: Blob,
  durationMs: number,
  onEvent: (e: import("./api").CallTurnEvent) => void,
) {
  if (isLocalMode()) {
    const eng = await local();
    return eng.localCallTurnStream(friendId, blob, durationMs, (e) =>
      onEvent(e as unknown as import("./api").CallTurnEvent),
    );
  }
  return server.callTurnStream(friendId, blob, durationMs, onEvent);
}


export async function commentMomentOnServer(momentId: string, text: string) {
  if (isLocalMode()) return (await local()).localCommentMoment(momentId, text);
  return server.commentMomentOnServer(momentId, text);
}

export async function addExpression(better: string, original = "", explanation = "") {
  if (isLocalMode()) return (await local()).localAddExpression(better, original, explanation);
  return server.addExpression(better, original, explanation);
}

/** 播放一段音频 URL：优先 WebAudio（荣耀/华为 WebView 更稳），失败再退回 <audio> */
export async function playVoiceUrl(url: string): Promise<boolean> {
  if (!url) return false;
  try {
    const { playUrlWebAudio } = await import("./local/tts");
    const ok = await playUrlWebAudio(url);
    if (ok) return true;
  } catch {
    /* 继续尝试 audio 元素 */
  }
  try {
    await new Promise<void>((resolve, reject) => {
      const a = new Audio(url);
      a.onended = () => resolve();
      a.onerror = () => reject(new Error("audio error"));
      void a.play().then(() => resolve()).catch(reject);
    });
    return true;
  } catch {
    return false;
  }
}

export async function exportLocalData(): Promise<string | null> {
  if (!isLocalMode()) return null;
  return (await local()).localExportData();
}

export async function importLocalData(json: string): Promise<boolean> {
  if (!isLocalMode()) return false;
  return (await local()).localImportData(json);
}

export async function wipeLocalData(): Promise<boolean> {
  if (!isLocalMode()) return false;
  return (await local()).localWipeData();
}

export async function callOpening(friendId: string): Promise<string> {
  if (isLocalMode()) return (await local()).localCallOpening(friendId);
  return "Can you hear me?";
}
