"use client";

import type { ImageThemeResult } from "./image-theme";
import { makeSnapshot, palettePayloadToSnapshot, validatePalettePayload } from "./palette";
import { themeStore } from "./theme-store";
import type { ThemePalettePayload, ThemeSnapshot } from "./types";

/**
 * Preview-only source adapter. It turns an imported phone palette or a locally
 * generated image palette into a validated ThemeSnapshot and publishes it.
 * Production never calls this module.
 */
export const PreviewThemeAdapter = {
  fromPhonePalette(payload: unknown, label = "手机色板导入"): ThemeSnapshot | null {
    const validated = validatePalettePayload(payload);
    if (!validated.ok || !validated.payload) return null;
    const snapshot = palettePayloadToSnapshot({ ...validated.payload, label }, Date.now());
    themeStore.applySnapshot(snapshot, { persist: false });
    return snapshot;
  },

  fromImage(result: ImageThemeResult, label: string): ThemeSnapshot {
    const snapshot = makeSnapshot({
      revision: Date.now(),
      source: "image",
      label,
      palettes: result.palettes,
      capabilities: {
        systemSemanticRoles: true,
        dynamicColor: true,
        transparency: true,
        haptics: false,
        reducedMotion: false,
        contrast: "standard",
      },
      meta: {
        sourceColor: result.sourceColor,
        completeness: "partial",
        note: `图片仅在浏览器本地量化；摘要 ${result.digest.slice(0, 12)}…，${result.quantizedSize} 个量化簇。`,
      },
    });
    themeStore.applySnapshot(snapshot, { persist: false });
    return snapshot;
  },

  fromPayload(payload: ThemePalettePayload, label?: string): ThemeSnapshot {
    return themeStore.applyPalettePayload(payload, label);
  },
};
