/**
 * Compatibility facade for the image-theme worker.
 *
 * The single source of truth for seed → Material 3 palettes is
 * `palette-from-seed.ts` (shared by the dynamic system theme and the local
 * image theme). These wrappers keep the worker's older `dark: boolean` /
 * `(seed, contrast)` signatures without duplicating the role/tone mapping.
 */
import { paletteFromSeed, palettesFromSeed } from "./palette-from-seed";
import type { ThemeColors } from "./types";

export function paletteFromSource(sourceHex: string, dark: boolean, contrast = 0): ThemeColors {
  return paletteFromSeed(sourceHex, dark ? "dark" : "light", contrast);
}

export function palettesFromSource(sourceHex: string, contrast = 0) {
  return palettesFromSeed(sourceHex, contrast);
}
