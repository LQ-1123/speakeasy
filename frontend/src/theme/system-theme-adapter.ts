"use client";

import { nativeBridge } from "../w4/v3/clientApi";
import { makeSnapshot, normalizeHex, validateSnapshot } from "./palette";
import { palettesFromSeed } from "./palette-from-seed";
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
      dark?: boolean;
    };
    const accent = normalizeHex(legacy.accent);
    if (!accent) {
      // The bridge cannot read the wallpaper accent right now (or the shell
      // never exposed it). Return no snapshot: ThemeStore keeps the palette it
      // already has (e.g. the stored warm-brown wallpaper palette) instead of
      // resetting it to the Honor light-blue fixture. Fresh installs already
      // start from honorSnapshot(), so nothing regresses for them.
      return { snapshot: null, capabilities, complete: false };
    }
    return {
      snapshot: legacyThemeToSnapshot(accent, capabilities),
      capabilities,
      complete: false,
    };
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

/**
 * Android 12+ only exposes the wallpaper accent through the legacy bridge
 * (`getTheme()` → `{ accent, onAccent, dark, insets }`). Patching that accent
 * onto the Honor fixture left every container role (bubbles, tab pill, tags,
 * avatar wells, notification banner) stuck on the old light blue.
 *
 * Expand the single accent into the complete M3 light + dark palette instead:
 * `primary`, `primaryContainer`, `secondaryContainer`, `surfaceTint`, … all
 * follow the wallpaper hue, while `on*` roles keep their paired contrast.
 * `onAccent` / container overrides are intentionally not copied from the bridge:
 * they are single-mode values and would break the derived dark palette; the
 * complete native palette stays on the `getThemeSnapshot()` path above.
 *
 * Only called with a validated accent (see readSystemThemeSnapshot): a missing
 * or malformed accent must yield no snapshot, never a fallback palette, so a
 * transiently unreadable bridge cannot overwrite a stored wallpaper theme with
 * the Honor light-blue fixture.
 */
function legacyThemeToSnapshot(
  accent: string,
  capabilities: Partial<ThemeCapabilities>,
): ThemeSnapshot {
  const palettes = palettesFromSeed(accent);
  return makeSnapshot({
    revision: 1,
    source: "android-system",
    label: "Android 系统配色",
    palettes,
    capabilities: {
      ...capabilities,
      systemSemanticRoles: false,
    },
    meta: {
      sourceColor: accent,
      // The bridge supplied one accent; the remaining roles are M3-derived.
      completeness: "partial",
      note: "Android 12+ 仅提供壁纸 accent；已按 M3 TonalSpot 推导完整亮/暗语义色板",
    },
  });
}
