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

const MIN_GAP_MS = 70;
let lastAt = 0;
let lastType: HapticType | null = null;
const history: HapticEvent[] = [];

function enabled(): boolean {
  return themeStore.getSnapshot().settings.haptics;
}

/**
 * Semantic haptics. One action maps to exactly one call; continuous gestures are
 * debounced. The browser adapter deliberately does not vibrate — it only records
 * events for the preview panel. Android will implement `performHaptic`.
 */
export function haptic(type: HapticType, options?: { force?: boolean }): boolean {
  if (!enabled()) return false;
  const now = Date.now();
  if (!options?.force && now - lastAt < MIN_GAP_MS) return false;
  lastAt = now;
  lastType = type;

  const native = nativeBridge();
  let performed = false;
  if (native?.performHaptic) {
    try {
      native.performHaptic(NATIVE_TYPE[type]);
      performed = true;
    } catch {
      performed = false;
    }
  }

  const event: HapticEvent = {
    type,
    at: now,
    platform: performed ? "android" : "web",
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
