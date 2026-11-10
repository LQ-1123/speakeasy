"use client";

import { nativeBridge } from "../w4/v3/clientApi";
import { themeStore } from "./theme-store";

export type HapticType =
  | "selection"
  | "toggle"
  | "longPress"
  | "recordStart"
  | "recordCancel"
  | "send"
  | "save"
  | "success"
  | "error"
  | "callControl";

export interface HapticEvent {
  type: HapticType;
  at: number;
  platform: "web" | "android";
  performed: boolean;
}

const NATIVE_TYPE: Record<HapticType, string> = {
  selection: "selection",
  toggle: "toggle",
  longPress: "longPress",
  recordStart: "recordStart",
  recordCancel: "recordCancel",
  send: "send",
  save: "save",
  success: "success",
  error: "error",
  callControl: "callControl",
};

/**
 * Browser/PWA fallback. Android maps the same actions to real vibration patterns
 * through NativeBridge.performHaptic; a mobile browser may expose navigator.vibrate.
 * Durations mirror the native patterns so the feel stays consistent.
 */
const WEB_PATTERN: Record<HapticType, number | number[]> = {
  selection: 8,
  toggle: 12,
  longPress: 22,
  recordStart: 16,
  recordCancel: [12, 40, 12],
  send: 10,
  save: 14,
  success: [10, 60, 16],
  error: [34, 70, 34],
  callControl: 18,
};

const MIN_GAP_MS = 70;
let lastAt = 0;
let lastType: HapticType | null = null;
const history: HapticEvent[] = [];

function enabled(): boolean {
  return themeStore.getSnapshot().settings.haptics;
}

function webHaptic(type: HapticType): boolean {
  if (typeof navigator === "undefined" || typeof navigator.vibrate !== "function") return false;
  try {
    return navigator.vibrate(WEB_PATTERN[type]) === true;
  } catch {
    return false;
  }
}

/**
 * Semantic haptics. One action maps to exactly one call; continuous gestures are
 * debounced. The Android bridge reports whether the OS actually vibrated
 * (system haptics off / no vibrator => false). Without a native bridge the
 * browser vibrate API is used when available (mobile PWA).
 */
export function haptic(type: HapticType, options?: { force?: boolean }): boolean {
  if (!enabled()) return false;
  const now = Date.now();
  if (!options?.force && now - lastAt < MIN_GAP_MS) return false;
  lastAt = now;
  lastType = type;

  const native = nativeBridge();
  let performed = false;
  let nativeHandled = false;
  if (native?.performHaptic) {
    try {
      performed = native.performHaptic(NATIVE_TYPE[type]) === true;
      nativeHandled = true;
    } catch {
      performed = false;
    }
  }
  /* Only fall back when the platform has no native implementation at all — never
     bypass an Android user who turned system touch feedback off. */
  if (!nativeHandled) performed = webHaptic(type);

  const event: HapticEvent = {
    type,
    at: now,
    platform: nativeHandled ? "android" : "web",
    performed,
  };
  history.push(event);
  if (history.length > 50) history.shift();
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("speakeasy:haptic", { detail: event }));
  }
  return performed;
}

export function hapticHistory(): HapticEvent[] {
  return history.slice();
}

export function resetHapticHistory(): void {
  history.length = 0;
  lastAt = 0;
  lastType = null;
}

/** Named service boundary used by screens and platform adapters. */
export const HapticService = {
  trigger: haptic,
  history: hapticHistory,
  reset: resetHapticHistory,
};
