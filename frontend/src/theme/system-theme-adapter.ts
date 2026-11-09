"use client";

import { nativeBridge } from "../w4/v3/clientApi";
import { honorSnapshot, makeSnapshot, validateSnapshot } from "./palette";
import type { ThemeCapabilities, ThemeSnapshot } from "./types";

export interface SystemThemeRead {
  snapshot: ThemeSnapshot | null;
  capabilities: Partial<ThemeCapabilities>;
  /** True when the native bridge supplied the complete semantic palette. */
  complete: boolean;
}

/**
 * System source adapter (Android WebView today, browser fallback otherwise).
 * It never asserts dynamic-color availability from an OS version: capability is
 * reported only when resource reads succeed and every required role is present.
 */
export function readSystemThemeSnapshot(): SystemThemeRead {
  const native = nativeBridge();
  const capabilities: Partial<ThemeCapabilities> = {};
  if (!native) return { snapshot: null, capabilities, complete: false };

  try {
    const raw = JSON.parse(native.getCapabilities?.() || "{}") as {
      dynamicColor?: boolean;
      semanticColors?: boolean;
      haptics?: boolean;
    };
    capabilities.dynamicColor = Boolean(raw.dynamicColor);
    capabilities.systemSemanticRoles = Boolean(raw.semanticColors);
    capabilities.haptics = Boolean(raw.haptics);
  } catch {
    /* Older bridge. */
  }

  const complete = capabilities.systemSemanticRoles === true;
  try {
    const payload = native.getThemeSnapshot?.();
    if (payload) {
      const parsed = validateSnapshot(JSON.parse(payload));
      if (parsed.ok && parsed.snapshot) {
        return { snapshot: parsed.snapshot, capabilities, complete: complete || parsed.snapshot.capabilities.systemSemanticRoles };
      }
    }
  } catch {
    /* Fall through to the legacy accent bridge. */
  }

  try {
    const legacy = JSON.parse(native.getTheme?.() || "{}") as {
      accent?: string;
      onAccent?: string;
      primaryContainer?: string;
      onPrimaryContainer?: string;
    };
    if (legacy.accent) {
      return {
        snapshot: legacyThemeToSnapshot(legacy, capabilities),
        capabilities,
        complete: false,
      };
    }
  } catch {
    /* No usable system theme. */
  }
  return { snapshot: null, capabilities, complete };
}

/**
 * Theme events are separate from insets/keyboard events. Re-sample only on
 * foreground / configuration / wallpaper notifications — never on IME changes.
 */
export function installSystemThemeListener(onChange: () => void): () => void {
  if (typeof window === "undefined") return () => undefined;
  const onNative = (raw: Event) => {
    const detail = (raw as CustomEvent<{ type?: string }>).detail;
    if (detail?.type === "theme") onChange();
  };
  const onForeground = () => {
    if (document.visibilityState === "visible") onChange();
  };
  window.addEventListener("speakeasy:native", onNative);
  document.addEventListener("visibilitychange", onForeground);
  window.addEventListener("focus", onForeground);
  return () => {
    window.removeEventListener("speakeasy:native", onNative);
    document.removeEventListener("visibilitychange", onForeground);
    window.removeEventListener("focus", onForeground);
  };
}

function legacyThemeToSnapshot(
  raw: {
    accent?: string;
    onAccent?: string;
    primaryContainer?: string;
    onPrimaryContainer?: string;
  },
  capabilities: Partial<ThemeCapabilities>,
): ThemeSnapshot {
  const base = honorSnapshot(1);
  const light = { ...base.palettes.light };
  const dark = { ...base.palettes.dark };
  if (raw.accent) {
    light.primary = raw.accent;
    light.primaryContainer = raw.primaryContainer ?? light.primaryContainer;
    dark.primary = raw.accent;
  }
  if (raw.onAccent) {
    light.onPrimary = raw.onAccent;
    dark.onPrimary = raw.onAccent;
  }
  if (raw.primaryContainer) {
    light.primaryContainer = raw.primaryContainer;
    dark.primaryContainer = raw.primaryContainer;
  }
  if (raw.onPrimaryContainer) {
    light.onPrimaryContainer = raw.onPrimaryContainer;
    dark.onPrimaryContainer = raw.onPrimaryContainer;
  }
  return makeSnapshot({
    revision: 1,
    source: "android-system",
    label: "Android 系统配色",
    palettes: { light, dark },
    capabilities: {
      ...capabilities,
      systemSemanticRoles: false,
    },
    meta: { note: "legacy getTheme() compatibility path" },
  });
}
