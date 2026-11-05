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
  if (isLocalMode()) return null;
  return server.deleteExpressionOnServer(id);
}

export async function generateMoments() {
  if (isLocalMode()) return [];
  return server.generateMoments();
}

export async function loadExpressions() {
  if (isLocalMode()) return [];
  return server.loadExpressions();
}

export async function loadMoments() {
  if (isLocalMode()) return [];
  return server.loadMoments();
}

export async function loadSettings() {
  if (isLocalMode()) return null;
  return server.loadSettings();
}

export async function patchExpression(id: string, fields: Record<string, unknown>) {
  if (isLocalMode()) return null;
  return server.patchExpression(id, fields);
}

export async function publishMomentToServer(text: string, mood: string) {
  if (isLocalMode()) return null;
  return server.publishMomentToServer(text, mood);
}

export async function saveTts(body: Record<string, string | undefined>) {
  if (isLocalMode()) return null;
  return server.saveTts(body);
}

export async function deleteMoment(momentId: string) {
  if (isLocalMode()) return null;
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
